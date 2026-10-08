"""CDPDirectBackend — raw CDP websocket engine.

Connects to a Chrome DevTools Protocol websocket endpoint and reuses
the existing :class:`CDPBridge` via a :class:`WebSocketCDPSession` adapter.
This gives us compositor-level click/type, evaluate, screenshot capture,
event buffering, and stealth features without Playwright or Patchright.

Architecture::

    WebSocketCDPSession  ←→  raw websocket  ←→  Chrome
            ↓
        CDPBridge  (reused ~150 lines of logic)
            ↓
    CDPDirectPage / CDPDirectStealthBridge

``websockets`` is an optional dependency — importing this module never
crashes.  The :class:`CDPDirectEngine` raises on ``start()`` if the
library is unavailable.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Optional

import aiohttp

from super_browser.browser.cdp import CDPBridge, CDPResult
from super_browser.browser.config import SessionConfig
from super_browser.browser.engine import EngineCapabilities

logger = logging.getLogger(__name__)

# -- Optional dependency --------------------------------------------------

try:
    import websockets  # type: ignore[import-untyped]

    _WEBSOCKETS_AVAILABLE = True
except ImportError:
    websockets = None  # type: ignore[assignment]
    _WEBSOCKETS_AVAILABLE = False


# =====================================================================
# WebSocketCDPSession — adapter for CDPBridge
# =====================================================================


class WebSocketCDPSession:
    """Thin async adapter that wraps a raw CDP websocket.

    Provides the ``.send(method, params) -> dict`` and ``.on(event, handler)``
    interfaces that :class:`CDPBridge` expects from its underlying session.
    Message-ID correlation is used for request/response matching, and a
    background reader task dispatches CDP events to registered handlers.
    """

    def __init__(self, ws_url: str) -> None:
        self._ws_url = ws_url
        self._ws: Any = None
        self._msg_id = 0
        self._pending: dict[int, asyncio.Future[dict]] = {}
        self._handlers: dict[str, list[Callable]] = {}
        self._reader_task: Optional[asyncio.Task[None]] = None
        self._session_id: Optional[str] = None
        self._target_id: Optional[str] = None

    # -- Lifecycle --------------------------------------------------

    async def connect(self) -> None:
        """Establish the websocket and start the background reader."""
        if not _WEBSOCKETS_AVAILABLE:
            raise RuntimeError(
                "websockets is required for CDPDirectBackend. "
                "Install with: pip install websockets>=12.0"
            )
        self._ws = await websockets.connect(self._ws_url, max_size=2**24)  # type: ignore[union-attr]
        self._reader_task = asyncio.create_task(self._reader_loop())

    async def close(self) -> None:
        """Close the websocket and cancel the reader."""
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
            self._reader_task = None
        if self._ws is not None:
            await self._ws.close()
            self._ws = None
        # Resolve any pending futures with an error
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(RuntimeError("WebSocket closed"))
        self._pending.clear()

    # -- CDPBridge interface ----------------------------------------

    async def send(self, method: str, params: Optional[dict] = None) -> dict:
        """Send a JSON-RPC message and wait for the response.

        Returns the ``result`` dict from the CDP response on success,
        or a dict with an ``error`` key on failure.
        """
        if self._ws is None:
            return {"error": {"message": "WebSocket not connected"}}
        self._msg_id += 1
        msg_id = self._msg_id
        payload: dict[str, Any] = {"id": msg_id, "method": method, "params": params or {}}
        if self._session_id:
            payload["sessionId"] = self._session_id
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict] = loop.create_future()
        self._pending[msg_id] = fut
        try:
            await self._ws.send(json.dumps(payload))
        except Exception as exc:
            self._pending.pop(msg_id, None)
            return {"error": {"message": str(exc)}}
        try:
            return await asyncio.wait_for(fut, timeout=30.0)
        except asyncio.TimeoutError:
            self._pending.pop(msg_id, None)
            return {"error": {"message": f"Timeout waiting for response to {method}"}}

    def on(self, event: str, handler: Callable) -> None:
        """Register an event handler (for CDPBridge compatibility)."""
        self._handlers.setdefault(event, []).append(handler)

    @property
    def id(self) -> Optional[str]:
        """Return the CDP session ID (for CDPBridge.session_id)."""
        return self._session_id

    @id.setter
    def id(self, value: Optional[str]) -> None:
        self._session_id = value

    @property
    def target_id(self) -> Optional[str]:
        """CDP target ID."""
        return self._target_id

    @target_id.setter
    def target_id(self, value: Optional[str]) -> None:
        self._target_id = value

    # -- Background reader ------------------------------------------

    async def _reader_loop(self) -> None:
        """Read messages from the websocket and dispatch."""
        try:
            async for raw in self._ws:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                # Response to a pending request
                if "id" in msg:
                    msg_id = msg["id"]
                    fut = self._pending.pop(msg_id, None)
                    if fut is not None and not fut.done():
                        if "error" in msg:
                            fut.set_result(msg)
                        else:
                            fut.set_result(msg.get("result", {}))
                # CDP event
                method = msg.get("method")
                if method:
                    params = msg.get("params", {})
                    # Wildcard handlers (CDPBridge registers on("*", ...))
                    for handler in self._handlers.get("*", []):
                        try:
                            handler({"method": method, "params": params})
                        except Exception:
                            pass
                    # Specific handlers
                    for handler in self._handlers.get(method, []):
                        try:
                            handler(params)
                        except Exception:
                            pass
        except asyncio.CancelledError:
            pass
        except Exception:
            pass


# =====================================================================
# CDPDirectStealthBridge
# =====================================================================


class CDPDirectStealthBridge:
    """Wraps :class:`CDPBridge` to satisfy the :class:`StealthBridge` protocol.

    Every method delegates to the underlying CDPBridge — zero behavioural
    change.  ``inject_script_before_load`` is a stub wired in BATCH-49.
    """

    def __init__(self, cdp: CDPBridge) -> None:
        self._cdp = cdp

    # -- StealthBridge protocol ------------------------------------

    async def cdp_send(self, method: str, params: dict) -> CDPResult:
        """Send a raw CDP command and return the result."""
        return await self._cdp.send(method, params)

    async def inject_script_before_load(self, js: str) -> None:
        """Inject JS to run before page scripts execute.

        Stub — wired in BATCH-49 via inject_delivery.py.
        """
        logger.debug("inject_script_before_load called (%d chars) — stub", len(js))

    async def get_ax_tree(self) -> dict:
        """Get the full accessibility tree via CDP."""
        result = await self._cdp.send("Accessibility.getFullAXTree", {})
        if result.ok and result.data:
            return result.data
        return {}

    async def get_all_cookies(self) -> list[dict]:
        """Get all browser cookies via CDP."""
        result = await self._cdp.send("Network.getAllCookies", {})
        if result.ok and result.data:
            return result.data.get("cookies", [])
        return []

    async def set_cookies(self, cookies: list[dict]) -> None:
        """Set browser cookies via CDP."""
        await self._cdp.send("Network.setCookies", {"cookies": cookies})

    async def capture_screenshot_cdp(self, params: dict) -> dict:
        """Capture screenshot via CDP with custom parameters."""
        result = await self._cdp.send("Page.captureScreenshot", params)
        if result.ok and result.data:
            return result.data
        return {}

    # -- Convenience -----------------------------------------------

    @property
    def cdp(self) -> CDPBridge:
        """Direct access to the underlying CDPBridge."""
        return self._cdp


# =====================================================================
# CDPDirectPage
# =====================================================================


# The 21 EnginePage members mapped to CDP commands.
_ENGINE_PAGE_MEMBERS: list[str] = [
    "goto", "title", "url", "close", "content",
    "click", "fill", "select_option", "hover", "drag_and_drop",
    "scroll", "type_text", "press_key", "set_input_files",
    "evaluate", "screenshot",
    "route", "unroute_all",
    "frame_locator", "expect_download",
    "stealth_bridge",
]


class CDPDirectPage:
    """Implements the :class:`EnginePage` protocol via raw CDP commands.

    Most operations delegate to :class:`CDPBridge` (which in turn uses
    the :class:`WebSocketCDPSession` adapter).  Some operations use
    direct JavaScript evaluation for simplicity (``click``, ``fill``,
    ``select_option``).
    """

    def __init__(self, cdp: CDPBridge, ws_session: WebSocketCDPSession) -> None:
        self._cdp = cdp
        self._ws_session = ws_session
        self._stealth_bridge = CDPDirectStealthBridge(cdp)
        self._current_url: str = ""

    # -- Navigation ------------------------------------------------

    async def goto(self, url: str, *, wait_until: str = "load", **kwargs: Any) -> None:
        """Navigate to URL via Page.navigate."""
        result = await self._cdp.send("Page.navigate", {"url": url})
        self._current_url = url
        if not result.ok:
            raise RuntimeError(f"Navigation failed: {result.error}")

    async def title(self) -> str:
        """Get page title via Runtime.evaluate."""
        result = await self._cdp.evaluate("document.title")
        if result.ok and result.data:
            value = result.data.get("result", {}).get("value", "")
            return str(value)
        return ""

    @property
    def url(self) -> str:
        """Get current page URL."""
        return self._current_url

    @url.setter
    def url(self, value: str) -> None:
        self._current_url = value

    async def close(self) -> None:
        """Close this page via CDP target close."""
        target_id = self._ws_session.target_id
        if target_id:
            await self._cdp.send("Target.closeTarget", {"targetId": target_id})
        await self._ws_session.close()

    async def activate(self) -> None:
        """Not supported: CDP-direct advertises multi_tab=False (PR 1 P5).

        Stored pages are not genuinely independently addressable until a
        proper per-target CDP session implementation lands.
        """
        raise NotImplementedError(
            "CDP-direct multi-tab activation is not supported (multi_tab=False)"
        )

    async def content(self) -> str:
        """Get page HTML content via Runtime.evaluate."""
        result = await self._cdp.evaluate("document.documentElement.outerHTML")
        if result.ok and result.data:
            return str(result.data.get("result", {}).get("value", ""))
        return ""

    # -- Interaction -----------------------------------------------

    async def click(self, selector: str, **kwargs: Any) -> None:
        """Click an element via JavaScript."""
        await self._cdp.evaluate(
            f'document.querySelector({selector!r}).click()'
        )

    async def fill(self, selector: str, value: str, **kwargs: Any) -> None:
        """Fill an input via JavaScript."""
        await self._cdp.evaluate(
            f'(function(){{var el=document.querySelector({selector!r});'
            f'el.value={value!r};el.dispatchEvent(new Event("input",{{bubbles:true}}));}})()'
        )

    async def select_option(self, selector: str, value: Any) -> None:
        """Select an option via JavaScript."""
        await self._cdp.evaluate(
            f'(function(){{var el=document.querySelector({selector!r});'
            f'el.value={str(value)!r};el.dispatchEvent(new Event("change",{{bubbles:true}}));}})()'
        )

    async def hover(self, selector: str) -> None:
        """Hover over an element via Input.dispatchMouseEvent."""
        # Get element center coordinates
        result = await self._cdp.evaluate(
            f'(function(){{var el=document.querySelector({selector!r});'
            f'var r=el.getBoundingClientRect();'
            f'return JSON.stringify({{x:r.left+r.width/2,y:r.top+r.height/2}});}})()'
        )
        if result.ok and result.data:
            coords_str = result.data.get("result", {}).get("value", "{}")
            import json as _json
            coords = _json.loads(coords_str) if isinstance(coords_str, str) else coords_str
            await self._cdp.send("Input.dispatchMouseEvent", {
                "type": "mouseMoved",
                "x": coords.get("x", 0),
                "y": coords.get("y", 0),
            })

    async def drag_and_drop(self, source: str, target: str) -> None:
        """Drag from source to target via Input.dispatchMouseEvent."""
        result = await self._cdp.evaluate(
            f'(function(){{'
            f'var s=document.querySelector({source!r}).getBoundingClientRect();'
            f'var t=document.querySelector({target!r}).getBoundingClientRect();'
            f'return JSON.stringify({{'
            f'sx:s.left+s.width/2,sy:s.top+s.height/2,'
            f'tx:t.left+t.width/2,ty:t.top+t.height/2}});}})()'
        )
        if result.ok and result.data:
            import json as _json
            coords_str = result.data.get("result", {}).get("value", "{}")
            coords = _json.loads(coords_str) if isinstance(coords_str, str) else coords_str
            sx, sy = coords.get("sx", 0), coords.get("sy", 0)
            tx, ty = coords.get("tx", 0), coords.get("ty", 0)
            # mousePressed at source
            await self._cdp.send("Input.dispatchMouseEvent", {
                "type": "mousePressed", "x": sx, "y": sy,
                "button": "left", "clickCount": 1,
            })
            # mouseMoved to target
            await self._cdp.send("Input.dispatchMouseEvent", {
                "type": "mouseMoved", "x": tx, "y": ty,
            })
            # mouseReleased at target
            await self._cdp.send("Input.dispatchMouseEvent", {
                "type": "mouseReleased", "x": tx, "y": ty,
                "button": "left", "clickCount": 1,
            })

    async def scroll(
        self,
        direction: str,
        amount: int,
        target: Optional[str] = None,
    ) -> None:
        """Scroll via JavaScript."""
        delta_map = {"down": (0, 100), "up": (0, -100), "right": (100, 0), "left": (-100, 0)}
        dx, dy = delta_map.get(direction, (0, 100))
        if target:
            await self._cdp.evaluate(
                f'document.querySelector({target!r}).scrollBy({dx * amount},{dy * amount})'
            )
        else:
            await self._cdp.evaluate(
                f'window.scrollBy({dx * amount},{dy * amount})'
            )

    async def type_text(self, text: str) -> None:
        """Type text character-by-character via CDPBridge.compositor_type."""
        await self._cdp.compositor_type(text)

    async def press_key(self, key: str) -> None:
        """Press a key via CDPBridge.compositor_key_press."""
        await self._cdp.compositor_key_press(key)

    async def set_input_files(self, selector: str, path: str) -> None:
        """Set files on a file input element — not supported via raw CDP."""
        raise NotImplementedError("set_input_files is not supported via raw CDP")

    # -- Evaluation ------------------------------------------------

    async def evaluate(self, expression: str, *args: Any, **kwargs: Any) -> Any:
        """Evaluate JavaScript expression and return result."""
        result = await self._cdp.evaluate(expression)
        if result.ok and result.data:
            return result.data.get("result", {}).get("value")
        return None

    async def screenshot(self, **kwargs: Any) -> bytes:
        """Capture screenshot bytes, supporting png and jpeg formats.

        Forwarded kwargs:
          full_page: bool — capture beyond viewport.
          type: str — "png" or "jpeg" (Playwright spelling).
          format: str — "png" or "jpeg" (CDP spelling; preferred here).
          quality: int — 1-100, jpeg only.
        """
        import base64

        fmt = kwargs.get("format") or kwargs.get("type") or "png"
        quality = kwargs.get("quality")
        full_page = kwargs.get("full_page", False)
        result = await self._cdp.capture_screenshot(
            format=fmt, quality=quality, full_page=full_page,
        )
        if result.ok and result.data and "data" in result.data:
            return base64.b64decode(result.data["data"])
        return b""

    # -- Routing ---------------------------------------------------

    async def route(self, pattern: str, handler: Callable) -> None:
        """Intercept requests via Fetch.enable + Fetch.requestPaused events."""
        # Register handler for requestPaused events
        self._cdp.on_event("Fetch.requestPaused", handler)
        await self._cdp.send("Fetch.enable", {
            "patterns": [{"urlPattern": pattern}],
        })

    async def unroute_all(self) -> None:
        """Remove all route handlers via Fetch.disable."""
        await self._cdp.send("Fetch.disable", {})

    # -- Frames ----------------------------------------------------

    def frame_locator(self, selector: str) -> Any:
        """Frame targeting is complex via raw CDP — not supported."""
        raise NotImplementedError("frame_locator is not supported via raw CDP")

    # -- Downloads -------------------------------------------------

    async def expect_download(self) -> Any:
        """Download waiting is not supported via raw CDP."""
        raise NotImplementedError("expect_download is not supported via raw CDP")

    # -- Stealth bridge --------------------------------------------

    @property
    def stealth_bridge(self) -> Optional[CDPDirectStealthBridge]:
        """Access to low-level CDP for stealth features."""
        return self._stealth_bridge

    # -- Backward compatibility ------------------------------------

    @property
    def cdp(self) -> CDPBridge:
        """Direct access to CDPBridge."""
        return self._cdp


# =====================================================================
# CDPDirectEngine
# =====================================================================


class CDPDirectEngine:
    """Implements :class:`BrowserEngine` via a raw CDP websocket connection.

    On ``start()``, the engine:
    1. HTTP GET ``/json`` on the endpoint to discover page targets.
    2. Connects a websocket to the first page's ``webSocketDebuggerUrl``.
    3. Wraps the socket in :class:`WebSocketCDPSession`.
    4. Creates a :class:`CDPBridge` from that adapter.
    """

    def __init__(
        self,
        endpoint: str = "",
        config: Optional[SessionConfig] = None,
    ) -> None:
        self._endpoint = endpoint
        self._config = config or SessionConfig()
        self._ws_session: Optional[WebSocketCDPSession] = None
        self._cdp: Optional[CDPBridge] = None

    # -- BrowserEngine protocol ------------------------------------

    async def start(self, config: Any = None) -> None:
        """Connect to the CDP endpoint and set up the bridge."""

        endpoint = self._endpoint
        if config is not None:
            if isinstance(config, SessionConfig):
                endpoint = endpoint or config.endpoint
            elif hasattr(config, "endpoint"):
                endpoint = endpoint or getattr(config, "endpoint", "")

        if not endpoint:
            raise RuntimeError(
                "CDPDirectBackend requires an endpoint (e.g. http://localhost:9222). "
                "Set config.endpoint or pass endpoint= to the constructor."
            )

        # Discover targets via HTTP /json
        async with aiohttp.ClientSession() as http:
            async with http.get(f"{endpoint}/json") as resp:
                targets = await resp.json()

        if not targets:
            raise RuntimeError(f"No CDP targets found at {endpoint}/json")

        # Use the first page target
        page_target = None
        for t in targets:
            if t.get("type") == "page":
                page_target = t
                break
        if page_target is None:
            page_target = targets[0]

        ws_url = page_target["webSocketDebuggerUrl"]
        target_id = page_target.get("id", "")

        # Connect websocket
        ws_session = WebSocketCDPSession(ws_url)
        ws_session.target_id = target_id
        await ws_session.connect()

        # Create CDPBridge with adapter
        bridge_config = SessionConfig()
        cdp = CDPBridge(ws_session, bridge_config)

        self._ws_session = ws_session
        self._cdp = cdp

    async def stop(self) -> None:
        """Close websocket and release resources."""
        if self._ws_session is not None:
            await self._ws_session.close()
            self._ws_session = None
        self._cdp = None

    async def new_page(self) -> CDPDirectPage:
        """Create a new browser page via CDP."""
        if self._cdp is None or self._ws_session is None:
            raise RuntimeError("Engine not started. Call start() first.")

        # Create a new target via CDP
        result = await self._cdp.send("Target.createTarget", {"url": "about:blank"})
        if result.ok and result.data:
            # Attach to the new target via a new websocket session
            # For simplicity, reuse the existing session if browser-level
            page = CDPDirectPage(self._cdp, self._ws_session)
            page.url = "about:blank"
            return page

        # Fallback: return page using existing session
        return CDPDirectPage(self._cdp, self._ws_session)

    @property
    def capabilities(self) -> EngineCapabilities:
        """Report full CDP capabilities."""
        return EngineCapabilities(
            cdp=True,
            bidi=False,
            stealth_inject_before=True,
            stealth_inject_after=True,
            network_intercept=True,
            # P5: stored CDPDirectPage instances are aliases of the same
            # websocket/session, not independently addressable targets — do
            # not advertise multi-tab until a per-target session lands.
            multi_tab=False,
            screenshots=True,
            name="cdp",
        )

    @property
    def backend_name(self) -> str:
        """Return the backend identifier string."""
        return "cdp"

    # -- Context manager -------------------------------------------

    async def __aenter__(self) -> CDPDirectEngine:
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.stop()
