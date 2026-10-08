"""Tests for SubagentDelegator."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from super_browser.agent.delegator import SubagentDelegator
from super_browser.agent.registry import ToolRegistry


def _make_session_mock():
    session = MagicMock()

    async def make_page():
        page = MagicMock()
        page.url = "https://example.com"
        page.title = AsyncMock(return_value="Child Page")
        page.close = AsyncMock()
        page.cdp = MagicMock()
        return page

    session.new_page = AsyncMock(side_effect=make_page)
    return session


class _NoOpLLM:
    async def propose_action(self, prompt, *, tools=None):
        return {"done": True}

    async def create_plan(self, instruction, *, tools=None):
        return [{"description": instruction}]

    async def replan(self, **kwargs):
        return [{"description": "retry"}]


class TestSubagentDelegator:
    def test_parallel_execution(self):
        async def _test():
            session = _make_session_mock()
            registry = ToolRegistry()
            delegator = SubagentDelegator(session, registry, _NoOpLLM(), max_concurrency=2)
            result = await delegator.delegate(["task A", "task B", "task C"], max_concurrency=2)
            assert result.completed_count == 3
            assert result.failed_count == 0
        asyncio.run(_test())

    def test_child_isolation(self):
        async def _test():
            session = _make_session_mock()
            call_count = 0  # noqa: F841
            original_url = "https://example.com"  # noqa: F841

            registry = ToolRegistry()
            delegator = SubagentDelegator(session, registry, _NoOpLLM(), max_concurrency=1)
            result = await delegator.delegate(["task A"])
            # Parent state should be unchanged
            assert result.completed_count == 1
        asyncio.run(_test())

    def test_concurrency_limit(self):
        async def _test():
            session = _make_session_mock()
            registry = ToolRegistry()

            running = 0
            max_running = 0

            class SlowLLM(_NoOpLLM):
                async def propose_action(self, prompt, *, tools=None):
                    nonlocal running, max_running
                    running += 1
                    max_running = max(max_running, running)
                    await asyncio.sleep(0.05)
                    running -= 1
                    return {"done": True}

            delegator = SubagentDelegator(session, registry, SlowLLM(), max_concurrency=2)
            result = await delegator.delegate(["A", "B", "C", "D"], max_concurrency=2)
            assert result.completed_count == 4
            assert max_running <= 2
        asyncio.run(_test())

    def test_abort_signal(self):
        async def _test():
            session = _make_session_mock()
            registry = ToolRegistry()
            signal = asyncio.Event()
            signal.set()
            delegator = SubagentDelegator(session, registry, _NoOpLLM(), max_concurrency=2)
            result = await delegator.delegate(["A", "B"], abort_signal=signal)
            assert result.cancelled_count == 2
        asyncio.run(_test())

    # -- C5: Children inherit subsystems --

    def test_children_inherit_subsystems(self):
        """C5: Child AgentLoop should receive all parent subsystems."""
        async def _test():
            session = _make_session_mock()
            registry = ToolRegistry()

            async def click_handler(target: str = "#btn"):
                from super_browser.results import action_result
                return action_result(ok=True, data={"target": target})
            registry.register(click_handler, toolsets=())

            mock_recovery = MagicMock()
            mock_recovery.execute_with_recovery = AsyncMock(
                side_effect=lambda action_fn, **kw: action_fn()
            )
            mock_security = MagicMock()
            mock_flow = MagicMock()

            # LLM that returns one action then done
            class ActionThenDoneLLM:
                def __init__(self):
                    self.called = False
                async def propose_action(self, prompt, *, tools=None):
                    if not self.called:
                        self.called = True
                        return {"action": "click_handler", "params": {"target": "#btn"}}
                    return {"done": True}
                async def create_plan(self, instruction, *, tools=None):
                    return [{"description": instruction}]
                async def replan(self, **kwargs):
                    return [{"description": "retry"}]

            delegator = SubagentDelegator(
                session, registry, ActionThenDoneLLM(),
                max_concurrency=2,
                recovery_coordinator=mock_recovery,
                flow_logger=mock_flow,
                security_manager=mock_security,
            )
            result = await delegator.delegate(["task A"])
            assert result.completed_count == 1
            # Recovery coordinator should have been called by the child
            assert mock_recovery.execute_with_recovery.call_count >= 1
        asyncio.run(_test())

    def test_children_work_without_subsystems(self):
        """C5: Backward compat — children still work when no subsystems are provided."""
        async def _test():
            session = _make_session_mock()
            registry = ToolRegistry()
            delegator = SubagentDelegator(
                session, registry, _NoOpLLM(), max_concurrency=2,
            )
            result = await delegator.delegate(["task A", "task B"])
            assert result.completed_count == 2
        asyncio.run(_test())
