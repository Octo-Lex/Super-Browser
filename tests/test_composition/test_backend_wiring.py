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
  → stop) is a hard gate as of P2. The full Playwright FAÇADE vertical —
  start → navigate → observe → screenshot → tabs → base page → stop — is
  a hard gate since P5 made tab ownership engine-owned. Selenium/CDP have
  factory-level tests in ``test_engine_factory.py``; their backend unit
  tests live under ``tests/test_browser/``.
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
@pytest.mark.browser_integration
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
# Real-browser vertical: Playwright Chromium façade — HARD since P5
# ============================================================================


@pytest.mark.integration
@pytest.mark.browser_integration
async def test_playwright_facade_lifecycle(tmp_path: Path) -> None:
    """backend='playwright' must construct PlaywrightEngine and run the full
    façade lifecycle: start → navigate → observe → screenshot → tabs (open,
    switch, close) → back to the base page → stop. Hard gate since P5 made
    tab ownership engine-owned."""
    pytest.importorskip("playwright")
    from super_browser.browser.backends.playwright_backend import PlaywrightEngine
    from super_browser.browser.page import NormalizedPage

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

        tab_b = await sb.open_tab("about:blank")
        assert tab_b.ok, f"open_tab failed: {tab_b.error}"
        assert isinstance(sb._page, NormalizedPage)
        tab_c = await sb.open_tab("about:blank")
        assert tab_c.ok
        switched = await sb.switch_tab(tab_b.data.tab_id)
        assert switched.ok, f"switch_tab failed: {switched.error}"
        closed = await sb.close_tab(tab_c.data.tab_id)
        assert closed.ok
        observed_after = await sb.observe()
        assert observed_after.ok, f"observe after close failed: {observed_after.error}"
        closed_b = await sb.close_tab(tab_b.data.tab_id)
        assert closed_b.ok
        base_observed = await sb.observe()
        assert base_observed.ok, (
            "the base page must be usable after every managed tab closes"
        )
    finally:
        await sb.stop()


# ============================================================================
# Real-browser vertical: Patchright full multi-tab lifecycle (P5 proof)
# ============================================================================


@pytest.mark.integration
@pytest.mark.browser_integration
async def test_patchright_multitab_lifecycle(tmp_path: Path) -> None:
    """open A → open B → switch A → operate → close A → operate B → close B →
    operate on the original base page → stop."""
    from super_browser.browser.page import NormalizedPage

    fixture = tmp_path / "wiring_pr.html"
    fixture.write_text(
        "<html><body><h1>PR TAB LIFECYCLE</h1></body></html>", encoding="utf-8"
    )
    sb = SuperBrowser(config=Config(browser=SessionConfig(headless=True)))
    await sb.start()
    try:
        await sb.navigate(fixture.as_uri())
        tab_a = await sb.open_tab("about:blank")
        assert tab_a.ok
        tab_b = await sb.open_tab("about:blank")
        assert tab_b.ok
        switched = await sb.switch_tab(tab_a.data.tab_id)
        assert switched.ok
        assert isinstance(sb._page, NormalizedPage)
        observed_a = await sb.observe()
        assert observed_a.ok, f"operate on A failed: {observed_a.error}"
        closed_a = await sb.close_tab(tab_a.data.tab_id)
        assert closed_a.ok
        observed_b = await sb.observe()
        assert observed_b.ok, f"operate on B after closing A failed: {observed_b.error}"
        closed_b = await sb.close_tab(tab_b.data.tab_id)
        assert closed_b.ok
        base_observed = await sb.observe()
        assert base_observed.ok, (
            "operate on the original base page failed after all tabs closed"
        )
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
