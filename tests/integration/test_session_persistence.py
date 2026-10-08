"""BATCH-52: Session persistence tests (save_session / load_session).

Tests the cookie save/load cycle via StealthBridge without a real browser.
All CDP/StealthBridge calls are mocked.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from super_browser.agent.facade import SuperBrowser
from super_browser.results import ActionMethod

# =====================================================================
# Helpers
# =====================================================================

def _make_facade_with_bridge(tmp_path: Path) -> tuple[SuperBrowser, MagicMock]:
    """Create a SuperBrowser with a mocked stealth_bridge."""
    bridge = MagicMock()
    bridge.get_all_cookies = AsyncMock(return_value=[
        {"name": "session", "value": "abc123", "domain": ".example.com"},
        {"name": "csrf", "value": "xyz789", "domain": ".example.com"},
    ])
    bridge.set_cookies = AsyncMock()

    engine_page = MagicMock()
    engine_page.url = "https://example.com/dashboard"

    page = MagicMock()
    page.stealth_bridge = bridge

    facade = SuperBrowser.__new__(SuperBrowser)
    facade._page = page
    facade._running = True
    facade._security_manager = None

    return facade, bridge


# =====================================================================
# save_session tests
# =====================================================================

class TestSaveSession:
    """TEST-52-01: save_session serializes cookies to JSON."""

    @pytest.mark.asyncio
    async def test_save_session_writes_file(self, tmp_path: Path) -> None:
        facade, bridge = _make_facade_with_bridge(tmp_path)
        session_file = tmp_path / "session.json"

        result = await facade.save_session(str(session_file))

        assert result.ok
        assert session_file.exists()
        data = json.loads(session_file.read_text(encoding="utf-8"))
        assert data["version"] == "1.0"
        assert len(data["cookies"]) == 2
        assert data["cookies"][0]["name"] == "session"

    @pytest.mark.asyncio
    async def test_save_session_result_metadata(self, tmp_path: Path) -> None:
        facade, bridge = _make_facade_with_bridge(tmp_path)
        session_file = tmp_path / "session.json"

        result = await facade.save_session(str(session_file))

        assert result.ok
        assert result.data["cookie_count"] == 2
        assert result.data["path"] == str(session_file)
        assert result.meta.method == ActionMethod.SELECTOR

    @pytest.mark.asyncio
    async def test_save_session_creates_parent_dirs(self, tmp_path: Path) -> None:
        facade, bridge = _make_facade_with_bridge(tmp_path)
        session_file = tmp_path / "nested" / "dir" / "session.json"

        result = await facade.save_session(str(session_file))

        assert result.ok
        assert session_file.exists()

    @pytest.mark.asyncio
    async def test_save_session_no_browser(self) -> None:
        facade = SuperBrowser.__new__(SuperBrowser)
        facade._page = None

        result = await facade.save_session("test.json")

        assert not result.ok
        assert "not started" in result.error.message.lower()

    @pytest.mark.asyncio
    async def test_save_session_no_stealth_bridge(self) -> None:
        page = MagicMock()
        page.stealth_bridge = None

        facade = SuperBrowser.__new__(SuperBrowser)
        facade._page = page

        result = await facade.save_session("test.json")

        assert not result.ok
        assert "no stealth bridge" in result.error.message.lower()

    @pytest.mark.asyncio
    async def test_save_session_captures_url(self, tmp_path: Path) -> None:
        facade, bridge = _make_facade_with_bridge(tmp_path)
        facade._page.url = "https://example.com/dashboard"
        session_file = tmp_path / "session.json"

        await facade.save_session(str(session_file))

        data = json.loads(session_file.read_text(encoding="utf-8"))
        assert "example.com" in data["url"]


# =====================================================================
# load_session tests
# =====================================================================

class TestLoadSession:
    """TEST-52-02: load_session restores cookies from JSON."""

    @pytest.mark.asyncio
    async def test_load_session_restores_cookies(self, tmp_path: Path) -> None:
        facade, bridge = _make_facade_with_bridge(tmp_path)
        session_file = tmp_path / "session.json"
        session_file.write_text(json.dumps({
            "version": "1.0",
            "timestamp": time.time(),
            "url": "https://example.com",
            "cookies": [
                {"name": "session", "value": "abc123", "domain": ".example.com"},
                {"name": "csrf", "value": "xyz789", "domain": ".example.com"},
            ],
        }), encoding="utf-8")

        result = await facade.load_session(str(session_file))

        assert result.ok
        assert result.data["cookie_count"] == 2
        bridge.set_cookies.assert_called_once()

    @pytest.mark.asyncio
    async def test_load_session_file_not_found(self, tmp_path: Path) -> None:
        facade, bridge = _make_facade_with_bridge(tmp_path)

        result = await facade.load_session(str(tmp_path / "nonexistent.json"))

        assert not result.ok
        assert "not found" in result.error.message.lower()

    @pytest.mark.asyncio
    async def test_load_session_invalid_json(self, tmp_path: Path) -> None:
        facade, bridge = _make_facade_with_bridge(tmp_path)
        bad_file = tmp_path / "bad.json"
        bad_file.write_text("{not valid json", encoding="utf-8")

        result = await facade.load_session(str(bad_file))

        assert not result.ok
        assert "invalid json" in result.error.message.lower()

    @pytest.mark.asyncio
    async def test_load_session_unsupported_version(self, tmp_path: Path) -> None:
        facade, bridge = _make_facade_with_bridge(tmp_path)
        session_file = tmp_path / "session.json"
        session_file.write_text(json.dumps({
            "version": "2.0",
            "cookies": [],
        }), encoding="utf-8")

        result = await facade.load_session(str(session_file))

        assert not result.ok
        assert "unsupported" in result.error.message.lower()

    @pytest.mark.asyncio
    async def test_load_session_empty_cookies(self, tmp_path: Path) -> None:
        facade, bridge = _make_facade_with_bridge(tmp_path)
        session_file = tmp_path / "session.json"
        session_file.write_text(json.dumps({
            "version": "1.0",
            "timestamp": time.time(),
            "cookies": [],
        }), encoding="utf-8")

        result = await facade.load_session(str(session_file))

        assert result.ok
        assert result.data["cookie_count"] == 0
        bridge.set_cookies.assert_not_called()

    @pytest.mark.asyncio
    async def test_load_session_no_browser(self) -> None:
        facade = SuperBrowser.__new__(SuperBrowser)
        facade._page = None

        result = await facade.load_session("test.json")

        assert not result.ok
        assert "not started" in result.error.message.lower()

    @pytest.mark.asyncio
    async def test_load_session_no_stealth_bridge(self) -> None:
        page = MagicMock()
        page.stealth_bridge = None

        facade = SuperBrowser.__new__(SuperBrowser)
        facade._page = page

        result = await facade.load_session("test.json")

        assert not result.ok
        assert "no stealth bridge" in result.error.message.lower()


# =====================================================================
# Round-trip test
# =====================================================================

class TestSessionRoundTrip:
    """TEST-52-03: save → load round-trip preserves cookies."""

    @pytest.mark.asyncio
    async def test_save_load_roundtrip(self, tmp_path: Path) -> None:
        facade, bridge = _make_facade_with_bridge(tmp_path)
        session_file = tmp_path / "session.json"

        # Save
        save_result = await facade.save_session(str(session_file))
        assert save_result.ok

        # Reset mock to verify load calls set_cookies
        bridge.set_cookies.reset_mock()

        # Load
        load_result = await facade.load_session(str(session_file))
        assert load_result.ok
        assert load_result.data["cookie_count"] == 2
        bridge.set_cookies.assert_called_once()

        # Verify cookies match
        saved_cookies = json.loads(session_file.read_text())["cookies"]
        loaded_cookies = bridge.set_cookies.call_args[0][0]
        assert loaded_cookies == saved_cookies
