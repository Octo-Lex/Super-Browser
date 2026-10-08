"""Tab ownership tests — PR 1 item P5.

TabManager owns NormalizedPages created by the engine (never raw backend
pages); activation goes through EnginePage.activate(); the initial façade
page is an unlisted base_page that close_tab falls back to when the last
managed tab closes.

Also covers the P5 lifecycle rules: façade stop is engine-owned (exactly
one engine.stop, never a legacy session double-stop), and backends
advertising multi_tab=False get structured refusals.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from super_browser.browser.page import NormalizedPage
from super_browser.browser.tabs import TabManager


class _FakeEnginePage:
    def __init__(self, name: str) -> None:
        self.name = name
        self.navigated_to: str = ""
        self.activations = 0
        self.closed = False

    @property
    def url(self) -> str:
        return self.navigated_to or f"https://fake/{self.name}"

    async def goto(self, url: str, *args: Any, **kwargs: Any) -> None:
        self.navigated_to = url

    async def title(self) -> str:
        return self.name

    async def activate(self) -> None:
        self.activations += 1

    async def close(self) -> None:
        self.closed = True


class _FakeEngine:
    def __init__(self) -> None:
        self.pages: list[_FakeEnginePage] = []

    async def new_page(self) -> _FakeEnginePage:
        page = _FakeEnginePage(f"p{len(self.pages) + 1}")
        self.pages.append(page)
        return page


def _manager_with_base() -> tuple[TabManager, _FakeEngine, NormalizedPage]:
    engine = _FakeEngine()
    base = NormalizedPage(engine_page=_FakeEnginePage("base"))
    return TabManager(engine, base), engine, base


# ============================================================================
# TabManager ownership
# ============================================================================


async def test_open_stores_normalized_page_from_engine() -> None:
    tm, engine, _ = _manager_with_base()
    tab = await tm.open_tab("https://example.com/a")

    page = tm.active_page()
    assert isinstance(page, NormalizedPage)
    assert page.engine_page is engine.pages[0]
    assert page.engine_page.navigated_to == "https://example.com/a"
    assert tab.tab_id == 1
    assert page.engine_page.activations >= 1, "open_tab must activate"


async def test_switch_activates_selected_page() -> None:
    tm, engine, _ = _manager_with_base()
    tab_a = await tm.open_tab()
    await tm.open_tab()
    a, b = engine.pages[0], engine.pages[1]
    a.activations = b.activations = 0

    await tm.switch_tab(tab_a.tab_id)

    assert tm.active_page().engine_page is a
    assert a.activations == 1, "switch must activate the selected page"
    assert b.activations == 0


async def test_close_inactive_preserves_active_page() -> None:
    tm, engine, _ = _manager_with_base()
    tab_a = await tm.open_tab()
    await tm.open_tab()
    b = engine.pages[1]

    replacement = await tm.close_tab(tab_a.tab_id)

    assert replacement is None, "closing an inactive tab changes nothing"
    assert engine.pages[0].closed is True
    assert tm.active_page().engine_page is b
    assert b.closed is False


async def test_close_active_reactivates_replacement() -> None:
    tm, engine, _ = _manager_with_base()
    await tm.open_tab()  # A
    tab_b = await tm.open_tab()  # B, now active
    a = engine.pages[0]
    a.activations = 0

    replacement = await tm.close_tab(tab_b.tab_id)

    assert replacement is not None and replacement.engine_page is a
    assert a.activations == 1, "the replacement page must be activated"
    assert tm.active_page().engine_page is a


async def test_close_final_managed_tab_falls_back_to_base() -> None:
    tm, engine, base = _manager_with_base()
    tab_a = await tm.open_tab()
    base_engine = base.engine_page
    base_engine.activations = 0

    replacement = await tm.close_tab(tab_a.tab_id)

    assert replacement is None
    assert engine.pages[0].closed is True
    assert base_engine.activations == 1, "the base page must be reactivated"
    assert tm.active_tab_id is None


async def test_list_tabs_reports_managed_tabs_only() -> None:
    tm, _, base = _manager_with_base()
    await tm.open_tab("https://example.com/1")
    await tm.open_tab("https://example.com/2")

    snap = await tm.list_tabs()

    assert snap.count == 2, "the unlisted base page must never be listed"
    assert {t.url for t in snap.tabs} == {
        "https://example.com/1",
        "https://example.com/2",
    }
    assert base.engine_page.url not in {t.url for t in snap.tabs}


# ============================================================================
# Façade lifecycle — engine-owned stop
# ============================================================================


def _started_facade_with_mock_engine() -> Any:
    from super_browser import SuperBrowser

    sb = SuperBrowser()
    engine = MagicMock()
    engine.stop = AsyncMock()
    engine.session = MagicMock()
    engine.session.stop = AsyncMock()
    engine.capabilities.multi_tab = True
    sb._engine = engine
    sb._session = engine.session
    sb._running = True
    return sb, engine


async def test_facade_stop_calls_engine_stop_exactly_once() -> None:
    sb, engine = _started_facade_with_mock_engine()
    await sb.stop()
    engine.stop.assert_awaited_once()


async def test_facade_stop_never_double_stops_legacy_session() -> None:
    """PatchrightEngine.stop() owns its BrowserSession — the façade must not
    also call session.stop() (Patchright) and must not skip engine stop when
    _session is None (every other backend, pre-P5)."""
    sb, engine = _started_facade_with_mock_engine()
    await sb.stop()
    engine.session.stop.assert_not_awaited()
    assert sb._engine is None and sb._session is None


async def test_facade_stop_stops_engine_even_without_legacy_session() -> None:
    sb = __import__("super_browser").SuperBrowser()
    engine = MagicMock()
    engine.stop = AsyncMock()
    sb._engine = engine
    sb._session = None  # non-Patchright shape — the pre-P5 shutdown defect
    await sb.stop()
    engine.stop.assert_awaited_once()


# ============================================================================
# Capability gate — multi_tab=False backends refuse structurally
# ============================================================================


async def test_facade_open_tab_refuses_when_multi_tab_unsupported() -> None:
    from super_browser import SuperBrowser

    sb = SuperBrowser()
    engine = MagicMock()
    engine.capabilities.multi_tab = False
    engine.capabilities.name = "cdp"
    sb._engine = engine
    sb._running = True

    result = await sb.open_tab("https://example.com/")

    assert result.ok is False
    assert result.error is not None
    assert "multi-tab is not supported by backend 'cdp'" in result.error.message


async def test_cdp_direct_engine_advertises_multi_tab_false() -> None:
    pytest.importorskip("aiohttp")
    from super_browser.browser.backends.cdp_backend import CDPDirectEngine

    assert CDPDirectEngine().capabilities.multi_tab is False


# ============================================================================
# Selenium window-handle semantics
# ============================================================================


class _FakeSeleniumDriver:
    def __init__(self) -> None:
        self.windows: list[str] = ["w1"]
        self.current = "w1"
        self.closed: list[str] = []
        self.switch_to = MagicMock()
        self.switch_to.window = MagicMock(
            side_effect=lambda handle: setattr(self, "current", handle)
        )

    @property
    def current_window_handle(self) -> str:
        return self.current

    def open_window(self, name: str) -> str:
        self.windows.append(name)
        self.current = name
        return name

    def close_current(self) -> None:
        self.closed.append(self.current)
        self.windows.remove(self.current)
        self.current = self.windows[-1]

    def close(self) -> None:
        self.close_current()


def _selenium_page(driver: _FakeSeleniumDriver) -> Any:
    from super_browser.browser.backends.selenium_backend import SeleniumPage

    return SeleniumPage(driver, "chrome")


def test_selenium_page_captures_distinct_window_handle() -> None:
    driver = _FakeSeleniumDriver()
    page_one = _selenium_page(driver)
    driver.open_window("w2")
    page_two = _selenium_page(driver)

    assert page_one._window_handle == "w1"
    assert page_two._window_handle == "w2"
    assert page_one._window_handle != page_two._window_handle


async def test_selenium_activate_switches_to_page_handle() -> None:
    driver = _FakeSeleniumDriver()
    page_one = _selenium_page(driver)
    driver.open_window("w2")
    page_two = _selenium_page(driver)

    await page_one.activate()
    assert driver.current == "w1"
    await page_two.activate()
    assert driver.current == "w2"


async def test_selenium_close_inactive_cannot_close_active_window() -> None:
    driver = _FakeSeleniumDriver()
    page_one = _selenium_page(driver)
    driver.open_window("w2")
    _selenium_page(driver)

    # w2 is current; closing the background w1 must close w1, not w2.
    await page_one.close()
    assert driver.closed == ["w1"]
    assert "w2" in driver.windows


# ============================================================================
# P2-review: Selenium window-aware metadata + background-close restoration
# ============================================================================


class _WindowAwareSeleniumDriver(_FakeSeleniumDriver):
    """Adds per-window metadata so title/current_url read per-window state."""

    def __init__(self) -> None:
        super().__init__()
        self.titles: dict[str, str] = {"w1": "One", "w2": "Two"}
        self.urls: dict[str, str] = {
            "w1": "https://example.com/one",
            "w2": "https://example.com/two",
        }

    @property
    def title(self) -> str:
        return self.titles[self.current]

    @property
    def current_url(self) -> str:
        return self.urls[self.current]

    @property
    def window_handles(self) -> list[str]:
        return list(self.windows)


def _two_window_pages() -> tuple[Any, Any, Any]:
    driver = _WindowAwareSeleniumDriver()
    page_one = _selenium_page(driver)
    driver.open_window("w2")
    page_two = _selenium_page(driver)
    return driver, page_one, page_two


async def test_selenium_metadata_is_window_aware() -> None:
    """Two SeleniumPages must report their OWN windows' metadata — even while
    a different window is the driver's current selection — and the prior
    selection must be restored after the read."""
    driver, page_one, page_two = _two_window_pages()
    driver.switch_to.window("w2")  # w2 is current

    assert await page_one.title() == "One"
    assert page_one.url == "https://example.com/one"
    assert await page_two.title() == "Two"
    assert page_two.url == "https://example.com/two"
    # The prior selection (w2) was restored after reading page_one.
    assert driver.current == "w2"


async def test_selenium_close_background_tab_restores_previous_window() -> None:
    """Closing a BACKGROUND tab must not leave the driver on a webdriver-
    chosen window — the previously selected window is restored."""
    driver, page_one, page_two = _two_window_pages()
    driver.switch_to.window("w2")  # w2 is current; w1 is background

    await page_one.close()

    assert driver.closed == ["w1"]
    assert driver.current == "w2", (
        "closing the background tab must restore the previously active window"
    )
    assert "w1" not in driver.window_handles


async def test_selenium_close_active_window_keeps_webdriver_choice() -> None:
    """Closing the CURRENT window needs no restore — the webdriver picks a
    remaining window, and TabManager reactivation takes it from there."""
    driver, page_one, page_two = _two_window_pages()
    driver.switch_to.window("w2")  # w2 is current

    await page_two.close()

    assert driver.closed == ["w2"]
    assert "w2" not in driver.windows
