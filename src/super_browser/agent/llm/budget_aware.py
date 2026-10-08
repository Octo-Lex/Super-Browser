"""BudgetAwareLLMClient — wrapper that records every LLM call in TokenBudgetGovernor.

Decorates any :class:`LLMClient` so that each call to
:meth:`propose_action`, :meth:`create_plan`, or :meth:`replan`
automatically estimates its USD cost and records a
:class:`TokenUsageRecord` via the governor.

HB-04-01 compliance: the ``LLMClient`` Protocol is **not** modified.
"""

from __future__ import annotations

import logging
from typing import Any

from super_browser.agent.llm.protocol import LLMClient
from super_browser.budget.client import BudgetExhaustedError
from super_browser.budget.governor import TokenBudgetGovernor
from super_browser.budget.types import BudgetScope, TokenUsageRecord

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Simple price map — cost per 1 k tokens (input / output).
# Falls back to 0 when the model is unknown so the governor still
# receives a record (just with zero estimated cost).
# ---------------------------------------------------------------------------

_PRICE_PER_1K: dict[str, tuple[float, float]] = {
    # Anthropic
    "claude-haiku-4-20250414": (0.0008, 0.004),
    "claude-sonnet-4-20250514": (0.003, 0.015),
    "claude-opus-4-20250514": (0.015, 0.075),
    # OpenAI
    "gpt-4o": (0.0025, 0.01),
    "gpt-4o-mini": (0.00015, 0.0006),
    "gpt-4-turbo": (0.01, 0.03),
    "o3": (0.002, 0.008),
    "o3-mini": (0.0011, 0.0044),
    "o4-mini": (0.0011, 0.0044),
}


def _estimate_cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
) -> float:
    """Estimate USD cost using a simple per-1k-token price map."""
    input_price, output_price = _PRICE_PER_1K.get(model, (0.0, 0.0))
    cost = (input_tokens * input_price + output_tokens * output_price) / 1_000
    return cost


def _extract_tokens(result: Any) -> tuple[int, int]:
    """Extract (input_tokens, output_tokens) from an LLM response dict or list."""
    # propose_action returns a dict that may contain a "tokens" key.
    if isinstance(result, dict) and "tokens" in result:
        tok = result["tokens"]
        return tok.get("input", 0), tok.get("output", 0)
    # create_plan / replan return list[dict] — no token metadata,
    # so we return 0, 0.  The caller may enrich this later.
    return 0, 0


class BudgetAwareLLMClient:
    """Wraps any :class:`LLMClient` and records every call in the governor.

    Parameters
    ----------
    client:
        The underlying :class:`LLMClient` to delegate to.
    governor:
        The :class:`TokenBudgetGovernor` that receives usage records.
    model:
        Model identifier string (used for cost estimation and record keeping).
    """

    def __init__(
        self,
        client: LLMClient,
        governor: TokenBudgetGovernor,
        model: str,
    ) -> None:
        self._client = client
        self._governor = governor
        self._model = model

    # -- Public properties --

    @property
    def budget_remaining(self) -> float:
        """Remaining daily budget in USD."""
        return self._governor.daily_remaining

    # -- Budget enforcement (PR 2) --------------------------------------------

    def _enforce(self, action_name: str) -> None:
        """Refuse the call when the daily cap is already spent.

        Records land after each call, so this pre-call check blocks the
        FIRST call that would exceed the cap's remainder. Raises
        :class:`BudgetExhaustedError`, which the agent loop translates into
        a ``budget_exhausted`` completion.
        """
        block = self._governor.check_budget(
            BudgetScope.DAILY, estimated_cost_usd=0.0
        )
        if block is not None:
            raise BudgetExhaustedError(block)

    # -- LLMClient interface --------------------------------------------------

    async def propose_action(
        self,
        prompt: str,
        *,
        tools: list[dict] | None = None,
    ) -> dict:
        """Delegate to the wrapped client and record cost."""
        self._enforce("propose_action")
        result = await self._client.propose_action(prompt, tools=tools)
        self._record(result, action_name="propose_action")
        return result

    async def create_plan(
        self,
        instruction: str,
        *,
        tools: list[dict],
    ) -> list[dict]:
        """Delegate to the wrapped client and record cost."""
        self._enforce("create_plan")
        result = await self._client.create_plan(instruction, tools=tools)
        self._record(result, action_name="create_plan")
        return result

    async def replan(
        self,
        *,
        instruction: str,
        original_plan: list[dict],
        failed_step: int,
        error: str,
    ) -> list[dict]:
        """Delegate to the wrapped client and record cost."""
        self._enforce("replan")
        result = await self._client.replan(
            instruction=instruction,
            original_plan=original_plan,
            failed_step=failed_step,
            error=error,
        )
        self._record(result, action_name="replan")
        return result

    # -- Budget recording -----------------------------------------------------

    def record_raw_usage(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        action_name: str = "compress",
    ) -> None:
        """Record a usage entry directly (outside the LLMClient protocol).

        Useful for components like :class:`ContextCompressor` that want to
        track their own LLM spending without routing through
        :meth:`propose_action` / :meth:`create_plan` / :meth:`replan`.
        """
        estimated_cost = _estimate_cost_usd(
            self._model, input_tokens, output_tokens
        )

        record = TokenUsageRecord(
            model=self._model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost_usd=estimated_cost,
            action_name=action_name,
        )

        alert = self._governor.record_usage(record)
        if alert is not None:
            logger.warning(
                "Budget alert after %s: %s (%.2f%% of cap)",
                action_name,
                alert.level,
                alert.usage_pct,
            )

    def _record(self, result: Any, *, action_name: str) -> None:
        """Create a :class:`TokenUsageRecord` and feed it to the governor."""
        input_tokens, output_tokens = _extract_tokens(result)
        estimated_cost = _estimate_cost_usd(
            self._model, input_tokens, output_tokens
        )

        record = TokenUsageRecord(
            model=self._model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost_usd=estimated_cost,
            action_name=action_name,
        )

        alert = self._governor.record_usage(record)
        if alert is not None:
            logger.warning(
                "Budget alert after %s: %s (%.2f%% of cap)",
                action_name,
                alert.level,
                alert.usage_pct,
            )
