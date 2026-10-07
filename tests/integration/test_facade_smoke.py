"""Facade smoke tests — verify every public method on SuperBrowser.

Imports work, no crashes. All LLM calls are mocked (HB-14-01).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from super_browser.agent.facade import SuperBrowser
from super_browser.interaction.decorator import agent_action
from super_browser.results import action_result

from .conftest import MockLLMClient


def _mocked_browser() -> SuperBrowser:
    """Return a fully mocked SuperBrowser ready for smoke testing."""
    sb = SuperBrowser()
    sb._session = MagicMock()
    sb._page = MagicMock()
    sb._page.url = "https://example.com"
    sb._page.title = AsyncMock(return_value="Example")
    sb._page.goto = AsyncMock()
    sb._page.close = AsyncMock()
    sb._page.raw_page = MagicMock()
    sb._page.cdp = MagicMock()
    sb._controller = MagicMock()
    sb._controller._page = sb._page
    sb._controller._cdp = MagicMock()
    sb._controller._snapshot_provider = MagicMock()
    sb._controller.click = AsyncMock(return_value=action_result(ok=True))
    sb._controller.fill = AsyncMock(return_value=action_result(ok=True))
    sb._controller.capture_ax_snapshot = AsyncMock(return_value=MagicMock(nodes={}, to_compact_str=MagicMock(return_value="")))
    sb._running = True
    return sb


# ═══════════════════════════════════════════════════════════════════════════
# Import smoke
# ═══════════════════════════════════════════════════════════════════════════

class TestImportSmoke:
    """Verify all public imports work."""

    def test_import_super_browser(self) -> None:
        from super_browser import SuperBrowser
        assert SuperBrowser is not None

    def test_import_config(self) -> None:
        from super_browser import Config
        assert Config is not None

    def test_import_action_result(self) -> None:
        from super_browser import ActionResult
        assert ActionResult is not None

    def test_import_create_llm(self) -> None:
        from super_browser import create_llm
        assert create_llm is not None


# ═══════════════════════════════════════════════════════════════════════════
# Method smoke — each public method called once, no crash
# ═══════════════════════════════════════════════════════════════════════════

class TestNavigateSmoke:

    def test_navigate_no_crash(self) -> None:
        sb = _mocked_browser()

        async def _test() -> None:
            result = await sb.navigate("https://example.com")
            assert result.ok
        asyncio.run(_test())


class TestClickSmoke:

    def test_click_no_crash(self) -> None:
        sb = _mocked_browser()

        async def _test() -> None:
            result = await sb.click("#btn")
            assert result.ok
        asyncio.run(_test())


class TestFillSmoke:

    def test_fill_no_crash(self) -> None:
        sb = _mocked_browser()

        async def _test() -> None:
            result = await sb.fill("#email", "test@example.com")
            assert result.ok
        asyncio.run(_test())


class TestActSmoke:

    def test_act_no_crash(self) -> None:
        sb = _mocked_browser()
        sb._llm_client = MockLLMClient(
            action_responses=[{"done": True, "summary": "Done"}],
        )

        async def _test() -> None:
            result = await sb.act("do something", max_steps=3)
            assert result.ok
        asyncio.run(_test())


class TestExtractSmoke:

    def test_extract_no_crash(self) -> None:
        sb = _mocked_browser()

        async def _test() -> None:
            result = await sb.extract("get data")
            assert result.ok
        asyncio.run(_test())


class TestObserveSmoke:

    def test_observe_no_crash(self) -> None:
        sb = _mocked_browser()

        async def _test() -> None:
            result = await sb.observe()
            assert result.ok
        asyncio.run(_test())


class TestDelegateSmoke:

    def test_delegate_no_crash(self) -> None:
        sb = _mocked_browser()
        # No session set → returns empty DelegationResult
        sb._session = None

        async def _test() -> None:
            result = await sb.delegate(["task 1"])
            assert result is not None
        asyncio.run(_test())


class TestToolsSmoke:

    def test_tools_no_crash(self) -> None:
        sb = _mocked_browser()
        desc = sb.tools()
        assert isinstance(desc, str)


class TestRegisterToolSmoke:

    def test_register_tool_no_crash(self) -> None:
        sb = _mocked_browser()

        @agent_action
        async def my_tool(x: int) -> None:
            """Test tool."""

        sb.register_tool(my_tool)
        assert sb._registry.get("my_tool") is not None


class TestAbortSmoke:

    def test_abort_no_crash(self) -> None:
        sb = SuperBrowser()
        sb.abort()
        assert sb._abort_signal.is_set()


class TestIsRunningSmoke:

    def test_is_running_no_crash(self) -> None:
        sb = SuperBrowser()
        assert not sb.is_running

        sb2 = _mocked_browser()
        assert sb2.is_running


class TestLifecycleSmoke:

    def test_start_stop_no_crash(self) -> None:
        """start/stop with mocked engine."""
        sb = SuperBrowser()

        with patch("super_browser.browser.factory._detect_backend", return_value="patchright"):
            with patch("super_browser.browser.backends.patchright_backend.PatchrightEngine") as MockEngine:
                mock_engine = AsyncMock()
                mock_page = MagicMock()
                mock_page.url = "about:blank"
                mock_page.title = AsyncMock(return_value="Blank")
                mock_page.engine_page = MagicMock()
                mock_page.engine_page.cdp = MagicMock()
                mock_page.raw_page = MagicMock()
                mock_session = AsyncMock()
                mock_session._context = MagicMock()
                mock_engine.session = mock_session
                mock_engine.new_page = AsyncMock(return_value=mock_page)
                mock_engine.start = AsyncMock()
                mock_engine.stop = AsyncMock()
                MockEngine.return_value = mock_engine

                async def _test() -> None:
                    await sb.start()
                    assert sb.is_running
                    await sb.stop()
                    assert not sb.is_running
                asyncio.run(_test())

    def test_context_manager_no_crash(self) -> None:
        """Async context manager start/stop."""
        sb = SuperBrowser()
        sb.start = AsyncMock()
        sb.stop = AsyncMock()

        async def _test() -> None:
            async with sb:
                pass
        asyncio.run(_test())


class TestConfigureVerificationSmoke:

    def test_configure_verification_no_crash(self) -> None:
        sb = _mocked_browser()
        # configure_verification should not crash
        sb.configure_verification()
