"""BudgetAwareLLMClient — LLM client wrapper with budget governance."""

from __future__ import annotations

from typing import Any, Optional

from super_browser.budget.cascade import ModelCascade
from super_browser.budget.compressor import ContextCompressor
from super_browser.budget.cost_estimator import CostEstimator
from super_browser.budget.credential_pool import CircuitBreaker, CredentialPool
from super_browser.budget.governor import TokenBudgetGovernor
from super_browser.budget.types import (
    BudgetBlock,
    BudgetScope,
    TokenUsageRecord,
)


class BudgetExhaustedError(Exception):
    def __init__(self, block: BudgetBlock) -> None:
        self.block = block
        super().__init__(f"Budget exhausted: {block.exhausted_scope}")


class AllProvidersCircuitOpenError(Exception):
    pass


class BudgetCascadeClient:
    """Internal budget-aware LLM client with model cascade and circuit breaker.

    This class is used internally by the budget subsystem. For the public
    facade-facing client, see ``agent.llm.budget_aware.BudgetAwareLLMClient``.
    """

    def __init__(
        self,
        governor: TokenBudgetGovernor,
        cascade: ModelCascade,
        credential_pool: CredentialPool,
        circuit_breaker: CircuitBreaker,
        compressor: ContextCompressor,
        llm_client: Optional[Any] = None,
    ) -> None:
        self._governor = governor
        self._cascade = cascade
        self._credential_pool = credential_pool
        self._circuit_breaker = circuit_breaker
        self._compressor = compressor
        self._llm_client = llm_client
        self._estimator = CostEstimator()

    @property
    def budget_remaining(self) -> float:
        """Remaining daily budget in USD (PR 2: the facade exposes this)."""
        return self._governor.daily_remaining

    async def call(
        self,
        messages: list[dict],
        *,
        action_type: str = "general",
        complexity: str = "simple",
        trace_id: str = "",
    ) -> tuple[Any, TokenUsageRecord]:
        if not trace_id:
            try:
                from super_browser.tracing.flow_logger import _current_context
                ctx = _current_context.get()
                if ctx is not None:
                    trace_id = ctx.trace_id
            except Exception:
                pass

        # Step 1: Select model via cascade
        cascade_result = self._cascade.select_model(action_type, complexity=complexity)
        model = cascade_result.model
        provider = cascade_result.provider
        cost_tier = cascade_result.selected_tier.tier

        # Step 2: Check budget
        estimated_cost = self._estimator.estimate(model, 1000, 500)
        for scope in (BudgetScope.DAILY, BudgetScope.PER_ACTION):
            block = self._governor.check_budget(scope, estimated_cost_usd=estimated_cost)
            if block:
                raise BudgetExhaustedError(block)

        # Step 3: Select credential
        credential = self._credential_pool.select(provider)
        credential_id = credential.credential_id if credential else ""

        # Step 4: Check circuit breaker
        if self._circuit_breaker.is_open(provider):
            raise AllProvidersCircuitOpenError(f"Provider {provider} circuit is open")

        # Step 5: Execute LLM call
        response = None
        input_tokens = 0
        output_tokens = 0

        if self._llm_client is not None:
            try:
                result = self._llm_client(messages, model=model, api_key=credential.api_key if credential else "")
                if hasattr(result, '__await__'):
                    result = await result
                response = result
                input_tokens = getattr(response, 'input_tokens', 1000) if hasattr(response, 'input_tokens') else 1000
                output_tokens = getattr(response, 'output_tokens', 500) if hasattr(response, 'output_tokens') else 500
            except Exception as exc:
                status = getattr(exc, 'status_code', 0)
                if status in (429, 402) and credential:
                    self._credential_pool.report_failure(credential.credential_id, status)
                raise
        else:
            input_tokens = 1000
            output_tokens = 500

        # Step 6: Record usage
        cost_usd = self._estimator.estimate(model, input_tokens, output_tokens)
        record = TokenUsageRecord(
            model=model,
            provider=provider,
            credential_id=credential_id,
            cost_tier=cost_tier,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost_usd=cost_usd,
            action_name=action_type,
            trace_id=trace_id,
        )
        self._governor.record_usage(record)

        if credential:
            self._credential_pool.report_success(credential.credential_id)
        self._circuit_breaker.record_success(provider)

        return response, record
