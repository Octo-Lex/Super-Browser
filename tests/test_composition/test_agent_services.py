"""
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from super_browser import Config, SuperBrowser
from super_browser.agent.loop import AgentLoop
from super_browser.agent.registry import ToolRegistry
from super_browser.config import AgentConfig
from super_browser.results.types import action_result
from super_browser.security.types import SecurityCheckResult

# ============================================================================
# Shared fixtures
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


async def _started_facade(**agent_flags: Any) -> SuperBrowser:
    """A facade started with a mocked engine and arbitrary AgentConfig flags."""
    engine, _ = _mock_engine()
    cfg = Config(agent=AgentConfig(**agent_flags))
    sb = SuperBrowser(config=cfg)
    with patch(
        "super_browser.browser.factory._detect_backend",
        return_value="patchright",
    ), patch(
        "super_browser.browser.backends.patchright_backend.PatchrightEngine",
        return_value=engine,
    ):
        await sb.start()
    return sb


class _ScriptedLLM:
    """Dispatches one action, then completes."""

    def __init__(self) -> None:
        self.calls = 0

    async def propose_action(self, prompt: str, *, tools: Any = None) -> dict:
        self.calls += 1
        if self.calls == 1:
            return {"action": "scroll", "params": {"direction": "down", "amount": 100}}
        return {"done": True, "summary": "finished"}

    async def create_plan(self, instruction: str, *, tools: Any = None) -> list:
        return [{"action": "scroll"}]

    async def replan(self, **kwargs: Any) -> list:
        return [{"action": "scroll"}]


# ============================================================================
# Security gating — the net path, not just construction
# ============================================================================


class _FakeSecurityManager:
    def __init__(self, passed: bool) -> None:
        self.check_action = AsyncMock(
            return_value=SecurityCheckResult(passed=passed, blocked_by="test-policy")
        )


async def test_security_block_stops_mutation_before_the_controller() -> None:
    sb = await _started_facade()
    sb._security_manager = _FakeSecurityManager(passed=False)
    sb._controller = MagicMock()
    sb._controller.click = AsyncMock()

    result = await sb.click("#btn")

    assert result.ok is False
    assert result.error is not None
    assert result.error.category.value == "security"
    sb._controller.click.assert_not_awaited()
    sb._security_manager.check_action.assert_called_once()


async def test_security_pass_allows_mutation_and_consults_manager() -> None:
    sb = await _started_facade()
    sb._security_manager = _FakeSecurityManager(passed=True)
    sb._controller = MagicMock()
    click_ok = MagicMock()
    click_ok.ok = True
    sb._controller.click = AsyncMock(return_value=click_ok)

    result = await sb.click("#btn")

    assert result.ok is True
    sb._security_manager.check_action.assert_called_once()
    sb._controller.click.assert_awaited_once()


async def test_no_security_manager_means_no_gate_by_default() -> None:
    """enable_security defaults False: the mutation runs and no manager is
    consulted. This pins the documented default-vs-enabled distinction."""
    sb = await _started_facade()
    assert sb._security_manager is None
    sb._controller = MagicMock()
    click_ok = MagicMock()
    click_ok.ok = True
    sb._controller.click = AsyncMock(return_value=click_ok)

    result = await sb.click("#btn")

    assert result.ok is True
    sb._controller.click.assert_awaited_once()


# ============================================================================
# Recovery routing — loop dispatch crosses the coordinator
# ============================================================================


async def test_recovery_coordinator_receives_loop_dispatch() -> None:
    sb = await _started_facade(enable_recovery=True)
    sb._llm_client = _ScriptedLLM()
    coordinator = MagicMock()
    coordinator.execute_with_recovery = AsyncMock(
        side_effect=lambda action_fn, **kw: action_fn()
    )
    sb._coordinator = coordinator

    result = await sb.act("scroll the page")

    assert result.ok is True
    coordinator.execute_with_recovery.assert_awaited()


async def test_no_coordinator_means_direct_dispatch() -> None:
    """Default: recovery disabled, dispatch runs without it — pins the
    default-vs-enabled distinction."""
    sb = await _started_facade()
    sb._llm_client = _ScriptedLLM()
    assert sb._coordinator is None

    result = await sb.act("scroll the page")

    assert result.ok is True


# ============================================================================
# Vision composition — flag-gated fallback + lazy analyze path
# ============================================================================


async def test_vision_enabled_connects_controller_fallback() -> None:
    sb = await _started_facade(enable_vision=True)

    assert sb._vision_controller is not None
    assert sb._controller._vision_controller is sb._vision_controller


async def test_vision_disabled_by_default_keeps_controller_unwired() -> None:
    sb = await _started_facade()

    assert sb._vision_controller is None
    assert sb._controller._vision_controller is None


async def test_lazy_analyze_without_provider_reports_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """analyze_image is lazily usable WITHOUT the enable_vision flag — but
    with no provider env configured it reports vision_unavailable instead
    of crashing or silently succeeding."""
    for key in (
        "SB_ANTHROPIC_API_KEY",
        "SB_OPENAI_API_KEY",
        "SB_UITARS_MODEL_PATH",
    ):
        monkeypatch.delenv(key, raising=False)
    sb = await _started_facade()

    result = await sb.analyze_image(question="what is on the page?")

    assert result.ok is False
    assert result.error == "vision_unavailable"


# ============================================================================
# Stealth policy — evaluated at dispatch, verdicts honored
# ============================================================================


def _stealth_manager(verdict: str) -> MagicMock:
    manager = MagicMock()
    manager.evaluate_action = MagicMock(
        return_value=MagicMock(verdict=MagicMock(value=verdict))
    )
    manager.config = MagicMock(confirm_callback=None)
    return manager


def _loop_with_stealth(stealth_manager: Any) -> AgentLoop:
    registry = ToolRegistry()

    async def scroll(direction: str = "down", amount: int = 100):
        return action_result(ok=True, data={"direction": direction})

    registry.register(scroll, toolsets=())
    controller = MagicMock()
    controller._page = MagicMock()
    controller._page.url = "https://example.com/"
    return AgentLoop(
        controller=controller,
        registry=registry,
        llm_client=_ScriptedLLM(),
        stealth_manager=stealth_manager,
    )


async def test_stealth_policy_evaluates_at_dispatch() -> None:
    stealth = _stealth_manager("allow")
    loop = _loop_with_stealth(stealth)

    result = await loop.run("scroll")

    assert result.completion_reason == "success"
    stealth.evaluate_action.assert_called_once()
    assert stealth.evaluate_action.call_args[0][0] == "scroll"


async def test_stealth_deny_blocks_the_action() -> None:
    stealth = _stealth_manager("deny")
    loop = _loop_with_stealth(stealth)

    result = await loop.run("scroll")

    assert result.completion_reason == "success"  # loop survives
    step = result.steps[0]
    assert step.action_result.ok is False
    assert step.action_result.error.category.value == "security"
    assert "Stealth policy denied" in step.action_result.error.message


# ============================================================================
# MCP surface contract — default mode refuses mutations structurally
# ============================================================================


async def test_mcp_default_mode_refuses_mutation_not_unknown_tool() -> None:
    from super_browser.mcp_server import (
        MCPBrowserRuntime,
        build_server,
    )

    runtime = MCPBrowserRuntime()
    runtime._sb = MagicMock()
    server = build_server(runtime)
    dispatcher = server._sb_dispatcher  # type: ignore[attr-defined]

    result = await dispatcher.dispatch("click", {"target": "#btn"})
    payload = json.loads(result[0].text)

    assert payload["ok"] is False
    assert "refusal" in payload, "must be a policy refusal, not unknown-tool"
    assert "disable" in payload["refusal"]["reason"]
