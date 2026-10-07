"""Vision capture characterization tests — the 2.13.1 release gate.

Encodes the defect reproduced by execution on 2026-10-07 (working tree at
``9aff421``, headless Patchright):

    start()
      -> _page is a PatchrightPage
      -> _capture_region_bytes(format="png")
      -> TypeError: Page.screenshot() got an unexpected keyword argument 'format'

    open_tab()
      -> _page is replaced by a PageHandle (accepts format=)
      -> the identical capture succeeds

Test split (per the frozen plan, PLAN-COMPOSITION-HARDENING.md step 2):

- ``test_fresh_start_capture_succeeds_without_tab_operation`` — the gate.
  Needs only Patchright, no Tesseract. **Expected FAIL on 2.13.0** by
  design; it goes green in the 2.13.1 superseding PR and stays as the
  regression test. Commit it with that PR, not to main.
- ``test_capture_after_open_tab_succeeds`` — characterizes the accidental
  recovery path; passes today and must keep passing.
- ``test_page_abstraction_is_normalized_after_start_and_tab`` — the PR 1
  invariant ("_page is never an arbitrary raw backend Page"), marked
  ``xfail(strict=False)`` because 2.13.0 violates it. PR 1 removes the
  marker in its own diff so the invariant becomes a hard gate.
- ``test_extract_image_text_vertical_on_local_fixture`` — the full OCR
  vertical. Skips where no ``tesseract`` binary exists (e.g. dev Windows
  boxes); runs on CI runners that install Tesseract.

Browser-backed tests carry the ``integration`` marker (real Chromium via
Patchright, headless). The module skips cleanly when Patchright is absent.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

pytest.importorskip("patchright")

from super_browser import Config, SuperBrowser  # noqa: E402
from super_browser.browser.config import SessionConfig  # noqa: E402


def _headless_config() -> Config:
    return Config(browser=SessionConfig(headless=True))


# ============================================================================
# The 2.13.1 gate — fresh-start capture, no tab operation
# ============================================================================


@pytest.mark.integration
async def test_fresh_start_capture_succeeds_without_tab_operation() -> None:
    """2.13.1 release gate: capture works immediately after start().

    Fails on 2.13.0 with TypeError (the reproduced defect). Passes once the
    screenshot keyword compatibility fix lands. Requires only Patchright.
    """
    sb = SuperBrowser(config=_headless_config())
    await sb.start()
    try:
        img_bytes, mime = await sb._capture_region_bytes(format="png")
        assert len(img_bytes) > 0
        assert mime == "image/png"
    finally:
        await sb.stop()


# ============================================================================
# Recovery-half characterization — passes on 2.13.0, must keep passing
# ============================================================================


@pytest.mark.integration
async def test_capture_after_open_tab_succeeds() -> None:
    """After one tab operation, capture works (the accidental recovery path).

    This is the behavior that let the defect ship: any test that touched a
    tab before capturing exercised PageHandle and never saw the TypeError.
    """
    sb = SuperBrowser(config=_headless_config())
    await sb.start()
    try:
        await sb.open_tab("about:blank")
        img_bytes, mime = await sb._capture_region_bytes(format="png")
        assert len(img_bytes) > 0
        assert mime == "image/png"
    finally:
        await sb.stop()


# ============================================================================
# PR 1 invariant — xfail until the normalized page abstraction lands
# ============================================================================


@pytest.mark.integration
@pytest.mark.xfail(
    strict=False,
    reason="PR 1 invariant (PLAN-COMPOSITION-HARDENING.md step 4): start() "
    "still leaves a raw PatchrightPage; remove this marker in the PR 1 diff.",
)
async def test_page_abstraction_is_normalized_after_start_and_tab() -> None:
    """After composition hardening, _page is PageHandle-shaped everywhere.

    Today: start() -> PatchrightPage, open_tab() -> PageHandle (mixed
    abstractions — the root cause of the format=/type= split). PR 1 must
    make both sites yield the same normalized abstraction, then delete the
    xfail marker so this becomes a permanent guard.
    """
    from super_browser.browser.page import PageHandle

    sb = SuperBrowser(config=_headless_config())
    await sb.start()
    try:
        assert isinstance(sb._page, PageHandle), (
            f"after start(), _page is {type(sb._page).__name__}, not PageHandle"
        )
        await sb.open_tab("about:blank")
        assert isinstance(sb._page, PageHandle), (
            f"after open_tab(), _page is {type(sb._page).__name__}, not PageHandle"
        )
    finally:
        await sb.stop()


# ============================================================================
# Full OCR vertical — runs only where the tesseract binary exists
# ============================================================================


@pytest.mark.integration
@pytest.mark.skipif(
    shutil.which("tesseract") is None,
    reason="requires the tesseract binary (full extract_image_text vertical)",
)
async def test_extract_image_text_vertical_on_local_fixture(tmp_path: Path) -> None:
    """start() -> local rendered-text page -> extract_image_text() succeeds.

    This is the semantic half of the release gate: the exact call chain a
    user hits (facade OCR tool -> _capture_region_bytes -> tesseract). The
    fixture is a deterministic local HTML file, no network.
    """
    fixture = tmp_path / "ocr_gate.html"
    fixture.write_text(
        "<html><body style='background:#ffffff'>"
        "<h1 style='color:#000000;font-size:64px;margin:40px'>"
        "SUPERBROWSER OCR GATE 12345"
        "</h1></body></html>",
        encoding="utf-8",
    )
    sb = SuperBrowser(config=_headless_config())
    await sb.start()
    try:
        await sb.navigate(fixture.as_uri())
        result = await sb.extract_image_text()
        assert result.ok, f"extract_image_text failed: {result.error}"
        text = (result.data or {}).get("text", "")
        assert "SUPERBROWSER" in text.upper(), (
            f"fixture text not recognized by OCR; got: {text[:200]!r}"
        )
    finally:
        await sb.stop()
