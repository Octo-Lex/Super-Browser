"""Browser-integration gate — real Patchright and Playwright verticals
through the public facade, plus real dead-page MCP recovery (PR 2, step 7).

Every vertical here drives a REAL browser engine through the public
``SuperBrowser`` facade — no mocked backends — because the gate's purpose
is to prove the composition holds against actual engines.

Coverage per the frozen step-7 contract:
- Browser start/stop and page lifecycle.
- Navigate and observe.
- Click and verify.
- Screenshot capture.
- Dead-page MCP recovery (force-close the underlying page; the MCP runtime
  must transparently relaunch).

These verticals carry the ``browser_integration`` marker: the dedicated
CI job runs them explicitly on Ubuntu/Chromium with both browser
binaries provisioned and fails the gate if any of them is skipped.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from super_browser import Config, SuperBrowser
from super_browser.browser.config import SessionConfig
from super_browser.mcp_server import MCPBrowserRuntime

pytestmark = [pytest.mark.integration, pytest.mark.browser_integration]


# ============================================================================
# Fixtures and helpers
# ============================================================================


def _headless() -> Config:
    return Config(browser=SessionConfig(headless=True))


def _headless_backend(backend: str) -> Config:
    return Config(browser=SessionConfig(headless=True, backend=backend))


def _gate_fixture(tmp_path: Path, marker: str) -> str:
    """Write a local fixture page with a clickable button and return its URI.

    Clicking the button rewrites the document title to CLICKED, so a click
    can be verified through the public facade without extra tooling.
    """
    fixture = tmp_path / f"gate_{marker}.html"
    fixture.write_text(
        "<html><head><title>gate</title></head><body>"
        "<button id='b' onclick=\"document.title='CLICKED'\">press</button>"
        "</body></html>",
        encoding="utf-8",
    )
    return fixture.as_uri()


async def _full_vertical(sb: SuperBrowser, uri: str) -> None:
    """The frozen browser-integration contract on one live facade."""
    await sb.navigate(uri)
    observed = await sb.observe()
    assert observed.ok, f"observe failed: {observed.error}"

    clicked = await sb.click("#b")
    assert clicked.ok, f"click failed: {clicked.error}"

    shot = await sb._capture_region_bytes(format="png")
    assert len(shot[0]) > 0, "screenshot capture failed"


# ============================================================================
# Patchright and Playwright full lifecycle verticals
# ============================================================================


@pytest.mark.asyncio
async def test_patchright_browser_integration_gate(tmp_path: Path) -> None:
    sb = SuperBrowser(config=_headless_backend("patchright"))
    await sb.start()
    try:
        await _full_vertical(sb, _gate_fixture(tmp_path, "patchright"))
    finally:
        await sb.stop()


@pytest.mark.asyncio
async def test_playwright_browser_integration_gate(tmp_path: Path) -> None:
    sb = SuperBrowser(config=_headless_backend("playwright"))
    await sb.start()
    try:
        await _full_vertical(sb, _gate_fixture(tmp_path, "playwright"))
    finally:
        await sb.stop()


# ============================================================================
# Dead-page MCP recovery — real runtime, real browser, forced page death
# ============================================================================


@pytest.mark.asyncio
async def test_mcp_dead_page_recovery(tmp_path: Path) -> None:
    """Force-close the underlying page behind the MCP runtime's back; the
    next runtime call must detect the dead handle, tear down the stale
    facade, and relaunch a usable browser."""
    fixture = tmp_path / "recovery.html"
    fixture.write_text(
        "<html><body><h1>RECOVERY GATE</h1></body></html>", encoding="utf-8"
    )
    runtime = MCPBrowserRuntime(
        config=Config(browser=SessionConfig(headless=True, backend="patchright"))
    )

    sb = await runtime.get_browser()
    await sb.navigate(fixture.as_uri())
    assert sb.is_alive is True

    # Force page death BEHIND the runtime's back (simulates a crash or an
    # external close — exactly what the recovery path exists for).
    await sb._page.engine_page.close()
    assert sb.is_alive is False

    recovered = await runtime.get_browser()
    assert recovered is not sb, "the stale facade must be replaced"
    assert recovered.is_alive is True

    await recovered.navigate(fixture.as_uri())
    assert recovered.is_alive is True
    await runtime.shutdown()
