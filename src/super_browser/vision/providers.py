"""Vision providers — abstract base and concrete implementations."""

from __future__ import annotations

import base64
import json
import logging
import re
import time
from abc import abstractmethod
from typing import Any, Optional

from super_browser.interaction.types import VisionRequest, VisionResponse
from super_browser.interaction.vision import VisionProvider
from super_browser.vision.coords import resize_coordinates

logger = logging.getLogger(__name__)


class VisionProviderBase(VisionProvider):
    """Extended ABC adding health_check, cost, and resolution metadata."""

    @abstractmethod
    async def health_check(self) -> bool: ...

    @property
    @abstractmethod
    def cost_per_1k_tokens(self) -> float: ...

    @property
    @abstractmethod
    def default_resolution(self) -> tuple[int, int]: ...


class AnthropicCUAProvider(VisionProviderBase):
    """Anthropic Computer Use API provider."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "claude-sonnet-4-20250514",
        max_tokens: int = 4096,
    ) -> None:
        self._model = model
        self._max_tokens = max_tokens
        self._api_key = api_key
        self._client: Any = None
        try:
            import anthropic
            self._client = anthropic.AsyncAnthropic(api_key=api_key)
        except ImportError:
            pass

    @property
    def name(self) -> str:
        return "anthropic"

    @property
    def model_id(self) -> str:
        return self._model

    @property
    def cost_per_1k_tokens(self) -> float:
        return 3.0

    @property
    def default_resolution(self) -> tuple[int, int]:
        return (1280, 800)

    async def locate(self, request: VisionRequest) -> VisionResponse:
        if self._client is None:
            return VisionResponse(found=False, model=self._model, provider=self.name)
        start = time.monotonic()
        try:
            b64 = base64.b64encode(request.screenshot).decode()
            message = await self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                tools=[{
                    "type": "computer_20250124",
                    "name": "computer",
                    "display_width_px": self.default_resolution[0],
                    "display_height_px": self.default_resolution[1],
                }],
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": request.mime_type,
                                "data": b64,
                            },
                        },
                        {
                            "type": "text",
                            "text": f"Locate the element: {request.element_description}. Return the click coordinates.",
                        },
                    ],
                }],
            )
            coords = self._parse_cua_response(message)
            if coords is None:
                dur = (time.monotonic() - start) * 1000
                return VisionResponse(found=False, model=self._model, duration_ms=dur, provider=self.name)
            model_x, model_y = coords
            screen_x, screen_y = resize_coordinates(
                model_x, model_y, self.default_resolution, request.viewport_size,
            )
            dur = (time.monotonic() - start) * 1000
            return VisionResponse(
                found=True, x=float(screen_x), y=float(screen_y),
                confidence=0.85, model=self._model,
                duration_ms=dur, provider=self.name,
            )
        except Exception as exc:
            logger.warning("Anthropic CUA error: %s", exc)
            dur = (time.monotonic() - start) * 1000
            return VisionResponse(found=False, model=self._model, duration_ms=dur, provider=self.name)

    async def analyze(self, request: VisionRequest) -> VisionResponse:
        """Answer a free-form question about the screenshot via plain messages
        (no computer tool). The element_description carries the question."""
        if self._client is None:
            return VisionResponse(found=False, model=self._model, provider=self.name)
        start = time.monotonic()
        try:
            b64 = base64.b64encode(request.screenshot).decode()
            message = await self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": request.mime_type,
                                "data": b64,
                            },
                        },
                        {
                            "type": "text",
                            "text": (
                                f"{request.element_description}\n\n"
                                'Return JSON: {"answer": str, "confidence": float}'
                            ),
                        },
                    ],
                }],
            )
            # Extract text from the first text content block.
            text = ""
            for block in getattr(message, "content", []):
                if getattr(block, "type", None) == "text":
                    text = getattr(block, "text", "") or ""
                    break
            dur = (time.monotonic() - start) * 1000
            return VisionResponse(
                found=True, raw_response=text, model=self._model,
                confidence=0.8, duration_ms=dur, provider=self.name,
            )
        except Exception as exc:
            logger.warning("Anthropic analyze error: %s", exc)
            dur = (time.monotonic() - start) * 1000
            return VisionResponse(found=False, model=self._model, duration_ms=dur, provider=self.name)

    async def health_check(self) -> bool:
        if self._client is None:
            return False
        try:
            await self._client.messages.create(
                model=self._model,
                max_tokens=10,
                messages=[{"role": "user", "content": "ping"}],
            )
            return True
        except Exception:
            return False

    def _parse_cua_response(self, message: Any) -> Optional[tuple[int, int]]:
        for block in getattr(message, "content", []):
            if block.type == "tool_use" and block.name == "computer":
                inp = block.input
                if isinstance(inp, dict):
                    coords = inp.get("coordinate")
                    if coords and len(coords) >= 2:
                        return (int(coords[0]), int(coords[1]))
                    action = inp.get("action", "")
                    if action in ("left_click", "click"):
                        x, y = inp.get("coordinate", [0, 0])
                        return (int(x), int(y))
        return None


class OpenAIResponseProvider(VisionProviderBase):
    """OpenAI Responses API provider."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "gpt-4o-mini",
        max_tokens: int = 4096,
    ) -> None:
        self._model = model
        self._max_tokens = max_tokens
        self._api_key = api_key
        self._client: Any = None
        try:
            from openai import AsyncOpenAI
            self._client = AsyncOpenAI(api_key=api_key)
        except (ImportError, Exception):
            pass

    @property
    def name(self) -> str:
        return "openai"

    @property
    def model_id(self) -> str:
        return self._model

    @property
    def cost_per_1k_tokens(self) -> float:
        return 0.15

    @property
    def default_resolution(self) -> tuple[int, int]:
        return (1280, 720)

    async def locate(self, request: VisionRequest) -> VisionResponse:
        if self._client is None:
            return VisionResponse(found=False, model=self._model, provider=self.name)
        start = time.monotonic()
        try:
            b64 = base64.b64encode(request.screenshot).decode()
            data_uri = f"data:{request.mime_type};base64,{b64}"
            response = await self._client.chat.completions.create(
                model=self._model,
                max_tokens=self._max_tokens,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": data_uri}},
                        {
                            "type": "text",
                            "text": (
                                f"Locate the element: {request.element_description}. "
                                'Return JSON: {"found": true, "x": <int>, "y": <int>, "confidence": <float>}'
                            ),
                        },
                    ],
                }],
                response_format={"type": "json_object"},
            )
            text = response.choices[0].message.content
            coords = self._parse_json_response(text)
            if coords is None:
                dur = (time.monotonic() - start) * 1000
                return VisionResponse(found=False, model=self._model, duration_ms=dur, provider=self.name)
            model_x, model_y, conf = coords
            screen_x, screen_y = resize_coordinates(
                model_x, model_y, self.default_resolution, request.viewport_size,
            )
            dur = (time.monotonic() - start) * 1000
            return VisionResponse(
                found=True, x=float(screen_x), y=float(screen_y),
                confidence=conf, model=self._model,
                duration_ms=dur, provider=self.name,
            )
        except Exception as exc:
            logger.warning("OpenAI provider error: %s", exc)
            dur = (time.monotonic() - start) * 1000
            return VisionResponse(found=False, model=self._model, duration_ms=dur, provider=self.name)

    async def analyze(self, request: VisionRequest) -> VisionResponse:
        """Answer a free-form question about the screenshot. The
        element_description carries the question. Prefers JSON output mode,
        but falls back to plain text when the server rejects
        ``response_format`` (e.g. LM Studio and other OpenAI-compatible
        servers that only accept ``json_schema`` or ``text``). The controller
        handles both JSON and free-text answers."""
        if self._client is None:
            return VisionResponse(found=False, model=self._model, provider=self.name)
        start = time.monotonic()
        b64 = base64.b64encode(request.screenshot).decode()
        data_uri = f"data:{request.mime_type};base64,{b64}"
        messages = [{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": data_uri}},
                {
                    "type": "text",
                    "text": (
                        f"{request.element_description}\n\n"
                        'Return JSON: {"answer": str, "confidence": float}'
                    ),
                },
            ],
        }]
        common = {"model": self._model, "max_tokens": self._max_tokens, "messages": messages}
        try:
            # First attempt: strict JSON mode (official OpenAI).
            response = await self._client.chat.completions.create(
                **common, response_format={"type": "json_object"},
            )
        except Exception as exc:
            # Many OpenAI-compatible servers reject response_format=json_object.
            # Retry without it — the model returns free text, which the
            # controller parses via its non-JSON fallback path.
            if "response_format" not in str(exc):
                logger.warning("OpenAI analyze error: %s", exc)
                dur = (time.monotonic() - start) * 1000
                return VisionResponse(found=False, model=self._model, duration_ms=dur, provider=self.name)
            try:
                response = await self._client.chat.completions.create(**common)
            except Exception as exc2:
                logger.warning("OpenAI analyze fallback error: %s", exc2)
                dur = (time.monotonic() - start) * 1000
                return VisionResponse(found=False, model=self._model, duration_ms=dur, provider=self.name)
        text = response.choices[0].message.content
        dur = (time.monotonic() - start) * 1000
        return VisionResponse(
            found=True, raw_response=text, model=self._model,
            confidence=0.8, duration_ms=dur, provider=self.name,
        )

    async def health_check(self) -> bool:
        if self._client is None:
            return False
        try:
            await self._client.chat.completions.create(
                model=self._model,
                max_tokens=10,
                messages=[{"role": "user", "content": "ping"}],
            )
            return True
        except Exception:
            return False

    def _parse_json_response(self, text: Optional[str]) -> Optional[tuple[int, int, float]]:
        if not text:
            return None
        try:
            data = json.loads(text)
            if data.get("found") and "x" in data and "y" in data:
                return (int(data["x"]), int(data["y"]), float(data.get("confidence", 0.8)))
        except (json.JSONDecodeError, ValueError):
            pass
        return None


class UITARSProvider(VisionProviderBase):
    """Local UI-TARS model provider for visual grounding."""

    def __init__(
        self,
        model_path: Optional[str] = None,
        device: str = "cuda",
        max_new_tokens: int = 512,
    ) -> None:
        self._model_path = model_path
        self._device = device
        self._max_new_tokens = max_new_tokens
        self._model: Any = None
        self._processor: Any = None
        self._loaded = False
        try:
            self._try_load()
        except ImportError:
            pass

    def _try_load(self) -> None:
        try:
            from transformers import AutoModelForCausalLM, AutoProcessor
            path = self._model_path or "UI-TARS-7B"
            self._processor = AutoProcessor.from_pretrained(path, trust_remote_code=True)
            self._model = AutoModelForCausalLM.from_pretrained(
                path, trust_remote_code=True, torch_dtype="auto",
            ).to(self._device)
            self._loaded = True
        except Exception:
            self._loaded = False

    @property
    def name(self) -> str:
        return "uitars"

    @property
    def model_id(self) -> str:
        return self._model_path or "UI-TARS-7B"

    @property
    def cost_per_1k_tokens(self) -> float:
        return 0.0

    @property
    def default_resolution(self) -> tuple[int, int]:
        return (1280, 720)

    async def locate(self, request: VisionRequest) -> VisionResponse:
        if not self._loaded:
            return VisionResponse(found=False, model=self.model_id)
        start = time.monotonic()
        try:
            from io import BytesIO

            from PIL import Image
            img = Image.open(BytesIO(request.screenshot)).convert("RGB")
            prompt = (
                f"Locate the element in this screenshot: {request.element_description}\n"
                "Output the coordinates in the format: <point>x y</point>"
            )
            inputs = self._processor(text=prompt, images=img, return_tensors="pt").to(self._device)
            output = self._model.generate(**inputs, max_new_tokens=self._max_new_tokens)
            decoded = self._processor.decode(output[0], skip_special_tokens=False)
            coords = self._parse_point_output(decoded)
            if coords is None:
                dur = (time.monotonic() - start) * 1000
                return VisionResponse(found=False, model=self.model_id, duration_ms=dur)
            model_x, model_y = coords
            screen_x, screen_y = resize_coordinates(
                model_x, model_y, self.default_resolution, request.viewport_size,
            )
            dur = (time.monotonic() - start) * 1000
            return VisionResponse(
                found=True, x=float(screen_x), y=float(screen_y),
                confidence=0.75, model=self.model_id,
                token_cost=0.0, duration_ms=dur,
            )
        except Exception as exc:
            logger.warning("UITARS provider error: %s", exc)
            dur = (time.monotonic() - start) * 1000
            return VisionResponse(found=False, model=self.model_id, duration_ms=dur)

    async def health_check(self) -> bool:
        return self._loaded

    def _parse_point_output(self, text: str) -> Optional[tuple[int, int]]:
        m = re.search(r"<point>\s*(\d+)\s+(\d+)\s*</point>", text)
        if m:
            return (int(m.group(1)), int(m.group(2)))
        m = re.search(r"\((\d+)\s*,\s*(\d+)\)", text)
        if m:
            return (int(m.group(1)), int(m.group(2)))
        return None
