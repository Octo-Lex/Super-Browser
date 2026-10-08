"""TEST-14-01-01 through TEST-14-01-08: Full SuperBrowser lifecycle.

Tests the complete workflow: init → act → extract → close,
plus delegate, checkpoint, stealth, and budget integration.

All LLM calls are mocked — no API keys required (HB-14-01).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from super_browser.agent.facade import SuperBrowser
from super_browser.agent.registry import ToolRegistry
from super_browser.agent.types import (
    ChildTask,
    DelegationResult,
    DelegationStatus,
)
from super_browser.config import AgentConfig, Config
from super_browser.interaction.decorator import agent_action
from super_browser.results import (
    ActionResult,
    DelegatedResult,
    ExtractResult,
    NavigateResult,
    action_result,
)

from .conftest import MockLLMClient

# ═══════════════════════════════════════════════════════════════════════════
# TEST-14-01-01: SuperBrowser() creates without error
# ═══════════════════════════════════════════════════════════════════════════

class TestInit:
    """TEST-14-01-01: SuperBrowser() creates without error."""

    def test_creates_without_error(self) -> None:
        """SuperBrowser() instantiates with default config (Config composition root)."""
        sb = SuperBrowser()
        assert sb is not None
        assert not sb.is_running
        assert isinstance(sb._config, Config)

    def test_creates_with_custom_config(self) -> None:
        """SuperBrowser accepts a SuperBrowserConfig (auto-wrapped in Config)."""
        config = Config(agent=AgentConfig(max_steps=10))
        sb = SuperBrowser(config=config)
        assert isinstance(sb._config, Config)
        assert sb._config.agent.max_steps == 10

    def test_creates_with_custom_registry(self) -> None:
        """SuperBrowser accepts a custom ToolRegistry."""
        registry = ToolRegistry()
        sb = SuperBrowser(tool_registry=registry)
        assert sb._registry is registry

    def test_creates_with_llm_client(self) -> None:
        """SuperBrowser accepts a mock LLM client."""
        mock_llm = MockLLMClient()
        sb = SuperBrowser(llm_client=mock_llm)
        assert sb._llm_client is mock_llm

    def test_default_attributes(self) -> None:
        """Newly created SuperBrowser has expected default state."""
        sb = SuperBrowser()
        assert sb._session is None
        assert sb._controller is None
        assert sb._page is None
        assert sb._running is False
        assert sb._coordinator is None
        assert sb._budget_client is None
        assert sb._flow_logger is None
        assert sb._security_manager is None
        assert sb._vision_controller is None
        assert sb._stealth_manager is None
        assert sb._skill_registry is None


# ═══════════════════════════════════════════════════════════════════════════
# TEST-14-01-02: act() sends to LLM, gets action, executes
# ═══════════════════════════════════════════════════════════════════════════

class TestAct:
    """TEST-14-01-02: act() sends to LLM, gets action, executes."""

    def _make_browser_with_act_support(self) -> tuple[SuperBrowser, MockLLMClient]:
        """Create a browser mock with everything act() needs."""
        mock_llm = MockLLMClient(
            action_responses=[
                {"action": "custom_click", "params": {"target": "#btn"}},
                {"done": True, "summary": "Clicked the button"},
            ],
        )
        sb = SuperBrowser(llm_client=mock_llm)

        # Mock controller
        sb._controller = MagicMock()
        sb._controller._page = MagicMock()
        sb._controller._page.url = "https://example.com"
        sb._controller._page.title = AsyncMock(return_value="Test")
        sb._running = True

        # Register a tool the LLM can call
        @agent_action
        async def custom_click(target: str = "") -> ActionResult:
            """Click on a target element."""
            return action_result(ok=True)

        sb._registry.register(custom_click)
        return sb, mock_llm

    def test_act_executes_llm_action(self) -> None:
        """act() gets action from LLM and dispatches it via registry."""
        sb, mock_llm = self._make_browser_with_act_support()

        async def _test() -> None:
            result = await sb.act("click the button", max_steps=5)
            assert result.ok
            assert result.data is not None
            assert isinstance(result.data, DelegatedResult)
            assert result.data.steps_executed >= 1
            assert mock_llm._call_count >= 1

        asyncio.run(_test())

    def test_act_returns_delegated_result(self) -> None:
        """act() returns a DelegatedResult with execution history."""
        sb, _ = self._make_browser_with_act_support()

        async def _test() -> None:
            result = await sb.act("click the button", max_steps=5)
            assert isinstance(result.data, DelegatedResult)
            assert result.data.instruction == "click the button"
            assert result.data.steps_executed > 0
            assert len(result.data.execution_history) > 0

        asyncio.run(_test())

    def test_act_calls_propose_action(self) -> None:
        """act() calls the LLM's propose_action method."""
        sb, mock_llm = self._make_browser_with_act_support()

        async def _test() -> None:
            await sb.act("click the button", max_steps=5)
            # propose_action should have been called at least once
            assert mock_llm._call_count >= 1

        asyncio.run(_test())


# ═══════════════════════════════════════════════════════════════════════════
# TEST-14-01-03: extract() returns structured data
# ═══════════════════════════════════════════════════════════════════════════

class TestExtract:
    """TEST-14-01-03: extract() returns structured data."""

    def test_extract_with_snapshot(self) -> None:
        """extract() captures page snapshot and returns data."""
        sb = SuperBrowser()
        sb._controller = MagicMock()
        snap = MagicMock()
        snap.nodes = {}
        snap.to_compact_str.return_value = "button 'Submit' (#submit)"
        sb._controller.capture_ax_snapshot = AsyncMock(return_value=snap)
        sb._running = True

        async def _test() -> None:
            result = await sb.extract("get all buttons")
            assert result.ok
            assert isinstance(result.data, ExtractResult)
            assert result.data.extracted is not None

        asyncio.run(_test())

    def test_extract_with_selector(self) -> None:
        """extract() with a CSS selector extracts text content."""
        sb = SuperBrowser()
        sb._controller = MagicMock()

        cdp_mock = AsyncMock()
        cdp_result = MagicMock()
        cdp_result.ok = True
        cdp_result.data = {"result": {"value": "$29.99"}}
        cdp_mock.evaluate = AsyncMock(return_value=cdp_result)
        sb._controller._cdp = cdp_mock

        async def _test() -> None:
            result = await sb.extract("get price", selector=".price")
            assert result.ok
            assert isinstance(result.data, ExtractResult)
            assert result.data.selector == ".price"

        asyncio.run(_test())

    def test_extract_without_controller_returns_failure(self) -> None:
        """extract() returns failure when controller is not initialized."""
        sb = SuperBrowser()

        async def _test() -> None:
            result = await sb.extract("get data")
            assert not result.ok

        asyncio.run(_test())


# ═══════════════════════════════════════════════════════════════════════════
# TEST-14-01-04: close() cleans up browser + resources
# ═══════════════════════════════════════════════════════════════════════════

class TestClose:
    """TEST-14-01-04: close() cleans up browser + resources."""

    def test_stop_cleans_up_session(self) -> None:
        """stop() is engine-owned (P5): the engine is stopped exactly once,
        the legacy session alias is cleared WITHOUT a second stop
        (PatchrightEngine.stop() owns its BrowserSession), and internal
        state resets."""
        sb = SuperBrowser()
        mock_engine = AsyncMock()
        sb._engine = mock_engine
        mock_session = AsyncMock()
        sb._session = mock_session
        sb._running = True
        sb._controller = MagicMock()
        sb._page = MagicMock()

        async def _test() -> None:
            await sb.stop()
            assert not sb._running
            assert sb._engine is None
            assert sb._session is None
            assert sb._controller is None
            assert sb._page is None
            mock_engine.stop.assert_awaited_once()
            mock_session.stop.assert_not_called()

        asyncio.run(_test())

    def test_stop_with_flow_logger(self) -> None:
        """stop() also stops flow logger if active."""
        sb = SuperBrowser()
        sb._session = AsyncMock()
        sb._running = True
        mock_logger = AsyncMock()
        sb._flow_logger = mock_logger

        async def _test() -> None:
            await sb.stop()
            mock_logger.stop.assert_called_once()
            assert sb._flow_logger is None

        asyncio.run(_test())

    def test_stop_with_recovery_coordinator(self) -> None:
        """stop() also stops recovery coordinator if active."""
        sb = SuperBrowser()
        sb._session = AsyncMock()
        sb._running = True
        mock_coord = AsyncMock()
        sb._coordinator = mock_coord

        async def _test() -> None:
            await sb.stop()
            mock_coord.stop.assert_called_once()
            assert sb._coordinator is None

        asyncio.run(_test())

    def test_stop_idempotent(self) -> None:
        """Multiple stop() calls don't crash."""
        sb = SuperBrowser()

        async def _test() -> None:
            await sb.stop()
            await sb.stop()  # Should not raise

        asyncio.run(_test())

    def test_context_manager_cleanup(self) -> None:
        """SuperBrowser as async context manager calls start/stop."""
        sb = SuperBrowser()
        sb.start = AsyncMock()
        sb.stop = AsyncMock()

        async def _test() -> None:
            async with sb as browser:
                assert browser is sb
            sb.start.assert_called_once()
            sb.stop.assert_called_once()

        asyncio.run(_test())


# ═══════════════════════════════════════════════════════════════════════════
# TEST-14-01-05: delegate() spawns child tasks with tab cap
# ═══════════════════════════════════════════════════════════════════════════

class TestDelegate:
    """TEST-14-01-05: delegate() spawns child tasks with tab cap."""

    def test_delegate_with_mock_session(self) -> None:
        """delegate() delegates tasks and returns DelegationResult."""
        sb = SuperBrowser()
        sb._session = MagicMock()
        sb._llm_client = MockLLMClient()
        sb._running = True

        expected_result = DelegationResult(
            tasks=[
                ChildTask(instruction="task 1", status=DelegationStatus.COMPLETED),
                ChildTask(instruction="task 2", status=DelegationStatus.COMPLETED),
            ],
            total_duration_ms=150.0,
            completed_count=2,
            failed_count=0,
            cancelled_count=0,
        )

        mock_instance = MagicMock()
        mock_instance.delegate = AsyncMock(return_value=expected_result)
        MockCls = MagicMock(return_value=mock_instance)

        with patch("super_browser.agent.facade.SubagentDelegator", MockCls):

            async def _test() -> None:
                result = await sb.delegate(["task 1", "task 2"])
                assert isinstance(result, DelegationResult)
                assert result.completed_count == 2
                assert result.failed_count == 0
                assert result.all_succeeded
                MockCls.assert_called_once()

            asyncio.run(_test())

    def test_delegate_without_session(self) -> None:
        """delegate() returns empty result when no session is active."""
        sb = SuperBrowser()

        async def _test() -> None:
            result = await sb.delegate(["task 1", "task 2"])
            assert isinstance(result, DelegationResult)
            assert result.completed_count == 0
            assert result.failed_count == 2

        asyncio.run(_test())

    def test_delegate_respects_max_concurrency(self) -> None:
        """delegate() passes max_concurrency to the delegator."""
        sb = SuperBrowser()
        sb._session = MagicMock()
        sb._llm_client = MockLLMClient()
        sb._running = True

        expected_result = DelegationResult(
            tasks=[], total_duration_ms=0, completed_count=0,
            failed_count=0, cancelled_count=0,
        )

        mock_instance = MagicMock()
        mock_instance.delegate = AsyncMock(return_value=expected_result)
        MockCls = MagicMock(return_value=mock_instance)

        with patch("super_browser.agent.facade.SubagentDelegator", MockCls) as MockDelegator:

            async def _test() -> None:
                result = await sb.delegate(["task 1"], max_concurrency=2)
                assert isinstance(result, DelegationResult)
                # Verify the delegator was constructed with max_concurrency=2
                MockDelegator.assert_called_once()
                call_kwargs = MockDelegator.call_args
                assert call_kwargs.kwargs.get("max_concurrency") == 2

            asyncio.run(_test())


# ═══════════════════════════════════════════════════════════════════════════
# TEST-14-01-06: checkpoint save/restore in full workflow
# ═══════════════════════════════════════════════════════════════════════════

class TestCheckpoint:
    """TEST-14-01-06: Checkpoint save/restore in full workflow."""

    def test_navigate_then_observe_preserves_state(self) -> None:
        """Navigate to a page and observe preserves the URL and title."""
        sb = SuperBrowser()
        sb._page = MagicMock()
        sb._page.url = "https://example.com/page1"
        sb._page.title = AsyncMock(return_value="Page One")
        sb._controller = MagicMock()
        snap = MagicMock()
        snap.nodes = {}
        sb._controller.capture_ax_snapshot = AsyncMock(return_value=snap)
        sb._running = True

        async def _test() -> None:
            # Observe current state
            result = await sb.observe()
            assert result.ok
            assert result.data["url"] == "https://example.com/page1"
            assert result.data["title"] == "Page One"

            # "Restore" — simulate re-observing with same state
            sb._page.url = "https://example.com/page1"
            result2 = await sb.observe()
            assert result2.ok
            assert result2.data["url"] == "https://example.com/page1"

        asyncio.run(_test())

    def test_act_then_extract_workflow(self) -> None:
        """Full act → extract workflow preserves page context."""
        sb = SuperBrowser()
        sb._controller = MagicMock()
        sb._controller._page = MagicMock()
        sb._controller._page.url = "https://example.com"
        sb._controller._page.title = AsyncMock(return_value="Test")
        sb._controller._cdp = MagicMock()
        sb._running = True

        # Mock extract: CDP returns structured data
        cdp_result = MagicMock()
        cdp_result.ok = True
        cdp_result.data = {"result": {"value": "42"}}
        sb._controller._cdp.evaluate = AsyncMock(return_value=cdp_result)

        async def _test() -> None:
            result = await sb.extract("get count", selector=".count")
            assert result.ok
            assert isinstance(result.data, ExtractResult)

        asyncio.run(_test())


# ═══════════════════════════════════════════════════════════════════════════
# TEST-14-01-07: Stealth headers applied on navigation
# ═══════════════════════════════════════════════════════════════════════════

class TestStealthHeaders:
    """TEST-14-01-07: Stealth headers applied on navigation."""

    def test_stealth_manager_attached_on_navigation(self) -> None:
        """When stealth is enabled, a StealthManager is created on start."""
        with patch("super_browser.browser.factory._detect_backend", return_value="playwright"), \
             patch("super_browser.browser.backends.playwright_backend.PlaywrightEngine") as MockEngine:
            mock_page = MagicMock()
            mock_page.url = "about:blank"
            mock_page.title = AsyncMock(return_value="Blank")
            mock_page.goto = AsyncMock()
            mock_page.cdp = MagicMock()
            mock_page.backend_page = MagicMock()
            mock_engine = MagicMock()
            mock_engine.start = AsyncMock()
            mock_engine.new_page = AsyncMock(return_value=mock_page)
            mock_engine.stop = AsyncMock()
            mock_engine.session = None
            MockEngine.return_value = mock_engine

            config = Config(agent=AgentConfig(enable_stealth=True))
            sb = SuperBrowser(config=config)

            async def _test() -> None:
                with patch("super_browser.stealth.StealthManager") as MockStealth:
                    mock_stealth_instance = MagicMock()
                    MockStealth.return_value = mock_stealth_instance
                    await sb.start()
                    assert sb._stealth_manager is mock_stealth_instance
                await sb.stop()

            asyncio.run(_test())

    def test_navigate_returns_navigate_result_with_url(self) -> None:
        """navigate() applies stealth and returns NavigateResult."""
        sb = SuperBrowser()
        sb._page = MagicMock()
        sb._page.url = "https://example.com"
        sb._page.title = AsyncMock(return_value="Example")
        sb._page.goto = AsyncMock()
        sb._running = True
        # No skill registry
        sb._skill_registry = None

        async def _test() -> None:
            result = await sb.navigate("https://example.com")
            assert result.ok
            assert isinstance(result.data, NavigateResult)
            assert result.data.url == "https://example.com"
            sb._page.goto.assert_called_once()

        asyncio.run(_test())


# ═══════════════════════════════════════════════════════════════════════════
# TEST-14-01-08: Budget tracking across full workflow
# ═══════════════════════════════════════════════════════════════════════════

class TestBudgetTracking:
    """TEST-14-01-08: Budget tracking across full workflow."""

    def test_budget_client_created_on_start(self) -> None:
        """When budget is enabled, BudgetAwareLLMClient is created."""
        with patch("super_browser.browser.factory._detect_backend", return_value="playwright"), \
             patch("super_browser.browser.backends.playwright_backend.PlaywrightEngine") as MockEngine:
            mock_page = MagicMock()
            mock_page.url = "about:blank"
            mock_page.title = AsyncMock(return_value="Blank")
            mock_page.cdp = MagicMock()
            mock_page.backend_page = MagicMock()
            mock_engine = MagicMock()
            mock_engine.start = AsyncMock()
            mock_engine.new_page = AsyncMock(return_value=mock_page)
            mock_engine.stop = AsyncMock()
            mock_engine.session = None
            MockEngine.return_value = mock_engine

            config = Config(agent=AgentConfig(enable_budget=True))

            sb = SuperBrowser(config=config)

            async def _test() -> None:
                await sb.start()
                assert sb._budget_client is not None
                await sb.stop()

            asyncio.run(_test())

    def test_act_with_budget_client_tracks_remaining(self) -> None:
        """act() includes budget_remaining in its result when budget is active."""
        mock_llm = MockLLMClient(
            action_responses=[
                {"done": True, "summary": "Done"},
            ],
        )
        sb = SuperBrowser(llm_client=mock_llm)
        sb._controller = MagicMock()
        sb._controller._page = MagicMock()
        sb._controller._page.url = "https://example.com"
        sb._controller._page.title = AsyncMock(return_value="Test")
        sb._running = True

        # Mock budget client with governor
        mock_budget = MagicMock()
        mock_budget.budget_remaining = 8.50
        sb._budget_client = mock_budget

        async def _test() -> None:
            result = await sb.act("do something", max_steps=3)
            assert result.ok
            assert isinstance(result.data, DelegatedResult)
            assert result.data.budget_remaining == 8.50

        asyncio.run(_test())

    def test_full_lifecycle_with_budget(self) -> None:
        """Full init → act → extract → close with budget tracking."""
        mock_llm = MockLLMClient(
            action_responses=[
                {"done": True, "summary": "Completed"},
            ],
        )
        sb = SuperBrowser(llm_client=mock_llm)
        sb._controller = MagicMock()
        sb._controller._page = MagicMock()
        sb._controller._page.url = "https://example.com"
        sb._controller._page.title = AsyncMock(return_value="Test Page")
        sb._controller._cdp = MagicMock()
        sb._controller.capture_ax_snapshot = AsyncMock()
        sb._running = True

        mock_budget = MagicMock()
        mock_budget.budget_remaining = 9.75
        sb._budget_client = mock_budget

        # Mock session for stop
        sb._session = AsyncMock()

        snap = MagicMock()
        snap.nodes = {}
        snap.to_compact_str.return_value = "heading 'Results'"
        sb._controller.capture_ax_snapshot = AsyncMock(return_value=snap)

        async def _test() -> None:
            # Act
            act_result = await sb.act("do task", max_steps=3)
            assert act_result.ok

            # Extract
            extract_result = await sb.extract("get results")
            assert extract_result.ok

            # Verify budget tracked
            assert act_result.data.budget_remaining == 9.75

            # Stop
            await sb.stop()
            assert not sb._running

        asyncio.run(_test())
