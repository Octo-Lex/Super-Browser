"""MultimodalController — three-tier cascade interaction engine."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable
from typing import Any, Optional
from urllib.parse import urlparse

from super_browser.browser.cdp import CDPBridge
from super_browser.browser.page import NormalizedPage
from super_browser.interaction.cache import TierPreferenceCache
from super_browser.interaction.decorator import agent_action
from super_browser.interaction.recovery import StaleRefDetector
from super_browser.interaction.snapshot import SnapshotProvider
from super_browser.interaction.types import (
    AXSnapshot,
    CascadeResult,
    Tier,
    TierAttempt,
    TierOutcome,
    VisionRequest,
)
from super_browser.interaction.vision import VisionProviderFactory
from super_browser.results import (
    ActionError,
    ActionMethod,
    ActionResult,
    ClickResult,
    DragResult,
    ErrorCategory,
    FailureCategory,
    FillResult,
    HoverResult,
    KeypressResult,
    ScrollResult,
    SelectResult,
    action_result,
    timed_action_result,
)

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUTS: dict[Tier, float] = {
    Tier.SELECTOR: 5.0,
    Tier.COORDINATE: 3.0,
    Tier.VISION: 15.0,
}


class FrameInteractionTarget:
    """Adapter that normalises a Playwright ``FrameLocator`` to the
    selector-tier method signatures used by ``MultimodalController``.

    Playwright ``FrameLocator`` requires ``.locator(sel).click()`` while
    the controller's selector-tier methods call ``target.click(sel)``.
    This adapter bridges that gap so the same calling convention works
    whether interactions target the top-level page or an iframe.

    Methods that have no frame-scoped equivalent (e.g. viewport scroll
    without a target element) raise ``NotImplementedError`` so the
    controller's cascade falls back to coordinate/CDP tiers.
    """

    def __init__(self, frame_locator: Any) -> None:
        self._frame = frame_locator

    async def click(self, selector: str, **kwargs: Any) -> None:
        await self._frame.locator(selector).click(**kwargs)

    async def fill(self, selector: str, value: str, **kwargs: Any) -> None:
        await self._frame.locator(selector).fill(value, **kwargs)

    async def select_option(self, selector: str, **kwargs: Any) -> None:
        await self._frame.locator(selector).select_option(**kwargs)

    async def hover(self, selector: str) -> None:
        await self._frame.locator(selector).hover()

    async def check(self, selector: str) -> None:
        await self._frame.locator(selector).check()

    async def uncheck(self, selector: str) -> None:
        await self._frame.locator(selector).uncheck()

    async def focus(self, selector: str) -> None:
        await self._frame.locator(selector).focus()

    async def type_text(self, selector: str, text: str, *, delay: int = 0) -> None:
        await self._frame.locator(selector).type(text, delay=delay)

    async def drag_and_drop(self, source: str, target: str) -> None:
        src = self._frame.locator(source)
        dst = self._frame.locator(target)
        await src.drag_to(dst)

    async def scroll(
        self,
        direction: str,
        amount: int,
        target: Optional[str] = None,
    ) -> None:
        if target:
            await self._frame.locator(target).scroll(direction, amount)
        else:
            raise NotImplementedError(
                "Viewport scroll not supported inside frames; "
                "use coordinate-tier scroll or provide a target element"
            )


class MultimodalController:

    def __init__(
        self,
        # P3: the façade hands the controller a NormalizedPage. Cascade and
        # capability behavior are unchanged here — P4 owns making the
        # coordinate tier genuinely capability-aware.
        page: NormalizedPage,
        cdp: CDPBridge,
        tier_cache: Optional[TierPreferenceCache] = None,
        vision_provider: Optional[VisionProviderFactory] = None,
        *,
        tier_timeouts: Optional[dict[Tier, float]] = None,
        snapshot_provider: Optional[SnapshotProvider] = None,
        vision_controller: Any = None,
    ) -> None:
        self._page = page
        self._cdp = cdp
        self._cache = tier_cache
        self._vision_factory = vision_provider
        self._timeouts = tier_timeouts or dict(_DEFAULT_TIMEOUTS)
        self._snapshot_provider = snapshot_provider or SnapshotProvider(cdp)
        self._ax_snapshot: Optional[AXSnapshot] = None
        self._last_url: str = ""
        self._two_phase: bool = False
        self._verifier: Any = None
        self._vision_controller = vision_controller
        self._frame_target: Optional["FrameInteractionTarget"] = None

    # =====================================================================
    # Frame scoping
    # =====================================================================

    @property
    def _interaction_target(self) -> Any:
        """The target for selector-based interactions.

        Returns a :class:`FrameInteractionTarget` adapter when inside an
        iframe (set by ``enter_frame``), otherwise the top-level engine
        page.  The adapter normalises Playwright's ``FrameLocator`` API
        (``.locator(sel).click()``) to match the selector-tier method
        signatures used by this controller (``.click(sel)``).
        """
        if self._frame_target is not None:
            return self._frame_target
        return self._page.engine_page

    def _set_frame_locator(self, frame_locator: Any) -> None:
        """Set the active frame for scoping selector-based interactions.

        Wraps the raw Playwright ``FrameLocator`` in a
        :class:`FrameInteractionTarget` adapter so that selector-tier
        methods receive the same calling convention as ``engine_page``.
        """
        self._frame_target = FrameInteractionTarget(frame_locator)

    def _clear_frame_locator(self) -> None:
        """Clear the frame, returning to the top-level page."""
        self._frame_target = None

    # =====================================================================
    # Action methods
    # =====================================================================

    @agent_action(security_level="sensitive")
    async def click(
        self,
        target: str,
        *,
        button: str = "left",
        click_count: int = 1,
        description: Optional[str] = None,
    ) -> ActionResult:
        """Click on an element identified by selector, coordinates, or description."""

        async def t1():
            await self._interaction_target.click(target, button=button, click_count=click_count)
            return action_result(
                ok=True,
                data=ClickResult(target=target, method=ActionMethod.SELECTOR),
                method=ActionMethod.SELECTOR,
            )

        async def t2():
            coords = await self._resolve_to_coordinates(target)
            if coords is None:
                return action_result(ok=False, error=ActionError(ErrorCategory.SELECTOR_NOT_FOUND, f"Cannot resolve coordinates for '{target}'"))
            x, y = coords
            await self._cdp.compositor_click(x, y, button=button, click_count=click_count)
            return action_result(
                ok=True,
                data=ClickResult(target=target, method=ActionMethod.COORDINATE, coordinates=(x, y)),
                method=ActionMethod.COORDINATE,
            )

        async def t3():
            return await self._vision_click(target, description or target, button, click_count)

        result, _ = await self._execute_with_stale_recovery("click", target, description, t1, t2, t3)
        return result

    @agent_action(security_level="sensitive")
    async def fill(
        self,
        target: str,
        value: str,
        *,
        clear_first: bool = True,
        description: Optional[str] = None,
    ) -> ActionResult:

        async def t1():
            if clear_first:
                await self._interaction_target.click(target)
                await self._cdp.compositor_key_press("a", modifiers=2)
            await self._interaction_target.fill(target, value)
            return action_result(
                ok=True,
                data=FillResult(selector=target, value_entered=value, method=ActionMethod.SELECTOR, character_count=len(value), clear_first=clear_first),
                method=ActionMethod.SELECTOR,
            )

        async def t2():
            coords = await self._resolve_to_coordinates(target)
            if coords is None:
                return action_result(ok=False, error=ActionError(ErrorCategory.SELECTOR_NOT_FOUND, f"Cannot resolve coordinates for '{target}'"))
            x, y = coords
            await self._cdp.compositor_click(x, y)
            if clear_first:
                await self._cdp.compositor_key_press("a", modifiers=2)
            await self._cdp.compositor_type(value)
            return action_result(
                ok=True,
                data=FillResult(selector=target, value_entered=value, method=ActionMethod.COORDINATE, character_count=len(value), clear_first=clear_first),
                method=ActionMethod.COORDINATE,
            )

        async def t3():
            return await self._vision_fill(target, value, description or target, clear_first)

        result, _ = await self._execute_with_stale_recovery("fill", target, description, t1, t2, t3)
        return result

    @agent_action(security_level="sensitive")
    async def select(
        self,
        target: str,
        option: str,
        *,
        by: str = "text",
        description: Optional[str] = None,
    ) -> ActionResult:

        async def t1():
            await self._interaction_target.select_option(target, **{by: option})
            return action_result(
                ok=True,
                data=SelectResult(selector=target, option=option, method=ActionMethod.SELECTOR, by=by),
                method=ActionMethod.SELECTOR,
            )

        async def t2():
            coords = await self._resolve_to_coordinates(target)
            if coords is None:
                return action_result(ok=False, error=ActionError(ErrorCategory.SELECTOR_NOT_FOUND, f"Cannot resolve '{target}'"))
            await self._cdp.compositor_click(*coords)
            await asyncio.sleep(0.1)
            snap = await self.capture_ax_snapshot()
            for node in snap.find_by_role("option") + snap.find_by_role("treeitem"):
                if option.lower() in node.name.lower():
                    if node.center:
                        await self._cdp.compositor_click(*node.center)
                        return action_result(
                            ok=True,
                            data=SelectResult(selector=target, option=option, method=ActionMethod.COORDINATE, by=by),
                            method=ActionMethod.COORDINATE,
                        )
            return action_result(ok=False, error=ActionError(ErrorCategory.SELECTOR_NOT_FOUND, f"Option '{option}' not found"))

        async def t3():
            return await self._vision_click(target, description or target)

        result, _ = await self._cascade("select", target, description, t1, t2, t3)
        return result

    @agent_action(security_level="sensitive")
    async def hover(
        self,
        target: str,
        *,
        description: Optional[str] = None,
    ) -> ActionResult:

        async def t1():
            await self._interaction_target.hover(target)
            return action_result(
                ok=True,
                data=HoverResult(target=target, method=ActionMethod.SELECTOR),
                method=ActionMethod.SELECTOR,
            )

        async def t2():
            coords = await self._resolve_to_coordinates(target)
            if coords is None:
                return action_result(ok=False, error=ActionError(ErrorCategory.SELECTOR_NOT_FOUND, f"Cannot resolve coordinates for '{target}'"))
            await self._cdp.send("Input.dispatchMouseEvent", {
                "type": "mouseMoved", "x": coords[0], "y": coords[1],
            })
            return action_result(
                ok=True,
                data=HoverResult(target=target, method=ActionMethod.COORDINATE, coordinates=coords),
                method=ActionMethod.COORDINATE,
            )

        async def t3():
            coords = await self._vision_locate(description or target)
            if coords is None:
                return action_result(ok=False, error=ActionError(ErrorCategory.SELECTOR_NOT_FOUND, "Vision could not locate element"))
            await self._cdp.send("Input.dispatchMouseEvent", {
                "type": "mouseMoved", "x": coords[0], "y": coords[1],
            })
            return action_result(
                ok=True,
                data=HoverResult(target=target, method=ActionMethod.VISION, coordinates=coords),
                method=ActionMethod.VISION,
            )

        result, _ = await self._cascade("hover", target, description, t1, t2, t3)
        return result

    @agent_action(security_level="sensitive")
    async def check(
        self,
        target: str,
        *,
        description: Optional[str] = None,
    ) -> ActionResult:
        """Check a checkbox or radio button.

        Selector-tier only — coordinate/vision fallbacks would be toggle-like
        (clicks that can uncheck an already-checked box), violating the
        idempotent state-enforcing semantics implied by the tool name.
        """

        async def t1():
            await self._interaction_target.check(target)
            return action_result(ok=True, data={"target": target}, method=ActionMethod.SELECTOR)

        result, _ = await self._cascade("check", target, description, t1)
        return result

    @agent_action(security_level="sensitive")
    async def uncheck(
        self,
        target: str,
        *,
        description: Optional[str] = None,
    ) -> ActionResult:
        """Uncheck a checkbox.

        Selector-tier only — same rationale as ``check``: coordinate/vision
        fallbacks would be toggle-like, not state-enforcing.
        """

        async def t1():
            await self._interaction_target.uncheck(target)
            return action_result(ok=True, data={"target": target}, method=ActionMethod.SELECTOR)

        result, _ = await self._cascade("uncheck", target, description, t1)
        return result

    @agent_action(security_level="sensitive")
    async def focus(
        self,
        target: str,
        *,
        description: Optional[str] = None,
    ) -> ActionResult:
        """Focus an element."""

        async def t1():
            await self._interaction_target.focus(target)
            return action_result(ok=True, data={"target": target}, method=ActionMethod.SELECTOR)

        # Focus has no meaningful coordinate/vision fallback — a click would be
        # a different action, not a focus. Only selector-tier.
        result, _ = await self._cascade("focus", target, description, t1)
        return result

    @agent_action(security_level="sensitive")
    async def type_text(
        self,
        target: str,
        text: str,
        *,
        delay: int = 0,
        description: Optional[str] = None,
    ) -> ActionResult:
        """Type text character-by-character (per-keystroke), unlike fill.

        Triggers JS keydown/keypress/keyup listeners, suitable for autocomplete,
        masked inputs, and debounce handlers.
        """

        async def t1():
            await self._interaction_target.type_text(target, text, delay=delay)
            return action_result(ok=True, data={"target": target, "text_length": len(text)}, method=ActionMethod.SELECTOR)

        async def t2():
            coords = await self._resolve_to_coordinates(target)
            if coords is None:
                return action_result(ok=False, error=ActionError(ErrorCategory.SELECTOR_NOT_FOUND, f"Cannot resolve '{target}'"))
            # Click to focus, then type via CDP.
            await self._cdp.send("Input.dispatchMouseEvent", {
                "type": "mousePressed", "x": coords[0], "y": coords[1],
                "button": "left", "clickCount": 1,
            })
            await self._cdp.send("Input.dispatchMouseEvent", {
                "type": "mouseReleased", "x": coords[0], "y": coords[1],
                "button": "left", "clickCount": 1,
            })
            for char in text:
                await self._cdp.send("Input.dispatchKeyEvent", {
                    "type": "char", "text": char,
                })
                if delay:
                    await asyncio.sleep(delay / 1000)
            return action_result(ok=True, data={"target": target, "method": "coordinate"}, method=ActionMethod.COORDINATE)

        result, _ = await self._cascade("type_text", target, description, t1, t2)
        return result

    @agent_action(security_level="dangerous")
    async def drag(
        self,
        source: str,
        destination: str,
        *,
        steps: int = 5,
        source_description: Optional[str] = None,
        destination_description: Optional[str] = None,
    ) -> ActionResult:

        async def t1():
            await self._interaction_target.drag_and_drop(source, destination)
            return action_result(
                ok=True,
                data=DragResult(source=source, destination=destination, method=ActionMethod.SELECTOR),
                method=ActionMethod.SELECTOR,
            )

        async def t2():
            src = await self._resolve_to_coordinates(source)
            dst = await self._resolve_to_coordinates(destination)
            if src is None or dst is None:
                return action_result(ok=False, error=ActionError(ErrorCategory.SELECTOR_NOT_FOUND, "Cannot resolve source/destination"))
            await self._cdp.send("Input.dispatchMouseEvent", {
                "type": "mousePressed", "x": src[0], "y": src[1], "button": "left", "clickCount": 1,
            })
            for i in range(1, steps + 1):
                frac = i / steps
                mx = src[0] + (dst[0] - src[0]) * frac
                my = src[1] + (dst[1] - src[1]) * frac
                await self._cdp.send("Input.dispatchMouseEvent", {
                    "type": "mouseMoved", "x": mx, "y": my,
                })
            await self._cdp.send("Input.dispatchMouseEvent", {
                "type": "mouseReleased", "x": dst[0], "y": dst[1], "button": "left", "clickCount": 1,
            })
            return action_result(
                ok=True,
                data=DragResult(source=source, destination=destination, method=ActionMethod.COORDINATE, source_coords=src, dest_coords=dst),
                method=ActionMethod.COORDINATE,
            )

        async def t3():
            src = await self._vision_locate(source_description or source)
            dst = await self._vision_locate(destination_description or destination)
            if src is None or dst is None:
                return action_result(ok=False, error=ActionError(ErrorCategory.SELECTOR_NOT_FOUND, "Vision could not locate source/destination"))
            await self._cdp.compositor_click(*src)
            await self._cdp.compositor_click(*dst)
            return action_result(
                ok=True,
                data=DragResult(source=source, destination=destination, method=ActionMethod.VISION, source_coords=src, dest_coords=dst),
                method=ActionMethod.VISION,
            )

        result, _ = await self._cascade("drag", source, source_description, t1, t2, t3)
        return result

    @agent_action(security_level="safe")
    async def scroll(
        self,
        *,
        direction: str = "down",
        amount: int = 3,
        target: Optional[str] = None,
    ) -> ActionResult:
        delta_map = {"down": (0, 100), "up": (0, -100), "right": (100, 0), "left": (-100, 0)}
        dx, dy = delta_map.get(direction, (0, 100))

        async def t1():
            await self._interaction_target.scroll(direction, amount, target=target)
            return action_result(
                ok=True,
                data=ScrollResult(direction=direction, amount=amount, method=ActionMethod.SELECTOR),
                method=ActionMethod.SELECTOR,
            )

        async def t2():
            await self._cdp.send("Input.dispatchMouseEvent", {
                "type": "mouseWheel", "deltaX": dx * amount, "deltaY": dy * amount,
                "x": 0, "y": 0,
            })
            return action_result(
                ok=True,
                data=ScrollResult(direction=direction, amount=amount, method=ActionMethod.COORDINATE),
                method=ActionMethod.COORDINATE,
            )

        result, _ = await self._execute_with_stale_recovery("scroll", target or "page", None, t1, t2, None)
        return result

    @agent_action(security_level="safe")
    async def keypress(
        self,
        key: str,
        *,
        modifiers: int = 0,
    ) -> ActionResult:
        start = time.monotonic()
        await self._cdp.compositor_key_press(key, modifiers=modifiers)
        return timed_action_result(
            ok=True,
            start_ns=start,
            data=KeypressResult(key=key, modifiers=modifiers),
            method=ActionMethod.COORDINATE,
        )

    # =====================================================================
    # AX Snapshot
    # =====================================================================

    async def capture_ax_snapshot(self) -> AXSnapshot:
        url = self._page.url
        title = await self._page.title()
        self._ax_snapshot = await self._snapshot_provider.capture_ax_only(url, title)
        self._last_url = url
        return self._ax_snapshot

    # =====================================================================
    # Visual Verification (GAP-03)
    # =====================================================================

    def enable_verification(self, verifier: Any) -> None:
        self._two_phase = True
        self._verifier = verifier

    @property
    def two_phase(self) -> bool:
        return self._two_phase

    # =====================================================================
    # Cascade engine
    # =====================================================================

    async def _execute_with_stale_recovery(
        self,
        action: str,
        target: str,
        description: Optional[str],
        tier1_fn: Callable,
        tier2_fn: Callable,
        tier3_fn: Optional[Callable] = None,
    ) -> tuple[ActionResult, CascadeResult]:
        """Execute cascade with automatic stale-ref retry."""
        result, cascade = await self._cascade(action, target, description, tier1_fn, tier2_fn, tier3_fn)

        if result.ok:
            return result, cascade

        # Check if error looks like a stale ref
        error_msg = str(result.error.message) if result.error else ""
        if not StaleRefDetector.is_stale(error_msg):
            return result, cascade

        # Auto-retry: refresh snapshot and try cascade again
        try:
            await self.capture_ax_snapshot()
        except Exception:
            pass  # snapshot refresh failed, still return retry result

        retry_result, retry_cascade = await self._cascade(
            action, target, description, tier1_fn, tier2_fn, tier3_fn,
        )

        if retry_result.ok:
            return retry_result, retry_cascade

        # Both attempts failed — mark as stale ref with recovery guidance
        retry_result.failure_category = FailureCategory.STALE_REF
        retry_result.next_actions = StaleRefDetector.get_next_actions(action, target)
        return retry_result, retry_cascade

    async def _cascade(
        self,
        action: str,
        target: str,
        description: Optional[str],
        tier1_fn: Callable,
        tier2_fn: Callable,
        tier3_fn: Optional[Callable] = None,
    ) -> tuple[ActionResult, CascadeResult]:
        start = time.monotonic()
        domain = self._extract_domain()
        pattern = self._classify_selector_pattern(target)

        preferred = self._cache.get(domain, pattern) if self._cache else None

        tier_order: list[Tier] = list(Tier)
        if preferred:
            tier_order = [preferred] + [t for t in Tier if t != preferred]

        has_vision = (
            self._vision_factory is not None
            and self._vision_factory.get_provider() is not None
        )
        if not has_vision:
            tier_order = [t for t in tier_order if t != Tier.VISION]

        fn_map: dict[Tier, Optional[Callable]] = {
            Tier.SELECTOR: tier1_fn,
            Tier.COORDINATE: tier2_fn,
            Tier.VISION: tier3_fn,
        }

        attempts: list[TierAttempt] = []

        for tier in tier_order:
            fn = fn_map.get(tier)
            if fn is None:
                attempts.append(TierAttempt(tier, TierOutcome.UNAVAILABLE, 0.0))
                continue

            tier_start = time.monotonic()
            timeout = self._timeouts.get(tier, 5.0)
            try:
                result = await asyncio.wait_for(fn(), timeout=timeout)
                duration = (time.monotonic() - tier_start) * 1000

                if result.ok:
                    attempts.append(TierAttempt(tier, TierOutcome.SUCCESS, duration))
                    if self._cache:
                        self._cache.record_success(domain, pattern, tier)
                        await self._cache.persist(domain)
                    total = (time.monotonic() - start) * 1000
                    cascade = CascadeResult(action, target, tuple(attempts), tier, total)
                    return result, cascade
                else:
                    attempts.append(TierAttempt(tier, TierOutcome.FAILED, duration, error=str(result.error.message if result.error else "unknown")))
                    if self._cache:
                        self._cache.record_failure(domain, pattern, tier)

            except asyncio.TimeoutError:
                duration = (time.monotonic() - tier_start) * 1000
                attempts.append(TierAttempt(tier, TierOutcome.FAILED, duration, error="timeout"))
                if self._cache:
                    self._cache.record_failure(domain, pattern, tier)

            except Exception as exc:
                duration = (time.monotonic() - tier_start) * 1000
                attempts.append(TierAttempt(tier, TierOutcome.FAILED, duration, error=str(exc)))
                if self._cache:
                    self._cache.record_failure(domain, pattern, tier)

        total = (time.monotonic() - start) * 1000
        cascade = CascadeResult(action, target, tuple(attempts), None, total)
        error_summary = "; ".join(
            f"T{a.tier.value}:{a.error}" for a in attempts if a.error
        )
        error = ActionError(
            category=ErrorCategory.SELECTOR_NOT_FOUND,
            message=f"All tiers failed for {action}('{target}')",
            recoverable=True,
            retry_hint=error_summary,
        )
        result = action_result(ok=False, error=error)
        return result, cascade

    # =====================================================================
    # Coordinate resolution
    # =====================================================================

    async def _resolve_to_coordinates(self, target: str) -> Optional[tuple[float, float]]:
        if target.startswith("@"):
            if self._ax_snapshot is None:
                await self.capture_ax_snapshot()
            node = self._ax_snapshot.resolve(target) if self._ax_snapshot else None
            if node and node.center:
                return node.center
            return None

        if target.startswith("//") or target.startswith("./"):
            # HB-09-01: No f-string interpolation — selector passed as JSON arg
            selector_json = json.dumps(target)
            expr = (
                '(function() {'
                '  var xpath = JSON.parse(' + selector_json + ');'
                '  var r = document.evaluate(xpath, document, null, '
                '    XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;'
                '  if (!r) return null;'
                '  var rect = r.getBoundingClientRect();'
                '  return JSON.stringify({x: rect.x, y: rect.y, w: rect.width, h: rect.height});'
                '})()'
            )
            result = await self._cdp.evaluate(expr)
            if result.ok and result.data:
                val = result.data.get("result", {}).get("value")
                if val:
                    b = json.loads(val)
                    return (b["x"] + b["w"] / 2, b["y"] + b["h"] / 2)
            return None

        # HB-09-01: No f-string interpolation — selector passed as JSON arg
        selector_json = json.dumps(target)
        expr = (
            '(function() {'
            '  var sel = JSON.parse(' + selector_json + ');'
            '  var el = document.querySelector(sel);'
            '  if (!el) return null;'
            '  var rect = el.getBoundingClientRect();'
            '  return JSON.stringify({x: rect.x, y: rect.y, w: rect.width, h: rect.height});'
            '})()'
        )
        result = await self._cdp.evaluate(expr)
        if result.ok and result.data:
            val = result.data.get("result", {}).get("value")
            if val:
                b = json.loads(val)
                return (b["x"] + b["w"] / 2, b["y"] + b["h"] / 2)
        return None

    # =====================================================================
    # Vision helpers
    # =====================================================================

    async def _vision_locate(self, description: str) -> Optional[tuple[float, float]]:
        snap_result = await self._cdp.capture_screenshot(format="png")
        if not snap_result.ok or not snap_result.data:
            return None
        screenshot_data = snap_result.data.get("data")
        if not screenshot_data:
            return None
        import base64
        screenshot_bytes = base64.b64decode(screenshot_data)

        if self._vision_controller is not None:
            response = await self._vision_controller.locate_element(
                screenshot_bytes, description, (1280, 720),
                ax_snapshot=self._ax_snapshot,
            )
            if response.found and response.x is not None and response.y is not None:
                return (response.x, response.y)
            return None

        if not self._vision_factory:
            return None
        provider = self._vision_factory.get_provider()
        if not provider:
            return None
        request = VisionRequest(
            screenshot=screenshot_bytes,
            element_description=description,
            page_url=self._page.url,
            viewport_size=(1280, 720),
        )
        response = await provider.locate(request)
        if response.found and response.x is not None and response.y is not None:
            return (response.x, response.y)
        return None

    async def _vision_click(self, target: str, description: str, button: str = "left", click_count: int = 1) -> ActionResult:
        coords = await self._vision_locate(description)
        if coords is None:
            return action_result(ok=False, error=ActionError(ErrorCategory.SELECTOR_NOT_FOUND, f"Vision could not locate '{target}'"))
        await self._cdp.compositor_click(coords[0], coords[1], button=button, click_count=click_count)
        return action_result(
            ok=True,
            data=ClickResult(target=target, method=ActionMethod.VISION, coordinates=coords),
            method=ActionMethod.VISION,
        )

    async def _vision_fill(self, target: str, value: str, description: str, clear_first: bool) -> ActionResult:
        coords = await self._vision_locate(description)
        if coords is None:
            return action_result(ok=False, error=ActionError(ErrorCategory.SELECTOR_NOT_FOUND, f"Vision could not locate '{target}'"))
        await self._cdp.compositor_click(*coords)
        if clear_first:
            await self._cdp.compositor_key_press("a", modifiers=2)
        await self._cdp.compositor_type(value)
        return action_result(
            ok=True,
            data=FillResult(selector=target, value_entered=value, method=ActionMethod.VISION, character_count=len(value), clear_first=clear_first),
            method=ActionMethod.VISION,
        )

    # =====================================================================
    # Utility
    # =====================================================================

    def _extract_domain(self) -> str:
        try:
            return urlparse(self._page.url).hostname or ""
        except Exception:
            return ""

    def _classify_selector_pattern(self, target: str) -> str:
        if target.startswith("@"):
            return "@ref"
        if target.startswith("//") or target.startswith("./"):
            return "xpath"
        # tag.class-name-suffix -> tag.*
        m = re.match(r"^([a-zA-Z][\w-]*)\.[\w-]+", target)
        if m:
            return f"{m.group(1)}.*"
        # #id-prefix-suffix -> #prefix-*
        m = re.match(r"^#([\w]+)[\w-]*", target)
        if m:
            return f"#{m.group(1)}-*"
        return target
