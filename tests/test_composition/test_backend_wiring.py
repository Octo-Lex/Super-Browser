"""Backend wiring characterization tests — PR 1 item P1.

Two layers, per the approved PR 1 package (PLAN-COMPOSITION-HARDENING.md
step 4):

- **Current-behavior tests** pass on ``ecd6576`` and must keep passing
  through the refactor. They pin what ``_detect_backend`` actually does
  today so the factory rewrite cannot silently change unrelated behavior.

- **Target-behavior tests** are ``xfail(strict=False)``: they encode the
  approved design (mode-normalized detection; per-backend engine routing).
  P2 flips them to passing and removes the markers, preserving red→green
  evidence inside the PR.

Verified composition bug encoded here: ``_detect_backend`` matches
uppercase ``"PATCHRIGHT"``/``"CLOAK"`` inside ``str(mode)``, but
``SessionMode`` values are lowercase (``"patchright_launch"``,
``"cloak_launch"``), so mode-based detection can never fire — import
probing masks it by returning ``"patchright"`` regardless.

Browser-backed verticals run one real lifecycle per backend. Playwright
Chromium is ``xfail`` until P2 routes ``backend="playwright"`` to
``PlaywrightEngine`` (today the façade falls into ``BrowserSession``,
leaving ``sb._engine`` as ``None``). Selenium/CDP get factory-level tests
when the factory exists (P2); their backend unit tests already exist under
``tests/test_browser/``.
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
# Target behavior — mode-based detection (bug: uppercase check vs lowercase
# StrEnum values). xfail until P2 normalizes the comparison.
# ============================================================================


@pytest.mark.xfail(
    strict=False,
    reason="P2: _detect_backend checks uppercase substrings against lowercase "
    "SessionMode values, so mode-based routing never fires (import probing "
    "masks it). PR 1 normalizes the check and these flip to hard gates.",
)
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
# Real-browser vertical: Playwright Chromium — xfail until P2 routes it
# ============================================================================


@pytest.mark.integration
@pytest.mark.xfail(
    strict=False,
    reason="P2: backend='playwright' still falls into BrowserSession "
    "(Patchright-based); sb._engine stays None and no PlaywrightEngine is "
    "constructed. PR 1's factory routes it and removes this marker.",
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
