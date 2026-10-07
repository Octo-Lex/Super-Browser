"""Backend wiring characterization tests — PR 1 items P1 and P2.

Test layers, per the approved PR 1 package (PLAN-COMPOSITION-HARDENING.md
step 4):

- **Characterization tests** pin behavior that must survive the refactor:
  explicit-backend passthrough and auto-detection precedence.

- **Mode-based detection** was a verified composition bug on ``ecd6576``:
  ``_detect_backend`` matched uppercase ``"PATCHRIGHT"``/``"CLOAK"``
  inside ``str(mode)`` while ``SessionMode`` values are lowercase
  (``"patchright_launch"``, ``"cloak_launch"``), so mode routing never
  fired — import probing masked it by returning ``"patchright"``
  regardless. P2 normalized the matching; these are hard gates now.

- **Browser-backed verticals** run one real lifecycle per backend. The
  Playwright ENGINE smoke (factory → start → new_page → goto → screenshot
  → stop) is a hard gate as of P2. The full Playwright FAÇADE vertical
  stays ``xfail(strict=False)`` until P3–P5 close the controller, page,
  and tab seams — factory routing alone cannot make that path valid.
  Selenium/CDP have factory-level tests in ``test_engine_factory.py``;
  their backend unit tests live under ``tests/test_browser/``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("patchright")

from super_browser import Config, SuperBrowser  # noqa: E402
from super_browser.browser.config import SessionConfig, SessionMode  # noqa: E402
from super_browser.browser.engine import _detect_backend  # noqa: E402

# ============================================================================
# Current behavior — explicit backend names pass through detection verbatim
# ============================================================================


@pytest.mark.parametrize("backend", ["patchright", "playwright", "selenium", "cdp"])
def test_explicit_backend_name_passes_through(backend: str) -> None:
    assert _detect_backend(SessionConfig(backend=backend)) == backend


def test_auto_detection_resolves_patchright_when_installed() -> None:
    # Import probing order: patchright → playwright → selenium.
    pytest.importorskip("patchright")
    assert _detect_backend(SessionConfig(backend="auto")) == "patchright"


# ============================================================================
# Mode-based detection — flipped to hard gates by P2 (normalized matching)
# ============================================================================


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (SessionMode.PATCHRIGHT_LAUNCH, "patchright"),
        (SessionMode.PATCHRIGHT_ATTACH, "patchright"),
        (SessionMode.CLOAK_LAUNCH, "cloak"),
    ],
)
def test_mode_detection_routes_by_mode(mode: SessionMode, expected: str) -> None:
    assert _detect_backend(SessionConfig(mode=mode, backend="auto")) == expected


# ============================================================================
# Real-browser vertical: Patchright (default backend) — green today
# ============================================================================


def _headless() -> Config:
    return Config(browser=SessionConfig(headless=True))


@pytest.mark.integration
async def test_patchright_facade_lifecycle(tmp_path: Path) -> None:
    """start → navigate → observe → screenshot → tab operation → stop."""
    fixture = tmp_path / "wiring.html"
    fixture.write_text(
        "<html><body><h1>WIRING GATE</h1></body></html>", encoding="utf-8"
    )
    sb = SuperBrowser(config=_headless())
    await sb.start()
    try:
        assert sb._engine is not None, "default backend must expose an engine"
        await sb.navigate(fixture.as_uri())
        observed = await sb.observe()
        assert observed.ok, f"observe failed: {observed.error}"
        img, mime = await sb._capture_region_bytes(format="png")
        assert len(img) > 0 and mime == "image/png"
        await sb.open_tab("about:blank")
        assert await sb._page.title() is not None
    finally:
        await sb.stop()


# ============================================================================
# Real-browser vertical: Playwright Chromium façade — xfailed until P3–P5
# ============================================================================


@pytest.mark.integration
@pytest.mark.xfail(
    strict=False,
    reason="P3–P5: P2 lands factory routing (see test_playwright_engine_smoke), "
    "but the full façade path still needs the normalized page (P3), the "
    "controller's CDPBridge/capability contract (P4), and engine-page tab "
    "ownership (P5). observe/open_tab remain Patchright-shaped until then.",
)
async def test_playwright_facade_lifecycle(tmp_path: Path) -> None:
    """backend='playwright' must construct PlaywrightEngine and work end to end."""
    pytest.importorskip("playwright")
    from super_browser.browser.backends.playwright_backend import PlaywrightEngine

    fixture = tmp_path / "wiring_pw.html"
    fixture.write_text(
        "<html><body><h1>PW WIRING GATE</h1></body></html>", encoding="utf-8"
    )
    sb = SuperBrowser(config=Config(browser=SessionConfig(headless=True, backend="playwright")))
    await sb.start()
    try:
        assert isinstance(sb._engine, PlaywrightEngine), (
            f"backend='playwright' produced engine {type(sb._engine).__name__!r}, "
            "not PlaywrightEngine — the composition defect"
        )
        await sb.navigate(fixture.as_uri())
        observed = await sb.observe()
        assert observed.ok, f"observe failed: {observed.error}"
        img, mime = await sb._capture_region_bytes(format="png")
        assert len(img) > 0
        await sb.open_tab("about:blank")
    finally:
        await sb.stop()


# ============================================================================
# Real-browser ENGINE smoke: Playwright Chromium — hard gate from P2.
# Deliberately engine-level only: no façade, no controller, no tabs.
# ============================================================================


@pytest.mark.integration
async def test_playwright_engine_smoke(tmp_path: Path) -> None:
    """factory → PlaywrightEngine.start() → new_page() → goto → screenshot → stop."""
    pytest.importorskip("playwright")
    from super_browser.browser.backends.playwright_backend import PlaywrightEngine
    from super_browser.browser.factory import create_browser_engine

    fixture = tmp_path / "pw_smoke.html"
    fixture.write_text(
        "<html><body><h1>PW ENGINE SMOKE</h1></body></html>", encoding="utf-8"
    )
    engine = create_browser_engine(
        Config(browser=SessionConfig(headless=True, backend="playwright"))
    )
    assert isinstance(engine, PlaywrightEngine)
    await engine.start()
    try:
        page = await engine.new_page()
        await page.goto(fixture.as_uri())
        assert "PW ENGINE SMOKE" in await page.title() or (await page.content())
        png = await page.screenshot()
        assert len(png) > 0
    finally:
        await engine.stop()
