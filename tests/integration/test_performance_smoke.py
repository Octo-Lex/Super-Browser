"""TEST-14-02-05 through TEST-14-02-07: Performance smoke tests.

Baseline performance assertions:
- Cold start <5s
- Single action latency <2s
- 10 sequential actions <30s

All LLM calls mocked — no API keys required (HB-14-01).
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

from super_browser.agent.facade import SuperBrowser
from super_browser.agent.loop import AgentLoop
from super_browser.agent.registry import ToolRegistry
from super_browser.config import AgentConfig, Config
from super_browser.interaction.decorator import agent_action
from super_browser.results import ActionResult, action_result

from .conftest import MockLLMClient


def _mocked_browser_for_perf() -> SuperBrowser:
    """Create a mocked browser suitable for performance tests."""
    mock_llm = MockLLMClient(
        action_responses=[{"done": True, "summary": "Done"}],
    )
    sb = SuperBrowser(llm_client=mock_llm)
    sb._session = MagicMock()
    sb._page = MagicMock()
    sb._page.url = "https://example.com"
    sb._page.title = AsyncMock(return_value="Test")
    sb._page.goto = AsyncMock()
    sb._page.raw_page = MagicMock()
    sb._page.cdp = MagicMock()
    sb._controller = MagicMock()
    sb._controller._page = sb._page
    sb._controller._cdp = MagicMock()
    sb._controller._snapshot_provider = MagicMock()
    sb._controller.click = AsyncMock(return_value=action_result(ok=True))
    sb._controller.fill = AsyncMock(return_value=action_result(ok=True))
    sb._controller.capture_ax_snapshot = AsyncMock(return_value=MagicMock(
        nodes={}, to_compact_str=MagicMock(return_value=""),
    ))
    sb._running = True
    return sb


# ═══════════════════════════════════════════════════════════════════════════
# TEST-14-02-05: Cold start <5s (mocked LLM)
# ═══════════════════════════════════════════════════════════════════════════

class TestColdStart:
    """TEST-14-02-05: SuperBrowser() cold start <5s."""

    def test_init_completes_under_5s(self) -> None:
        """SuperBrowser() instantiation takes <5s (with mocked LLM)."""
        start = time.monotonic()
        for _ in range(100):
            sb = SuperBrowser()
            assert sb is not None
        elapsed = time.monotonic() - start
        # 100 instantiations should complete in well under 5s
        assert elapsed < 5.0, f"100 inits took {elapsed:.2f}s, expected <5s"

    def test_init_with_config_under_5s(self) -> None:
        """SuperBrowser(config=...) instantiation takes <5s."""
        config = Config(agent=AgentConfig(
                max_steps=50,
                trace_enabled=True,
                enable_recovery=False,
                enable_budget=False,
            ))

        start = time.monotonic()
        for _ in range(100):
            sb = SuperBrowser(config=config)
            assert sb is not None
        elapsed = time.monotonic() - start
        assert elapsed < 5.0, f"100 configured inits took {elapsed:.2f}s"

    def test_start_with_mocked_browser_under_5s(self) -> None:
        """SuperBrowser.start() with mocked engine completes <5s."""
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
                    start = time.monotonic()
                    await sb.start()
                    elapsed = time.monotonic() - start
                    assert elapsed < 5.0, f"start() took {elapsed:.2f}s, expected <5s"
                    await sb.stop()

                asyncio.run(_test())


# ═══════════════════════════════════════════════════════════════════════════
# TEST-14-02-06: Single action latency <2s (mocked LLM)
# ═══════════════════════════════════════════════════════════════════════════

class TestSingleActionLatency:
    """TEST-14-02-06: Single action latency <2s with mocked LLM."""

    def test_act_completes_under_2s(self) -> None:
        """act() with mocked LLM completes in <2s."""
        sb = _mocked_browser_for_perf()

        async def _test() -> None:
            start = time.monotonic()
            result = await sb.act("do something", max_steps=3)
            elapsed = time.monotonic() - start
            assert result.ok
            assert elapsed < 2.0, f"act() took {elapsed:.2f}s, expected <2s"

        asyncio.run(_test())

    def test_navigate_completes_under_2s(self) -> None:
        """navigate() completes in <2s."""
        sb = _mocked_browser_for_perf()

        async def _test() -> None:
            start = time.monotonic()
            result = await sb.navigate("https://example.com")
            elapsed = time.monotonic() - start
            assert result.ok
            assert elapsed < 2.0, f"navigate() took {elapsed:.2f}s"

        asyncio.run(_test())

    def test_extract_completes_under_2s(self) -> None:
        """extract() completes in <2s."""
        sb = _mocked_browser_for_perf()

        async def _test() -> None:
            start = time.monotonic()
            result = await sb.extract("get data")
            elapsed = time.monotonic() - start
            assert result.ok
            assert elapsed < 2.0, f"extract() took {elapsed:.2f}s"

        asyncio.run(_test())

    def test_observe_completes_under_2s(self) -> None:
        """observe() completes in <2s."""
        sb = _mocked_browser_for_perf()

        async def _test() -> None:
            start = time.monotonic()
            result = await sb.observe()
            elapsed = time.monotonic() - start
            assert result.ok
            assert elapsed < 2.0, f"observe() took {elapsed:.2f}s"

        asyncio.run(_test())


# ═══════════════════════════════════════════════════════════════════════════
# TEST-14-02-07: 10 sequential actions complete in <30s
# ═══════════════════════════════════════════════════════════════════════════

class TestSequentialActions:
    """TEST-14-02-07: 10 sequential actions complete in <30s."""

    def test_ten_act_calls_under_30s(self) -> None:
        """10 sequential act() calls complete in <30s with mocked LLM."""
        sb = _mocked_browser_for_perf()

        async def _test() -> None:
            start = time.monotonic()
            for i in range(10):
                # Reset mock LLM call count for each iteration
                sb._llm_client._call_count = 0
                result = await sb.act(f"task {i}", max_steps=3)
                assert result.ok, f"act() failed on iteration {i}"
            elapsed = time.monotonic() - start
            assert elapsed < 30.0, f"10 sequential acts took {elapsed:.2f}s, expected <30s"

        asyncio.run(_test())

    def test_ten_mixed_actions_under_30s(self) -> None:
        """10 mixed actions (navigate/extract/observe/act) complete in <30s."""
        sb = _mocked_browser_for_perf()

        async def _test() -> None:
            start = time.monotonic()
            for i in range(10):
                if i % 4 == 0:
                    result = await sb.navigate("https://example.com")
                elif i % 4 == 1:
                    result = await sb.extract("get data")
                elif i % 4 == 2:
                    result = await sb.observe()
                else:
                    sb._llm_client._call_count = 0
                    result = await sb.act(f"task {i}", max_steps=3)
                assert result.ok, f"Action {i} failed"
            elapsed = time.monotonic() - start
            assert elapsed < 30.0, f"10 mixed actions took {elapsed:.2f}s, expected <30s"

        asyncio.run(_test())

    def test_ten_loop_iterations_under_30s(self) -> None:
        """AgentLoop with 10 steps (mocked LLM) completes in <30s."""
        # Create an LLM that returns 10 actions then done
        action_responses = [
            {"action": "click", "params": {"target": f"#btn{i}"}}
            for i in range(9)
        ] + [
            {"done": True, "summary": "All done"},
        ]

        mock_llm = MockLLMClient(action_responses=action_responses)
        registry = ToolRegistry()

        @agent_action
        async def click(target: str = "") -> ActionResult:
            """Click action."""
            return action_result(ok=True)

        registry.register(click)

        controller = MagicMock()
        controller._page = MagicMock()
        controller._page.url = "https://example.com"
        controller._page.title = AsyncMock(return_value="Test")

        loop = AgentLoop(
            controller=controller,
            registry=registry,
            llm_client=mock_llm,
            max_steps=15,
        )

        async def _test() -> None:
            start = time.monotonic()
            result = await loop.run("click all buttons")
            elapsed = time.monotonic() - start
            assert result.total_steps == 10, f"Expected 10 steps, got {result.total_steps}"
            assert elapsed < 30.0, f"10 loop steps took {elapsed:.2f}s, expected <30s"

        asyncio.run(_test())
