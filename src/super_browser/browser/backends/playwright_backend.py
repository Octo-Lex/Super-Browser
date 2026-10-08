"""PlaywrightBackend — browser automation via the standard Playwright library.

Supports Chromium (full CDP stealth), Firefox (BiDi future), and WebKit.
Playwright is the most popular browser automation library and provides the
natural second backend after Patchright.

Layer 1: PlaywrightEngine manages browser lifecycle
Layer 2: PlaywrightPage wraps a Playwright Page object
Layer 3: PlaywrightStealthBridge wraps CDP session (Chromium only)
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from super_browser.browser.cdp import CDPBridge, CDPResult
from super_browser.browser.config import SessionConfig
from super_browser.browser.engine import EngineCapabilities

logger = logging.getLogger(__name__)


# =====================================================================
# PlaywrightStealthBridge
# =====================================================================


class PlaywrightStealthBridge:
    """CDP-based stealth bridge for Chromium via Playwright.

    Created only for Chromium pages (which have CDP access).
    Firefox and WebKit pages return ``None`` for ``stealth_bridge``.
    """

    def __init__(self, cdp_session: Any) -> None:
        self._cdp = cdp_session

    # -- StealthBridge protocol ------------------------------------

    async def cdp_send(self, method: str, params: dict) -> CDPResult:
        """Send a raw CDP command."""
        try:
            result = await self._cdp.send(method, params)
            return CDPResult(ok=True, data=result)
        except Exception as e:
            return CDPResult(ok=False, error=str(e))

    async def inject_script_before_load(self, js: str) -> None:
        """Inject JS to run before page scripts execute.

        Stub — wired in BATCH-49 via inject_delivery.py.
        """
        logger.debug("inject_script_before_load called (%d chars) — stub", len(js))

    async def get_ax_tree(self) -> dict:
        """Get the full accessibility tree via CDP."""
        result = await self._cdp.send("Accessibility.getFullAXTree", {})
        return result if isinstance(result, dict) else {}

    async def get_all_cookies(self) -> list[dict]:
        """Get all browser cookies via CDP."""
        result = await self._cdp.send("Network.getAllCookies", {})
        if isinstance(result, dict):
            return result.get("cookies", [])
        return []

    async def set_cookies(self, cookies: list[dict]) -> None:
        """Set browser cookies via CDP."""
        await self._cdp.send("Network.setCookies", {"cookies": cookies})

    async def capture_screenshot_cdp(self, params: dict) -> dict:
        """Capture screenshot via CDP with custom parameters."""
        result = await self._cdp.send("Page.captureScreenshot", params)
        return result if isinstance(result, dict) else {}


# =====================================================================
# PlaywrightPage
# =====================================================================


class PlaywrightPage:
    """Wraps a Playwright ``Page`` to satisfy the :class:`EnginePage` protocol.

    For Chromium pages, ``stealth_bridge`` returns a
    :class:`PlaywrightStealthBridge`. For Firefox/WebKit, ``stealth_bridge``
    returns ``None``.
    """

    def __init__(
        self,
        page: Any,
        browser_type: str = "chromium",
        context: Any = None,
    ) -> None:
        self._page = page
        self._browser_type = browser_type
        self._context = context
        self._stealth_bridge: Optional[PlaywrightStealthBridge] = None
        self._cdp_session: Any = None
        self._cdp_bridge: Optional[CDPBridge] = None

    async def _ensure_cdp(self) -> None:
        """Lazily create CDP session for Chromium pages."""
        if (
            self._cdp_session is None
            and self._browser_type == "chromium"
            and self._context is not None
        ):
            try:
                self._cdp_session = await self._context.new_cdp_session(self._page)
                self._stealth_bridge = PlaywrightStealthBridge(self._cdp_session)
                # P4: the controller-facing .cdp is a real CDPBridge. The raw
                # Playwright CDP session stays with the stealth bridge, whose
                # cdp_send path consumes it directly.
                self._cdp_bridge = CDPBridge(self._cdp_session, SessionConfig())
            except Exception as exc:
                logger.warning("Failed to create CDP session: %s", exc)

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

    async def activate(self) -> None:
        """Bring this page to the foreground (EnginePage P5)."""
        await self._page.bring_to_front()

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
        If *target* is ``None``, scrolls the viewport via ``mouse.wheel``.
        """
        delta_map = {
            "down": (0, 100),
            "up": (0, -100),
            "right": (100, 0),
            "left": (-100, 0),
        }
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
    def stealth_bridge(self) -> Optional[PlaywrightStealthBridge]:
        """Access to low-level CDP for stealth features. ``None`` for Firefox/WebKit."""
        return self._stealth_bridge

    @property
    def cdp(self) -> Optional[CDPBridge]:
        """Controller-facing CDP bridge for Chromium; ``None`` for Firefox/WebKit.

        P4: this is a real :class:`CDPBridge` wrapping the raw Playwright CDP
        session (which the stealth bridge retains), so transport presence —
        not backend name — is the capability signal.
        """
        return self._cdp_bridge

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
    def engine_page(self) -> PlaywrightPage:
        """Self — PlaywrightPage IS the EnginePage wrapper."""
        return self


# =====================================================================
# PlaywrightEngine
# =====================================================================


class PlaywrightEngine:
    """Wraps Playwright to satisfy the :class:`BrowserEngine` protocol.

    Supports Chromium (full CDP), Firefox (BiDi future), and WebKit (no CDP).
    """

    def __init__(self, config: Any = None, *, browser_type: str = "chromium") -> None:
        self._config = config
        self._browser_type = browser_type
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None

    # -- BrowserEngine protocol ------------------------------------

    async def start(self, config: Any = None) -> None:
        """Launch or connect to the browser.

        P2-review fix: consumes the stored (normalized) SessionConfig —
        ``browser_type`` and ``headless`` — instead of constructor defaults
        and a hardcoded headless flag. Unsupported browser types fail
        explicitly rather than silently launching Chromium.
        """
        from playwright.async_api import async_playwright

        effective_cfg = config or self._config
        browser_type = (
            getattr(effective_cfg, "browser_type", None) or self._browser_type
        )
        headless = bool(getattr(effective_cfg, "headless", True))

        self._playwright = await async_playwright().start()

        launch_method = {
            "chromium": self._playwright.chromium.launch,
            "firefox": self._playwright.firefox.launch,
            "webkit": self._playwright.webkit.launch,
        }
        if browser_type not in launch_method:
            raise ValueError(
                f"Unsupported browser type for the playwright backend: "
                f"{browser_type!r}. Choose from: chromium, firefox, webkit."
            )
        launcher = launch_method[browser_type]
        self._browser_type = browser_type
        self._browser = await launcher(headless=headless)
        self._context = await self._browser.new_context()

    async def stop(self) -> None:
        """Close browser and release resources."""
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        self._browser = None
        self._context = None
        self._playwright = None

    async def new_page(self) -> PlaywrightPage:
        """Create a new browser page/tab, wrapped as PlaywrightPage."""
        if self._context is None:
            raise RuntimeError("Engine not started. Call start() first.")
        page = await self._context.new_page()
        pw_page = PlaywrightPage(page, self._browser_type, self._context)
        if self._browser_type == "chromium":
            await pw_page._ensure_cdp()
        return pw_page

    @property
    def capabilities(self) -> EngineCapabilities:
        """Report what this engine supports — varies by browser type."""
        if self._browser_type == "chromium":
            return EngineCapabilities(
                cdp=True,
                bidi=False,
                stealth_inject_before=True,
                network_intercept=True,
                multi_tab=True,
                screenshots=True,
                name="playwright-chromium",
            )
        if self._browser_type == "firefox":
            return EngineCapabilities(
                cdp=False,
                bidi=True,
                stealth_inject_after=True,
                network_intercept=False,
                multi_tab=True,
                screenshots=True,
                name="playwright-firefox",
            )
        # webkit
        return EngineCapabilities(
            cdp=False,
            bidi=False,
            stealth_inject_after=True,
            network_intercept=False,
            multi_tab=True,
            screenshots=True,
            name="playwright-webkit",
        )

    @property
    def backend_name(self) -> str:
        """Return the backend identifier string."""
        return "playwright"

    @property
    def context(self) -> Any:
        """BrowserContext (for TabManager backward compat)."""
        return self._context

    async def __aenter__(self) -> PlaywrightEngine:
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.stop()
