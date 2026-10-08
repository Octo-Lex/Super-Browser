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
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from super_browser import Config, SuperBrowser
from super_browser.agent.llm.budget_aware import BudgetAwareLLMClient
from super_browser.config import AgentConfig, BudgetConfig
from super_browser.results import CompletionReason

# ============================================================================
# Scripted LLM stubs
# ============================================================================


class _TokenReportingLLM:
    """Scrolls forever, reporting tokens so each call records ~$0.0105
    (1000 in / 500 out on claude-sonnet-4-20250514 pricing)."""

    def __init__(self) -> None:
        self.calls = 0

    async def propose_action(self, prompt: str, *, tools: Any = None) -> dict:
        self.calls += 1
        return {
            "action": "scroll",
            "params": {"direction": "down", "amount": 100},
            "tokens": {"input": 1000, "output": 500},
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
# Tiny-cap enforcement — the USER's configured cap, end to end
# ============================================================================


async def test_tiny_configured_daily_cap_blocks_and_terminates_run() -> None:
    llm = _TokenReportingLLM()
    sb = await _started_budget_facade(0.001, llm)

    result = await sb.act("scroll a few times")

    # The default $10 cap would never block a $0.0105 call — a pass proves
    # Config.budget reached the governor.
    assert result.ok is False
    assert result.data.completion_reason == CompletionReason.BUDGET_EXHAUSTED
    # create_plan + the first propose_action ran; the SECOND propose_action
    # was refused BEFORE reaching the stub.
    assert llm.calls == 2


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
    llm = _TokenReportingLLM()
    sb = await _started_budget_facade(0.001, llm)
    await sb.act("scroll a few times")

    remaining = sb._budget_client.budget_remaining
    assert isinstance(remaining, float)
    # A spend was recorded against a $0.001 cap — the property no longer
    # crashes act() (the pre-PR-2 latent AttributeError).
    assert remaining < 0.001


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
