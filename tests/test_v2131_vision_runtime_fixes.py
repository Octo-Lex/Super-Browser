"""Tests for v2.13.1 hotfix — real vision runtime hardening.

Two latent bugs in v2.13.0 that the FakeVisionProvider tests missed:

1. ``_capture_region_bytes`` called ``self._page.screenshot(format=...)``.
   ``PageHandle.screenshot()`` translates ``format``→``type``, but a raw
   Playwright/Patchright ``Page`` forwards kwargs verbatim and rejects
   ``format=`` (it wants ``type=``). This broke both ``analyze_image`` and
   ``extract_image_text`` on first use after ``start()``.

2. ``OpenAIResponseProvider.analyze()`` hardcoded
   ``response_format={"type": "json_object"}``, which official OpenAI accepts
   but OpenAI-compatible servers (LM Studio, etc.) reject with a 400. The
   provider returned ``found=False`` → ``vision_unavailable``, even with a
   capable model answering.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from super_browser.interaction.types import VisionRequest

# ============================================================================
# Fix 1 — screenshot keyword compatibility
# ============================================================================


class _RawPlaywrightPage:
    """Mimics a raw Playwright/Patchright Page: screenshot() takes ``type=``."""

    def __init__(self, png_bytes: bytes = b"\x89PNG fake") -> None:
        self._png = png_bytes
        self.last_kwargs: dict[str, Any] = {}

    async def screenshot(self, **kwargs: Any) -> bytes:
        # Playwright raises if it gets an unknown kwarg like 'format'.
        if "format" in kwargs:
            raise TypeError(
                "screenshot() got an unexpected keyword argument 'format'"
            )
        self.last_kwargs = kwargs
        return self._png


class _StrictPageHandleLike:
    """Mimics the real PageHandle.screenshot() public signature exactly.

    Accepts ``format=`` and ``quality=`` but does NOT accept the Playwright-
    internal ``type=`` kwarg — raising TypeError on it, the way the strict
    public wrapper would. This is what forces the fallback path.
    """

    def __init__(self, png_bytes: bytes = b"\x89PNG fake") -> None:
        self._png = png_bytes
        self.last_kwargs: dict[str, Any] = {}

    async def screenshot(
        self,
        *,
        path: Any = None,
        full_page: bool = False,
        format: str = "png",
        quality: int | None = None,
    ) -> bytes:
        # The real PageHandle.screenshot() has a fixed public signature and
        # rejects unknown kwargs. A raw 'type=' call raises before reaching
        # the body; we simulate that by inspecting what we received.
        self.last_kwargs = {
            "path": path, "full_page": full_page,
            "format": format, "quality": quality,
        }
        return self._png


class TestCaptureRegionKeywordCompat:
    """_capture_region_bytes must work with raw (type=) and wrapper (format=) pages."""

    @pytest.mark.asyncio
    async def test_raw_page_uses_type_not_format(self):
        from super_browser import SuperBrowser

        sb = SuperBrowser()
        raw = _RawPlaywrightPage()
        sb._page = raw
        # Must not raise.
        img_bytes, mime = await sb._capture_region_bytes(format="png")
        assert img_bytes == b"\x89PNG fake"
        assert mime == "image/png"
        # The raw page received type=, never format=.
        assert raw.last_kwargs.get("type") == "png"
        assert "format" not in raw.last_kwargs

    @pytest.mark.asyncio
    async def test_raw_page_jpeg_uses_type_jpeg(self):
        from super_browser import SuperBrowser

        sb = SuperBrowser()
        raw = _RawPlaywrightPage(b"\xff\xd8 JPEG fake")
        sb._page = raw
        img_bytes, mime = await sb._capture_region_bytes(format="jpeg", quality=70)
        assert mime == "image/jpeg"
        assert raw.last_kwargs.get("type") == "jpeg"
        assert raw.last_kwargs.get("quality") == 70

    @pytest.mark.asyncio
    async def test_strict_wrapper_page_receives_format_not_type(self):
        """A strict PageHandle (rejects type=) must receive format= via fallback."""
        from super_browser import SuperBrowser

        sb = SuperBrowser()
        wrapper = _StrictPageHandleLike()
        sb._page = wrapper
        img_bytes, mime = await sb._capture_region_bytes(format="png")
        assert img_bytes == b"\x89PNG fake"
        assert mime == "image/png"
        # The wrapper received format= (via the fallback), NOT type=.
        assert wrapper.last_kwargs.get("format") == "png"
        assert "type" not in wrapper.last_kwargs

    @pytest.mark.asyncio
    async def test_strict_wrapper_jpeg_receives_format_jpeg(self):
        from super_browser import SuperBrowser

        sb = SuperBrowser()
        wrapper = _StrictPageHandleLike(b"\xff\xd8 JPEG fake")
        sb._page = wrapper
        img_bytes, mime = await sb._capture_region_bytes(format="jpeg", quality=65)
        assert mime == "image/jpeg"
        assert wrapper.last_kwargs.get("format") == "jpeg"
        assert wrapper.last_kwargs.get("quality") == 65

    @pytest.mark.asyncio
    async def test_png_mime_returned(self):
        from super_browser import SuperBrowser

        sb = SuperBrowser()
        sb._page = _RawPlaywrightPage()
        _, mime = await sb._capture_region_bytes(format="png")
        assert mime == "image/png"

    @pytest.mark.asyncio
    async def test_jpeg_mime_returned(self):
        from super_browser import SuperBrowser

        sb = SuperBrowser()
        sb._page = _RawPlaywrightPage()
        _, mime = await sb._capture_region_bytes(format="jpeg", quality=80)
        assert mime == "image/jpeg"


# ============================================================================
# Fix 2 — OpenAI-compatible response_format fallback
# ============================================================================


class _MockChatCompletions:
    """Mock for client.chat.completions.create that can reject response_format."""

    def __init__(
        self,
        *,
        reject_response_format: bool = False,
        json_answer: str | None = None,
        text_answer: str | None = None,
        reject_all: bool = False,
        reject_message: str = "unrelated error",
    ) -> None:
        self._reject_rf = reject_response_format
        self._json_answer = json_answer or '{"answer": "red", "confidence": 0.9}'
        self._text_answer = text_answer or "The color is red."
        self._reject_all = reject_all
        self._reject_message = reject_message
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        # If asked to reject ALL calls with a non-response_format error.
        if self._reject_all:
            raise RuntimeError(self._reject_message)
        # If response_format was passed and we're configured to reject it.
        if self._reject_rf and "response_format" in kwargs:
            raise ValueError(
                "'response_format.type' must be 'json_schema' or 'text'"
            )
        # Otherwise succeed. Return JSON if response_format was honored,
        # free text otherwise.
        text = self._json_answer if "response_format" in kwargs else self._text_answer
        msg = MagicMock()
        msg.content = text
        choice = MagicMock()
        choice.message = msg
        resp = MagicMock()
        resp.choices = [choice]
        return resp


class _MockAsyncOpenAI:
    def __init__(self, completions: _MockChatCompletions) -> None:
        self.chat = MagicMock()
        self.chat.completions = completions


class TestOpenAIResponseFormatFallback:
    """analyze() retries without response_format when the server rejects it."""

    @pytest.mark.asyncio
    async def test_json_mode_used_first_for_official_openai(self):
        """When the server accepts response_format, it's used (official OpenAI)."""
        from super_browser.vision.providers import OpenAIResponseProvider

        comps = _MockChatCompletions(reject_response_format=False)
        provider = OpenAIResponseProvider(api_key="sk-test", model="gpt-4o-mini")
        provider._client = _MockAsyncOpenAI(comps)

        req = VisionRequest(
            screenshot=b"\x89PNG fake", element_description="what color?",
            page_url="", viewport_size=(10, 10),
        )
        resp = await provider.analyze(req)

        assert resp.found is True
        assert len(comps.calls) == 1
        assert comps.calls[0].get("response_format") == {"type": "json_object"}

    @pytest.mark.asyncio
    async def test_rejected_response_format_triggers_retry_without_it(self):
        """LM Studio rejects json_object → provider retries without response_format."""
        from super_browser.vision.providers import OpenAIResponseProvider

        comps = _MockChatCompletions(reject_response_format=True)
        provider = OpenAIResponseProvider(api_key="lm-studio", model="glm-ocr")
        provider._client = _MockAsyncOpenAI(comps)

        req = VisionRequest(
            screenshot=b"\x89PNG fake", element_description="what color?",
            page_url="", viewport_size=(10, 10),
        )
        resp = await provider.analyze(req)

        assert resp.found is True
        assert resp.raw_response is not None
        # Two calls: first with response_format (rejected), second without.
        assert len(comps.calls) == 2
        assert "response_format" in comps.calls[0]
        assert "response_format" not in comps.calls[1]

    @pytest.mark.asyncio
    async def test_arbitrary_error_not_blindly_retried(self):
        """A non-response_format error must NOT trigger the retry path."""
        from super_browser.vision.providers import OpenAIResponseProvider

        comps = _MockChatCompletions(
            reject_all=True, reject_message="connection timeout",
        )
        provider = OpenAIResponseProvider(api_key="sk-test", model="gpt-4o-mini")
        provider._client = _MockAsyncOpenAI(comps)

        req = VisionRequest(
            screenshot=b"\x89PNG fake", element_description="what?",
            page_url="", viewport_size=(10, 10),
        )
        resp = await provider.analyze(req)

        # Must not retry — returned found=False after the single failed call.
        assert resp.found is False
        assert len(comps.calls) == 1

    @pytest.mark.asyncio
    async def test_retry_returns_found_true_with_raw_response(self):
        from super_browser.vision.providers import OpenAIResponseProvider

        comps = _MockChatCompletions(
            reject_response_format=True, text_answer="the square is red",
        )
        provider = OpenAIResponseProvider(api_key="lm-studio", model="glm-ocr")
        provider._client = _MockAsyncOpenAI(comps)

        req = VisionRequest(
            screenshot=b"\x89PNG fake", element_description="color?",
            page_url="", viewport_size=(10, 10),
        )
        resp = await provider.analyze(req)
        assert resp.found is True
        assert resp.raw_response == "the square is red"
        assert resp.provider == "openai"


# ============================================================================
# Controller still parses both JSON and free-text answers
# ============================================================================


class TestControllerAnswerParsing:
    """analyze_state parses JSON when present, falls back to raw text otherwise."""

    @pytest.mark.asyncio
    async def test_json_answer_parsed(self):
        from super_browser.vision import VisionController
        from super_browser.vision.factory import VisionProviderFactory
        from super_browser.vision.providers import OpenAIResponseProvider

        comps = _MockChatCompletions(
            json_answer='{"answer": "blue", "confidence": 0.77}',
        )
        provider = OpenAIResponseProvider(api_key="sk-test", model="gpt-4o-mini")
        provider._client = _MockAsyncOpenAI(comps)
        factory = VisionProviderFactory(providers={"openai": provider})
        controller = VisionController(factory=factory)

        si = await controller.analyze_state(b"\x89PNG fake", "what color?")
        assert si.answer == "blue"
        assert si.confidence == 0.77
        assert si.provider == "openai"

    @pytest.mark.asyncio
    async def test_free_text_answer_used_when_non_json(self):
        from super_browser.vision import VisionController
        from super_browser.vision.factory import VisionProviderFactory
        from super_browser.vision.providers import OpenAIResponseProvider

        comps = _MockChatCompletions(
            reject_response_format=True, text_answer="The square is green.",
        )
        provider = OpenAIResponseProvider(api_key="lm-studio", model="glm-ocr")
        provider._client = _MockAsyncOpenAI(comps)
        factory = VisionProviderFactory(providers={"openai": provider})
        controller = VisionController(factory=factory)

        si = await controller.analyze_state(b"\x89PNG fake", "what color?")
        # Free-text path: answer is the raw text.
        assert "green" in si.answer
        assert si.provider == "openai"
