"""SuperBrowser facade — primary entry point for all browser automation."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable
from typing import TYPE_CHECKING, Any, Optional

from super_browser.agent.delegator import SubagentDelegator
from super_browser.agent.loop import AgentLoop
from super_browser.agent.registry import ToolRegistry
from super_browser.agent.types import DelegationResult, StepEvent, StreamEvent
from super_browser.browser.config import SessionConfig
from super_browser.browser.engine import _detect_backend
from super_browser.browser.session import BrowserSession
from super_browser.browser.tabs import TabManager, TabSnapshot
from super_browser.config import Config
from super_browser.interaction.controller import MultimodalController
from super_browser.results import (
    ActionError,
    ActionMethod,
    ActionResult,
    CompletionReason,
    DelegatedResult,
    DownloadResult,
    ErrorCategory,
    ExtractResult,
    NavigateResult,
    NetworkInterceptResult,
    ShadowQueryResult,
    UploadResult,
    action_result,
    timed_action_result,
)

if TYPE_CHECKING:
    from super_browser.agent.llm.protocol import LLMClient
    from super_browser.memory.store import MemoryStore

logger = logging.getLogger(__name__)


class SuperBrowser:

    def __init__(
        self,
        config: Optional[Config] = None,
        *,
        tool_registry: Optional[ToolRegistry] = None,
        llm_client: Optional[LLMClient] = None,
    ) -> None:
        if config is None:
            self._config = Config()
        elif isinstance(config, Config):
            self._config = config
        else:
            raise TypeError(
                f"config must be Config or None, got {type(config).__name__}"
            )
        self._registry = tool_registry or ToolRegistry()
        self._llm_client = llm_client
        self._session: Optional[BrowserSession] = None
        self._engine: Any = None
        self._controller: Optional[MultimodalController] = None
        self._page: Any = None
        self._abort_signal = asyncio.Event()
        self._running = False
        self._coordinator: Any = None
        self._budget_client: Any = None
        self._flow_logger: Any = None
        self._security_manager: Any = None
        self._vision_controller: Any = None
        self._stealth_manager: Any = None
        self._skill_registry: Any = None
        self._tab_manager: Optional[TabManager] = None
        self._frame_stack: list[Any] = []  # stack of Frame objects
        self._network_interceptors: list[Any] = []
        self._recorder: Any = None  # Optional[SessionRecorder]
        self._event_bus: Any = None  # Optional[EventBus]
        self._memory_store: Optional[MemoryStore] = None  # Set via enable_memory()
        # Diagnostics: session-wide page-event capture (console/errors/network).
        # Attached to every raw page via start()/_attach_page(); survives tab
        # switches because listener closures bind to the raw Page, not PageHandle.
        from super_browser.agent.diagnostics import DiagnosticsBuffer
        buf_size = getattr(self._config, "event_buffer_size", 500) if self._config else 500
        self._diagnostics = DiagnosticsBuffer(max_size=buf_size)

    # -- Lifecycle --

    @property
    def diagnostics(self) -> Any:
        """Session-wide diagnostics buffer (console/page-errors/network).

        Exposed for the MCP diagnostics tools and direct SDK consumers. Reads
        are snapshots (non-destructive); see :class:`DiagnosticsBuffer`.
        """
        return self._diagnostics

    def _attach_diagnostics(self, raw_page: Any) -> None:
        """Wire diagnostics listeners onto a raw page.

        Called from :meth:`start` (initial page) and :meth:`_attach_page`
        (tab open/switch). Idempotent by raw-page identity inside the buffer,
        so it is safe to call on every tab switch.
        """
        self._diagnostics.attach(raw_page)

    async def start(self) -> None:
        cfg = self._config
        # -- Determine backend & session config from composition root --
        backend_name = _detect_backend(cfg)
        session_config = cfg.browser if isinstance(cfg, Config) else SessionConfig(headless=True)
        if backend_name == "patchright":
            from super_browser.browser.backends.patchright_backend import PatchrightEngine
            self._engine = PatchrightEngine(session_config)
            await self._engine.start()
            self._session = self._engine.session
            self._page = await self._engine.new_page()
        else:
            self._session = BrowserSession(session_config)
            await self._session.start()
            self._page = await self._session.new_page()
        self._controller = MultimodalController(self._page, self._page.engine_page.cdp)
        # Wire diagnostics listeners onto the initial page.
        self._attach_diagnostics(self._page.backend_page)
        self._running = True
        self._register_builtin_tools()
        self._configure_verification()
        self._configure_vision()
        self._configure_stealth()
        self._configure_skills()
        # -- Recovery --
        if cfg.agent.enable_recovery:
            from super_browser.recovery import RecoveryCoordinator

            self._coordinator = RecoveryCoordinator(
                session=self._session, controller=self._controller,
            )
            await self._coordinator.start()
        # -- Budget --
        if cfg.agent.enable_budget:
            from super_browser.budget import (
                BudgetAwareLLMClient,
                CircuitBreaker,
                ContextCompressor,
                CredentialPool,
                ModelCascade,
                TokenBudgetGovernor,
            )

            governor = TokenBudgetGovernor()
            cascade = ModelCascade(governor=governor)
            pool = CredentialPool()
            cb = CircuitBreaker()
            comp = ContextCompressor()
            self._budget_client = BudgetAwareLLMClient(governor, cascade, pool, cb, comp)
        # -- Tracing --
        if cfg.tracing.enabled or cfg.agent.trace_enabled:
            from super_browser.tracing import FlowLogger
            from super_browser.tracing.sinks import ConsoleSink
            sinks = [ConsoleSink()]
            trace_dir = cfg.tracing.output_dir or cfg.agent.trace_output_dir
            if trace_dir:
                from pathlib import Path

                from super_browser.tracing.sinks import FileSink
                path = Path(trace_dir) / "trace.jsonl"
                sinks.append(FileSink(path))
            self._flow_logger = FlowLogger(sinks=sinks)
            await self._flow_logger.start()
        # -- Security --
        if cfg.agent.enable_security:
            from super_browser.security import SecurityManager
            sec_config = cfg.security
            self._security_manager = SecurityManager(sec_config)
        logger.info("SuperBrowser started")

    async def stop(self) -> None:
        self._running = False
        if self._flow_logger:
            await self._flow_logger.stop()
            self._flow_logger = None
        if self._coordinator:
            await self._coordinator.stop()
            self._coordinator = None
        if self._session:
            await self._session.stop()
            self._session = None
        self._controller = None
        self._page = None
        logger.info("SuperBrowser stopped")

    async def __aenter__(self) -> SuperBrowser:
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.stop()

    @property
    def is_alive(self) -> bool:
        """Whether the browser session and page are still usable.

        Returns False when the page is closed, None, or the backend reports
        it as dead. Used by MCPBrowserRuntime to detect stale handles.
        """
        if not self._page:
            return False
        try:
            return self._page.is_alive
        except (AttributeError, Exception):
            # PageHandle without is_alive: assume alive if page object exists.
            return True

    # -- Facade methods --

    async def navigate(self, url: str, *, wait_until: str = "domcontentloaded") -> ActionResult:
        """Navigate to a URL. Enforces facade security before side effects."""
        if not self._page:
            return action_result(ok=False, error=ActionError(ErrorCategory.BROWSER_CRASH, "Not started"))
        params = {"url": url}
        sec = await self._check_facade_security("navigate", params, url=url)
        if sec is not None:
            return sec
        url = params["url"]  # consume potentially redacted URL
        return await self._navigate_impl(url, wait_until=wait_until)

    async def _navigate_impl(self, url: str, *, wait_until: str = "domcontentloaded") -> ActionResult:
        """Navigation logic without facade security check.

        Security is enforced by the caller — either :meth:`navigate` (direct
        SDK path) or :meth:`AgentLoop._dispatch_action` (agent loop path).
        Registered as the ``navigate`` tool so the agent loop does not
        double-check security.
        """
        start = time.monotonic()
        if not self._page:
            return action_result(ok=False, error=ActionError(ErrorCategory.BROWSER_CRASH, "Not started"))
        await self._page.goto(url, wait_until=wait_until)
        final_url = self._page.url
        title = await self._page.title()
        skills_data = {}
        if self._skill_registry:
            try:
                discovered = await self._skill_registry.auto_discover(url)
                skills_data = {"skills": [{"id": s.skill_id, "name": s.name} for s in discovered]}  # noqa: F841
            except Exception:
                pass
        return timed_action_result(
            ok=True,
            start_ns=start,
            data=NavigateResult(url=url, final_url=final_url, title=title),
            method=ActionMethod.SELECTOR,
        )

    async def reload(self, *, wait_until: str = "domcontentloaded") -> ActionResult:
        """Reload the current page."""
        if not self._page:
            return action_result(ok=False, error=ActionError(ErrorCategory.BROWSER_CRASH, "Not started"))
        params = {"wait_until": wait_until}
        sec = await self._check_facade_security("reload", params, security_level="sensitive")
        if sec is not None:
            return sec
        wait_until = params["wait_until"]
        start = time.monotonic()
        await self._page.backend_page.reload(wait_until=wait_until)
        return timed_action_result(
            ok=True, start_ns=start, data={"url": self._page.url},
            method=ActionMethod.SELECTOR,
        )

    async def go_back(self, *, wait_until: str = "domcontentloaded") -> ActionResult:
        """Navigate to the previous page in browser history."""
        if not self._page:
            return action_result(ok=False, error=ActionError(ErrorCategory.BROWSER_CRASH, "Not started"))
        params = {"wait_until": wait_until}
        sec = await self._check_facade_security("go_back", params, security_level="sensitive")
        if sec is not None:
            return sec
        wait_until = params["wait_until"]
        start = time.monotonic()
        response = await self._page.backend_page.go_back(wait_until=wait_until)
        if response is None:
            return action_result(ok=False, error=ActionError(
                ErrorCategory.PAGE_ERROR, "No history entry to go back to"))
        return timed_action_result(
            ok=True, start_ns=start, data={"url": self._page.url},
            method=ActionMethod.SELECTOR,
        )

    async def go_forward(self, *, wait_until: str = "domcontentloaded") -> ActionResult:
        """Navigate to the next page in browser history."""
        if not self._page:
            return action_result(ok=False, error=ActionError(ErrorCategory.BROWSER_CRASH, "Not started"))
        params = {"wait_until": wait_until}
        sec = await self._check_facade_security("go_forward", params, security_level="sensitive")
        if sec is not None:
            return sec
        wait_until = params["wait_until"]
        start = time.monotonic()
        response = await self._page.backend_page.go_forward(wait_until=wait_until)
        if response is None:
            return action_result(ok=False, error=ActionError(
                ErrorCategory.PAGE_ERROR, "No history entry to go forward to"))
        return timed_action_result(
            ok=True, start_ns=start, data={"url": self._page.url},
            method=ActionMethod.SELECTOR,
        )

    async def click(self, target: str, *, description: Optional[str] = None) -> ActionResult:
        if not self._controller:
            return action_result(ok=False, error=ActionError(ErrorCategory.BROWSER_CRASH, "Browser not started. Call await sb.start() first."))
        sec = await self._check_facade_security("click", {"target": target})
        if sec is not None:
            return sec
        return await self._controller.click(target, description=description)

    async def fill(self, target: str, value: str, *, clear_first: bool = True, description: Optional[str] = None) -> ActionResult:
        if not self._controller:
            return action_result(ok=False, error=ActionError(ErrorCategory.BROWSER_CRASH, "Browser not started. Call await sb.start() first."))
        params = {"target": target, "value": value}
        sec = await self._check_facade_security("fill", params)
        if sec is not None:
            return sec
        return await self._controller.fill(params["target"], params["value"], clear_first=clear_first, description=description)

    async def act(self, instruction: str, *, max_steps: int = 50) -> ActionResult:
        if not self._controller:
            return action_result(ok=False)

        if self._llm_client is None:
            raise ConfigurationError(
                "No LLM client configured. Pass llm_client= to SuperBrowser()."
            )

        if not self._controller:
            return action_result(ok=False, error=ActionError(ErrorCategory.BROWSER_CRASH, "Browser not started. Call await sb.start() first."))

        loop = AgentLoop(
            controller=self._controller,
            registry=self._registry,
            llm_client=self._llm_client,
            max_steps=max_steps,
            recovery_coordinator=self._coordinator,
            budget_client=self._budget_client,
            flow_logger=self._flow_logger,
            security_manager=self._security_manager,
            stealth_manager=getattr(self, '_stealth_manager', None),
            debug_config=getattr(self._config, 'debug_config', None),
            retry_budget=getattr(self._config, 'retry_budget', None),
        )
        # Wire memory into the loop if enabled
        if self._memory_store is not None and self._page:
            try:
                current_url = self._page.url
            except Exception:
                current_url = ""
            loop.set_memory_store(self._memory_store, current_url=current_url)
        result = await loop.run(instruction)
        return action_result(
            ok=result.completion_reason == "success",
            data=DelegatedResult(
                instruction=instruction,
                completion_reason=CompletionReason.SUCCESS if result.completion_reason == "success" else CompletionReason.ERROR,
                summary=f"Completed in {result.total_steps} steps",
                steps_executed=result.total_steps,
                budget_remaining=self._budget_client.budget_remaining if self._budget_client else 0.0,
                execution_history=[{"step": s.step_number, "action": s.action_name} for s in result.steps],
            ),
        )

    async def act_stream(
        self,
        instruction: str,
        *,
        max_steps: int = 50,
    ) -> AsyncIterator[StreamEvent]:
        """Run the agent loop and yield streaming lifecycle events.

        Unlike :meth:`act` which blocks until completion, ``act_stream``
        yields a :class:`StreamEvent` for each step lifecycle event, allowing
        callers to observe progress in real time.

        The final event is ``StepEvent.DONE`` with ``completion_reason``,
        ``total_steps``, and ``total_duration_ms``.

        Usage::

            async for event in sb.act_stream("Fill the form"):
                if event.type == "step_complete":
                    print(f"Step done: {event.data}")
                if event.type == "done":
                    print(f"Finished: {event.data['completion_reason']}")

        Returns:
            AsyncIterator[StreamEvent] — yields events until the loop completes.
        """
        if not self._controller:
            yield StreamEvent(type=StepEvent.ABORT, data={"reason": "not_started"})
            return

        if self._llm_client is None:
            raise ConfigurationError(
                "No LLM client configured. Pass llm_client= to SuperBrowser()."
            )

        loop = AgentLoop(
            controller=self._controller,
            registry=self._registry,
            llm_client=self._llm_client,
            max_steps=max_steps,
            recovery_coordinator=self._coordinator,
            budget_client=self._budget_client,
            flow_logger=self._flow_logger,
            security_manager=self._security_manager,
            stealth_manager=getattr(self, '_stealth_manager', None),
            debug_config=getattr(self._config, 'debug_config', None),
            retry_budget=getattr(self._config, 'retry_budget', None),
        )
        if self._memory_store is not None and self._page:
            try:
                current_url = self._page.url
            except Exception:
                current_url = ""
            loop.set_memory_store(self._memory_store, current_url=current_url)

        async for event in loop.run_stream(instruction):
            yield event

    async def extract(self, query: str, *, selector: Optional[str] = None, schema: Optional[dict] = None) -> ActionResult:
        """Extract content from the current page.

        :param query: Description of what to extract.
        :param selector: Optional CSS selector for targeted extraction.
        :param schema: Optional JSON schema to validate/structure the output.
        :returns: ActionResult with data=ExtractResult.
        """
        import json as _json
        start = time.monotonic()
        if not self._controller:
            return action_result(ok=False, error=ActionError(ErrorCategory.BROWSER_CRASH, "Browser not started. Call await sb.start() first."))

        if selector:
            # Use CDP Runtime.evaluate with expression argument to avoid injection
            # We escape the selector for safe embedding in a JS string literal
            safe_selector = selector.replace('\\', '\\\\').replace("'", "\\'").replace('\n', '\\n').replace('\r', '\\r')
            result = await self._controller._cdp.evaluate(
                f"(function() {{ var el = document.querySelector('{safe_selector}');"
                f" return el ? el.textContent : null; }})()"
            )
            if result.ok and 'exceptionDetails' not in result.data:
                extracted = result.data.get("result", {}).get("value")
            else:
                extracted = None
        else:
            snap = await self._controller.capture_ax_snapshot()
            extracted = snap.to_compact_str()

        # Schema validation
        if schema and extracted is not None:
            try:
                import jsonschema
                # If extracted is a string, try to parse as JSON
                if isinstance(extracted, str):
                    try:
                        parsed = _json.loads(extracted)
                    except (_json.JSONDecodeError, ValueError):
                        parsed = {"text": extracted}
                else:
                    parsed = extracted
                jsonschema.validate(parsed, schema)
            except ImportError:
                # jsonschema not installed — skip validation
                pass
            except Exception as e:
                return timed_action_result(
                    ok=False, start_ns=start,
                    error=ActionError(ErrorCategory.SELECTOR_NOT_FOUND, f"Schema validation failed: {e}"),
                )

        return timed_action_result(
            ok=True,
            start_ns=start,
            data=ExtractResult(selector=selector or query, extracted=extracted, element_count=0),
            method=ActionMethod.SELECTOR,
        )

    async def observe(self) -> ActionResult:
        start = time.monotonic()
        if not self._controller:
            return action_result(ok=False, error=ActionError(ErrorCategory.BROWSER_CRASH, "Browser not started. Call await sb.start() first."))

        url = self._page.url
        title = await self._page.title()
        snap = await self._controller.capture_ax_snapshot()
        interactive_count = sum(1 for n in snap.nodes.values() if n.is_interactive)

        # Build actionable targets from the AX snapshot.
        # The ref (e.g. "@e0") is directly usable as a `target` argument for
        # click/fill/type_text/etc. — the controller's coordinate tier resolves
        # @refs to element coordinates via the snapshot.
        _ROLE_ACTION_MAP = {
            "button": "click", "link": "click", "menuitem": "click",
            "tab": "click", "treeitem": "click", "option": "click",
            "textbox": "fill", "searchbox": "fill", "spinbutton": "fill",
            "combobox": "select_option", "checkbox": "click", "radio": "click",
            "slider": "fill", "switch": "click",
        }

        # Only include nodes whose center is resolvable (bounds present),
        # because @refs are resolved via the coordinate tier. Nodes without
        # bounds cannot be acted on and would produce a confusing target.
        # Checkbox/radio map to "click" (not "check") because check/uncheck
        # are selector-tier only and cannot resolve @refs.
        all_interactive = [
            n for n in snap.nodes.values()
            if n.is_interactive and not n.disabled and n.center is not None
        ]
        _MAX_TARGETS = 50
        capped = all_interactive[:_MAX_TARGETS]
        targets = [
            {
                "target": n.ref if n.ref.startswith("@") else f"@{n.ref}",
                "role": n.role,
                "name": n.name,
                "action_hint": _ROLE_ACTION_MAP.get(n.role, "click"),
            }
            for n in capped
        ]

        # Build images array from non-interactive image-role nodes.
        # These are metadata-only (alt/name/bounds) — they must NOT appear in
        # targets because they cannot be acted on via coordinate resolution.
        # Only images with a non-empty name are included (others carry no
        # useful metadata). Images without bounds are still included since
        # bounds are informational here, not action-enabling.
        all_images = [
            n for n in snap.nodes.values()
            if n.role == "image" and n.name
        ]
        _MAX_IMAGES = 50
        capped_images = all_images[:_MAX_IMAGES]
        images = [
            {
                "ref": n.ref if n.ref.startswith("@") else f"@{n.ref}",
                "role": n.role,
                "name": n.name,
                "alt": n.name,
                **(
                    {"bounds": {
                        "x": n.bounds[0], "y": n.bounds[1],
                        "width": n.bounds[2], "height": n.bounds[3],
                    }}
                    if n.bounds else {}
                ),
            }
            for n in capped_images
        ]

        return timed_action_result(
            ok=True,
            start_ns=start,
            data={
                "url": url, "title": title,
                "interactive_elements": interactive_count,
                "total_elements": len(snap.nodes),
                "targets": targets,
                "targets_truncated": len(all_interactive) > _MAX_TARGETS,
                "images": images,
                "images_truncated": len(all_images) > _MAX_IMAGES,
            },
        )

    async def _capture_region_bytes(
        self,
        *,
        selector: Optional[str] = None,
        bounds: Optional[dict[str, float]] = None,
        full_page: bool = False,
        format: str = "png",
        quality: Optional[int] = None,
    ) -> tuple[bytes, str]:
        """Capture a screenshot, cropped to ``selector``/``bounds`` if given.

        Returns ``(image_bytes, mime_type)``. The crop region is resolved via
        the raw backend page (``backend_page``), not the engine wrapper, which
        does not expose ``query_selector``. Reused by OCR and vision analysis.
        """
        crop_bounds: Optional[tuple[int, int, int, int]] = None

        if selector is not None:
            try:
                raw_page = getattr(self._page, "backend_page", None)
                if raw_page is not None:
                    el = await raw_page.query_selector(selector)
                    if el is not None:
                        box = await el.bounding_box()
                        if box is not None and box["width"] > 0 and box["height"] > 0:
                            crop_bounds = (
                                int(box["x"]), int(box["y"]),
                                int(box["width"]), int(box["height"]),
                            )
            except Exception:
                pass  # best-effort; fall through to uncropped capture

        if bounds is not None:
            crop_bounds = (
                int(bounds["x"]), int(bounds["y"]),
                int(bounds["width"]), int(bounds["height"]),
            )

        # The page object can be either a raw Playwright/Patchright Page
        # (which takes ``type=``) or a strict PageHandle wrapper (whose public
        # signature takes ``format=``, translating internally). Try the raw
        # Playwright spelling first; if the wrapper rejects ``type=`` as an
        # unexpected keyword, retry with the public ``format=`` spelling.
        playwright_kwargs: dict[str, Any] = {"full_page": full_page}
        public_kwargs: dict[str, Any] = {"full_page": full_page}
        if format == "jpeg":
            playwright_kwargs["type"] = "jpeg"
            public_kwargs["format"] = "jpeg"
            if quality is not None:
                playwright_kwargs["quality"] = quality
                public_kwargs["quality"] = quality
        else:
            playwright_kwargs["type"] = "png"
            public_kwargs["format"] = "png"
        try:
            img_bytes = await self._page.screenshot(**playwright_kwargs)
        except TypeError as exc:
            if "type" not in str(exc) or "unexpected keyword" not in str(exc).lower():
                raise
            img_bytes = await self._page.screenshot(**public_kwargs)

        if crop_bounds is not None:
            from io import BytesIO

            from PIL import Image as PILImage

            img = PILImage.open(BytesIO(img_bytes))
            x, y, w, h = crop_bounds
            img = img.crop((x, y, x + w, y + h))
            out = BytesIO()
            # Re-encode to the requested format on crop.
            pil_format = "JPEG" if format == "jpeg" else "PNG"
            save_kwargs: dict[str, Any] = {}
            if pil_format == "JPEG" and quality is not None:
                save_kwargs["quality"] = quality
            img.convert("RGB" if pil_format == "JPEG" else "RGBA").save(
                out, format=pil_format, **save_kwargs,
            )
            img_bytes = out.getvalue()

        mime = "image/jpeg" if format == "jpeg" else "image/png"
        return img_bytes, mime

    async def extract_image_text(
        self,
        *,
        selector: Optional[str] = None,
        bounds: Optional[dict[str, float]] = None,
        full_page: bool = False,
        language: str = "eng",
        min_confidence: float = 0.0,
    ) -> ActionResult:
        """Extract text from a screenshot region via OCR.

        Captures a screenshot (viewport, full page, or cropped to bounds/selector),
        runs Tesseract OCR, and returns structured word boxes + joined text.

        Raises RuntimeError if OCR dependencies are unavailable.
        """
        from super_browser.results.types import action_result

        if not self._page:
            return action_result(
                ok=False,
                error="Not started",
            )

        # Check OCR availability early.
        try:
            import pytesseract  # noqa: F401
            from PIL import Image  # noqa: F401
        except ImportError:
            raise RuntimeError(
                "Tesseract OCR is not installed or the requested language pack is unavailable."
            )

        png_bytes, _mime = await self._capture_region_bytes(
            selector=selector, bounds=bounds, full_page=full_page, format="png",
        )

        # Run OCR with the specified language.
        try:
            from io import BytesIO

            from PIL import Image as PILImage

            img = PILImage.open(BytesIO(png_bytes)).convert("RGB")
            # Use the language parameter for tesseract.
            config = f"--psm 11 -l {language}"
            data = pytesseract.image_to_data(
                img, config=config, output_type=pytesseract.Output.DICT,
            )
        except Exception as exc:
            if "language" in str(exc).lower() or "tesseract" in str(exc).lower():
                raise RuntimeError(
                    "Tesseract OCR is not installed or the requested language pack is unavailable."
                ) from exc
            raise

        # Build word list. Parse confidence with float() — pytesseract can
        # return decimal strings like "96.59" or "-1" for non-text regions.
        words: list[dict[str, Any]] = []
        n = len(data.get("text", []))
        for i in range(n):
            text = str(data["text"][i]).strip()
            if not text:
                continue
            try:
                conf_raw = float(data["conf"][i])
            except (TypeError, ValueError):
                continue
            if conf_raw <= 0:
                continue
            confidence = conf_raw / 100.0
            if confidence < min_confidence:
                continue
            words.append({
                "text": text,
                "x": float(data["left"][i]),
                "y": float(data["top"][i]),
                "w": float(data["width"][i]),
                "h": float(data["height"][i]),
                "confidence": round(confidence, 4),
            })

        joined_text = " ".join(w["text"] for w in words)

        return action_result(
            ok=True,
            data={
                "text": joined_text,
                "words": words,
                "language": language,
                "source": {
                    "selector": selector,
                    "bounds": bounds,
                    "full_page": full_page,
                },
            },
        )

    def _lazy_vision_controller(self) -> Any:
        """Build a VisionController from env-configured providers on demand.

        Used by :meth:`analyze_image` so an explicit vision call works when
        provider env vars are set, even if ``enable_vision`` is False (which
        gates only the automatic interaction-fallback). Returns ``None`` when
        no provider is configured.
        """
        if self._vision_controller is not None:
            return self._vision_controller
        try:
            from super_browser.vision import (
                VisionController,
                VisionProviderFactory,
            )
        except ImportError:
            return None
        factory = VisionProviderFactory.from_env()
        if not factory.provider_names:
            return None
        return VisionController(factory=factory)

    async def analyze_image(
        self,
        *,
        question: str,
        selector: Optional[str] = None,
        bounds: Optional[dict[str, float]] = None,
        full_page: bool = False,
        format: str = "png",
        quality: Optional[int] = None,
    ) -> ActionResult:
        """Answer a natural-language question about a page region via a
        configured vision provider (semantic visual QA, not OCR).

        Captures a screenshot (viewport, full page, or cropped), then asks the
        vision-LLM provider the question and returns the answer + metadata.
        Returns ``error="vision_unavailable"`` when no provider is configured
        or every provider declined.
        """
        from super_browser.results.types import action_result

        if not self._page:
            return action_result(ok=False, error="Not started")

        controller = self._lazy_vision_controller()
        if controller is None:
            return action_result(ok=False, error="vision_unavailable")

        image_bytes, mime = await self._capture_region_bytes(
            selector=selector, bounds=bounds, full_page=full_page,
            format=format, quality=quality,
        )

        si = await controller.analyze_state(image_bytes, question, mime_type=mime)

        # No-provider / all-declined: convert the controller's internal
        # sentinel into a structured vision_unavailable error. We key on
        # provider identity, never on confidence alone — a real answer may
        # legitimately be low-confidence.
        if si.provider is None and si.answer == "No provider available":
            return action_result(ok=False, error="vision_unavailable")

        return action_result(
            ok=True,
            data={
                "answer": si.answer,
                "confidence": si.confidence,
                "provider": si.provider,
                "model": si.model,
                "token_cost": si.token_cost,
                "duration_ms": si.duration_ms,
                "source": {
                    "selector": selector,
                    "bounds": bounds,
                    "full_page": full_page,
                },
            },
        )

    # -- Tab helper (CHK-07) --

    async def _attach_page(self, page_obj: Any) -> None:
        """Wire a raw Playwright Page into the facade's _page and _controller.

        Shared by :meth:`open_tab` and :meth:`switch_tab`.
        """
        ctx = self._engine.context
        if ctx is None:
            raise RuntimeError("Browser context not available — engine not started?")
        from super_browser.browser.page import PageHandle
        cdp_session = await ctx.new_cdp_session(page_obj)
        from super_browser.browser.cdp import CDPBridge
        from super_browser.browser.config import SessionConfig as _SC
        cdp = CDPBridge(cdp_session, _SC())
        self._page = PageHandle(page_obj, cdp)
        self._controller = MultimodalController(self._page, self._page.engine_page.cdp)
        # Wire diagnostics listeners onto the new tab/switched page.
        self._attach_diagnostics(self._page.backend_page)

    # -- Multi-Tab --

    async def open_tab(self, url: Optional[str] = None) -> ActionResult:
        """Open a new browser tab, optionally navigating to a URL.

        :param url: Optional URL to navigate to.
        :returns: ActionResult with data=TabHandle.
        """
        start = time.monotonic()
        if not self._session:
            return action_result(ok=False, error=ActionError(ErrorCategory.BROWSER_CRASH, "Browser not started"))
        ctx = self._engine.context
        if ctx is None:
            return action_result(ok=False, error=ActionError(ErrorCategory.BROWSER_CRASH, "Browser context not available"))
        if self._tab_manager is None:
            self._tab_manager = TabManager(ctx)
        params = {"url": url or ""}
        sec = await self._check_facade_security("open_tab", params, url=url or "")
        if sec is not None:
            return sec
        url = params["url"] or None  # consume potentially redacted URL
        try:
            tab = await self._tab_manager.open_tab(url)
            # Update page reference and controller to new tab
            page_obj = self._tab_manager.get_page(tab.tab_id)
            await self._attach_page(page_obj)
            return timed_action_result(ok=True, start_ns=start, data=tab)
        except Exception as e:
            return timed_action_result(ok=False, start_ns=start, error=ActionError(ErrorCategory.NAVIGATION, str(e)))

    async def switch_tab(self, tab_id: int) -> ActionResult:
        """Switch to a different tab by ID.

        :param tab_id: The tab ID from open_tab().
        :returns: ActionResult with data=TabHandle.
        """
        start = time.monotonic()
        if not self._tab_manager:
            return action_result(ok=False, error=ActionError(ErrorCategory.BROWSER_CRASH, "No tabs open"))
        params = {"tab_id": tab_id}
        sec = await self._check_facade_security("switch_tab", params, security_level="sensitive")
        if sec is not None:
            return sec
        tab_id = params["tab_id"]
        try:
            tab = await self._tab_manager.switch_tab(tab_id)
            page_obj = self._tab_manager.get_page(tab_id)
            await self._attach_page(page_obj)
            return timed_action_result(ok=True, start_ns=start, data=tab)
        except KeyError as e:
            return timed_action_result(ok=False, start_ns=start, error=ActionError(ErrorCategory.SELECTOR_NOT_FOUND, str(e)))

    async def close_tab(self, tab_id: int) -> ActionResult:
        """Close a tab by ID.

        :param tab_id: The tab ID to close.
        """
        start = time.monotonic()
        if not self._tab_manager:
            return action_result(ok=False, error=ActionError(ErrorCategory.BROWSER_CRASH, "No tabs open"))
        params = {"tab_id": tab_id}
        sec = await self._check_facade_security("close_tab", params, security_level="sensitive")
        if sec is not None:
            return sec
        tab_id = params["tab_id"]
        try:
            await self._tab_manager.close_tab(tab_id)
            return timed_action_result(ok=True, start_ns=start, data={"closed_tab": tab_id})
        except KeyError as e:
            return timed_action_result(ok=False, start_ns=start, error=ActionError(ErrorCategory.SELECTOR_NOT_FOUND, str(e)))

    async def list_tabs(self) -> ActionResult:
        """List all open tabs."""
        start = time.monotonic()
        if not self._tab_manager:
            return timed_action_result(ok=True, start_ns=start, data=TabSnapshot())
        snap = await self._tab_manager.list_tabs()
        return timed_action_result(ok=True, start_ns=start, data=snap)

    # -- File I/O --

    async def upload_file(self, selector: str, file_path: str) -> ActionResult:
        """Upload a file to an <input type='file'> element.

        :param selector: CSS selector for the file input.
        :param file_path: Absolute or relative path to the file.
        :returns: ActionResult with data=UploadResult.
        """
        start = time.monotonic()
        if not self._page:
            return action_result(ok=False, error=ActionError(ErrorCategory.BROWSER_CRASH, "Browser not started"))
        params = {"selector": selector, "file_path": file_path}
        sec = await self._check_facade_security("upload_file", params, security_level="dangerous")
        if sec is not None:
            return sec
        selector = params["selector"]
        file_path = params["file_path"]
        try:
            await self._page.engine_page.set_input_files(selector, file_path)
            import os
            fname = os.path.basename(file_path)
            return timed_action_result(
                ok=True, start_ns=start,
                data=UploadResult(selector=selector, file_path=file_path, file_name=fname),
            )
        except Exception as e:
            return timed_action_result(
                ok=False, start_ns=start,
                error=ActionError(ErrorCategory.SELECTOR_NOT_FOUND, f"Upload failed: {e}"),
            )

    async def download(self, url_or_selector: str, *, save_path: Optional[str] = None) -> ActionResult:
        """Download a file by clicking a link or navigating to a URL.

        :param url_or_selector: URL to download from, or selector for a download link.
        :param save_path: Optional directory to save the file.
        :returns: ActionResult with data=DownloadResult.
        """
        start = time.monotonic()
        if not self._page:
            return action_result(ok=False, error=ActionError(ErrorCategory.BROWSER_CRASH, "Browser not started"))
        params = {"url_or_selector": url_or_selector, "save_path": save_path or ""}
        # For URL-mode downloads, check against the download target URL.
        # For selector-mode, _check_facade_security derives current page URL.
        security_url = url_or_selector if url_or_selector.startswith(("http://", "https://")) else ""
        sec = await self._check_facade_security("download", params, url=security_url)
        if sec is not None:
            return sec
        url_or_selector = params["url_or_selector"]
        save_path = params["save_path"] or None
        try:
            # Start listening for download
            async with self._page.engine_page.expect_download() as download_info:
                if url_or_selector.startswith("http"):
                    await self._page.engine_page.evaluate(
                        "(url) => { const a = document.createElement('a'); a.href = url; a.download = ''; a.click(); }",
                        url_or_selector,
                    )
                else:
                    await self._page.engine_page.click(url_or_selector)
            download = await download_info.value
            suggested = download.suggested_filename
            if save_path:
                import os
                dest = os.path.join(save_path, suggested)
                await download.save_as(dest)
            else:
                dest = await download.path()
            file_size = 0
            import os
            if dest and os.path.exists(dest):
                file_size = os.path.getsize(dest)
            return timed_action_result(
                ok=True, start_ns=start,
                data=DownloadResult(
                    url=url_or_selector,
                    file_path=str(dest) if dest else "",
                    file_size_bytes=file_size,
                    suggested_filename=suggested,
                ),
            )
        except Exception as e:
            return timed_action_result(
                ok=False, start_ns=start,
                error=ActionError(ErrorCategory.NAVIGATION, f"Download failed: {e}"),
            )

    # -- iframe --

    async def enter_frame(self, selector: str) -> ActionResult:
        """Enter an iframe, scoping subsequent interactions to it.

        :param selector: CSS selector for the iframe element.
        :returns: ActionResult indicating success.
        """
        start = time.monotonic()
        if not self._page:
            return action_result(ok=False, error=ActionError(ErrorCategory.BROWSER_CRASH, "Browser not started"))
        params = {"selector": selector}
        sec = await self._check_facade_security("enter_frame", params, security_level="sensitive")
        if sec is not None:
            return sec
        selector = params["selector"]
        try:
            frame = self._page.engine_page.frame_locator(selector)
            self._frame_stack.append(frame)
            if self._controller:
                self._controller._set_frame_locator(frame)
            return timed_action_result(ok=True, start_ns=start, data={"frame": selector, "depth": len(self._frame_stack)})
        except Exception as e:
            return timed_action_result(ok=False, start_ns=start, error=ActionError(ErrorCategory.SELECTOR_NOT_FOUND, f"Frame not found: {e}"))

    async def exit_frame(self) -> ActionResult:
        """Exit the current iframe, returning to the parent frame."""
        start = time.monotonic()
        sec = await self._check_facade_security("exit_frame", {}, security_level="sensitive")
        if sec is not None:
            return sec
        if self._frame_stack:
            self._frame_stack.pop()
            if self._controller:
                if self._frame_stack:
                    self._controller._set_frame_locator(self._frame_stack[-1])
                else:
                    self._controller._clear_frame_locator()
            return timed_action_result(ok=True, start_ns=start, data={"depth": len(self._frame_stack)})
        return timed_action_result(ok=True, start_ns=start, data={"depth": 0})

    def _current_frame(self) -> Any:
        """Get the current frame (top of stack) or the raw page."""
        if self._frame_stack:
            return self._frame_stack[-1]
        return self._page.engine_page.backend_page if self._page else None
        # NOTE: Returns underlying Playwright Page for backward compat.

    # -- Shadow DOM --

    async def query_shadow(self, host_selector: str, inner_selector: str) -> ActionResult:
        """Query an element inside a Shadow DOM.

        :param host_selector: CSS selector for the custom element (shadow host).
        :param inner_selector: CSS selector inside the shadow root.
        :returns: ActionResult with data=ShadowQueryResult.
        """
        start = time.monotonic()
        if not self._controller:
            return action_result(ok=False, error=ActionError(ErrorCategory.BROWSER_CRASH, "Browser not started"))
        try:
            import json as _json
            host_json = _json.dumps(host_selector)
            inner_json = _json.dumps(inner_selector)
            expr = (
                '(function() {'
                '  var host = document.querySelector(JSON.parse(' + host_json + '));'
                '  if (!host || !host.shadowRoot) return JSON.stringify({found: false});'
                '  var el = host.shadowRoot.querySelector(JSON.parse(' + inner_json + '));'
                '  if (!el) return JSON.stringify({found: false});'
                '  var rect = el.getBoundingClientRect();'
                '  return JSON.stringify({'
                '    found: true,'
                '    text: el.textContent || "",'
                '    bounds: {x: rect.x, y: rect.y, w: rect.width, h: rect.height}'
                '  });'
                '})()'
            )
            result = await self._controller._cdp.evaluate(expr)
            if result.ok and result.data:
                val = result.data.get("result", {}).get("value")
                if val:
                    import json
                    parsed = json.loads(val)
                    return timed_action_result(
                        ok=True, start_ns=start,
                        data=ShadowQueryResult(
                            host_selector=host_selector,
                            inner_selector=inner_selector,
                            text=parsed.get("text"),
                            bounds=parsed.get("bounds"),
                            found=parsed.get("found", False),
                        ),
                    )
            return timed_action_result(
                ok=True, start_ns=start,
                data=ShadowQueryResult(host_selector=host_selector, inner_selector=inner_selector, found=False),
            )
        except Exception as e:
            return timed_action_result(
                ok=False, start_ns=start,
                error=ActionError(ErrorCategory.SELECTOR_NOT_FOUND, f"Shadow query failed: {e}"),
            )

    # -- Network Interception --

    async def intercept_requests(self, pattern: str = "*", *, action: str = "log") -> ActionResult:
        """Enable network request interception.

        :param pattern: URL glob pattern to match (e.g. "**/api/**").
        :param action: "log", "block", or "mock".
        :returns: ActionResult with data=NetworkInterceptResult.
        """
        start = time.monotonic()
        if not self._page:
            return action_result(ok=False, error=ActionError(ErrorCategory.BROWSER_CRASH, "Browser not started"))
        # Derive security level from action: block/mock modify traffic, log only observes
        security_level = "dangerous" if action in ("block", "mock") else "sensitive"
        params = {"pattern": pattern, "action": action}
        sec = await self._check_facade_security("intercept_requests", params, security_level=security_level)
        if sec is not None:
            return sec
        pattern = params["pattern"]
        action = params["action"]
        try:
            intercepted = []

            async def handle_route(route: Any) -> None:
                req = route.request
                intercepted.append({"url": req.url, "method": req.method})
                if action == "block":
                    await route.abort()
                else:
                    await route.continue_()

            await self._page.engine_page.route(pattern, handle_route)
            self._network_interceptors.append({"pattern": pattern, "action": action, "requests": intercepted})

            return timed_action_result(
                ok=True, start_ns=start,
                data=NetworkInterceptResult(pattern=pattern, action=action),
            )
        except Exception as e:
            return timed_action_result(
                ok=False, start_ns=start,
                error=ActionError(ErrorCategory.SECURITY, f"Interception failed: {e}"),
            )

    async def block_requests(self, pattern: str = "*") -> ActionResult:
        """Block all requests matching a URL pattern.

        :param pattern: URL glob pattern to block.
        """
        return await self.intercept_requests(pattern, action="block")

    async def mock_response(self, pattern: str, body: str, *, content_type: str = "application/json", status: int = 200) -> ActionResult:
        """Mock a network response for matching requests.

        :param pattern: URL glob pattern to match.
        :param body: Response body to return.
        :param content_type: Content-Type header.
        :param status: HTTP status code.
        """
        start = time.monotonic()
        if not self._page:
            return action_result(ok=False, error=ActionError(ErrorCategory.BROWSER_CRASH, "Browser not started"))
        params = {"pattern": pattern, "body": body, "content_type": content_type, "status": status}
        sec = await self._check_facade_security("mock_response", params, security_level="dangerous")
        if sec is not None:
            return sec
        pattern = params["pattern"]
        body = params["body"]
        content_type = params["content_type"]
        status = params["status"]
        try:
            async def handle_mock(route: Any) -> None:
                await route.fulfill(
                    status=status,
                    headers={"Content-Type": content_type},
                    body=body,
                )

            await self._page.engine_page.route(pattern, handle_mock)
            self._network_interceptors.append({"pattern": pattern, "action": "mock", "body": body})

            return timed_action_result(
                ok=True, start_ns=start,
                data=NetworkInterceptResult(pattern=pattern, action="mock"),
            )
        except Exception as e:
            return timed_action_result(
                ok=False, start_ns=start,
                error=ActionError(ErrorCategory.SECURITY, f"Mock failed: {e}"),
            )

    async def clear_interceptions(self) -> ActionResult:
        """Remove all network request interceptions."""
        start = time.monotonic()
        if not self._page:
            return action_result(ok=False, error=ActionError(ErrorCategory.BROWSER_CRASH, "Browser not started"))
        sec = await self._check_facade_security("clear_interceptions", {})
        if sec is not None:
            return sec
        try:
            await self._page.engine_page.unroute_all()
            self._network_interceptors.clear()
            return timed_action_result(ok=True, start_ns=start, data={"cleared": True})
        except Exception as e:
            return timed_action_result(ok=False, start_ns=start, error=ActionError(ErrorCategory.SECURITY, str(e)))

    # -- Delegation --

    async def delegate(self, tasks: list[str], *, max_concurrency: int = 4) -> DelegationResult:
        if not self._session:
            return DelegationResult(tasks=[], total_duration_ms=0, completed_count=0, failed_count=len(tasks), cancelled_count=0)
        delegator = SubagentDelegator(
            self._session, self._registry, self._llm_client,
            max_concurrency=max_concurrency,
            recovery_coordinator=self._coordinator,
            budget_client=self._budget_client,
            flow_logger=self._flow_logger,
            security_manager=self._security_manager,
            stealth_manager=self._stealth_manager,
        )
        return await delegator.delegate(tasks, max_concurrency=max_concurrency)

    # -- Tool management --

    def tools(self, *, toolset: Optional[str] = None) -> str:
        return self._registry.build_tool_api_description(toolset=toolset)

    def register_tool(self, func: Callable, *, toolsets: tuple[str, ...] = ()) -> None:
        self._registry.register(func, toolsets=toolsets)

    # -- Abort --

    def abort(self) -> None:
        self._abort_signal.set()

    # -- Verification --

    def configure_verification(self, config: Any = None) -> None:
        from super_browser.verification import VisualVerifier
        from super_browser.verification.types import VerifierConfig as VC
        vconfig = config or VC()
        verifier = VisualVerifier(
            cdp=self._page.engine_page.cdp,
            snapshot_provider=self._controller._snapshot_provider,
            config=vconfig,
        )
        self._controller.enable_verification(verifier)

    async def _check_facade_security(
        self,
        action: str,
        params: dict[str, Any],
        *,
        url: str = "",
        security_level: str = "sensitive",
    ) -> ActionResult | None:
        """Enforce configured security policy on a direct facade call.

        Returns ``None`` when the action is allowed (or security is disabled).
        Returns an ``ActionResult`` with ``ErrorCategory.SECURITY`` when blocked.

        The *params* dict is passed by reference so that the security manager
        can redact values in-place before the caller uses them.
        """
        if self._security_manager is None:
            return None

        if not url:
            try:
                url = str(self._page.url) if self._page and hasattr(self._page, "url") else ""
            except Exception:
                url = ""

        from super_browser.security.types import SecurityLevel
        level = SecurityLevel(security_level)
        sec_result = await self._security_manager.check_action(
            action, params, url, level,
        )
        if not sec_result.passed:
            return action_result(
                ok=False,
                error=ActionError(
                    ErrorCategory.SECURITY,
                    f"Security check failed: {sec_result.blocked_by}",
                ),
            )
        return None

    def _make_controller_wrapper(self, method_name: str):
        """Create a late-binding wrapper for a controller method.

        The wrapper dereferences ``self._controller`` at call time, so after
        :meth:`_attach_page` replaces the controller, the registered tool still
        routes to the current controller and page.

        Signature and docstring are copied from the current controller method
        via :func:`functools.wraps` so the registry can introspect parameters.
        No security check is added — AgentLoop._dispatch_action handles that.
        """
        import functools

        original = getattr(self._controller, method_name)

        @functools.wraps(original)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            return await getattr(self._controller, method_name)(*args, **kwargs)

        wrapper.__name__ = method_name
        return wrapper

    def _register_builtin_tools(self) -> None:
        """Register built-in browser and facade tools into the registry.

        Called during :meth:`start` after the controller is created.
        Does not overwrite tools already registered by the user.
        """
        if not self._controller:
            return

        # Controller-level interaction tools.
        # Use late-binding wrappers so that after _attach_page() replaces
        # self._controller, the registered tools still route to the current
        # controller. Raw bound methods would capture the old controller
        # instance and act on a stale page after tab switches.
        for name in ("click", "fill", "select", "hover", "drag", "scroll", "keypress",
                      "check", "uncheck", "focus", "type_text"):
            method = getattr(self._controller, name, None)
            if method is not None and self._registry.get(name) is None:
                self._registry.register(self._make_controller_wrapper(name))

        # Facade-level tools
        # navigate: register _navigate_impl via a closure (no security check)
        # because AgentLoop._dispatch_action already enforces security.
        # The public navigate() method keeps its own check for direct SDK calls.
        if self._registry.get("navigate") is None:
            facade_ref = self

            async def navigate(url: str, *, wait_until: str = "domcontentloaded") -> ActionResult:
                """Navigate to a URL (security enforced by AgentLoop dispatcher)."""
                return await facade_ref._navigate_impl(url, wait_until=wait_until)

            self._registry.register(navigate)

        # extract, observe: no facade security check, safe to register directly
        for name in ("extract", "observe"):
            method = getattr(self, name, None)
            if method is not None and self._registry.get(name) is None:
                self._registry.register(method)

    def _configure_verification(self) -> None:
        if not self._config.agent.enable_verification:
            return
        try:
            from super_browser.verification import VisualVerifier
            from super_browser.verification.types import VerifierConfig as VC
            verifier = VisualVerifier(
                cdp=self._page.engine_page.cdp,
                snapshot_provider=self._controller._snapshot_provider,
                config=VC(),
            )
            self._controller.enable_verification(verifier)
        except Exception:
            pass  # verification optional — fail silently

    def _configure_vision(self) -> None:
        if not self._config.agent.enable_vision:
            return
        from pathlib import Path

        from super_browser.vision import VisionCache, VisionController, VisionProviderFactory
        factory = VisionProviderFactory.from_env()
        cache_dir_str = self._config.agent.vision_cache_dir
        cache_dir = Path(cache_dir_str) if cache_dir_str else None
        cache = VisionCache(cache_dir=cache_dir)
        self._vision_controller = VisionController(factory=factory, cache=cache)
        self._controller._vision_controller = self._vision_controller

    def _configure_stealth(self) -> None:
        if not self._config.agent.enable_stealth:
            return
        from super_browser.stealth import StealthManager
        stealth_config = self._config.stealth
        stealth_bridge = getattr(self._page.engine_page, "stealth_bridge", None)
        self._stealth_manager = StealthManager(
            stealth_config,
            stealth_bridge=stealth_bridge,
            cdp=self._page.engine_page.cdp if stealth_bridge is None else None,
            page=self._page.engine_page,
        )
        self._loop_stealth = self._stealth_manager

    def _configure_skills(self) -> None:
        if not self._config.agent.enable_skills:
            return
        from pathlib import Path

        from super_browser.skills import SkillRegistry
        skills_dir_str = self._config.agent.skills_dir
        skills_dir = Path(skills_dir_str) if skills_dir_str else None
        self._skill_registry = SkillRegistry(skills_dir=skills_dir)
        if self._page and hasattr(self._page, "cdp"):
            self._skill_registry.set_cdp(self._page.engine_page.cdp)

    async def learn_from_trajectory(
        self, domain: str, task_description: str, actions_taken: list[str],
        selectors_used: dict[str, str], *, preferred_tier: Optional[dict[str, str]] = None,
    ) -> Any:
        if not self._skill_registry:
            return None
        return await self._skill_registry.learn_from_trajectory(
            domain, task_description, actions_taken, selectors_used,
            preferred_tier=preferred_tier,
        )

    # -- Session Persistence --

    async def save_session(self, path: str) -> ActionResult:
        """Save cookies and session state to a JSON file.

        Serializes all browser cookies via StealthBridge along with
        metadata (URL, timestamp, version). Works across all backends.

        :param path: File path to write the session JSON.
        :returns: ActionResult with data containing the session metadata.
        """
        import json
        start = time.monotonic()
        if not self._page:
            return action_result(ok=False, error=ActionError(ErrorCategory.BROWSER_CRASH, "Browser not started."))
        stealth_bridge = getattr(self._page.engine_page, "stealth_bridge", None)
        if stealth_bridge is None:
            return action_result(ok=False, error=ActionError(ErrorCategory.VALIDATION, "No stealth bridge available for cookie access."))
        params = {"path": path}
        sec = await self._check_facade_security("save_session", params, security_level="dangerous")
        if sec is not None:
            return sec
        path = params["path"]
        try:
            cookies = await stealth_bridge.get_all_cookies()
            session_data = {
                "version": "1.0",
                "timestamp": time.time(),
                "url": str(self._page.url) if hasattr(self._page, "url") else "",
                "cookies": cookies,
            }
            from pathlib import Path as _Path
            target = _Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(session_data, indent=2), encoding="utf-8")
            return timed_action_result(
                ok=True,
                start_ns=start,
                data={"path": str(target), "cookie_count": len(cookies)},
                method=ActionMethod.SELECTOR,
            )
        except Exception as exc:
            return timed_action_result(
                ok=False,
                start_ns=start,
                error=ActionError(ErrorCategory.BROWSER_CRASH, f"save_session failed: {exc}"),
            )

    async def load_session(self, path: str) -> ActionResult:
        """Load cookies and session state from a JSON file.

        Reads a session JSON previously saved by :meth:`save_session`,
        validates the version, and restores cookies via StealthBridge.

        :param path: File path to read the session JSON from.
        :returns: ActionResult with data containing restored cookie count.
        """
        import json
        start = time.monotonic()
        if not self._page:
            return action_result(ok=False, error=ActionError(ErrorCategory.BROWSER_CRASH, "Browser not started."))
        stealth_bridge = getattr(self._page.engine_page, "stealth_bridge", None)
        if stealth_bridge is None:
            return action_result(ok=False, error=ActionError(ErrorCategory.VALIDATION, "No stealth bridge available for cookie access."))
        params = {"path": path}
        sec = await self._check_facade_security("load_session", params, security_level="dangerous")
        if sec is not None:
            return sec
        path = params["path"]
        try:
            from pathlib import Path as _Path
            source = _Path(path)
            if not source.exists():
                return action_result(ok=False, error=ActionError(ErrorCategory.VALIDATION, f"Session file not found: {path}"))
            session_data = json.loads(source.read_text(encoding="utf-8"))
            version = session_data.get("version", "")
            if version != "1.0":
                return action_result(ok=False, error=ActionError(ErrorCategory.VALIDATION, f"Unsupported session format version: {version}"))
            cookies = session_data.get("cookies", [])
            if not cookies:
                return timed_action_result(
                    ok=True,
                    start_ns=start,
                    data={"path": str(source), "cookie_count": 0, "message": "No cookies to restore."},
                    method=ActionMethod.SELECTOR,
                )
            await stealth_bridge.set_cookies(cookies)
            return timed_action_result(
                ok=True,
                start_ns=start,
                data={"path": str(source), "cookie_count": len(cookies)},
                method=ActionMethod.SELECTOR,
            )
        except json.JSONDecodeError as exc:
            return timed_action_result(
                ok=False,
                start_ns=start,
                error=ActionError(ErrorCategory.VALIDATION, f"Invalid JSON in session file: {exc}"),
            )
        except Exception as exc:
            return timed_action_result(
                ok=False,
                start_ns=start,
                error=ActionError(ErrorCategory.BROWSER_CRASH, f"load_session failed: {exc}"),
            )

    # -- Recording --

    def enable_recording(self, *, max_screenshots: int = 100) -> None:
        """Enable session recording. Call before or after start()."""
        from super_browser.events.bus import EventBus
        from super_browser.recording.recorder import SessionRecorder

        if self._event_bus is None:
            self._event_bus = EventBus()
        cdp = None
        if self._page and hasattr(self._page, "engine_page"):
            cdp = self._page.engine_page.cdp
        self._recorder = SessionRecorder(
            self._event_bus, cdp, max_screenshots=max_screenshots,
        )

    @property
    def recording(self) -> Any:
        """Access the active SessionRecorder, or None if recording is not enabled."""
        return self._recorder

    @property
    def event_bus(self) -> Any:
        """Access the EventBus, or None if not initialized."""
        return self._event_bus

    async def replay(self, path: str, *, delay_ms: float = 100) -> ActionResult:
        """Load a recording from *path* and replay it against this browser.

        :param path: Path to a recording JSON file.
        :param delay_ms: Delay between actions in milliseconds.
        :returns: ActionResult with data=ReplayReport.
        """
        start = time.monotonic()
        params = {"path": path}
        sec = await self._check_facade_security("replay", params, security_level="dangerous")
        if sec is not None:
            return sec
        path = params["path"]
        try:
            from super_browser.recording.persistence import load as load_recording
            from super_browser.recording.replayer import RecordingReplayer

            recording = load_recording(path)
            replayer = RecordingReplayer(self)
            report = await replayer.replay(recording, delay_ms=delay_ms)
            return timed_action_result(
                ok=True,
                start_ns=start,
                data=report,
            )
        except Exception as exc:
            return timed_action_result(
                ok=False,
                start_ns=start,
                error=ActionError(ErrorCategory.BROWSER_CRASH, f"Replay failed: {exc}"),
            )

    @property
    def is_running(self) -> bool:
        return self._running

    # -- Stealth Backend --

    @property
    def stealth_backend(self) -> str:
        """Name of the active stealth backend ('cloak' or 'patchright')."""
        if self._session is not None:
            return self._session.stealth_backend
        return "patchright"

    @property
    def cloak_config(self) -> Any:
        """The CloakConfig if CloakBrowser is available, else None."""
        if self._session is not None and self._engine is not None:
            return self._engine.cloak_config
        return None

    # -- Memory --

    def enable_memory(
        self,
        *,
        memory_dir: str = "~/.config/super-browser/memory",
        ttl_days: int = 30,
    ) -> None:
        """Enable per-domain memory persistence (opt-in).

        Call before or after :meth:`start`.
        """
        from super_browser.memory.integration import create_memory_store
        self._memory_store = create_memory_store(
            memory_enabled=True,
            memory_dir=memory_dir,
            ttl_days=ttl_days,
        )

    @property
    def memory(self) -> Optional[MemoryStore]:
        """Access the active MemoryStore, or None if memory is not enabled."""
        return self._memory_store


class ConfigurationError(Exception):
    """Raised when SuperBrowser is used without required configuration."""
