"""Engine factory tests — PR 1 item P2.

Proves the approved factory contract: one mapping per backend, cloak as
PatchrightEngine + cloak configuration (never a CloakEngine), browser_type
vocabulary normalization, explicit failure on unknown backends, and the
cloak preservation rule (ordinary Patchright construction receives NO cloak
configuration even though ``Config.cloak`` exists by default —
``BrowserSession`` attempts a cloak launch whenever cloak config is
present).

Non-default backends construct without their optional dependency installed
(the engines import lazily inside ``start()``), so the mapping tests here
run everywhere; the real-browser smoke for Playwright Chromium lives in
``test_backend_wiring.py``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from super_browser import Config
from super_browser.browser.config import SessionConfig, SessionMode

# ============================================================================
# Mapping — each explicit backend produces its engine
# ============================================================================


def test_factory_maps_patchright() -> None:
    from super_browser.browser.backends.patchright_backend import PatchrightEngine
    from super_browser.browser.factory import create_browser_engine

    engine = create_browser_engine(Config(browser=SessionConfig(backend="patchright")))
    assert isinstance(engine, PatchrightEngine)


def test_factory_maps_playwright() -> None:
    pytest.importorskip("playwright")
    from super_browser.browser.backends.playwright_backend import PlaywrightEngine
    from super_browser.browser.factory import create_browser_engine

    engine = create_browser_engine(Config(browser=SessionConfig(backend="playwright")))
    assert isinstance(engine, PlaywrightEngine)


def test_factory_maps_selenium() -> None:
    from super_browser.browser.backends.selenium_backend import SeleniumEngine
    from super_browser.browser.factory import create_browser_engine

    engine = create_browser_engine(Config(browser=SessionConfig(backend="selenium")))
    assert isinstance(engine, SeleniumEngine)


def test_factory_maps_cdp() -> None:
    pytest.importorskip("aiohttp")
    from super_browser.browser.backends.cdp_backend import CDPDirectEngine
    from super_browser.browser.factory import create_browser_engine

    engine = create_browser_engine(
        Config(
            browser=SessionConfig(
                backend="cdp", endpoint="ws://127.0.0.1:9222/devtools/browser"
            )
        )
    )
    assert isinstance(engine, CDPDirectEngine)


def test_factory_maps_cloak_mode_to_patchright_engine_with_cloak_config() -> None:
    from super_browser.browser.backends.patchright_backend import PatchrightEngine
    from super_browser.browser.factory import create_browser_engine

    cfg = Config(browser=SessionConfig(mode=SessionMode.CLOAK_LAUNCH))
    engine = create_browser_engine(cfg)
    assert isinstance(engine, PatchrightEngine), "cloak is a Patchright launch mode, not a separate engine"
    assert engine.cloak_config is cfg.cloak, "cloak route must carry the cloak configuration"


def test_factory_maps_explicit_cloak_backend() -> None:
    from super_browser.browser.backends.patchright_backend import PatchrightEngine
    from super_browser.browser.factory import create_browser_engine

    engine = create_browser_engine(Config(browser=SessionConfig(backend="cloak")))
    assert isinstance(engine, PatchrightEngine)
    assert engine._config.mode == SessionMode.CLOAK_LAUNCH


# ============================================================================
# Preservation rule — ordinary Patchright gets NO cloak configuration
# ============================================================================


def test_factory_ordinary_patchright_gets_no_cloak_config() -> None:
    """Config.cloak exists by default; leaking it into ordinary Patchright
    construction would make BrowserSession attempt a cloak launch on every
    normal start (cloak_config presence is the trigger)."""
    from super_browser.browser.factory import create_browser_engine

    cfg = Config(browser=SessionConfig(backend="patchright"))
    engine = create_browser_engine(cfg)
    assert engine.cloak_config is None


# ============================================================================
# Browser-type vocabulary normalization
# ============================================================================


def test_normalize_selenium_chromium_to_chrome() -> None:
    from super_browser.browser.factory import normalize_browser_type

    assert normalize_browser_type("selenium", "chromium") == "chrome"


def test_normalize_playwright_chrome_to_chromium() -> None:
    from super_browser.browser.factory import normalize_browser_type

    assert normalize_browser_type("playwright", "chrome") == "chromium"


@pytest.mark.parametrize(
    ("backend", "browser_type"),
    [("selenium", "firefox"), ("selenium", "safari"), ("playwright", "firefox")],
)
def test_normalize_passthrough_known_vocabulary(backend: str, browser_type: str) -> None:
    from super_browser.browser.factory import normalize_browser_type

    assert normalize_browser_type(backend, browser_type) == browser_type


def test_factory_constructs_selenium_with_normalized_browser_type() -> None:
    """Default SessionConfig.browser_type is 'chromium', which SeleniumEngine
    rejects (ValueError: Unsupported browser type). The factory must hand the
    engine the normalized value — and start(config) re-reads it from the
    config, so the ENGINE's stored config must be the normalized one."""
    from super_browser.browser.factory import create_browser_engine

    engine = create_browser_engine(Config(browser=SessionConfig(backend="selenium")))
    assert engine._browser_type == "chrome"
    assert engine._config.browser_type == "chrome"


def test_factory_unknown_backend_fails_explicitly() -> None:
    from super_browser.browser.factory import create_browser_engine

    with pytest.raises(ValueError, match="Unsupported backend"):
        create_browser_engine(Config(browser=SessionConfig(backend="nonexistent")))


# ============================================================================
# P2-review: engines consume the stored (normalized) config at start()
# ============================================================================


class TestPlaywrightEngineStartConfig:
    """PlaywrightEngine.start() must launch the CONFIGURED browser type with
    the CONFIGURED headless flag — not constructor defaults plus a hardcoded
    headless=True."""

    async def test_start_launches_configured_browser_type_and_headless(self):
        pytest.importorskip("playwright")
        from unittest.mock import AsyncMock, patch

        from super_browser.browser.backends import playwright_backend as pw_mod

        engine = pw_mod.PlaywrightEngine(
            SessionConfig(headless=False, browser_type="firefox")
        )
        firefox_launch = AsyncMock(
            return_value=MagicMock(new_context=AsyncMock(return_value=MagicMock()))
        )
        chromium_launch = AsyncMock()
        webkit_launch = AsyncMock()
        fake_pw = MagicMock()
        fake_pw.firefox.launch = firefox_launch
        fake_pw.chromium.launch = chromium_launch
        fake_pw.webkit.launch = webkit_launch

        with patch("playwright.async_api.async_playwright") as ap_factory:
            ap_factory.return_value.start = AsyncMock(return_value=fake_pw)
            await engine.start()

        firefox_launch.assert_awaited_once_with(headless=False)
        chromium_launch.assert_not_called()
        webkit_launch.assert_not_called()
        assert engine._browser_type == "firefox"

    async def test_start_launches_chromium_headless_when_configured(self):
        pytest.importorskip("playwright")
        from unittest.mock import AsyncMock, patch

        from super_browser.browser.backends import playwright_backend as pw_mod

        engine = pw_mod.PlaywrightEngine(SessionConfig(headless=True))
        chromium_launch = AsyncMock(
            return_value=MagicMock(new_context=AsyncMock(return_value=MagicMock()))
        )
        firefox_launch = AsyncMock()
        fake_pw = MagicMock()
        fake_pw.chromium.launch = chromium_launch
        fake_pw.firefox.launch = firefox_launch
        fake_pw.webkit.launch = AsyncMock()

        with patch("playwright.async_api.async_playwright") as ap_factory:
            ap_factory.return_value.start = AsyncMock(return_value=fake_pw)
            await engine.start()

        chromium_launch.assert_awaited_once_with(headless=True)
        firefox_launch.assert_not_called()

    async def test_start_rejects_unsupported_browser_type_explicitly(self):
        pytest.importorskip("playwright")
        from unittest.mock import AsyncMock, patch

        from super_browser.browser.backends import playwright_backend as pw_mod

        engine = pw_mod.PlaywrightEngine(SessionConfig(browser_type="safari"))
        with patch("playwright.async_api.async_playwright") as ap_factory:
            ap_factory.return_value.start = AsyncMock(return_value=MagicMock())
            with pytest.raises(ValueError, match="Unsupported browser type"):
                await engine.start()


class TestSeleniumEngineStartConfig:
    """SeleniumEngine.start() must consume the stored config: browser_type
    selection (factory normalizes chromium→chrome) and headless arguments.
    Selenium is not a test dependency, so the launcher layer is mocked."""

    def _fake_selenium_env(self, monkeypatch):
        import sys
        import types

        from super_browser.browser.backends import selenium_backend as se

        monkeypatch.setattr(se, "_SELENIUM_AVAILABLE", True)

        chrome_options_inst = MagicMock()
        chrome_options_inst.add_argument = MagicMock()
        chrome_options_cls = MagicMock(return_value=chrome_options_inst)
        mod_chrome_options = types.ModuleType("selenium.webdriver.chrome.options")
        mod_chrome_options.Options = chrome_options_cls
        mod_chrome_service = types.ModuleType("selenium.webdriver.chrome.service")
        mod_chrome_service.Service = MagicMock()

        firefox_options_inst = MagicMock()
        firefox_options_inst.add_argument = MagicMock()
        firefox_options_cls = MagicMock(return_value=firefox_options_inst)
        mod_firefox_options = types.ModuleType("selenium.webdriver.firefox.options")
        mod_firefox_options.Options = firefox_options_cls
        mod_firefox_service = types.ModuleType("selenium.webdriver.firefox.service")
        mod_firefox_service.Service = MagicMock()

        fake_webdriver = types.SimpleNamespace(
            Chrome=MagicMock(return_value=MagicMock()),
            Firefox=MagicMock(return_value=MagicMock()),
        )

        for name, mod in {
            "selenium.webdriver.chrome.options": mod_chrome_options,
            "selenium.webdriver.chrome.service": mod_chrome_service,
            "selenium.webdriver.firefox.options": mod_firefox_options,
            "selenium.webdriver.firefox.service": mod_firefox_service,
            # webdriver_manager import must fail → the no-Service fallback.
            "webdriver_manager": None,
            "webdriver_manager.chrome": None,
        }.items():
            monkeypatch.setitem(sys.modules, name, mod)

        monkeypatch.setattr(se, "webdriver", fake_webdriver)
        return chrome_options_inst, firefox_options_inst, fake_webdriver

    async def test_selenium_start_uses_chrome_with_configured_headless(
        self, monkeypatch
    ):
        chrome_options_inst, _, fake_webdriver = self._fake_selenium_env(monkeypatch)
        from super_browser.browser.factory import create_browser_engine

        engine = create_browser_engine(
            Config(browser=SessionConfig(backend="selenium", headless=True))
        )
        await engine.start()

        chrome_options_inst.add_argument.assert_any_call("--headless=new")
        fake_webdriver.Chrome.assert_called_once()

    async def test_selenium_start_uses_configured_firefox_headless(
        self, monkeypatch
    ):
        _, firefox_options_inst, fake_webdriver = self._fake_selenium_env(monkeypatch)
        from super_browser.browser.factory import create_browser_engine

        engine = create_browser_engine(
            Config(
                browser=SessionConfig(
                    backend="selenium", browser_type="firefox", headless=True
                )
            )
        )
        await engine.start()

        firefox_options_inst.add_argument.assert_any_call("-headless")
        fake_webdriver.Firefox.assert_called_once()
        fake_webdriver.Chrome.assert_not_called()

    async def test_selenium_safari_headless_fails_explicitly(self, monkeypatch):
        self._fake_selenium_env(monkeypatch)
        from super_browser.browser.factory import create_browser_engine

        engine = create_browser_engine(
            Config(
                browser=SessionConfig(
                    backend="selenium", browser_type="safari", headless=True
                )
            )
        )
        with pytest.raises(ValueError, match="does not support headless"):
            await engine.start()
