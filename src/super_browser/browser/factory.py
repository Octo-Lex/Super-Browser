"""Engine factory — the single composition point for backend selection.

PR 1 item P2 (PLAN-COMPOSITION-HARDENING.md step 4). ``SuperBrowser.start``
calls :func:`create_browser_engine` and no other module decides backends.
Mapping:

    patchright explicit/auto  -> PatchrightEngine
    playwright                -> PlaywrightEngine
    selenium                  -> SeleniumEngine
    cdp                       -> CDPDirectEngine
    cloak (mode or explicit)  -> PatchrightEngine + cloak configuration

There is no ``CloakEngine``: cloak is a launch mode of the Patchright stack
(``BrowserSession.start`` routes ``CLOAK_LAUNCH`` through
``CloakBrowserAdapter``), so the factory expresses it as configuration.

Preservation rule: cloak configuration is supplied ONLY on the cloak route.
``BrowserSession`` attempts a CloakBrowser launch whenever ``cloak_config``
is present on a normal launch, and ``Config.cloak`` exists by default —
leaking it into ordinary Patchright construction would silently change the
default backend for every user.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from super_browser.browser.config import SessionConfig, SessionMode
from super_browser.browser.engine import _detect_backend

__all__ = ["create_browser_engine", "normalize_browser_type"]

# Backend-specific browser_type vocabulary. SessionConfig.browser_type
# defaults to "chromium" (the Playwright spelling); SeleniumEngine accepts
# only "chrome"/"firefox"/"safari" and raises ValueError on anything else,
# so a default-config Selenium selection must be translated or it fails at
# start(). The chrome→chromium direction covers the inverse mismatch.
_BROWSER_TYPE_ALIASES: dict[tuple[str, str], str] = {
    ("selenium", "chromium"): "chrome",
    ("playwright", "chrome"): "chromium",
}


def normalize_browser_type(backend: str, browser_type: str) -> str:
    """Translate ``browser_type`` vocabulary for the selected backend."""
    return _BROWSER_TYPE_ALIASES.get((backend, browser_type), browser_type)


def create_browser_engine(config: Any) -> Any:
    """Build and return the engine for *config* (``Config`` or ``SessionConfig``).

    Construction is cheap and side-effect free; ``await engine.start()``
    performs the actual launch. Engines whose optional dependency is absent
    construct fine and raise a structured ``ImportError`` with install
    instructions from ``start()``.
    """
    session_config: SessionConfig = (
        config.browser if hasattr(config, "browser") else (config or SessionConfig())
    )
    backend = _detect_backend(session_config)

    if backend == "playwright":
        from super_browser.browser.backends.playwright_backend import PlaywrightEngine

        return PlaywrightEngine(
            replace(
                session_config,
                browser_type=normalize_browser_type(
                    "playwright", session_config.browser_type
                ),
            )
        )

    if backend == "selenium":
        from super_browser.browser.backends.selenium_backend import SeleniumEngine

        # Construct with the NORMALIZED config: SeleniumEngine.start() re-reads
        # browser_type from the config object when one is passed, so handing it
        # the original SessionConfig would defeat the translation.
        return SeleniumEngine(
            replace(
                session_config,
                browser_type=normalize_browser_type(
                    "selenium", session_config.browser_type
                ),
            )
        )

    if backend == "cdp":
        from super_browser.browser.backends.cdp_backend import CDPDirectEngine

        return CDPDirectEngine(
            endpoint=session_config.endpoint, config=session_config
        )

    if backend == "cloak":
        from super_browser.browser.backends.patchright_backend import PatchrightEngine

        cloak_config = getattr(config, "cloak", None)
        return PatchrightEngine(
            replace(session_config, mode=SessionMode.CLOAK_LAUNCH),
            cloak_config=cloak_config,
        )

    if backend == "patchright":
        from super_browser.browser.backends.patchright_backend import PatchrightEngine

        # Ordinary Patchright construction must NOT receive cloak
        # configuration — see the preservation rule in the module docstring.
        return PatchrightEngine(session_config)

    raise ValueError(
        f"Unsupported backend {backend!r}. "
        "Supported backends: patchright, playwright, selenium, cdp, cloak."
    )
