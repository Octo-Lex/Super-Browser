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
