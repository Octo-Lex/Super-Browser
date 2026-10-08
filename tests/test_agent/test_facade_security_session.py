"""Wave 9 tests — Security on session persistence facade actions.

Covers save_session() and load_session() — both enforce
_check_facade_security() with DANGEROUS level before any disk I/O,
cookie export, or cookie import.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock

from super_browser.agent.facade import SuperBrowser

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fake_security_manager(blocked: bool = False, blocked_by: str = "test policy"):
    mgr = MagicMock()
    result = MagicMock()
    result.passed = not blocked
    result.blocked_by = blocked_by if blocked else ""
    mgr.check_action = AsyncMock(return_value=result)
    return mgr


def _make_browser_with_mocks(*, security_manager=None, stealth_bridge=None) -> SuperBrowser:
    browser = SuperBrowser()
    browser._session = MagicMock()
    browser._page = MagicMock()
    browser._page.url = "https://example.com"
    if stealth_bridge is None:
        stealth_bridge = MagicMock()
        stealth_bridge.get_all_cookies = AsyncMock(
            return_value=[{"name": "session", "value": "abc123"}]
        )
        stealth_bridge.set_cookies = AsyncMock()
    browser._page.stealth_bridge = stealth_bridge
    browser._running = True
    browser._security_manager = security_manager
    return browser


def _write_session_file(path: str) -> None:
    data = {
        "version": "1.0",
        "timestamp": 1718300000.0,
        "url": "https://example.com",
        "cookies": [{"name": "session", "value": "xyz"}],
    }
    with open(path, "w") as f:
        json.dump(data, f)


# ---------------------------------------------------------------------------
# save_session() security
# ---------------------------------------------------------------------------

class TestSaveSessionSecurity:

    def test_calls_security_before_cookie_export(self) -> None:
        async def _test():
            mgr = _make_fake_security_manager(blocked=False)
            stealth = MagicMock()
            stealth.get_all_cookies = AsyncMock(return_value=[{"name": "s", "value": "v"}])
            browser = _make_browser_with_mocks(security_manager=mgr, stealth_bridge=stealth)
            await browser.save_session("/tmp/session.json")
            mgr.check_action.assert_called_once()
            assert mgr.check_action.call_args[0][0] == "save_session"
        asyncio.run(_test())

    def test_uses_dangerous_level(self) -> None:
        async def _test():
            mgr = _make_fake_security_manager(blocked=False)
            browser = _make_browser_with_mocks(security_manager=mgr)
            await browser.save_session("/tmp/session.json")
            from super_browser.security.types import SecurityLevel
            assert mgr.check_action.call_args[0][3] == SecurityLevel.DANGEROUS
        asyncio.run(_test())

    def test_blocked_does_not_export_cookies(self) -> None:
        async def _test():
            mgr = _make_fake_security_manager(blocked=True, blocked_by="credential export policy")
            stealth = MagicMock()
            stealth.get_all_cookies = AsyncMock(return_value=[{"name": "s", "value": "v"}])
            browser = _make_browser_with_mocks(security_manager=mgr, stealth_bridge=stealth)
            result = await browser.save_session("/tmp/session.json")
            assert not result.ok
            assert result.error.category.value == "security"
            stealth.get_all_cookies.assert_not_called()
        asyncio.run(_test())

    def test_blocked_does_not_write_file(self) -> None:
        async def _test():
            mgr = _make_fake_security_manager(blocked=True)
            stealth = MagicMock()
            stealth.get_all_cookies = AsyncMock(return_value=[{"name": "s", "value": "v"}])
            browser = _make_browser_with_mocks(security_manager=mgr, stealth_bridge=stealth)
            with tempfile.TemporaryDirectory() as tmpdir:
                session_file = os.path.join(tmpdir, "session.json")
                result = await browser.save_session(session_file)
                assert not result.ok
                assert not os.path.exists(session_file)
        asyncio.run(_test())

    def test_redacted_path_consumed(self) -> None:
        async def _test():
            mgr = _make_fake_security_manager(blocked=False)
            stealth = MagicMock()
            stealth.get_all_cookies = AsyncMock(return_value=[{"name": "s", "value": "v"}])
            browser = _make_browser_with_mocks(security_manager=mgr, stealth_bridge=stealth)
            with tempfile.TemporaryDirectory() as tmpdir:
                safe_path = os.path.join(tmpdir, "redirected.json")

                async def redacting_check(action_name, params, url, level):
                    if action_name == "save_session":
                        params["path"] = safe_path
                    r = MagicMock()
                    r.passed = True
                    r.blocked_by = ""
                    return r

                mgr.check_action = AsyncMock(side_effect=redacting_check)
                result = await browser.save_session("/malicious/path.json")
                assert result.ok
                assert os.path.exists(safe_path)
        asyncio.run(_test())

    def test_no_security_manager_allows(self) -> None:
        async def _test():
            browser = _make_browser_with_mocks(security_manager=None)
            with tempfile.TemporaryDirectory() as tmpdir:
                session_file = os.path.join(tmpdir, "session.json")
                result = await browser.save_session(session_file)
                assert result.ok
                assert os.path.exists(session_file)
        asyncio.run(_test())


# ---------------------------------------------------------------------------
# load_session() security
# ---------------------------------------------------------------------------

class TestLoadSessionSecurity:

    def test_calls_security_before_file_read(self) -> None:
        async def _test():
            mgr = _make_fake_security_manager(blocked=False)
            stealth = MagicMock()
            stealth.set_cookies = AsyncMock()
            browser = _make_browser_with_mocks(security_manager=mgr, stealth_bridge=stealth)
            with tempfile.TemporaryDirectory() as tmpdir:
                session_file = os.path.join(tmpdir, "session.json")
                _write_session_file(session_file)
                await browser.load_session(session_file)
                mgr.check_action.assert_called_once()
                assert mgr.check_action.call_args[0][0] == "load_session"
        asyncio.run(_test())

    def test_uses_dangerous_level(self) -> None:
        async def _test():
            mgr = _make_fake_security_manager(blocked=False)
            browser = _make_browser_with_mocks(security_manager=mgr)
            with tempfile.TemporaryDirectory() as tmpdir:
                session_file = os.path.join(tmpdir, "session.json")
                _write_session_file(session_file)
                await browser.load_session(session_file)
                from super_browser.security.types import SecurityLevel
                assert mgr.check_action.call_args[0][3] == SecurityLevel.DANGEROUS
        asyncio.run(_test())

    def test_blocked_does_not_read_file(self) -> None:
        async def _test():
            mgr = _make_fake_security_manager(blocked=True, blocked_by="credential import policy")
            stealth = MagicMock()
            stealth.set_cookies = AsyncMock()
            browser = _make_browser_with_mocks(security_manager=mgr, stealth_bridge=stealth)
            with tempfile.TemporaryDirectory() as tmpdir:
                session_file = os.path.join(tmpdir, "session.json")
                _write_session_file(session_file)
                result = await browser.load_session(session_file)
                assert not result.ok
                assert result.error.category.value == "security"
                stealth.set_cookies.assert_not_called()
        asyncio.run(_test())

    def test_redacted_path_consumed(self) -> None:
        async def _test():
            mgr = _make_fake_security_manager(blocked=False)
            stealth = MagicMock()
            stealth.set_cookies = AsyncMock()
            browser = _make_browser_with_mocks(security_manager=mgr, stealth_bridge=stealth)
            with tempfile.TemporaryDirectory() as tmpdir:
                safe_file = os.path.join(tmpdir, "safe.json")
                _write_session_file(safe_file)

                async def redacting_check(action_name, params, url, level):
                    if action_name == "load_session":
                        params["path"] = safe_file
                    r = MagicMock()
                    r.passed = True
                    r.blocked_by = ""
                    return r

                mgr.check_action = AsyncMock(side_effect=redacting_check)
                result = await browser.load_session("/malicious/path.json")
                assert result.ok
                stealth.set_cookies.assert_called_once()
        asyncio.run(_test())

    def test_no_security_manager_allows(self) -> None:
        async def _test():
            browser = _make_browser_with_mocks(security_manager=None)
            with tempfile.TemporaryDirectory() as tmpdir:
                session_file = os.path.join(tmpdir, "session.json")
                _write_session_file(session_file)
                result = await browser.load_session(session_file)
                assert result.ok
        asyncio.run(_test())
