"""Budget governance + abort lifecycle wiring tests — PR 2 (program step 5).

Proves the frozen step-5 acceptance criteria end to end through the public
facade:

- **Tiny-cap enforcement**: a deliberately tiny configured daily cap (from
  ``Config.budget``) blocks the agent's SECOND LLM call and terminates the
  run with ``CompletionReason.BUDGET_EXHAUSTED``. The default $10 cap would
  never block this, so a pass proves the USER's caps reach the governor.
- **Governed chain**: with budget enabled, ``sb._llm_client`` is the
  governed wrapper (agent.llm.budget_aware) sharing one governor with the
  cascade client — every planning path traverses the cap.
- **Abort lifecycle**: ``abort()`` terminates an active run (CANCELLED),
  and the next ``act()`` starts clean — a stale signal does not poison
  later runs.

Engines are mocked (the established patch-factory pattern); the LLM is a
scripted stub so no API key is needed.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from super_browser import Config, SuperBrowser
from super_browser.agent.llm.budget_aware import BudgetAwareLLMClient
from super_browser.config import AgentConfig, BudgetConfig
from super_browser.results import CompletionReason

# ============================================================================
# Scripted LLM stubs
# ============================================================================


class _TokenReportingLLM:
    """Reports tokens so each call records ~$0.0105 (1000 in / 500 out on
    claude-sonnet-4-20250514 pricing). When ``done_after`` calls are
    reached, proposes ``done`` instead (the done call reports tokens too)."""

    def __init__(self, done_after: int | None = None) -> None:
        self.calls = 0
        self.done_after = done_after

    async def propose_action(self, prompt: str, *, tools: Any = None) -> dict:
        self.calls += 1
        tokens = {"input": 1000, "output": 500}
        if self.done_after is not None and self.calls >= self.done_after:
            return {"done": True, "summary": "finished", "tokens": tokens}
        return {
            "action": "scroll",
            "params": {"direction": "down", "amount": 100},
            "tokens": tokens,
        }

    async def create_plan(self, instruction: str, *, tools: Any = None) -> list:
        self.calls += 1
        return [{"action": "scroll"}]

    async def replan(self, **kwargs: Any) -> list:
        self.calls += 1
        return [{"action": "scroll"}]


class _ScriptedLLM:
    """Scrolls forever until ``done_after`` calls are reached, then done.

    Yields control per call so an abort() from the test task lands between
    steps. ``first_call`` lets the test synchronize on the run starting.
    """

    def __init__(self, done_after: int | None = None) -> None:
        self.calls = 0
        self.done_after = done_after
        self.first_call = asyncio.Event()

    async def propose_action(self, prompt: str, *, tools: Any = None) -> dict:
        self.calls += 1
        self.first_call.set()
        await asyncio.sleep(0.05)
        if self.done_after is not None and self.calls >= self.done_after:
            return {"done": True, "summary": "finished"}
        return {"action": "scroll", "params": {"direction": "down", "amount": 50}}

    async def create_plan(self, instruction: str, *, tools: Any = None) -> list:
        self.calls += 1
        return [{"action": "scroll"}]

    async def replan(self, **kwargs: Any) -> list:
        return [{"action": "scroll"}]


# ============================================================================
# Mocked-engine start (established pattern)
# ============================================================================


def _mock_engine() -> tuple[MagicMock, MagicMock]:
    engine = MagicMock()
    engine.start = AsyncMock()
    engine.stop = AsyncMock()
    engine.session = None
    page = MagicMock()
    page.cdp = MagicMock()
    page.backend_page = MagicMock()
    engine.new_page = AsyncMock(return_value=page)
    engine.capabilities = MagicMock(multi_tab=True)
    return engine, page


async def _started_budget_facade(daily_cap: float, llm: Any) -> SuperBrowser:
    """A facade started with budget governance enabled and a mocked engine."""
    engine, _ = _mock_engine()
    cfg = Config(
        agent=AgentConfig(enable_budget=True),
        budget=BudgetConfig(daily_cap_usd=daily_cap),
    )
    sb = SuperBrowser(config=cfg, llm_client=llm)
    with patch(
        "super_browser.browser.factory._detect_backend",
        return_value="patchright",
    ), patch(
        "super_browser.browser.backends.patchright_backend.PatchrightEngine",
        return_value=engine,
    ):
        await sb.start()
    return sb


async def _started_plain_facade(llm: Any) -> SuperBrowser:
    """A facade started WITHOUT budget governance (abort isolation)."""
    engine, _ = _mock_engine()
    sb = SuperBrowser(config=Config(), llm_client=llm)
    with patch(
        "super_browser.browser.factory._detect_backend",
        return_value="patchright",
    ), patch(
        "super_browser.browser.backends.patchright_backend.PatchrightEngine",
        return_value=engine,
    ):
        await sb.start()
    return sb


# ============================================================================
# Admission control — unaffordable calls never reach the LLM
# ============================================================================


async def test_tiny_configured_cap_refuses_first_call_before_the_llm() -> None:
    """A $0.001 daily cap cannot afford the projected ~$0.015 request
    (prompt estimate + bounded output allowance at sonnet pricing), so the
    FIRST call is refused pre-flight: the underlying LLM is never touched,
    and the run terminates with BUDGET_EXHAUSTED. The default $10 cap would
    admit this call — a pass proves Config.budget reaches the governor."""
    llm = _TokenReportingLLM()
    sb = await _started_budget_facade(0.001, llm)

    result = await sb.act("scroll a few times")

    assert result.ok is False
    assert result.data.completion_reason == CompletionReason.BUDGET_EXHAUSTED
    assert llm.calls == 0, "an unaffordable call must never reach the LLM"


async def test_affordable_calls_are_admitted_and_recorded() -> None:
    """A generous cap admits the same calls: they reach the LLM, actual
    token usage is recorded, and the run completes normally. This preserves
    the distinction between estimated-cost admission and an absolute
    billing guarantee."""
    llm = _TokenReportingLLM(done_after=3)
    sb = await _started_budget_facade(5.00, llm)

    result = await sb.act("scroll a few times")

    assert result.ok is True
    assert result.data.completion_reason == CompletionReason.SUCCESS
    assert llm.calls == 3  # create_plan + 2 propose_action calls
    remaining = sb._budget_client.budget_remaining
    assert 0 < remaining < 5.00, "recorded usage must reduce the remaining budget"


async def test_governed_llm_sits_in_the_real_chain() -> None:
    llm = _TokenReportingLLM()
    sb = await _started_budget_facade(0.001, llm)

    assert isinstance(sb._llm_client, BudgetAwareLLMClient), (
        "the loop's LLM must be the governed wrapper, not the raw client"
    )
    assert sb._llm_client._client is llm
    assert sb._llm_client._governor is sb._budget_client._governor, (
        "wrapper and cascade client must share ONE governor"
    )


async def test_cascade_client_exposes_budget_remaining() -> None:
    llm = _TokenReportingLLM(done_after=3)
    sb = await _started_budget_facade(5.00, llm)
    await sb.act("scroll a few times")

    remaining = sb._budget_client.budget_remaining
    assert isinstance(remaining, float)
    assert 0 < remaining < 5.00, (
        "recorded usage must reduce the remaining budget without crashing act()"
    )


# ============================================================================
# Abort lifecycle
# ============================================================================


async def test_abort_terminates_active_run() -> None:
    llm = _ScriptedLLM()
    sb = await _started_plain_facade(llm)

    task = asyncio.create_task(sb.act("loop forever"))
    await asyncio.wait_for(llm.first_call.wait(), 5)
    await asyncio.sleep(0.1)  # let the current step finish
    sb.abort()

    result = await asyncio.wait_for(task, 10)

    assert result.data.completion_reason == CompletionReason.CANCELLED
    assert result.ok is False


async def test_stale_abort_does_not_poison_next_run() -> None:
    llm = _ScriptedLLM()
    sb = await _started_plain_facade(llm)

    task = asyncio.create_task(sb.act("loop forever"))
    await asyncio.wait_for(llm.first_call.wait(), 5)
    await asyncio.sleep(0.1)
    sb.abort()
    await asyncio.wait_for(task, 10)

    # A previous abort() left the signal SET. The next run must start clean.
    fresh = _ScriptedLLM(done_after=2)
    sb._llm_client = fresh
    result = await sb.act("finish quickly")

    assert result.ok is True
    assert result.data.completion_reason == CompletionReason.SUCCESS


# ============================================================================
# Duplicate-name removal — the budget package alias is gone
# ============================================================================


def test_budget_package_no_longer_exports_budget_aware_alias() -> None:
    """The alias collided with agent.llm.budget_aware.BudgetAwareLLMClient —
    the governed wrapper. The cascade client keeps its real name."""
    import super_browser.budget as budget_pkg

    assert not hasattr(budget_pkg, "BudgetAwareLLMClient")
    assert hasattr(budget_pkg, "BudgetCascadeClient")
    from super_browser.agent.llm.budget_aware import BudgetAwareLLMClient  # noqa: F401


# ============================================================================
# P2-review fix 2: budget governance preserves token streaming
# ============================================================================


class _StreamingStub:
    """Streaming-capable LLM: yields token events, then a done event whose
    result carries provider-reported token usage."""

    def __init__(self) -> None:
        self.stream_calls = 0

    async def propose_action(self, prompt: str, *, tools: Any = None) -> dict:
        return {"done": True, "summary": "s", "tokens": {"input": 50, "output": 20}}

    async def create_plan(self, instruction: str, *, tools: Any = None) -> list:
        return []

    async def replan(self, **kwargs: Any) -> list:
        return []

    async def propose_action_stream(
        self, prompt: str, *, tools: Any = None
    ) -> AsyncIterator[dict]:
        self.stream_calls += 1
        yield {"type": "token", "content": "he"}
        yield {"type": "token", "content": "llo"}
        result = {
            "action": "scroll",
            "params": {"direction": "down", "amount": 10},
            "tokens": {"input": 50, "output": 20},
        }
        yield {"type": "done", "result": result}


def _governed(cap: float, model: str, llm: Any) -> BudgetAwareLLMClient:
    from super_browser.budget.governor import TokenBudgetGovernor

    governor = TokenBudgetGovernor(config=BudgetConfig(daily_cap_usd=cap))
    return BudgetAwareLLMClient(llm, governor, model=model)


async def test_governed_wrapper_preserves_token_streaming() -> None:
    """The governed wrapper must forward token events and record usage from
    the final streamed result (providers include real token counts there)."""
    from super_browser.budget.governor import TokenBudgetGovernor

    stub = _StreamingStub()
    governor = TokenBudgetGovernor(config=BudgetConfig(daily_cap_usd=5.00))
    wrapped = BudgetAwareLLMClient(stub, governor, model="claude-sonnet-4-20250514")

    events = [event async for event in wrapped.propose_action_stream("hello")]

    kinds = [e.get("type") for e in events]
    assert kinds == ["token", "token", "done"], "token events must pass through"
    assert stub.stream_calls == 1
    # Usage recorded from the provider-reported tokens in the done result:
    # (50 x 0.003 + 20 x 0.015) / 1000 = 0.00045 USD.
    assert governor.daily_spend == pytest.approx(0.00045)


async def test_governed_streaming_admission_refuses_unaffordable() -> None:
    from super_browser.budget.client import BudgetExhaustedError
    from super_browser.budget.governor import TokenBudgetGovernor

    stub = _StreamingStub()
    governor = TokenBudgetGovernor(config=BudgetConfig(daily_cap_usd=0.001))
    wrapped = BudgetAwareLLMClient(stub, governor, model="claude-sonnet-4-20250514")

    with pytest.raises(BudgetExhaustedError):
        async for _ in wrapped.propose_action_stream("hello"):
            pass

    assert stub.stream_calls == 0, "unaffordable stream must never start"


# ============================================================================
# P2-review fix 3: restart does not nest budget wrappers
# ============================================================================


async def test_restart_does_not_nest_budget_wrappers() -> None:
    """start → stop → start must leave exactly one governed wrapper, built
    from the untouched original client, sharing one governor with the
    cascade client — not a wrapper wrapped in another wrapper."""

    llm = _TokenReportingLLM(done_after=2)
    engine_a, _ = _mock_engine()

    with patch(
        "super_browser.browser.factory._detect_backend", return_value="patchright"
    ), patch(
        "super_browser.browser.backends.patchright_backend.PatchrightEngine",
        return_value=engine_a,
    ):
        cfg = Config(
            agent=AgentConfig(enable_budget=True),
            budget=BudgetConfig(daily_cap_usd=5.00),
        )
        sb = SuperBrowser(config=cfg, llm_client=llm)
        with patch(
            "super_browser.browser.factory._detect_backend",
            return_value="patchright",
        ), patch(
            "super_browser.browser.backends.patchright_backend.PatchrightEngine",
            return_value=engine_a,
        ):
            await sb.start()

    await sb.stop()

    engine_b, _ = _mock_engine()
    with patch(
        "super_browser.browser.factory._detect_backend", return_value="patchright"
    ), patch(
        "super_browser.browser.backends.patchright_backend.PatchrightEngine",
        return_value=engine_b,
    ):
        await sb.start()

    assert isinstance(sb._llm_client, BudgetAwareLLMClient)
    assert not isinstance(sb._llm_client._client, BudgetAwareLLMClient), (
        "governed wrappers must never nest"
    )
    assert sb._llm_client._client is llm, "the original client must be preserved"
    assert sb._llm_client._governor is sb._budget_client._governor, (
        "exactly ONE governor must be active after restart"
    )

    # One LLM call → one usage record; reported remaining matches the
    # enforcing governor.
    result = await sb.act("one task")
    assert result.ok is True
    records = sb._llm_client._governor._records
    assert len(records) >= 1
    assert sb._llm_client.budget_remaining == sb._budget_client.budget_remaining
    assert sb._llm_client.budget_remaining < 5.00


# ============================================================================
# P2-review accounting gap: unknown pricing and planning calls cannot bypass
# ============================================================================


async def test_unknown_model_pricing_still_enforces() -> None:
    """Unknown models are priced at the most expensive known rate, so
    unknown pricing cannot silently bypass enforcement."""
    from super_browser.budget.client import BudgetExhaustedError
    from super_browser.budget.governor import TokenBudgetGovernor

    stub = _StreamingStub()
    governor = TokenBudgetGovernor(config=BudgetConfig(daily_cap_usd=0.001))
    wrapped = BudgetAwareLLMClient(stub, governor, model="totally-unknown-model")

    with pytest.raises(BudgetExhaustedError):
        async for _ in wrapped.propose_action_stream("hello world"):
            pass

    assert stub.stream_calls == 0


async def test_planning_calls_record_conservative_estimate() -> None:
    """create_plan/replan carry no provider usage metadata, so a clearly
    identified conservative estimate keeps planning spend visible."""
    from super_browser.budget.governor import TokenBudgetGovernor

    governor = TokenBudgetGovernor(config=BudgetConfig(daily_cap_usd=5.00))

    class _PlanOnlyLLM:
        async def create_plan(self, instruction: str, *, tools: Any = None) -> list:
            return [{"action": "scroll"}]

        async def replan(self, **kwargs: Any) -> list:
            return [{"action": "scroll"}]

    wrapped = BudgetAwareLLMClient(_PlanOnlyLLM(), governor, model="claude-sonnet-4-20250514")

    await wrapped.create_plan("plan my route", tools=[])
    await wrapped.replan(
        instruction="plan again", original_plan=[], failed_step=1, error="x"
    )

    assert governor.daily_spend > 0, "planning spend must be recorded"
    estimated = [r for r in governor._records if "(estimated)" in r.action_name]
    assert len(estimated) == 2, "both planning calls must record an estimate"
