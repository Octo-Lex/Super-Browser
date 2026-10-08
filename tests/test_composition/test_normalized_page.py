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
from unittest.mock import MagicMock

import pytest

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


# ============================================================================
# P6 adapter surfaces
# ============================================================================


class _RecordingBuffer:
    def __init__(self) -> None:
        self.attached: list[Any] = []

    def attach(self, native: Any) -> None:
        self.attached.append(native)


class _WithOn:
    def on(self, event: str, handler: Any) -> None:  # Playwright-style
        pass


class _BareNativePage:
    """No .on() — e.g. a Selenium WebDriver object."""


def test_attach_diagnostics_attaches_when_native_has_on() -> None:
    engine = _FakeEnginePage()
    engine.backend_page = _WithOn()
    page = NormalizedPage(engine_page=engine)
    buffer = _RecordingBuffer()

    page.attach_diagnostics(buffer)

    assert buffer.attached == [engine.backend_page]


def test_attach_diagnostics_noop_without_on() -> None:
    engine = _FakeEnginePage()
    engine.backend_page = _BareNativePage()
    page = NormalizedPage(engine_page=engine)
    buffer = _RecordingBuffer()

    page.attach_diagnostics(buffer)  # must not raise

    assert buffer.attached == []


def test_attach_diagnostics_noop_without_native_page() -> None:
    engine = _FakeEnginePage()
    engine.backend_page = None
    page = NormalizedPage(engine_page=engine)
    buffer = _RecordingBuffer()

    page.attach_diagnostics(buffer)

    assert buffer.attached == []


def test_stealth_bridge_derives_from_engine_page() -> None:
    engine = _FakeEnginePage()
    engine.stealth_bridge = object()
    assert NormalizedPage(engine_page=engine).stealth_bridge is engine.stealth_bridge
    assert NormalizedPage(engine_page=_FakeEnginePage()).stealth_bridge is None


class _HistoryNative:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def reload(self, wait_until: str = "load") -> None:
        self.calls.append(f"reload:{wait_until}")

    async def go_back(self, wait_until: str = "load") -> None:
        self.calls.append(f"go_back:{wait_until}")

    async def go_forward(self, wait_until: str = "load") -> None:
        self.calls.append(f"go_forward:{wait_until}")


async def test_history_navigation_delegates_to_native() -> None:
    engine = _FakeEnginePage()
    engine.backend_page = _HistoryNative()
    page = NormalizedPage(engine_page=engine)

    await page.reload(wait_until="domcontentloaded")
    await page.go_back(wait_until="load")
    await page.go_forward()

    assert engine.backend_page.calls == [
        "reload:domcontentloaded",
        "go_back:load",
        "go_forward:load",
    ]


async def test_history_navigation_raises_structured_without_native() -> None:
    page = NormalizedPage(engine_page=_FakeEnginePage())  # backend_page=None
    for call in (page.reload, page.go_back, page.go_forward):
        with pytest.raises(NotImplementedError):
            await call()


class _EvaluateEnginePage(_FakeEnginePage):
    """Returns a canned evaluate result (direct value or CDP envelope)."""

    def __init__(self, result: Any) -> None:
        super().__init__()
        self._result = result
        self.last_expr = ""

    async def evaluate(self, expression: str, *args: Any, **kwargs: Any) -> Any:
        self.last_expr = expression
        return self._result


async def test_selector_bounds_parses_box_from_direct_value() -> None:
    import json as _json

    box = {"x": 10.0, "y": 20.0, "w": 300.5, "h": 40.0}
    engine = _EvaluateEnginePage(_json.dumps(box))
    page = NormalizedPage(engine_page=engine)

    assert await page.selector_bounds("#btn") == (10, 20, 300, 40)
    # Selector must be JSON-encoded inside the expression, not interpolated.
    assert _json.dumps("#btn") in engine.last_expr
    assert "#btn" not in engine.last_expr.replace(_json.dumps("#btn"), "")


async def test_selector_bounds_handles_cdp_envelope() -> None:
    import json as _json

    box = _json.dumps({"x": 1, "y": 2, "w": 3, "h": 4})
    envelope = MagicMock()
    envelope.ok = True
    envelope.data = {"result": {"value": box}}
    engine = _EvaluateEnginePage(envelope)
    page = NormalizedPage(engine_page=engine)

    assert await page.selector_bounds("#x") == (1, 2, 3, 4)


async def test_selector_bounds_returns_none_for_missing_or_empty() -> None:
    assert await NormalizedPage(
        engine_page=_EvaluateEnginePage(None)
    ).selector_bounds("#gone") is None
    import json as _json

    assert await NormalizedPage(
        engine_page=_EvaluateEnginePage(
            _json.dumps({"x": 0, "y": 0, "w": 0, "h": 0})
        )
    ).selector_bounds("#hidden") is None


class _PollingNative:
    """Backend with NO wait_for_* family — exercises the polling fallback."""

    def __init__(self, ready_after: int) -> None:
        self.backend_page = self
        self.ready_after = ready_after
        self.reads = 0

    async def evaluate(self, expression: str, *args: Any, **kwargs: Any) -> Any:
        self.reads += 1
        if expression.strip() == "document.readyState":
            return "complete" if self.reads >= self.ready_after else "loading"
        if "innerText.includes" in expression:
            return self.reads >= self.ready_after
        return None

    @property
    def url(self) -> str:
        return "https://example.com/page"


async def test_wait_for_polling_fallback_ready_state() -> None:
    engine = _PollingNative(ready_after=2)
    page = NormalizedPage(engine_page=engine)

    matched = await page.wait_for(load_state="load", timeout_ms=2000)

    assert matched == "load_state"


async def test_wait_for_selector_polling_times_out_without_match() -> None:
    """A native-less backend whose evaluate cannot satisfy the selector must
    degrade to a structured TimeoutError — never an AttributeError."""
    engine = _PollingNative(ready_after=1)
    page = NormalizedPage(engine_page=engine)

    with pytest.raises(TimeoutError):
        await page.wait_for(selector="#never-matches", timeout_ms=400)


async def test_wait_for_text_polling_matches() -> None:
    engine = _PollingNative(ready_after=1)
    page = NormalizedPage(engine_page=engine)

    assert await page.wait_for(text="needle", timeout_ms=2000) == "text"


async def test_wait_for_networkidle_never_faked_without_native() -> None:
    page = NormalizedPage(engine_page=_PollingNative(1))

    with pytest.raises(NotImplementedError):
        await page.wait_for(load_state="networkidle", timeout_ms=500)


async def test_wait_for_timeout_raises_on_polling_backend() -> None:
    engine = _PollingNative(ready_after=10_000)
    page = NormalizedPage(engine_page=engine)

    with pytest.raises(TimeoutError):
        await page.wait_for(load_state="load", timeout_ms=300)


class _NativeWaitFor:
    """Backend WITH the full wait_for_* family — native path preserved."""

    def __init__(self) -> None:
        self.backend_page = self
        self.calls: list[str] = []

    async def wait_for_selector(self, selector: str, timeout: int = 10_000) -> None:
        self.calls.append(f"selector:{selector}")

    async def wait_for_function(self, expr: str, **kwargs: Any) -> None:
        self.calls.append("function")

    async def wait_for_url(self, url: str, timeout: int = 10_000) -> None:
        self.calls.append(f"url:{url}")

    async def wait_for_load_state(self, state: str, timeout: int = 10_000) -> None:
        self.calls.append(f"load_state:{state}")

    async def evaluate(self, expression: str, *args: Any, **kwargs: Any) -> Any:
        return None


async def test_wait_for_preserves_native_semantics_when_available() -> None:
    engine = _NativeWaitFor()
    engine.backend_page = engine
    page = NormalizedPage(engine_page=engine)

    await page.wait_for(selector="#x", timeout_ms=1234)
    await page.wait_for(text="needle", timeout_ms=1234)
    await page.wait_for(url="**/page", timeout_ms=1234)
    await page.wait_for(load_state="networkidle", timeout_ms=1234)

    assert engine.calls == [
        "selector:#x",
        "function",
        "url:**/page",
        "load_state:networkidle",
    ]
