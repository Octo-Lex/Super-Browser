"""SeleniumBackend — browser automation via Selenium WebDriver.

Wraps Selenium WebDriver behind the BrowserEngine / EnginePage / StealthBridge
protocols defined in ``engine.py``.  All WebDriver calls are dispatched via
``asyncio.to_thread()`` so that the synchronous WebDriver never blocks the
event loop.

Layer 1: SeleniumEngine manages WebDriver lifecycle
Layer 2: SeleniumPage wraps a WebDriver instance
Layer 3: SeleniumStealthBridge wraps driver.execute_cdp_cmd (Chrome only)

Capabilities per browser:
  - Chrome:  CDP, inject_before, inject_after
  - Firefox: BiDi, inject_after
  - Safari:  inject_after only

Limitations (NotImplementedError):
  - route / unroute_all  — Selenium cannot intercept requests
  - expect_download      — Selenium has no download-await primitive
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Optional

from super_browser.browser.cdp import CDPResult
from super_browser.browser.engine import EngineCapabilities

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional Selenium import — graceful degradation when not installed
# ---------------------------------------------------------------------------
try:
    from selenium import webdriver  # noqa: F401
    from selenium.webdriver.common.action_chains import ActionChains
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.support.ui import Select

    _SELENIUM_AVAILABLE = True
except ImportError:  # pragma: no cover
    _SELENIUM_AVAILABLE = False


# =====================================================================
# SeleniumStealthBridge
# =====================================================================


class SeleniumStealthBridge:
    """CDP-based stealth bridge for Chrome via Selenium.

    Uses ``driver.execute_cdp_cmd()`` which is only available on
    Chrome / Chromium WebDriver instances.  Instances for Firefox or
    Safari return ``None`` for ``stealth_bridge``.
    """

    def __init__(self, driver: Any) -> None:
        self._driver = driver

    # -- StealthBridge protocol ------------------------------------

    async def cdp_send(self, method: str, params: dict) -> CDPResult:
        """Send a raw CDP command via ``driver.execute_cdp_cmd``."""
        try:
            result = await asyncio.to_thread(
                self._driver.execute_cdp_cmd, method, params,
            )
            return CDPResult(ok=True, data=result, method=method)
        except Exception as exc:
            return CDPResult(ok=False, error=str(exc), method=method)

    async def inject_script_before_load(self, js: str) -> None:
        """Inject JS to run before page scripts execute.

        Uses ``Page.addScriptToEvaluateOnNewDocument`` via CDP.
        """
        try:
            await asyncio.to_thread(
                self._driver.execute_cdp_cmd,
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": js},
            )
        except Exception as exc:
            logger.warning("inject_script_before_load failed: %s", exc)

    async def get_ax_tree(self) -> dict:
        """Get the full accessibility tree via CDP."""
        try:
            result = await asyncio.to_thread(
                self._driver.execute_cdp_cmd,
                "Accessibility.getFullAXTree",
                {},
            )
            return result if isinstance(result, dict) else {}
        except Exception:
            return {}

    async def get_all_cookies(self) -> list[dict]:
        """Get all browser cookies via CDP."""
        try:
            result = await asyncio.to_thread(
                self._driver.execute_cdp_cmd,
                "Network.getAllCookies",
                {},
            )
            if isinstance(result, dict):
                return result.get("cookies", [])
            return []
        except Exception:
            return []

    async def set_cookies(self, cookies: list[dict]) -> None:
        """Set browser cookies via CDP."""
        await asyncio.to_thread(
            self._driver.execute_cdp_cmd,
            "Network.setCookies",
            {"cookies": cookies},
        )

    async def capture_screenshot_cdp(self, params: dict) -> dict:
        """Capture screenshot via CDP with custom parameters."""
        try:
            result = await asyncio.to_thread(
                self._driver.execute_cdp_cmd,
                "Page.captureScreenshot",
                params,
            )
            return result if isinstance(result, dict) else {}
        except Exception:
            return {}


# =====================================================================
# SeleniumPage
# =====================================================================


class SeleniumPage:
    """Wraps a Selenium ``WebDriver`` instance to satisfy EnginePage.

    Every interaction method wraps the synchronous WebDriver call in
    ``asyncio.to_thread()`` so the event loop is never blocked.
    """

    def __init__(
        self,
        driver: Any,
        browser_type: str = "chrome",
    ) -> None:
        self._driver = driver
        self._browser_type = browser_type
        self._stealth_bridge: Optional[SeleniumStealthBridge] = None

        # Chrome gets a stealth bridge (CDP via execute_cdp_cmd)
        if browser_type == "chrome":
            self._stealth_bridge = SeleniumStealthBridge(driver)

        # P5: capture THIS page's window handle at construction. SeleniumPage
        # methods route through the driver's *current* window, so without the
        # captured handle every page would be an alias of whatever window is
        # current. Construction happens right after the engine switches to the
        # new window, so the capture lands on the right one.
        self._window_handle: Any = None
        try:
            self._window_handle = driver.current_window_handle
        except Exception:
            self._window_handle = None

    async def activate(self) -> None:
        """Make this page's window the driver's current window (EnginePage P5)."""

        def _sync() -> None:
            if self._window_handle is not None:
                self._driver.switch_to.window(self._window_handle)

        await asyncio.to_thread(_sync)

    # -- Navigation ------------------------------------------------

    async def goto(self, url: str, *, wait_until: str = "load", **kwargs: Any) -> None:
        """Navigate to URL."""

        def _sync() -> None:
            self._driver.get(url)

        await asyncio.to_thread(_sync)

    def _read_on_own_window(self, read: Any) -> Any:
        """Read driver state from THIS page's window, restoring selection.

        Selenium exposes ``title``/``current_url`` for the driver's *current*
        window only. Without the handle switch, every SeleniumPage would
        report whatever window happens to be selected (P2-review fix).
        """
        driver = self._driver
        previous = getattr(driver, "current_window_handle", None)
        switched = False
        if (
            self._window_handle is not None
            and previous is not None
            and previous != self._window_handle
        ):
            driver.switch_to.window(self._window_handle)
            switched = True
        try:
            return read()
        finally:
            if switched:
                try:
                    driver.switch_to.window(previous)
                except Exception:
                    pass  # previous window vanished mid-read — keep new state

    async def title(self) -> str:
        """Get THIS page's window title (handle-aware)."""
        return await asyncio.to_thread(
            lambda: self._read_on_own_window(lambda: self._driver.title)
        )

    @property
    def url(self) -> str:
        """Get THIS page's window URL (handle-aware)."""
        return self._read_on_own_window(lambda: self._driver.current_url)

    async def close(self) -> None:
        """Close THIS page's window.

        P5: Selenium's ``driver.close()`` closes whatever window is current,
        so the page activates its own handle first. P2-review fix: the
        previously selected window is restored afterwards (when it still
        exists), so closing a background tab leaves the driver on the
        logical active window instead of a webdriver-chosen one.
        """

        def _sync() -> None:
            previous = self._driver.current_window_handle
            switching = (
                self._window_handle is not None
                and previous != self._window_handle
            )
            if switching:
                self._driver.switch_to.window(self._window_handle)
            self._driver.close()
            if switching:
                try:
                    if previous in set(self._driver.window_handles):
                        self._driver.switch_to.window(previous)
                except Exception:
                    pass  # no windows remain — nothing to restore

        await asyncio.to_thread(_sync)

    async def content(self) -> str:
        """Get page HTML content."""
        return await asyncio.to_thread(lambda: self._driver.page_source)

    # -- Interaction -----------------------------------------------

    async def click(self, selector: str, **kwargs: Any) -> None:
        """Click an element by CSS selector."""

        def _sync() -> None:
            element = self._driver.find_element(By.CSS_SELECTOR, selector)
            element.click()

        await asyncio.to_thread(_sync)

    async def fill(self, selector: str, value: str, **kwargs: Any) -> None:
        """Clear and fill an input element."""

        def _sync() -> None:
            element = self._driver.find_element(By.CSS_SELECTOR, selector)
            element.clear()
            element.send_keys(value)

        await asyncio.to_thread(_sync)

    async def select_option(self, selector: str, value: Any) -> None:
        """Select an option in a dropdown by value."""

        def _sync() -> None:
            element = self._driver.find_element(By.CSS_SELECTOR, selector)
            select = Select(element)
            select.select_by_value(str(value))

        await asyncio.to_thread(_sync)

    async def hover(self, selector: str) -> None:
        """Hover over an element."""

        def _sync() -> None:
            element = self._driver.find_element(By.CSS_SELECTOR, selector)
            ActionChains(self._driver).move_to_element(element).perform()

        await asyncio.to_thread(_sync)

    async def drag_and_drop(self, source: str, target: str) -> None:
        """Drag from source element to target element."""

        def _sync() -> None:
            src = self._driver.find_element(By.CSS_SELECTOR, source)
            tgt = self._driver.find_element(By.CSS_SELECTOR, target)
            ActionChains(self._driver).drag_and_drop(src, tgt).perform()

        await asyncio.to_thread(_sync)

    async def scroll(
        self,
        direction: str,
        amount: int,
        target: Optional[str] = None,
    ) -> None:
        """Scroll the page or a specific element.

        Uses ``window.scrollBy()`` for viewport scroll or
        ``element.scrollIntoView()`` for element scroll via JS.
        """
        delta_map = {
            "down": (0, amount),
            "up": (0, -amount),
            "right": (amount, 0),
            "left": (-amount, 0),
        }
        dx, dy = delta_map.get(direction, (0, amount))

        if target:

            def _sync_element() -> None:
                element = self._driver.find_element(By.CSS_SELECTOR, target)
                self._driver.execute_script("arguments[0].scrollIntoView();", element)

            await asyncio.to_thread(_sync_element)
        else:

            def _sync_viewport() -> None:
                self._driver.execute_script(f"window.scrollBy({dx}, {dy});")

            await asyncio.to_thread(_sync_viewport)

    async def type_text(self, text: str) -> None:
        """Type text into the currently focused element."""

        def _sync() -> None:
            ActionChains(self._driver).send_keys(text).perform()

        await asyncio.to_thread(_sync)

    async def press_key(self, key: str) -> None:
        """Press a keyboard key by name."""

        def _sync() -> None:
            selenium_key = getattr(Keys, key.upper(), key)
            ActionChains(self._driver).send_keys(selenium_key).perform()

        await asyncio.to_thread(_sync)

    async def set_input_files(self, selector: str, path: str) -> None:
        """Set files on a file input element."""

        def _sync() -> None:
            element = self._driver.find_element(By.CSS_SELECTOR, selector)
            element.send_keys(path)

        await asyncio.to_thread(_sync)

    # -- Evaluation ------------------------------------------------

    async def evaluate(self, expression: str, *args: Any, **kwargs: Any) -> Any:
        """Evaluate JavaScript expression and return result."""

        def _sync() -> Any:
            return self._driver.execute_script("return " + expression)

        return await asyncio.to_thread(_sync)

    async def screenshot(self, **kwargs: Any) -> bytes:
        """Capture screenshot as PNG bytes.

        Selenium only supports PNG output. When a jpeg is requested, the raw
        PNG is returned and the caller (page layer) handles re-encoding if
        Pillow is available; otherwise the PNG is returned as-is.
        """

        def _sync() -> bytes:
            return self._driver.get_screenshot_as_png()

        return await asyncio.to_thread(_sync)

    # -- Routing ---------------------------------------------------

    async def route(self, pattern: str, handler: Callable) -> None:
        """Selenium cannot intercept network requests."""
        raise NotImplementedError(
            "Selenium cannot intercept network requests. "
            "Use PatchrightBackend or CDPDirectBackend for route interception."
        )

    async def unroute_all(self) -> None:
        """No-op — Selenium does not support request routing."""
        # No routes to remove since route() raises NotImplementedError.

    # -- Frames ----------------------------------------------------

    def frame_locator(self, selector: str) -> Any:
        """Switch to an iframe by CSS selector.

        Returns the WebDriver instance switched into the frame context.
        """

        def _sync() -> Any:
            element = self._driver.find_element(By.CSS_SELECTOR, selector)
            self._driver.switch_to.frame(element)

        # Must run synchronously because the caller expects the switch
        # to have happened when the method returns.
        import threading

        thread = threading.Thread(target=_sync)
        thread.start()
        thread.join()
        return self._driver

    # -- Downloads -------------------------------------------------

    async def expect_download(self) -> Any:
        """Selenium has no download-await primitive."""
        raise NotImplementedError(
            "Selenium cannot await downloads. "
            "Use PatchrightBackend or PlaywrightBackend for download handling."
        )

    # -- Stealth bridge --------------------------------------------

    @property
    def stealth_bridge(self) -> Optional[SeleniumStealthBridge]:
        """CDP stealth bridge for Chrome, ``None`` for Firefox/Safari."""
        return self._stealth_bridge

    @property
    def backend_page(self) -> Any:
        """Underlying WebDriver instance. For advanced use."""
        return self._driver

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
    def engine_page(self) -> SeleniumPage:
        """Self — SeleniumPage IS the EnginePage wrapper."""
        return self


# =====================================================================
# SeleniumEngine
# =====================================================================


class SeleniumEngine:
    """Wraps Selenium WebDriver to satisfy the BrowserEngine protocol.

    Supports Chrome (CDP), Firefox (BiDi), and Safari.  Browser type is
    set via the ``browser_type`` constructor parameter.
    """

    def __init__(
        self,
        config: Any = None,
        *,
        browser_type: str = "chrome",
    ) -> None:
        self._config = config
        self._browser_type = browser_type
        self._driver: Any = None

    # -- BrowserEngine protocol ------------------------------------

    async def start(self, config: Any = None) -> None:
        """Launch the browser via Selenium WebDriver."""
        if not _SELENIUM_AVAILABLE:
            raise ImportError(
                "selenium is not installed. Install it with: "
                "pip install selenium"
            )

        effective_cfg = config or self._config
        effective_type = (
            getattr(effective_cfg, "browser_type", None) or self._browser_type
        )
        # P2-review fix: headless is honored from the stored (normalized)
        # config instead of being ignored.
        headless = bool(getattr(effective_cfg, "headless", False))

        driver: Any = None

        if effective_type == "chrome":
            try:
                from selenium.webdriver.chrome.options import Options
                from selenium.webdriver.chrome.service import Service

                options = Options()
                options.add_argument("--no-sandbox")
                options.add_argument("--disable-dev-shm-usage")
                if headless:
                    options.add_argument("--headless=new")

                # Try ChromeDriverManager first, fall back to default
                try:
                    from webdriver_manager.chrome import ChromeDriverManager

                    service = Service(
                        ChromeDriverManager().install(),
                    )
                    driver = webdriver.Chrome(
                        service=service, options=options,
                    )
                except ImportError:
                    driver = webdriver.Chrome(options=options)
            except Exception as exc:
                raise RuntimeError(
                    f"Failed to start Chrome WebDriver: {exc}"
                ) from exc

        elif effective_type == "firefox":
            try:
                from selenium.webdriver.firefox.options import Options  # type: ignore[assignment]
                from selenium.webdriver.firefox.service import Service  # type: ignore[assignment]

                options = Options()
                if headless:
                    # Firefox headless spelling (single dash).
                    options.add_argument("-headless")
                driver = webdriver.Firefox(options=options)  # type: ignore[arg-type]
            except Exception as exc:
                raise RuntimeError(
                    f"Failed to start Firefox WebDriver: {exc}"
                ) from exc

        elif effective_type == "safari":
            if headless:
                # Fail explicitly — Safari WebDriver has no headless mode.
                raise ValueError(
                    "Safari does not support headless mode; "
                    "set browser.headless=False or choose another browser_type."
                )
            try:
                driver = webdriver.Safari()
            except Exception as exc:
                raise RuntimeError(
                    f"Failed to start Safari WebDriver: {exc}"
                ) from exc

        else:
            raise ValueError(
                f"Unsupported browser type: {effective_type!r}. "
                "Choose from: chrome, firefox, safari."
            )

        self._driver = driver

    async def stop(self) -> None:
        """Close browser and release resources."""
        if self._driver is not None:
            await asyncio.to_thread(self._driver.quit)
            self._driver = None

    async def new_page(self) -> SeleniumPage:
        """Create a new SeleniumPage wrapping the active WebDriver.

        Selenium reuses the same WebDriver instance per browser —
        ``new_page()`` opens a new window/tab via JS and returns
        a SeleniumPage wrapping it.
        """
        if self._driver is None:
            raise RuntimeError("Engine not started. Call start() first.")

        # Open a new window/tab via JS
        await asyncio.to_thread(
            self._driver.execute_script, "window.open('', '_blank');",
        )
        # Switch to the new window
        await asyncio.to_thread(self._switch_to_last_window)

        return SeleniumPage(self._driver, self._browser_type)

    def _switch_to_last_window(self) -> None:
        """Switch to the most recently opened window."""
        handles = self._driver.window_handles
        if handles:
            self._driver.switch_to.window(handles[-1])

    @property
    def capabilities(self) -> EngineCapabilities:
        """Report what this engine supports — varies by browser type."""
        if self._browser_type == "chrome":
            return EngineCapabilities(
                cdp=True,
                bidi=False,
                stealth_inject_before=True,
                stealth_inject_after=True,
                network_intercept=False,
                multi_tab=True,
                screenshots=True,
                name="selenium-chrome",
            )
        if self._browser_type == "firefox":
            return EngineCapabilities(
                cdp=False,
                bidi=True,
                stealth_inject_after=True,
                network_intercept=False,
                multi_tab=True,
                screenshots=True,
                name="selenium-firefox",
            )
        # safari (and any other fallback)
        return EngineCapabilities(
            cdp=False,
            bidi=False,
            stealth_inject_after=True,
            network_intercept=False,
            multi_tab=True,
            screenshots=True,
            name="selenium-safari",
        )

    @property
    def backend_name(self) -> str:
        """Return the backend identifier string."""
        return "selenium"

    @property
    def driver(self) -> Any:
        """Direct access to the WebDriver (for backward compat)."""
        return self._driver

    async def __aenter__(self) -> SeleniumEngine:
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.stop()
