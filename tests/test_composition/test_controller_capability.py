"""Controller capability tests — PR 1 item P4.

Proves the P4 success criterion: transport presence (an Optional CDPBridge
on the NormalizedPage) decides whether coordinate and coordinate-backed
vision tiers exist — never backend name, never AttributeError-driven
detection.

Transport absent:
  - selector tier still works (click, fill with clear_first=True)
  - coordinate tier records UNAVAILABLE in the cascade
  - a CONFIGURED vision tier records UNAVAILABLE (it captures through CDP
    and dispatches coordinates through CDP)
  - keypress returns a structured "coordinate transport unavailable" result
  - SnapshotProvider with neither transport nor stealth bridge returns a
    valid empty AXSnapshot

Transport present (real browser):
  - PlaywrightPage.cdp is a CDPBridge (the raw session stays with the
    stealth bridge)
  - the full Playwright façade path minus tabs is hard-green
    (start → navigate → observe → screenshot → stop)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from super_browser.browser.config import SessionConfig
from super_browser.browser.page import NormalizedPage
from super_browser.config import Config
from super_browser.interaction.controller import MultimodalController
from super_browser.interaction.snapshot import SnapshotProvider
from super_browser.interaction.types import Tier, TierOutcome


class _SelectorEnginePage:
    """Engine page with working selector-tier methods and no transport."""

    def __init__(self) -> None:
        self.clicked: list[str] = []
        self.filled: list[tuple[str, str]] = []
        self.backend_page = None
        self.cdp = None
        self.stealth_bridge = None

    @property
    def url(self) -> str:
        return "https://example.com/"

    async def title(self) -> str:
        return "cap"

    async def click(self, selector: str, **kwargs: Any) -> None:
        self.clicked.append(selector)

    async def fill(self, selector: str, value: str, **kwargs: Any) -> None:
        self.filled.append((selector, value))


def _no_transport_controller(vision_configured: bool = False) -> MultimodalController:
    engine = _SelectorEnginePage()
    page = NormalizedPage(engine_page=engine)
    vision_factory = None
    if vision_configured:
        vision_factory = MagicMock()
        vision_factory.get_provider.return_value = MagicMock()
    return MultimodalController(page, None, vision_provider=vision_factory), engine


# ============================================================================
# Controller without a coordinate transport
# ============================================================================


async def test_selector_click_succeeds_without_cdp() -> None:
    controller, engine = _no_transport_controller()
    result = await controller.click("@e0")
    assert result.ok, f"selector click failed: {result.error}"
    assert engine.clicked == ["@e0"]


async def test_selector_fill_clear_first_succeeds_without_cdp() -> None:
    """The old selector tier called compositor_key_press when clear_first —
    making a portable fill impossible. EnginePage.fill replaces the value
    natively; the tier must not touch a transport."""
    controller, engine = _no_transport_controller()
    result = await controller.fill("#name", "Ada", clear_first=True)
    assert result.ok, f"selector fill failed: {result.error}"
    assert engine.filled == [("#name", "Ada")]


async def test_coordinate_and_vision_tiers_record_unavailable() -> None:
    """With a vision factory CONFIGURED but no transport, the cascade must
    record UNAVAILABLE for both tiers — structural absence, not a caught
    AttributeError disguised as a failed attempt."""
    controller, _ = _no_transport_controller(vision_configured=True)

    async def t2() -> Any:  # pragma: no cover — must never execute
        raise AssertionError("coordinate tier executed without a transport")

    async def t3() -> Any:  # pragma: no cover — must never execute
        raise AssertionError("vision tier executed without a transport")

    result, cascade = await controller._cascade("click", "@e0", None, None, t2, t3)
    by_tier = {a.tier: a for a in cascade.attempts}
    assert by_tier[Tier.COORDINATE].outcome == TierOutcome.UNAVAILABLE
    assert by_tier[Tier.VISION].outcome == TierOutcome.UNAVAILABLE


async def test_keypress_returns_structured_unavailable_without_cdp() -> None:
    controller, _ = _no_transport_controller()
    result = await controller.keypress("a")
    assert result.ok is False
    assert result.error is not None
    assert "coordinate transport unavailable" in result.error.message


# ============================================================================
# Snapshot degradation
# ============================================================================


async def test_snapshot_without_transport_or_stealth_is_valid_empty() -> None:
    provider = SnapshotProvider(cdp=None, stealth_bridge=None)
    snap = await provider.capture_ax_only("https://example.com/page", "Page")
    assert snap.url == "https://example.com/page"
    assert snap.title == "Page"
    assert snap.nodes == {}


async def test_snapshot_with_stealth_bridge_still_supply_metadata() -> None:
    """A Selenium Chrome stealth bridge can provide AX metadata even though
    coordinate dispatch is unavailable."""

    class _StealthOnly:
        async def get_ax_tree(self) -> dict:
            return {
                "nodes": [
                    {
                        "role": {"value": "link"},
                        "name": {"value": "Docs"},
                    }
                ]
            }

    provider = SnapshotProvider(cdp=None, stealth_bridge=_StealthOnly())
    snap = await provider.capture_ax_only("https://example.com/", "T")
    assert any(node.name == "Docs" for node in snap.nodes.values())


# ============================================================================
# Transport present — real Playwright Chromium (integration)
# ============================================================================


@pytest.mark.integration
async def test_playwright_page_cdp_is_cdpbridge(tmp_path: Path) -> None:
    pytest.importorskip("playwright")
    from super_browser.browser.cdp import CDPBridge
    from super_browser.browser.factory import create_browser_engine

    engine = create_browser_engine(
        Config(browser=SessionConfig(headless=True, backend="playwright"))
    )
    await engine.start()
    try:
        page = await engine.new_page()
        assert isinstance(page.cdp, CDPBridge), (
            f"PlaywrightPage.cdp is {type(page.cdp).__name__}, not CDPBridge — "
            "transport presence must be the capability signal"
        )
    finally:
        await engine.stop()


@pytest.mark.integration
async def test_playwright_facade_pre_tab_path_is_hard_green(tmp_path: Path) -> None:
    """Hard gate: start → navigate → observe → screenshot → stop on Playwright
    Chromium. Tab operations are NOT here — they stay with the xfailed full
    vertical until P5 retires the legacy ownership path."""
    pytest.importorskip("playwright")
    from super_browser import SuperBrowser

    fixture = tmp_path / "pw_p4.html"
    fixture.write_text(
        "<html><body><h1>P4 PRE-TAB</h1></body></html>", encoding="utf-8"
    )
    sb = SuperBrowser(
        config=Config(browser=SessionConfig(headless=True, backend="playwright"))
    )
    await sb.start()
    try:
        await sb.navigate(fixture.as_uri())
        observed = await sb.observe()
        assert observed.ok, f"observe failed: {observed.error}"
        img, mime = await sb._capture_region_bytes(format="png")
        assert len(img) > 0 and mime == "image/png"
    finally:
        await sb.stop()
