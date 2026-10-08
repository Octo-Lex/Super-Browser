"""PageHandle — legacy Patchright tab adapter — and NormalizedPage, the
backend-neutral façade page (PR 1 P3/P6)."""

from __future__ import annotations

import asyncio
import fnmatch
import io
import json
import time
from typing import TYPE_CHECKING, Any, Optional

from super_browser.browser.cdp import CDPBridge

if TYPE_CHECKING:
    from super_browser.browser.backends.patchright_backend import PatchrightPage


def _maybe_reencode_jpeg(png_bytes: bytes, quality: Optional[int]) -> bytes:
    """Re-encode PNG to JPEG using Pillow if available.

    Used for the Selenium fallback path (Selenium only produces PNG). If Pillow
    is not installed, the original PNG bytes are returned unchanged — the caller
    will detect the PNG magic and use image/png mime. If Pillow cannot parse the
    bytes (corrupt/empty screenshot), the original PNG is also returned.
    """
    try:
        from PIL import Image
    except ImportError:
        return png_bytes
    try:
        img = Image.open(io.BytesIO(png_bytes))
    except Exception:
        # Corrupt or unparseable image — return original bytes.
        return png_bytes
    if img.mode in ("RGBA", "LA", "P"):
        # JPEG has no alpha channel — composite onto white.
        background = Image.new("RGB", img.size, (255, 255, 255))
        if img.mode == "P":
            img = img.convert("RGBA")
        background.paste(img, mask=img.split()[-1] if "A" in img.mode else None)
        img = background
    elif img.mode != "RGB":
        img = img.convert("RGB")
    out = io.BytesIO()
    q = quality if quality is not None else 80
    img.save(out, format="JPEG", quality=q)
    return out.getvalue()


class PageHandle:
    """Wrapper around a Patchright Page with CDP bridge access.

    Delegates standard page operations to Patchright while
    providing CDP bridge for low-level compositor operations.
    """

    def __init__(self, page: Any, cdp: CDPBridge) -> None:
        self._page = page
        self._cdp = cdp
        self._engine_page: Optional[PatchrightPage] = None

    async def goto(
        self,
        url: str,
        wait_until: str = "domcontentloaded",
        timeout: Optional[float] = None,
    ) -> Any:
        """Navigate to URL. Delegates to Patchright page.goto()."""
        kwargs: dict[str, Any] = {"url": url, "wait_until": wait_until}
        if timeout is not None:
            kwargs["timeout"] = timeout * 1000
        return await self._page.goto(**kwargs)

    async def title(self) -> str:
        return await self._page.title()

    @property
    def url(self) -> str:
        return self._page.url

    @property
    def is_alive(self) -> bool:
        """Whether the underlying page is still open and usable.

        Returns False when the page is closed, None, or the backend reports
        it as closed. Backends without ``is_closed()`` are assumed alive —
        we don't want to falsely kill a working backend that lacks the method.
        """
        if self._page is None:
            return False
        try:
            return not self._page.is_closed()
        except (AttributeError, Exception):
            return True

    async def close(self) -> None:
        await self._page.close()

    async def content(self) -> str:
        return await self._page.content()

    async def screenshot(
        self,
        path: Optional[str] = None,
        full_page: bool = False,
        format: str = "png",
        quality: Optional[int] = None,
    ) -> bytes:
        """Capture a screenshot, normalizing format across backends.

        - Patchright/Playwright/CDP: forward as ``type`` (the Playwright
          spelling). The CDP backend accepts both ``type`` and ``format``.
        - Selenium: only PNG is supported; if jpeg is requested and Pillow is
          installed, re-encode the PNG; otherwise return PNG with a caller-visible
          discrepancy in mime.
        """
        kwargs: dict[str, Any] = {"full_page": full_page}
        if path:
            kwargs["path"] = path

        if format == "jpeg":
            kwargs["type"] = "jpeg"
            if quality is not None:
                kwargs["quality"] = quality
        else:
            kwargs["type"] = "png"

        raw = await self._page.screenshot(**kwargs)

        # Selenium fallback: it ignores format/quality and always returns PNG.
        # If jpeg was requested but we got PNG, try re-encoding with Pillow.
        if format == "jpeg" and raw.startswith(b"\x89PNG"):
            raw = _maybe_reencode_jpeg(raw, quality)

        return raw

    @property
    def cdp(self) -> CDPBridge:
        """Associated CDP bridge for compositor-level operations."""
        return self._cdp

    @property
    def engine_page(self) -> PatchrightPage:
        """Protocol-compliant page wrapper.

        Lazily creates a :class:`PatchrightPage` that wraps the raw
        Playwright Page and CDPBridge, satisfying the EnginePage protocol.
        """
        if self._engine_page is None:
            from super_browser.browser.backends.patchright_backend import PatchrightPage
            self._engine_page = PatchrightPage(self._page, self._cdp)
        return self._engine_page

    @property
    def backend_page(self) -> Any:
        """Underlying Patchright/Playwright Page for advanced usage."""
        return self._page

    @property
    def raw_page(self) -> Any:
        """Deprecated alias for :attr:`backend_page`.

        .. deprecated:: 2.0
            Use :attr:`backend_page` instead. Will be removed in v3.0.
        """
        import warnings
        warnings.warn(
            "raw_page is deprecated, use backend_page instead. "
            "Will be removed in v3.0.",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.backend_page


def _unwrap_evaluate(raw: Any) -> Any:
    """Normalize ``EnginePage.evaluate`` return shapes across backends.

    Playwright-family pages and Selenium return the JS value directly; the
    CDP backend returns a ``CDPResult`` envelope with the value nested under
    ``data.result.value``.
    """
    if hasattr(raw, "ok"):
        if not getattr(raw, "ok", False):
            return None
        data = getattr(raw, "data", None) or {}
        return (data.get("result", {}) or {}).get("value")
    return raw


class HistoryResult:
    """Normalized outcome of a history navigation (P2-review fix).

    ``navigated`` is False when the backend reports no entry in that
    direction (e.g. Playwright returning ``None`` from ``go_back``, or CDP
    reporting the edge of the navigation history). ``url`` is the page URL
    after the attempt. Facades consume this instead of inferring semantics
    from Playwright's ``Response | None``.
    """

    def __init__(self, navigated: bool, url: str = "") -> None:
        self.navigated = navigated
        self.url = url

    def __repr__(self) -> str:
        return f"HistoryResult(navigated={self.navigated!r}, url={self.url!r})"


class NormalizedPage:
    """Backend-neutral façade page — the object stored in ``SuperBrowser._page``.

    PR 1 item P3 (PLAN-COMPOSITION-HARDENING.md step 4). Contract:

    - ``engine_page``  — the exact ``EnginePage`` the selected engine built.
      Identity is preserved (no re-wrapping).
    - ``backend_page`` — native page/driver when one exists; an optional
      escape hatch. ``None`` for backends without one (e.g. CDP direct).
    - ``cdp``          — optional legacy low-level handle. TEMPORARY
      compatibility surface: on Playwright Chromium today this is a raw
      Playwright CDP session, not a ``CDPBridge``. P4 owns coordinate-
      transport semantics.

    Screenshot semantics are canonical here: callers pass
    ``format="png" | "jpeg"``; the Playwright-family engine pages speak
    ``format=`` natively as of P3, and Selenium's PNG-only output is
    re-encoded via the existing Pillow helper when available. The 2.13.1
    ``type=`` compatibility alias was removed in P7 together with the
    façade probe it served.

    ``PageHandle`` remains the legacy Patchright tab adapter until P5; it is
    no longer stored in ``SuperBrowser._page``.
    """

    def __init__(
        self,
        engine_page: Any,
        backend_page: Any = None,
        cdp: Any = None,
    ) -> None:
        self._engine_page = engine_page
        self._backend_page = (
            backend_page
            if backend_page is not None
            else getattr(engine_page, "backend_page", None)
        )
        self._cdp = cdp if cdp is not None else getattr(engine_page, "cdp", None)

    # -- Contract surfaces -------------------------------------------

    @property
    def engine_page(self) -> Any:
        """The exact ``EnginePage`` supplied by the engine."""
        return self._engine_page

    @property
    def backend_page(self) -> Any:
        """Native page/driver when one exists; ``None`` otherwise."""
        return self._backend_page

    @property
    def cdp(self) -> Any:
        """Legacy low-level handle; temporary compatibility surface (P4 owns semantics)."""
        return self._cdp

    # -- Portable page operations --------------------------------------

    @property
    def url(self) -> str:
        return self._engine_page.url

    @property
    def stealth_bridge(self) -> Any:
        """Backend stealth bridge when the engine supplies one (optional)."""
        return getattr(self._engine_page, "stealth_bridge", None)

    # -- Native adaptation (P6) ------------------------------------------
    #
    # The wrapper MAY inspect backend_page in these methods; callers may
    # not. They exist so agent/facade.py and mcp_server.py never need the
    # native object — backend_page stays a documented escape hatch, not a
    # composition dependency.

    def attach_diagnostics(self, buffer: Any) -> None:
        """Attach a diagnostics buffer to the native page's event stream.

        Graceful no-op when the native object does not expose
        Playwright/Patchright-style ``.on()`` (e.g. a Selenium WebDriver):
        the buffer simply records nothing for such backends, instead of
        crashing on a foreign event API.
        """
        native = self._backend_page
        if native is not None and hasattr(native, "on"):
            buffer.attach(native)

    def _require_native(self, operation: str) -> Any:
        """Raise a structured error when this backend has no native page.

        Kept for adapter methods that cannot degrade (P6 surfaces)."""
        if self._backend_page is None:
            raise NotImplementedError(
                f"{operation} requires a native page; "
                "this backend does not supply one"
            )
        return self._backend_page

    async def reload(self, wait_until: str = "load") -> Any:
        """Reload the page, normalizing backend semantics (P2-review fix).

        - Playwright/Patchright: native ``reload(wait_until=...)``.
        - Selenium: ``driver.refresh()`` via ``asyncio.to_thread`` (no
          ``wait_until`` concept — the call returns when the driver reports
          readiness).
        - CDP-direct: ``Page.reload`` over the transport.
        """
        native = self._backend_page
        if callable(getattr(native, "reload", None)):
            return await native.reload(wait_until=wait_until)
        if callable(getattr(native, "refresh", None)):
            await asyncio.to_thread(native.refresh)
            return None
        if self._cdp is not None:
            await self._cdp.send("Page.reload", {})
            return None
        raise NotImplementedError(
            "reload requires a native page or a CDP transport"
        )

    async def go_back(self, wait_until: str = "load") -> HistoryResult:
        return await self._navigate_history("back", wait_until)

    async def go_forward(self, wait_until: str = "load") -> HistoryResult:
        return await self._navigate_history("forward", wait_until)

    async def _navigate_history(self, direction: str, wait_until: str) -> HistoryResult:
        """Navigate history with normalized semantics (P2-review fix).

        - Playwright/Patchright: native ``go_back``/``go_forward`` returning
          ``Response | None`` (None = no entry).
        - Selenium: ``driver.back()``/``driver.forward()`` via
          ``asyncio.to_thread`` (the WebDriver cannot report the edge, so the
          result is reported as navigated).
        - CDP-direct: ``Page.getNavigationHistory`` +
          ``Page.navigateToHistoryEntry``, with a real edge check.
        """
        native = self._backend_page
        native_method = "go_back" if direction == "back" else "go_forward"
        if callable(getattr(native, native_method, None)):
            response = await getattr(native, native_method)(wait_until=wait_until)
            return HistoryResult(
                navigated=response is not None, url=self._engine_page.url
            )
        if callable(getattr(native, direction, None)):
            await asyncio.to_thread(getattr(native, direction))
            return HistoryResult(navigated=True, url=self._engine_page.url)
        if self._cdp is not None:
            history = await self._cdp.send("Page.getNavigationHistory", {})
            data = (history.data or {}) if getattr(history, "ok", False) else {}
            index = data.get("currentIndex", 0)
            entries = data.get("entries", [])
            target = index - 1 if direction == "back" else index + 1
            if 0 <= target < len(entries):
                entry = entries[target]
                await self._cdp.send(
                    "Page.navigateToHistoryEntry",
                    {"entryId": entry.get("id")},
                )
                return HistoryResult(
                    navigated=True, url=entry.get("url", self._engine_page.url)
                )
            return HistoryResult(navigated=False, url=self._engine_page.url)
        raise NotImplementedError(
            "history navigation requires a native page or a CDP transport"
        )

    async def selector_bounds(
        self, selector: str
    ) -> Optional[tuple[int, int, int, int]]:
        """Resolve a CSS selector to ``(x, y, width, height)``.

        Portable: uses ``EnginePage.evaluate`` with the selector JSON-encoded
        (never interpolated). Returns ``None`` when the element is missing or
        its box is empty.
        """
        expr = (
            "(function() {"
            "  var sel = JSON.parse(" + json.dumps(selector) + ");"
            "  var el = document.querySelector(sel);"
            "  if (!el) return null;"
            "  var r = el.getBoundingClientRect();"
            "  return JSON.stringify({x: r.x, y: r.y, w: r.width, h: r.height});"
            "})()"
        )
        payload = _unwrap_evaluate(await self._engine_page.evaluate(expr))
        if not payload:
            return None
        box = json.loads(payload)
        if box["w"] <= 0 or box["h"] <= 0:
            return None
        return (int(box["x"]), int(box["y"]), int(box["w"]), int(box["h"]))

    async def wait_for(
        self,
        *,
        selector: Optional[str] = None,
        text: Optional[str] = None,
        url: Optional[str] = None,
        load_state: Optional[str] = None,
        timeout_ms: int = 10_000,
    ) -> str:
        """Wait for exactly one page condition (MCP ``wait_for`` semantics).

        Native Patchright/Playwright semantics are preserved when the
        backend page supplies the ``wait_for_*`` family. Backends without
        them get a generic polling fallback for selector/text/url and basic
        ready-state waits. ``networkidle`` is NEVER faked: on a native-less
        backend it raises ``NotImplementedError`` instead.
        """
        given = [c for c in (selector, text, url, load_state) if c is not None]
        if len(given) != 1:
            raise ValueError("wait_for takes exactly one condition")

        native = self._backend_page
        native_complete = native is not None and all(
            callable(getattr(native, name, None))
            for name in (
                "wait_for_selector",
                "wait_for_function",
                "wait_for_url",
                "wait_for_load_state",
            )
        )
        if native_complete:
            if selector is not None:
                await native.wait_for_selector(selector, timeout=timeout_ms)
                return "selector"
            if text is not None:
                # arg= is supported by both Patchright and Playwright.
                await native.wait_for_function(
                    "(needle) => document.body && document.body.innerText.includes(needle)",
                    arg=text,
                    timeout=timeout_ms,
                )
                return "text"
            if url is not None:
                await native.wait_for_url(url, timeout=timeout_ms)
                return "url"
            await native.wait_for_load_state(load_state, timeout=timeout_ms)
            return "load_state"

        # Polling fallback for native-less backends.
        if load_state == "networkidle":
            raise NotImplementedError(
                "networkidle cannot be faithfully polled on this backend"
            )
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            if selector is not None:
                if await self.selector_bounds(selector) is not None:
                    return "selector"
            elif text is not None:
                needle = json.dumps(text)  # exact-case match, like native
                raw = _unwrap_evaluate(
                    await self._engine_page.evaluate(
                        "document.body && "
                        "document.body.innerText.includes(JSON.parse(" + needle + "))"
                    )
                )
                if raw:
                    return "text"
            elif url is not None:
                if fnmatch.fnmatch(self.url, url):
                    return "url"
            else:
                ready = _unwrap_evaluate(
                    await self._engine_page.evaluate("document.readyState")
                )
                if load_state in ("domcontentloaded", "load") and ready in (
                    "interactive",
                    "complete",
                ):
                    return "load_state"
                if load_state == "commit" and ready:
                    return "load_state"
            await asyncio.sleep(0.1)
        raise TimeoutError(f"wait_for: condition not met within {timeout_ms}ms")

    async def goto(self, url: str, *args: Any, **kwargs: Any) -> Any:
        return await self._engine_page.goto(url, *args, **kwargs)

    async def title(self) -> str:
        return await self._engine_page.title()

    async def close(self) -> None:
        await self._engine_page.close()

    async def activate(self) -> None:
        """Bring this page to the foreground (EnginePage.activate, P5)."""
        await self._engine_page.activate()

    async def content(self) -> str:
        return await self._engine_page.content()

    @property
    def is_alive(self) -> bool:
        """Alive unless the native object reports otherwise.

        Prefers a native ``is_closed()`` probe (the Playwright-family
        spelling); assumes alive when the backend exposes neither — a
        missing probe must not falsely kill a working backend.
        """
        native = self._backend_page if self._backend_page is not None else self._engine_page
        if native is None:
            return False
        is_closed = getattr(native, "is_closed", None)
        if callable(is_closed):
            try:
                return not is_closed()
            except Exception:
                return True
        return True

    # -- Screenshot ------------------------------------------------------

    async def screenshot(
        self,
        *,
        path: Optional[str] = None,
        full_page: bool = False,
        format: str = "png",
        quality: Optional[int] = None,
    ) -> bytes:
        """Canonical screenshot: ``format="png" | "jpeg"``.

        ``quality`` is forwarded only for JPEG — Playwright rejects PNG
        screenshots carrying a quality value. Selenium's PNG-only output is
        re-encoded to JPEG via the existing Pillow helper when requested and
        available.
        """
        kwargs: dict[str, Any] = {"full_page": full_page, "format": format}
        if path:
            kwargs["path"] = path
        if format == "jpeg" and quality is not None:
            kwargs["quality"] = quality
        raw = await self._engine_page.screenshot(**kwargs)
        if (
            format == "jpeg"
            and isinstance(raw, (bytes, bytearray))
            and raw.startswith(b"\x89PNG")
        ):
            raw = _maybe_reencode_jpeg(bytes(raw), quality)
        return raw
