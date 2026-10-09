"""BudgetAwareLLMClient — governed LLM client: admission control + usage recording.

Decorates any :class:`LLMClient` so that each call to
:meth:`propose_action`, :meth:`propose_action_stream`, :meth:`create_plan`,
or :meth:`replan` is first admitted against the daily budget cap (a
projected-cost check) and then has its usage recorded in the governor.

HB-04-01 compliance: the ``LLMClient`` Protocol is **not** modified —
``propose_action_stream`` is provided when the wrapped client supports it.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from super_browser.agent.llm.protocol import LLMClient
from super_browser.budget.client import BudgetExhaustedError
from super_browser.budget.governor import TokenBudgetGovernor
from super_browser.budget.types import BudgetScope, TokenUsageRecord

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Admission + estimation constants
# ---------------------------------------------------------------------------

# Heuristic: ~4 characters of prompt text per LLM token.
_CHARS_PER_TOKEN = 4
# Bounded output allowance used when projecting a request's cost before it
# is dispatched. Real output is recorded after the call.
_OUTPUT_ALLOWANCE_TOKENS = 1_000
# Bounded output estimate for planning calls (create_plan / replan), whose
# responses carry no provider usage metadata.
_PLANNING_OUTPUT_ALLOWANCE_TOKENS = 1_000

# ---------------------------------------------------------------------------
# Simple price map — cost per 1 k tokens (input / output).
# UNKNOWN models are priced at the most expensive known rate so unknown
# pricing cannot silently bypass enforcement (over-governed beats
# ungoverned).
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

_UNKNOWN_MODEL_FALLBACK_PRICE: tuple[float, float] = (0.015, 0.075)


def _estimate_cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
) -> float:
    """Estimate USD cost using a per-1k-token price map.

    Unknown models are priced at the most expensive known rate: an
    admission decision must not depend on recognizing the model name.
    """
    input_price, output_price = _PRICE_PER_1K.get(
        model, _UNKNOWN_MODEL_FALLBACK_PRICE
    )
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


class BudgetAwareLLMClient(LLMClient):
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

    def _admit(self, action_name: str, input_text: str) -> None:
        """Refuse the call up front when the projected cost exceeds the cap.

        The projection is the estimated input tokens (prompt length / 4)
        plus a bounded output allowance, priced at the model's known rate —
        or the most expensive known rate for unknown models, so unknown
        pricing cannot bypass enforcement. Raises
        :class:`BudgetExhaustedError` before the underlying client is
        touched.

        This is estimated-cost ADMISSION, not an absolute billing
        guarantee: the provider's actual bill can differ from the
        projection. Actual usage is recorded after each call.
        """
        input_estimate = max(1, len(input_text) // _CHARS_PER_TOKEN)
        projected_cost = _estimate_cost_usd(
            self._model, input_estimate, _OUTPUT_ALLOWANCE_TOKENS
        )
        block = self._governor.check_budget(
            BudgetScope.DAILY, estimated_cost_usd=projected_cost
        )
        if block is not None:
            raise BudgetExhaustedError(block)

    def _record_planning_estimate(self, input_text: str, action_name: str) -> None:
        """Record a conservative estimated usage for a planning call.

        ``create_plan``/``replan`` return lists without provider usage
        metadata, so actual tokens are unknowable at this boundary. A
        conservative estimate (real prompt size + bounded output allowance
        at the model's price) keeps planning spend visible; it is an
        ESTIMATE, marked as such in the record's action name.
        """
        input_estimate = max(1, len(input_text) // _CHARS_PER_TOKEN)
        estimated_cost = _estimate_cost_usd(
            self._model, input_estimate, _PLANNING_OUTPUT_ALLOWANCE_TOKENS
        )

        record = TokenUsageRecord(
            model=self._model,
            input_tokens=input_estimate,
            output_tokens=_PLANNING_OUTPUT_ALLOWANCE_TOKENS,
            estimated_cost_usd=estimated_cost,
            action_name=f"{action_name} (estimated)",
        )

        alert = self._governor.record_usage(record)
        if alert is not None:
            logger.warning(
                "Budget alert after %s: %s (%.2f%% of cap)",
                action_name,
                alert.level,
                alert.usage_pct,
            )

    # -- LLMClient interface --------------------------------------------------

    async def propose_action(
        self,
        prompt: str,
        *,
        tools: list[dict] | None = None,
    ) -> dict:
        """Admit, delegate to the wrapped client, and record actual cost."""
        self._admit("propose_action", prompt)
        result = await self._client.propose_action(prompt, tools=tools)
        self._record(result, action_name="propose_action")
        return result

    async def propose_action_stream(
        self,
        prompt: str,
        *,
        tools: list[dict] | None = None,
    ) -> AsyncIterator[dict]:
        """Stream the wrapped client's tokens under the same admission gate.

        Admission runs before the first token. Usage is recorded from the
        final streamed result, which providers deliver with real token
        counts. A wrapped client without streaming support degrades to a
        single governed ``propose_action`` call.
        """
        self._admit("propose_action_stream", prompt)
        stream = getattr(self._client, "propose_action_stream", None)
        if not callable(stream):
            result = await self._client.propose_action(prompt, tools=tools)
            self._record(result, action_name="propose_action")
            yield {"type": "done", "result": result}
            return

        final: dict | None = None
        async for event in stream(prompt, tools=tools):
            if isinstance(event, dict) and event.get("type") == "done":
                final = event.get("result")
            yield event
        if isinstance(final, dict):
            self._record(final, action_name="propose_action")

    async def create_plan(
        self,
        instruction: str,
        *,
        tools: list[dict],
    ) -> list[dict]:
        """Admit, delegate, and record a conservative planning estimate."""
        self._admit("create_plan", instruction)
        result = await self._client.create_plan(instruction, tools=tools)
        self._record_planning_estimate(instruction, action_name="create_plan")
        return result

    async def replan(
        self,
        *,
        instruction: str,
        original_plan: list[dict],
        failed_step: int,
        error: str,
    ) -> list[dict]:
        """Admit, delegate, and record a conservative planning estimate."""
        self._admit("replan", instruction)
        result = await self._client.replan(
            instruction=instruction,
            original_plan=original_plan,
            failed_step=failed_step,
            error=error,
        )
        self._record_planning_estimate(instruction, action_name="replan")
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
