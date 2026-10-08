"""SubagentDelegator — parallel child agents with isolated browser contexts."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from super_browser.agent.loop import AgentLoop
from super_browser.agent.registry import ToolRegistry
from super_browser.agent.types import ChildTask, DelegationResult, DelegationStatus

logger = logging.getLogger(__name__)


class SubagentDelegator:

    def __init__(
        self,
        browser_session: Any,
        registry: ToolRegistry,
        llm_client: Any,
        *,
        max_concurrency: int = 4,
        max_steps_per_child: int = 50,
        recovery_coordinator: Optional[Any] = None,
        flow_logger: Optional[Any] = None,
        security_manager: Optional[Any] = None,
        stealth_manager: Optional[Any] = None,
    ) -> None:
        self._session = browser_session
        self._registry = registry
        self._llm = llm_client
        self._max_concurrency = max_concurrency
        self._max_steps = max_steps_per_child
        self._recovery_coordinator = recovery_coordinator
        self._flow_logger = flow_logger
        self._security_manager = security_manager
        self._stealth_manager = stealth_manager
        self._open_tabs = 0

    @property
    def open_tabs(self) -> int:
        """Current number of open child tabs (read-only)."""
        return self._open_tabs

    async def delegate(
        self,
        tasks: list[str],
        *,
        max_concurrency: Optional[int] = None,
        abort_signal: Optional[asyncio.Event] = None,
    ) -> DelegationResult:
        start = time.monotonic()
        concurrency = max_concurrency or self._max_concurrency
        semaphore = asyncio.Semaphore(concurrency)

        children = [ChildTask(instruction=instr) for instr in tasks]

        async def _run_with_semaphore(task: ChildTask) -> ChildTask:
            async with semaphore:
                if abort_signal and abort_signal.is_set():
                    task.status = DelegationStatus.CANCELLED
                    return task
                return await self._run_child(task)

        results = await asyncio.gather(
            *[_run_with_semaphore(c) for c in children],
            return_exceptions=True,
        )

        final_tasks: list[ChildTask] = []
        for r in results:
            if isinstance(r, Exception):
                failed = ChildTask(instruction="error", status=DelegationStatus.FAILED, result=str(r))
                final_tasks.append(failed)
            else:
                final_tasks.append(r)

        duration = (time.monotonic() - start) * 1000
        completed = sum(1 for t in final_tasks if t.status == DelegationStatus.COMPLETED)
        failed = sum(1 for t in final_tasks if t.status == DelegationStatus.FAILED)
        cancelled = sum(1 for t in final_tasks if t.status == DelegationStatus.CANCELLED)

        # Sanity check — should never happen, but catch programming errors.
        assert self._open_tabs <= concurrency, (
            f"Tab cap violated: {self._open_tabs} open tabs > {concurrency} max_concurrency"
        )

        return DelegationResult(
            tasks=final_tasks,
            total_duration_ms=duration,
            completed_count=completed,
            failed_count=failed,
            cancelled_count=cancelled,
        )

    async def _run_child(self, task: ChildTask) -> ChildTask:
        task.status = DelegationStatus.RUNNING
        task.started_at = time.monotonic()

        page = None
        try:
            self._open_tabs += 1
            assert self._open_tabs <= self._max_concurrency, (
                f"Hard tab cap violated: {self._open_tabs} > {self._max_concurrency}"
            )
            page = await self._session.new_page()

            from super_browser.interaction.controller import MultimodalController
            controller = MultimodalController(page, page.cdp)

            child_loop = AgentLoop(
                controller=controller,
                registry=self._registry,
                llm_client=self._llm,
                max_steps=self._max_steps,
                recovery_coordinator=self._recovery_coordinator,
                flow_logger=self._flow_logger,
                security_manager=self._security_manager,
                stealth_manager=self._stealth_manager,
            )
            loop_result = await child_loop.run(task.instruction)

            task.result = loop_result
            task.status = DelegationStatus.COMPLETED

        except Exception as exc:
            logger.warning("Child task %s failed: %s", task.task_id[:8], exc)
            task.result = str(exc)
            task.status = DelegationStatus.FAILED
        finally:
            if page:
                try:
                    await page.close()
                except Exception:
                    pass
            self._open_tabs -= 1

        task.completed_at = time.monotonic()
        return task
