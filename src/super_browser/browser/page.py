"""PageHandle — wraps a Patchright Page with CDP bridge access."""

from __future__ import annotations

import io
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
    re-encoded via the existing Pillow helper when available. The internal
    ``type=`` spelling is accepted as a compatibility alias so the 2.13.1
    probe in ``_capture_region_bytes`` keeps working until P7 deletes it.

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
        **kwargs: Any,
    ) -> bytes:
        """Canonical screenshot: ``format="png" | "jpeg"``.

        The engine page receives the normalized vocabulary. The internal
        ``type=`` spelling is accepted as a compatibility alias (mapped to
        ``format`` when no explicit format was given). Selenium's PNG-only
        output is re-encoded to JPEG via Pillow when requested and available.
        """
        compat_type = kwargs.pop("type", None)
        if compat_type in ("png", "jpeg") and format == "png":
            format = compat_type
        kwargs["full_page"] = full_page
        if path:
            kwargs["path"] = path
        kwargs["format"] = format
        if quality is not None:
            kwargs.setdefault("quality", quality)
        raw = await self._engine_page.screenshot(**kwargs)
        if (
            format == "jpeg"
            and isinstance(raw, (bytes, bytearray))
            and raw.startswith(b"\x89PNG")
        ):
            raw = _maybe_reencode_jpeg(bytes(raw), quality)
        return raw
