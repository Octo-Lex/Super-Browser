"""Tests for P7.B — extract_image_text OCR inspect tool.

Verifies tool advertisement, argument validation, OCR-unavailable graceful
degradation, word normalization, confidence filtering, and redaction.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from super_browser.mcp_server import (
    DEFAULT_TOOL_NAMES,
    INSPECT_TOOL_NAMES,
    MCPAuthorizer,
    MCPBrowserRuntime,
    MCPSessionPolicy,
    ToolDispatcher,
)

FAKE_KEY = "sk-ant-api03-1234567890abcdefSECretKEY1234567890abcdef"


# ============================================================================
# Helpers
# ============================================================================


def _make_dispatcher(*, with_sm=False, ocr_words=None, ocr_available=True):
    """Build a dispatcher with a mock SuperBrowser that returns OCR results."""
    fake_sb = MagicMock()

    async def _extract_image_text(**kwargs):
        if not ocr_available:
            # Raise a RuntimeError that the handler catches and converts to
            # a structured ocr_unavailable error.
            raise OCRError("Tesseract OCR is not installed or the requested language pack is unavailable.")
        from super_browser.results.types import ActionResult as AR
        words = ocr_words or []
        text = " ".join(w["text"] for w in words)
        return AR(ok=True, data={
            "text": text,
            "words": words,
            "language": kwargs.get("language", "eng"),
            "source": {
                "selector": kwargs.get("selector"),
                "bounds": kwargs.get("bounds"),
                "full_page": kwargs.get("full_page", False),
            },
        })

    fake_sb.extract_image_text = AsyncMock(side_effect=_extract_image_text)
    fake_page = MagicMock()
    fake_page.is_alive = True
    fake_sb._page = fake_page

    runtime = MCPBrowserRuntime()
    runtime._sb = fake_sb

    if with_sm:
        from super_browser.security import SecurityConfig, SecurityManager
        sm = SecurityManager(SecurityConfig(
            redaction_enabled=True,
            domain_filter_enabled=False,
            injection_detection_enabled=False,
        ))
        return (
            ToolDispatcher(runtime, authorizer=MCPAuthorizer(MCPSessionPolicy(), security_manager=sm)),
            fake_sb,
        )
    return ToolDispatcher(runtime, authorizer=MCPAuthorizer(MCPSessionPolicy())), fake_sb


class OCRError(RuntimeError):
    """Raised when OCR is unavailable. Inherits from RuntimeError so the
    handler's `except RuntimeError` catches it."""
    pass


SAMPLE_WORDS = [
    {"text": "Almarai", "x": 120, "y": 340, "w": 80, "h": 18, "confidence": 0.96},
    {"text": "Fresh", "x": 205, "y": 340, "w": 45, "h": 18, "confidence": 0.94},
    {"text": "Milk", "x": 255, "y": 340, "w": 40, "h": 18, "confidence": 0.91},
    {"text": "2L", "x": 300, "y": 340, "w": 20, "h": 18, "confidence": 0.88},
    {"text": "SAR", "x": 120, "y": 370, "w": 30, "h": 18, "confidence": 0.72},
    {"text": "14.50", "x": 155, "y": 370, "w": 40, "h": 18, "confidence": 0.85},
]


# ============================================================================
# Tool advertisement
# ============================================================================


class TestToolAdvertisement:
    def test_advertised_in_default_tools(self):
        """extract_image_text is in the default inspect tool set."""
        assert "extract_image_text" in INSPECT_TOOL_NAMES
        assert "extract_image_text" in DEFAULT_TOOL_NAMES

    def test_not_action_tool(self):
        """OCR is inspect-tier, not gated behind allow_actions."""
        from super_browser.mcp_server import ACTION_TOOL_NAMES
        assert "extract_image_text" not in ACTION_TOOL_NAMES


# ============================================================================
# Argument validation (fails before screenshot/OCR)
# ============================================================================


class TestValidation:
    @pytest.mark.asyncio
    async def test_selector_and_bounds_mutually_exclusive(self):
        dispatcher, fake_sb = _make_dispatcher()
        result = await dispatcher.dispatch("extract_image_text", {
            "selector": "#product-img",
            "bounds": {"x": 0, "y": 0, "width": 100, "height": 100},
        })
        payload = json.loads(result[0].text)
        assert payload["ok"] is False
        assert "mutually exclusive" in str(payload).lower()
        fake_sb.extract_image_text.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_bounds_must_have_positive_width(self):
        dispatcher, fake_sb = _make_dispatcher()
        result = await dispatcher.dispatch("extract_image_text", {
            "bounds": {"x": 0, "y": 0, "width": 0, "height": 100},
        })
        payload = json.loads(result[0].text)
        assert payload["ok"] is False
        assert "width" in str(payload).lower()
        fake_sb.extract_image_text.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_bounds_must_have_positive_height(self):
        dispatcher, fake_sb = _make_dispatcher()
        result = await dispatcher.dispatch("extract_image_text", {
            "bounds": {"x": 0, "y": 0, "width": 100, "height": -5},
        })
        payload = json.loads(result[0].text)
        assert payload["ok"] is False
        assert "height" in str(payload).lower()
        fake_sb.extract_image_text.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_bounds_missing_field(self):
        dispatcher, fake_sb = _make_dispatcher()
        result = await dispatcher.dispatch("extract_image_text", {
            "bounds": {"x": 0, "y": 0, "width": 100},  # no height
        })
        payload = json.loads(result[0].text)
        assert payload["ok"] is False
        fake_sb.extract_image_text.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_invalid_language_rejected(self):
        dispatcher, fake_sb = _make_dispatcher()
        result = await dispatcher.dispatch("extract_image_text", {
            "language": "eng; rm -rf /",  # injection attempt
        })
        payload = json.loads(result[0].text)
        assert payload["ok"] is False
        assert "language" in str(payload).lower()
        fake_sb.extract_image_text.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_min_confidence_out_of_range(self):
        dispatcher, fake_sb = _make_dispatcher()
        result = await dispatcher.dispatch("extract_image_text", {
            "min_confidence": 1.5,
        })
        payload = json.loads(result[0].text)
        assert payload["ok"] is False
        assert "confidence" in str(payload).lower()
        fake_sb.extract_image_text.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_min_confidence_negative(self):
        dispatcher, fake_sb = _make_dispatcher()
        result = await dispatcher.dispatch("extract_image_text", {
            "min_confidence": -0.1,
        })
        payload = json.loads(result[0].text)
        assert payload["ok"] is False
        fake_sb.extract_image_text.assert_not_awaited()


# ============================================================================
# OCR unavailable — structured error
# ============================================================================


class TestOCRAvailable:
    @pytest.mark.asyncio
    async def test_ocr_unavailable_returns_structured_error(self):
        dispatcher, _ = _make_dispatcher(ocr_available=False)
        result = await dispatcher.dispatch("extract_image_text", {})
        payload = json.loads(result[0].text)
        assert payload["ok"] is False
        # Error must be structured with the ocr_unavailable kind.
        assert "ocr_unavailable" in payload
        assert isinstance(payload["ocr_unavailable"], str)

    @pytest.mark.asyncio
    async def test_ocr_unavailable_message_mentions_tesseract(self):
        dispatcher, _ = _make_dispatcher(ocr_available=False)
        result = await dispatcher.dispatch("extract_image_text", {})
        payload = json.loads(result[0].text)
        msg = payload.get("ocr_unavailable", "")
        assert "tesseract" in msg.lower() or "ocr" in msg.lower()


# ============================================================================
# OCR output shape and word normalization
# ============================================================================


class TestOCROutput:
    @pytest.mark.asyncio
    async def test_words_have_required_fields(self):
        dispatcher, _ = _make_dispatcher(ocr_words=SAMPLE_WORDS)
        result = await dispatcher.dispatch("extract_image_text", {})
        payload = json.loads(result[0].text)
        assert payload["ok"] is True
        for w in payload["data"]["words"]:
            for field in ("text", "x", "y", "w", "h", "confidence"):
                assert field in w, f"missing {field}"

    @pytest.mark.asyncio
    async def test_joined_text_present(self):
        dispatcher, _ = _make_dispatcher(ocr_words=SAMPLE_WORDS)
        result = await dispatcher.dispatch("extract_image_text", {})
        payload = json.loads(result[0].text)
        assert "text" in payload["data"]
        assert "Almarai" in payload["data"]["text"]

    @pytest.mark.asyncio
    async def test_language_echoed(self):
        dispatcher, _ = _make_dispatcher(ocr_words=SAMPLE_WORDS)
        result = await dispatcher.dispatch("extract_image_text", {"language": "eng+ara"})
        payload = json.loads(result[0].text)
        assert payload["data"]["language"] == "eng+ara"

    @pytest.mark.asyncio
    async def test_source_echoed(self):
        dispatcher, _ = _make_dispatcher(ocr_words=SAMPLE_WORDS)
        result = await dispatcher.dispatch("extract_image_text", {"full_page": True})
        payload = json.loads(result[0].text)
        assert payload["data"]["source"]["full_page"] is True


# ============================================================================
# min_confidence filtering
# ============================================================================


class TestConfidenceFilter:
    @pytest.mark.asyncio
    async def test_min_confidence_filters_low_words(self):
        dispatcher, _ = _make_dispatcher(ocr_words=SAMPLE_WORDS)
        result = await dispatcher.dispatch("extract_image_text", {"min_confidence": 0.90})
        payload = json.loads(result[0].text)
        words = payload["data"]["words"]
        # Only words with conf >= 0.90 should remain.
        for w in words:
            assert w["confidence"] >= 0.90
        # SAR (0.72) and 14.50 (0.85) should be filtered out.
        texts = [w["text"] for w in words]
        assert "SAR" not in texts
        assert "14.50" not in texts

    @pytest.mark.asyncio
    async def test_min_confidence_zero_keeps_all(self):
        dispatcher, _ = _make_dispatcher(ocr_words=SAMPLE_WORDS)
        result = await dispatcher.dispatch("extract_image_text", {"min_confidence": 0.0})
        payload = json.loads(result[0].text)
        assert len(payload["data"]["words"]) == len(SAMPLE_WORDS)


# ============================================================================
# Redaction
# ============================================================================


class TestRedaction:
    @pytest.mark.asyncio
    async def test_secret_in_text_redacted(self):
        """Secrets in OCR text output must be masked."""
        words_with_secret = [
            {"text": "Token:", "x": 0, "y": 0, "w": 50, "h": 20, "confidence": 0.95},
            {"text": FAKE_KEY, "x": 55, "y": 0, "w": 200, "h": 20, "confidence": 0.90},
        ]
        dispatcher, _ = _make_dispatcher(with_sm=True, ocr_words=words_with_secret)
        result = await dispatcher.dispatch("extract_image_text", {})
        payload = json.loads(result[0].text)
        # Secret must not appear in serialized output.
        assert FAKE_KEY not in json.dumps(payload)

    @pytest.mark.asyncio
    async def test_secret_in_word_text_redacted(self):
        """Secrets in individual word text must be masked."""
        words_with_secret = [
            {"text": FAKE_KEY, "x": 0, "y": 0, "w": 200, "h": 20, "confidence": 0.90},
        ]
        dispatcher, _ = _make_dispatcher(with_sm=True, ocr_words=words_with_secret)
        result = await dispatcher.dispatch("extract_image_text", {})
        payload = json.loads(result[0].text)
        assert FAKE_KEY not in json.dumps(payload)


# ============================================================================
# Default args (no selector, no bounds = viewport OCR)
# ============================================================================


class TestDefaultArgs:
    @pytest.mark.asyncio
    async def test_no_args_does_viewport_ocr(self):
        dispatcher, _ = _make_dispatcher(ocr_words=SAMPLE_WORDS)
        result = await dispatcher.dispatch("extract_image_text", {})
        payload = json.loads(result[0].text)
        assert payload["ok"] is True
        assert payload["data"]["source"]["selector"] is None
        assert payload["data"]["source"]["bounds"] is None
        assert payload["data"]["source"]["full_page"] is False


# ============================================================================
# Language type validation
# ============================================================================


class TestLanguageValidation:
    @pytest.mark.asyncio
    async def test_non_string_language_rejected(self):
        """Non-string language must fail before regex, not crash."""
        dispatcher, fake_sb = _make_dispatcher()
        result = await dispatcher.dispatch("extract_image_text", {"language": 123})
        payload = json.loads(result[0].text)
        assert payload["ok"] is False
        assert "language" in str(payload).lower()
        fake_sb.extract_image_text.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_list_language_rejected(self):
        dispatcher, fake_sb = _make_dispatcher()
        result = await dispatcher.dispatch("extract_image_text", {"language": ["eng"]})
        payload = json.loads(result[0].text)
        assert payload["ok"] is False
        fake_sb.extract_image_text.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_string_language_rejected(self):
        dispatcher, fake_sb = _make_dispatcher()
        result = await dispatcher.dispatch("extract_image_text", {"language": ""})
        payload = json.loads(result[0].text)
        assert payload["ok"] is False
        fake_sb.extract_image_text.assert_not_awaited()


# ============================================================================
# Facade-level: real OCR output normalization (decimal confidence, -1)
# ============================================================================


class TestFacadeOCRNormalization:
    """Tests that exercise the real facade OCR path with mocked pytesseract
    output containing decimal confidence strings and -1 values."""

    @pytest.mark.asyncio
    async def test_decimal_confidence_parsed_correctly(self):
        """pytesseract can return conf as '96.590439' — must not crash."""
        import sys
        import types

        from super_browser import SuperBrowser

        sb = SuperBrowser()
        sb._page = MagicMock()
        sb._page.screenshot = AsyncMock(return_value=b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
        sb._page.is_alive = True
        sb._page.selector_bounds = AsyncMock(return_value=None)

        mock_data = {
            "text": ["Milk", "2L", "", "SAR"],
            "conf": ["96.590439", "88.0", "-1", "72.5"],
            "left": [10, 60, 0, 10],
            "top": [10, 10, 0, 30],
            "width": [40, 20, 0, 30],
            "height": [18, 18, 0, 18],
        }

        fake_pytesseract = types.ModuleType("pytesseract")
        fake_pytesseract.image_to_data = MagicMock(return_value=mock_data)
        fake_pytesseract.Output = MagicMock(DICT="dict")

        # Mock PIL to avoid needing real PNG bytes.
        mock_img = MagicMock()
        mock_img.convert = MagicMock(return_value=mock_img)
        mock_img.crop = MagicMock(return_value=mock_img)
        fake_pil = types.ModuleType("PIL")
        fake_pil_image = types.ModuleType("PIL.Image")
        fake_pil_image.open = MagicMock(return_value=mock_img)
        fake_pil.Image = fake_pil_image

        orig_modules = dict(sys.modules)
        sys.modules["pytesseract"] = fake_pytesseract
        sys.modules["PIL"] = fake_pil
        sys.modules["PIL.Image"] = fake_pil_image

        try:
            result = await sb.extract_image_text()
        finally:
            sys.modules.clear()
            sys.modules.update(orig_modules)

        # Should not crash, and should normalize confidence correctly.
        assert result.ok is True
        words = result.data["words"]
        # 3 words (empty text and -1 conf are skipped).
        assert len(words) == 3
        # Decimal confidence normalized correctly.
        assert words[0]["text"] == "Milk"
        assert abs(words[0]["confidence"] - 0.9659) < 0.01
        assert words[1]["text"] == "2L"
        assert abs(words[1]["confidence"] - 0.88) < 0.01

    @pytest.mark.asyncio
    async def test_negative_confidence_skipped(self):
        """conf=-1 entries must be skipped, not crash."""
        import sys
        import types

        from super_browser import SuperBrowser

        sb = SuperBrowser()
        sb._page = MagicMock()
        sb._page.screenshot = AsyncMock(return_value=b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
        sb._page.is_alive = True
        sb._page.selector_bounds = AsyncMock(return_value=None)

        mock_data = {
            "text": ["Hello", "", "World"],
            "conf": ["95.0", "-1", "-1"],
            "left": [10, 0, 10],
            "top": [10, 0, 30],
            "width": [50, 0, 50],
            "height": [18, 0, 18],
        }

        fake_pytesseract = types.ModuleType("pytesseract")
        fake_pytesseract.image_to_data = MagicMock(return_value=mock_data)
        fake_pytesseract.Output = MagicMock(DICT="dict")

        mock_img = MagicMock()
        mock_img.convert = MagicMock(return_value=mock_img)
        mock_img.crop = MagicMock(return_value=mock_img)
        fake_pil = types.ModuleType("PIL")
        fake_pil_image = types.ModuleType("PIL.Image")
        fake_pil_image.open = MagicMock(return_value=mock_img)
        fake_pil.Image = fake_pil_image

        orig_modules = dict(sys.modules)
        sys.modules["pytesseract"] = fake_pytesseract
        sys.modules["PIL"] = fake_pil
        sys.modules["PIL.Image"] = fake_pil_image

        try:
            result = await sb.extract_image_text()
        finally:
            sys.modules.clear()
            sys.modules.update(orig_modules)

        assert result.ok is True
        words = result.data["words"]
        # Only "Hello" survives (World has conf=-1).
        assert len(words) == 1
        assert words[0]["text"] == "Hello"


# ============================================================================
# Facade-level: selector resolution (element bounds → crop → OCR)
# ============================================================================


class TestFacadeSelectorResolution:
    """Verify selector resolves element bounds and crops before OCR."""

    @pytest.mark.asyncio
    async def test_selector_resolves_element_bounds(self):
        """When selector is provided, the facade resolves element bounding box
        and crops the screenshot before OCR."""
        import sys
        import types

        from super_browser import SuperBrowser

        sb = SuperBrowser()
        sb._page = MagicMock()
        sb._page.screenshot = AsyncMock(return_value=b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
        sb._page.is_alive = True

        # P6: the crop region resolves through NormalizedPage.selector_bounds.
        sb._page.selector_bounds = AsyncMock(return_value=(100, 200, 300, 150))

        mock_data = {
            "text": ["Product"],
            "conf": ["95.0"],
            "left": [0],
            "top": [0],
            "width": [60],
            "height": [18],
        }

        fake_pytesseract = types.ModuleType("pytesseract")
        fake_pytesseract.image_to_data = MagicMock(return_value=mock_data)
        fake_pytesseract.Output = MagicMock(DICT="dict")

        mock_img = MagicMock()
        mock_img.convert = MagicMock(return_value=mock_img)
        mock_img.crop = MagicMock(return_value=mock_img)
        fake_pil = types.ModuleType("PIL")
        fake_pil_image = types.ModuleType("PIL.Image")
        fake_pil_image.open = MagicMock(return_value=mock_img)
        fake_pil.Image = fake_pil_image

        orig_modules = dict(sys.modules)
        sys.modules["pytesseract"] = fake_pytesseract
        sys.modules["PIL"] = fake_pil
        sys.modules["PIL.Image"] = fake_pil_image

        try:
            result = await sb.extract_image_text(selector="#product-img")
        finally:
            sys.modules.clear()
            sys.modules.update(orig_modules)

        assert result.ok is True
        # Selector was resolved through the adapter.
        sb._page.selector_bounds.assert_awaited_once_with("#product-img")
        # Source echoes the selector.
        assert result.data["source"]["selector"] == "#product-img"

    @pytest.mark.asyncio
    async def test_selector_not_found_falls_back_to_viewport(self):
        """When selector doesn't match any element, OCR runs on the viewport."""
        import sys
        import types

        from super_browser import SuperBrowser

        sb = SuperBrowser()
        sb._page = MagicMock()
        sb._page.screenshot = AsyncMock(return_value=b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
        sb._page.is_alive = True
        sb._page.selector_bounds = AsyncMock(return_value=None)  # not found

        mock_data = {
            "text": ["Fallback"],
            "conf": ["90.0"],
            "left": [0],
            "top": [0],
            "width": [70],
            "height": [18],
        }

        fake_pytesseract = types.ModuleType("pytesseract")
        fake_pytesseract.image_to_data = MagicMock(return_value=mock_data)
        fake_pytesseract.Output = MagicMock(DICT="dict")

        mock_img = MagicMock()
        mock_img.convert = MagicMock(return_value=mock_img)
        mock_img.crop = MagicMock(return_value=mock_img)
        fake_pil = types.ModuleType("PIL")
        fake_pil_image = types.ModuleType("PIL.Image")
        fake_pil_image.open = MagicMock(return_value=mock_img)
        fake_pil.Image = fake_pil_image

        orig_modules = dict(sys.modules)
        sys.modules["pytesseract"] = fake_pytesseract
        sys.modules["PIL"] = fake_pil
        sys.modules["PIL.Image"] = fake_pil_image

        try:
            result = await sb.extract_image_text(selector="#nonexistent")
        finally:
            sys.modules.clear()
            sys.modules.update(orig_modules)

        # Should not crash — falls back to viewport OCR.
        assert result.ok is True
        assert len(result.data["words"]) == 1
        assert result.data["source"]["selector"] == "#nonexistent"

    @pytest.mark.asyncio
    async def test_selector_resolves_through_normalized_page(self):
        """P6: the facade resolves the crop region via
        NormalizedPage.selector_bounds; backend_page and engine_page must
        NOT be touched (the old query_selector path is gone).

        This test fails on any code that reaches raw pages directly."""
        import sys
        import types
        from unittest.mock import PropertyMock

        from super_browser import SuperBrowser

        sb = SuperBrowser()
        sb._page = MagicMock()
        sb._page.screenshot = AsyncMock(return_value=b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
        sb._page.is_alive = True

        # Any raw-page reach fails loudly.
        type(sb._page).backend_page = PropertyMock(
            side_effect=AssertionError("facade must not use backend_page"),
        )

        sb._page.selector_bounds = AsyncMock(return_value=(10, 10, 200, 100))

        mock_data = {
            "text": ["Text"],
            "conf": ["95.0"],
            "left": [0], "top": [0], "width": [30], "height": [18],
        }

        fake_pytesseract = types.ModuleType("pytesseract")
        fake_pytesseract.image_to_data = MagicMock(return_value=mock_data)
        fake_pytesseract.Output = MagicMock(DICT="dict")

        mock_img = MagicMock()
        mock_img.convert = MagicMock(return_value=mock_img)
        mock_img.crop = MagicMock(return_value=mock_img)
        fake_pil = types.ModuleType("PIL")
        fake_pil_image = types.ModuleType("PIL.Image")
        fake_pil_image.open = MagicMock(return_value=mock_img)
        fake_pil.Image = fake_pil_image

        orig_modules = dict(sys.modules)
        sys.modules["pytesseract"] = fake_pytesseract
        sys.modules["PIL"] = fake_pil
        sys.modules["PIL.Image"] = fake_pil_image

        try:
            result = await sb.extract_image_text(selector="#img")
        finally:
            sys.modules.clear()
            sys.modules.update(orig_modules)

        assert result.ok is True
        # selector_bounds was called with the selector through the adapter.
        sb._page.selector_bounds.assert_awaited_once_with("#img")
