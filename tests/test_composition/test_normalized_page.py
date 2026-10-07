"""NormalizedPage unit tests — PR 1 item P3.

Covers the wrapper contract itself (PLAN-COMPOSITION-HARDENING.md step 4,
P3): engine_page identity preservation, backend_page/cdp derivation and
override, canonical screenshot vocabulary (format= with the internal type=
compatibility alias), Selenium PNG→JPEG handling, and is_alive behavior
when the native object has or lacks is_closed().

These are pure unit tests — a fake engine page is sufficient because the
wrapper's contract IS the seam under test. Browser-backed invariant
coverage (NormalizedPage after start/open_tab/switch_tab) lives in
test_vision_capture_regression.py.
"""

from __future__ import annotations

from typing import Any

from super_browser.browser.page import NormalizedPage, _maybe_reencode_jpeg


class _FakeEnginePage:
    """Records screenshot kwargs; configurable identity surfaces."""

    def __init__(
        self,
        *,
        backend_page: Any = None,
        cdp: Any = None,
        screenshot_bytes: bytes = b"\x89PNG fake",
    ) -> None:
        self.backend_page = backend_page
        self.cdp = cdp
        self._bytes = screenshot_bytes
        self.last_screenshot_kwargs: dict[str, Any] = {}

    @property
    def url(self) -> str:
        return "https://example.com/"

    async def goto(self, url: str, *args: Any, **kwargs: Any) -> None:
        self.last_goto = (url, args, kwargs)

    async def title(self) -> str:
        return "fake"

    async def close(self) -> None:
        self.closed = True

    async def content(self) -> str:
        return "<html></html>"

    async def screenshot(self, **kwargs: Any) -> bytes:
        self.last_screenshot_kwargs = kwargs
        return self._bytes


# ============================================================================
# Identity and derivation
# ============================================================================


def test_engine_page_identity_is_preserved() -> None:
    engine = _FakeEnginePage()
    page = NormalizedPage(engine_page=engine)
    assert page.engine_page is engine


def test_backend_page_and_cdp_derived_from_engine_page() -> None:
    backend = object()
    cdp = object()
    engine = _FakeEnginePage(backend_page=backend, cdp=cdp)
    page = NormalizedPage(engine_page=engine)
    assert page.backend_page is backend
    assert page.cdp is cdp


def test_backend_page_and_cdp_derive_none_when_engine_lacks_them() -> None:
    """CDPDirectPage has no backend_page; the derivation must tolerate that."""
    engine = _FakeEnginePage()
    assert engine.backend_page is None  # fake default: attribute exists, None
    page = NormalizedPage(engine_page=engine)
    assert page.backend_page is None
    assert page.cdp is None


def test_explicit_backend_page_and_cdp_override_derivation() -> None:
    engine = _FakeEnginePage(backend_page=object(), cdp=object())
    explicit_backend = object()
    explicit_cdp = object()
    page = NormalizedPage(
        engine_page=engine, backend_page=explicit_backend, cdp=explicit_cdp
    )
    assert page.backend_page is explicit_backend
    assert page.cdp is explicit_cdp


def test_missing_attributes_derive_none_via_getattr() -> None:
    class _Bare:
        pass

    page = NormalizedPage(engine_page=_Bare())
    assert page.backend_page is None
    assert page.cdp is None


# ============================================================================
# Portable pass-throughs
# ============================================================================


async def test_portable_operations_delegate_to_engine_page() -> None:
    engine = _FakeEnginePage()
    page = NormalizedPage(engine_page=engine)

    assert page.url == "https://example.com/"
    await page.goto("https://example.com/x", wait_until="domcontentloaded")
    assert engine.last_goto[0] == "https://example.com/x"
    assert await page.title() == "fake"
    assert await page.content() == "<html></html>"
    await page.close()
    assert engine.closed is True


# ============================================================================
# Screenshot semantics
# ============================================================================


async def test_screenshot_forwards_canonical_format() -> None:
    engine = _FakeEnginePage()
    page = NormalizedPage(engine_page=engine)

    await page.screenshot(format="png", full_page=True)
    assert engine.last_screenshot_kwargs["format"] == "png"
    assert engine.last_screenshot_kwargs["full_page"] is True


async def test_screenshot_forwards_jpeg_and_quality() -> None:
    engine = _FakeEnginePage()
    page = NormalizedPage(engine_page=engine)

    await page.screenshot(format="jpeg", quality=70)
    assert engine.last_screenshot_kwargs["format"] == "jpeg"
    assert engine.last_screenshot_kwargs["quality"] == 70


async def test_screenshot_accepts_internal_type_alias() -> None:
    """The 2.13.1 probe passes type=; the wrapper maps it to format= so the
    probe does not throw on every screenshot (P7 deletes the probe)."""
    engine = _FakeEnginePage()
    page = NormalizedPage(engine_page=engine)

    await page.screenshot(type="jpeg", quality=80)
    assert engine.last_screenshot_kwargs["format"] == "jpeg"
    assert "type" not in engine.last_screenshot_kwargs


async def test_selenium_png_output_reencoded_to_jpeg() -> None:
    """Selenium ignores format and always returns PNG; the wrapper re-encodes
    via the existing Pillow helper when jpeg was requested."""
    tiny_png = _make_tiny_png()
    engine = _FakeEnginePage(screenshot_bytes=tiny_png)
    page = NormalizedPage(engine_page=engine)

    out = await page.screenshot(format="jpeg")
    assert not out.startswith(b"\x89PNG"), "JPEG requested but PNG returned"
    # PNG path passes bytes through untouched.
    out_png = await page.screenshot(format="png")
    assert out_png == tiny_png


async def test_reencode_falls_back_to_original_without_pillow_bytes() -> None:
    # Direct helper check: unparseable bytes come back unchanged.
    assert _maybe_reencode_jpeg(b"\x89PNG not-really", 70) == b"\x89PNG not-really"


def _make_tiny_png() -> bytes:
    import io

    from PIL import Image

    img = Image.new("RGBA", (4, 4), (255, 0, 0, 128))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ============================================================================
# is_alive
# ============================================================================


class _WithIsClosed:
    def __init__(self, closed: bool) -> None:
        self._closed = closed

    def is_closed(self) -> bool:
        return self._closed


class _BareNative:
    """No is_closed, no is_alive — wrapper must assume alive."""


async def test_is_alive_false_when_backend_reports_closed() -> None:
    engine = _FakeEnginePage(backend_page=_WithIsClosed(closed=True))
    assert NormalizedPage(engine_page=engine).is_alive is False


async def test_is_alive_true_when_backend_reports_open() -> None:
    engine = _FakeEnginePage(backend_page=_WithIsClosed(closed=False))
    assert NormalizedPage(engine_page=engine).is_alive is True


async def test_is_alive_assumes_alive_without_probe() -> None:
    engine = _FakeEnginePage(backend_page=_BareNative())
    assert NormalizedPage(engine_page=engine).is_alive is True


async def test_is_alive_false_when_no_native_object() -> None:
    page = NormalizedPage(engine_page=None, backend_page=None, cdp=None)
    assert page.is_alive is False
