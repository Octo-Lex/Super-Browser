"""Browser platform abstraction — protocol definitions.

Defines the contract that any browser automation engine must satisfy.
Backend implementations (PatchrightBackend, PlaywrightBackend, etc.)
implement these protocols. Higher layers (facade, controller) depend
only on these protocols, never on specific backends.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Callable, Optional, Protocol, runtime_checkable

# Import CDPResult for StealthBridge return type compatibility
from super_browser.browser.cdp import CDPResult


class BackendType(StrEnum):
    """Available browser backend types."""

    AUTO = "auto"
    PATCHRIGHT = "patchright"
    PLAYWRIGHT = "playwright"
    SELENIUM = "selenium"
    CDP = "cdp"


class InjectionTiming(StrEnum):
    """When stealth scripts are injected relative to page JS."""

    BEFORE = "before"  # Before page JS runs (CDP body-splice, BiDi preload)
    AFTER = "after"  # After page JS runs (addScriptTag fallback)
    BOTH = "both"  # Both before and after


@dataclass
class EngineCapabilities:
    """What this browser engine supports — used for graceful degradation.

    Backends set these based on the underlying browser's capabilities.
    Higher layers check these to decide which features to enable.
    """

    cdp: bool = False  # Chrome DevTools Protocol available
    bidi: bool = False  # WebDriver BiDi available
    stealth_inject_before: bool = False  # Can inject JS before page scripts run
    stealth_inject_after: bool = True  # Can inject JS after page scripts run
    network_intercept: bool = False  # Can intercept/modify network requests
    multi_tab: bool = False  # Supports multiple tabs
    screenshots: bool = True  # Can capture screenshots
    name: str = "unknown"  # Human-readable backend name


@runtime_checkable
class BrowserEngine(Protocol):
    """What any browser automation engine must provide.

    This is the top-level protocol. Engines manage browser lifecycle
    and create page objects. The facade calls these methods to start,
    stop, and create pages.
    """

    async def start(self, config: Any) -> None:
        """Launch or connect to the browser."""
        ...

    async def stop(self) -> None:
        """Close browser and release resources."""
        ...

    async def new_page(self) -> EnginePage:
        """Create a new browser page/tab."""
        ...

    @property
    def capabilities(self) -> EngineCapabilities:
        """Report what this engine supports."""
        ...

    @property
    def backend_name(self) -> str:
        """Return the backend identifier string."""
        ...


@runtime_checkable
class EnginePage(Protocol):
    """What a page object must provide.

    This is the core interaction surface. Every method corresponds
    to a real browser action. Implementations delegate to the
    underlying browser API (Playwright, Selenium, etc.).
    """

    # -- Navigation --

    async def goto(self, url: str, *, wait_until: str = "load") -> None:
        """Navigate to URL."""
        ...

    async def title(self) -> str:
        """Get page title."""
        ...

    @property
    def url(self) -> str:
        """Get current page URL."""
        ...

    async def close(self) -> None:
        """Close this page."""
        ...

    async def activate(self) -> None:
        """Bring this page to the foreground (tab activation; PR 1 P5)."""
        ...

    async def content(self) -> str:
        """Get page HTML content."""
        ...

    # -- Interaction --

    async def click(self, selector: str, **kwargs: Any) -> None:
        """Click an element by selector."""
        ...

    async def fill(self, selector: str, value: str, **kwargs: Any) -> None:
        """Fill an input element with a value."""
        ...

    async def select_option(self, selector: str, value: Any) -> None:
        """Select an option in a dropdown."""
        ...

    async def hover(self, selector: str) -> None:
        """Hover over an element."""
        ...

    async def drag_and_drop(self, source: str, target: str) -> None:
        """Drag from source to target."""
        ...

    async def scroll(
        self,
        direction: str,
        amount: int,
        target: Optional[str] = None,
    ) -> None:
        """Scroll the page or a specific element.

        If *target* is provided, scrolls within that element.
        If *target* is None, scrolls the viewport via mouse wheel.
        *direction*: ``"up"`` or ``"down"``.
        *amount*: number of pixels/units to scroll.
        """
        ...

    async def type_text(self, text: str) -> None:
        """Type text into the currently focused element."""
        ...

    async def press_key(self, key: str) -> None:
        """Press a keyboard key."""
        ...

    async def set_input_files(self, selector: str, path: str) -> None:
        """Set files on a file input element."""
        ...

    # -- Evaluation --

    async def evaluate(self, expression: str) -> Any:
        """Evaluate JavaScript expression and return result."""
        ...

    async def screenshot(self) -> bytes:
        """Capture screenshot as PNG bytes."""
        ...

    # -- Routing --

    async def route(self, pattern: str, handler: Callable) -> None:
        """Intercept requests matching pattern."""
        ...

    async def unroute_all(self) -> None:
        """Remove all route handlers."""
        ...

    # -- Frames --

    def frame_locator(self, selector: str) -> Any:
        """Get a frame locator for the given selector."""
        ...

    # -- Downloads --

    async def expect_download(self) -> Any:
        """Context manager that waits for a download."""
        ...

    # -- Stealth bridge (optional) --

    @property
    def stealth_bridge(self) -> Optional[StealthBridge]:
        """Access to low-level CDP/BiDi for stealth features. None if unavailable."""
        ...


@runtime_checkable
class StealthBridge(Protocol):
    """Optional low-level protocol for stealth features.

    Engines that support CDP or BiDi implement this to give the
    stealth subsystem access to browser internals (accessibility tree,
    cookies, script injection, screenshot capture).

    Engines without CDP/BiDi return None for stealth_bridge.
    """

    async def cdp_send(self, method: str, params: dict) -> CDPResult:
        """Send a raw CDP command and return the result."""
        ...

    async def inject_script_before_load(self, js: str) -> None:
        """Inject JS to run before page scripts execute."""
        ...

    async def get_ax_tree(self) -> dict:
        """Get the full accessibility tree."""
        ...

    async def get_all_cookies(self) -> list[dict]:
        """Get all browser cookies."""
        ...

    async def set_cookies(self, cookies: list[dict]) -> None:
        """Set browser cookies."""
        ...

    async def capture_screenshot_cdp(self, params: dict) -> dict:
        """Capture screenshot via CDP with custom parameters."""
        ...


@runtime_checkable
class StealthInjector(Protocol):
    """How stealth JS payloads get delivered to the browser.

    Three implementations:
    - CDPInjector: Fetch.fulfillRequest body-splice (before page JS)
    - BiDiInjector: script.addPreloadScript (before page JS)
    - PageScriptInjector: page.addScriptTag (after page JS)
    """

    async def inject_before_load(self, js: str) -> None:
        """Inject JS before page scripts run."""
        ...

    async def inject_after_load(self, js: str) -> None:
        """Inject JS after page scripts run."""
        ...

    @property
    def injection_timing(self) -> InjectionTiming:
        """Report when this injector delivers scripts."""
        ...


def _detect_backend(config: Any = None) -> str:
    """Detect the best available browser backend.

    Precedence rules:
    1. If config.backend is set and not "auto", use that.
    2. If config has a mode field matching PATCHRIGHT_LAUNCH/ATTACH, use "patchright".
    3. If config has a mode field matching CLOAK_LAUNCH, use "cloak".
    4. Auto-detect by probing imports: patchright → playwright → selenium.
    5. If nothing found, raise RuntimeError with install instructions.
    """
    # 1. Explicit override — check Config.browser.backend first
    if config is not None:
        browser_cfg = getattr(config, "browser", None)
        if browser_cfg is not None and hasattr(type(browser_cfg), "backend"):
            backend = browser_cfg.backend
            mode = getattr(browser_cfg, "mode", None)
        else:
            backend = getattr(config, "backend", "auto")
            mode = getattr(config, "mode", None)

        if backend and backend != "auto":
            return backend

        # 2-3. Mode-based detection. SessionMode values are lowercase
        # ("patchright_launch", "cloak_launch"); normalize before matching so
        # the check fires for enum members and plain strings alike.
        if mode is not None:
            mode_str = str(getattr(mode, "value", mode)).lower()
            if "patchright" in mode_str:
                return "patchright"
            if "cloak" in mode_str:
                return "cloak"

    # 4. Auto-detect via import probing
    try:
        import patchright  # noqa: F401

        return "patchright"
    except ImportError:
        pass

    try:
        import playwright  # noqa: F401

        return "playwright"
    except ImportError:
        pass

    try:
        import selenium  # noqa: F401

        return "selenium"
    except ImportError:
        pass

    # 5. Nothing found
    raise RuntimeError(
        "No browser backend found. Install one of:\n"
        "  pip install superbrowser-sdk[patchright]\n"
        "  pip install superbrowser-sdk[playwright]\n"
        "  pip install superbrowser-sdk[selenium]"
    )
