"""PatchrightBackend — wraps existing Patchright code behind engine protocols.

This is a thin delegation layer. All behaviour comes from BrowserSession,
PageHandle, and CDPBridge — this module just adapts them to the BrowserEngine
/ EnginePage / StealthBridge protocols defined in ``engine.py``.

Layer 1: PatchrightEngine wraps BrowserSession
Layer 2: PatchrightPage wraps a Playwright Page object
Layer 3: PatchrightStealthBridge wraps CDPBridge
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from super_browser.browser.cdp import CDPBridge, CDPResult
from super_browser.browser.config import SessionConfig
from super_browser.browser.engine import EngineCapabilities

logger = logging.getLogger(__name__)


# =====================================================================
# PatchrightStealthBridge
# =====================================================================


class PatchrightStealthBridge:
    """Wraps :class:`CDPBridge` to satisfy the :class:`StealthBridge` protocol.

    Every method delegates directly to the underlying CDPBridge — zero
    behavioural change.  The ``inject_script_before_load`` method is a stub
    that will be wired to the inject-delivery pipeline in BATCH-49.
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
        # TODO(BATCH-49): Wire to inject_delivery.py CDP body-splice.
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
        """Direct access to the underlying CDPBridge (backward compat)."""
        return self._cdp


# =====================================================================
# PatchrightPage
# =====================================================================


class PatchrightPage:
    """Wraps a Playwright ``Page`` object to satisfy the :class:`EnginePage` protocol.

    Every method delegates to the underlying page — zero behavioural change.
    The only real logic is in :meth:`scroll`, which handles both
    locator-based element scroll and ``mouse.wheel`` viewport scroll.
    """

    def __init__(
        self,
        page: Any,
        cdp: CDPBridge,
    ) -> None:
        self._page = page
        self._cdp = cdp
        self._stealth_bridge = PatchrightStealthBridge(cdp)

    # -- Navigation ------------------------------------------------

    async def goto(self, url: str, *, wait_until: str = "load", **kwargs: Any) -> None:
        """Navigate to URL."""
        await self._page.goto(url, wait_until=wait_until, **kwargs)

    async def title(self) -> str:
        """Get page title."""
        return await self._page.title()

    @property
    def url(self) -> str:
        """Get current page URL."""
        return self._page.url

    async def close(self) -> None:
        """Close this page."""
        await self._page.close()

    async def content(self) -> str:
        """Get page HTML content."""
        return await self._page.content()

    # -- Interaction -----------------------------------------------

    async def click(self, selector: str, **kwargs: Any) -> None:
        """Click an element by selector."""
        await self._page.click(selector, **kwargs)

    async def fill(self, selector: str, value: str, **kwargs: Any) -> None:
        """Fill an input element with a value."""
        await self._page.fill(selector, value, **kwargs)

    async def select_option(self, selector: str, value: Any) -> None:
        """Select an option in a dropdown."""
        await self._page.select_option(selector, value)

    async def hover(self, selector: str) -> None:
        """Hover over an element."""
        await self._page.hover(selector)

    async def drag_and_drop(self, source: str, target: str) -> None:
        """Drag from source to target."""
        await self._page.drag_and_drop(source, target)

    async def scroll(
        self,
        direction: str,
        amount: int,
        target: Optional[str] = None,
    ) -> None:
        """Scroll the page or a specific element.

        If *target* is provided, scrolls within that element using
        ``locator(target).scroll(direction, amount)``.
        If *target* is None, scrolls the viewport via ``mouse.wheel``.
        """
        delta_map = {"down": (0, 100), "up": (0, -100), "right": (100, 0), "left": (-100, 0)}
        dx, dy = delta_map.get(direction, (0, 100))

        if target:
            await self._page.locator(target).scroll(direction, amount)
        else:
            await self._page.mouse.wheel(dx * amount, dy * amount)

    async def type_text(self, text: str) -> None:
        """Type text into the currently focused element."""
        await self._page.keyboard.type(text)

    async def press_key(self, key: str) -> None:
        """Press a keyboard key."""
        await self._page.keyboard.press(key)

    async def set_input_files(self, selector: str, path: str) -> None:
        """Set files on a file input element."""
        await self._page.set_input_files(selector, path)

    # -- Evaluation ------------------------------------------------

    async def evaluate(self, expression: str, *args: Any, **kwargs: Any) -> Any:
        """Evaluate JavaScript expression and return result."""
        return await self._page.evaluate(expression, *args, **kwargs)

    async def screenshot(self, **kwargs: Any) -> bytes:
        """Capture a screenshot.

        ``format="png" | "jpeg"`` is the canonical vocabulary (PR 1 P3);
        translated to the Playwright ``type=`` spelling here so callers never
        need to know the backend. A raw ``type=`` passes through untouched.
        """
        if "format" in kwargs:
            kwargs["type"] = kwargs.pop("format")
        return await self._page.screenshot(**kwargs)

    # -- Routing ---------------------------------------------------

    async def route(self, pattern: str, handler: Callable) -> None:
        """Intercept requests matching pattern."""
        await self._page.route(pattern, handler)

    async def unroute_all(self) -> None:
        """Remove all route handlers."""
        await self._page.unroute_all()

    # -- Frames ----------------------------------------------------

    def frame_locator(self, selector: str) -> Any:
        """Get a frame locator for the given selector."""
        return self._page.frame_locator(selector)

    # -- Downloads -------------------------------------------------

    async def expect_download(self) -> Any:
        """Context manager that waits for a download."""
        return self._page.expect_download()

    # -- Stealth bridge --------------------------------------------

    @property
    def stealth_bridge(self) -> Optional[PatchrightStealthBridge]:
        """Access to low-level CDP for stealth features."""
        return self._stealth_bridge

    # -- Backward compatibility ------------------------------------

    @property
    def cdp(self) -> CDPBridge:
        """Direct access to CDPBridge (for controller backward compat).

        Not in the EnginePage protocol — backend-specific.
        """
        return self._cdp

    @property
    def backend_page(self) -> Any:
        """Underlying Playwright Page. For advanced use."""
        return self._page

    @property
    def raw_page(self) -> Any:
        """Deprecated: use :attr:`backend_page`."""
        import warnings
        warnings.warn(
            "raw_page is deprecated, use backend_page instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.backend_page

    @property
    def engine_page(self) -> PatchrightPage:
        """Self — PatchrightPage IS the EnginePage wrapper."""
        return self


# =====================================================================
# PatchrightEngine
# =====================================================================


class PatchrightEngine:
    """Wraps :class:`BrowserSession` to satisfy the :class:`BrowserEngine` protocol.

    Internally delegates all lifecycle and page creation to BrowserSession.
    The ``context`` and ``cloak_config`` properties are exposed for backward
    compatibility with :class:`TabManager` and stealth configuration.
    """

    def __init__(self, config: Optional[SessionConfig] = None, *, cloak_config: Optional[Any] = None) -> None:
        self._config = config or SessionConfig()
        self._cloak_config = cloak_config
        self._session: Any = None  # BrowserSession created in start()

    # -- BrowserEngine protocol ------------------------------------

    async def start(self, config: Any = None) -> None:
        """Launch or connect to the browser via BrowserSession."""
        from super_browser.browser.session import BrowserSession

        effective_config = config or self._config
        self._session = BrowserSession(
            effective_config if isinstance(effective_config, SessionConfig) else SessionConfig(),
            cloak_config=self._cloak_config,
        )
        await self._session.start()

    async def stop(self) -> None:
        """Close browser and release resources."""
        if self._session is not None:
            await self._session.stop()
            self._session = None

    async def new_page(self) -> PatchrightPage:
        """Create a new browser page/tab, wrapped as PatchrightPage."""
        if self._session is None:
            raise RuntimeError("Engine not started. Call start() first.")
        handle = await self._session.new_page()
        return PatchrightPage(handle.backend_page, handle.cdp)

    @property
    def capabilities(self) -> EngineCapabilities:
        """Report what this engine supports."""
        return EngineCapabilities(
            cdp=True,
            bidi=False,
            stealth_inject_before=True,
            stealth_inject_after=True,
            network_intercept=True,
            multi_tab=True,
            screenshots=True,
            name="patchright",
        )

    @property
    def backend_name(self) -> str:
        """Return the backend identifier string."""
        return "patchright"

    # -- Backward compatibility ------------------------------------

    @property
    def context(self) -> Any:
        """BrowserContext from BrowserSession (for TabManager backward compat)."""
        if self._session is not None:
            return self._session._context
        return None

    @property
    def cloak_config(self) -> Any:
        """CloakConfig for stealth backend detection."""
        return self._cloak_config

    @property
    def stealth_backend(self) -> str:
        """Active stealth backend name."""
        if self._session is not None:
            return self._session.stealth_backend
        return "patchright"

    @property
    def session(self) -> Any:
        """Direct access to the BrowserSession (for backward compat)."""
        return self._session

    async def __aenter__(self) -> PatchrightEngine:
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.stop()
