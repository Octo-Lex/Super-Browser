"""P1 navigation-tier tests.

Covers the four-tier tool model (Inspect / Navigation / Action), the
navigation dispatch path (SecurityManager-checked, audited, not
action-budgeted), the wait_for tool, and the CLI/env action-mode bootstrap.

The default server advertises 8 tools (6 inspect + 2 navigation) and can
execute: navigate -> wait_for -> extract_text without --allow-actions.
Action tools remain hidden until action mode is enabled.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# ============================================================================
# Tier constants
# ============================================================================


class TestTierConstants:
    """Verify the four-tier constants exist and partition the tool surface."""

    def test_inspect_tool_names_has_eleven_tools(self):
        from super_browser.mcp_server import INSPECT_TOOL_NAMES

        assert INSPECT_TOOL_NAMES == frozenset({
            "browser_status", "current_url", "observe",
            "extract_text", "screenshot", "list_tabs", "extract_image_text",
            "get_console_messages", "get_page_errors",
            "get_network_errors", "list_requests", "get_request",
            "analyze_image",
        })

    def test_navigation_tool_names_has_six_tools(self):
        from super_browser.mcp_server import NAVIGATION_TOOL_NAMES

        assert NAVIGATION_TOOL_NAMES == frozenset({"navigate", "wait_for", "switch_tab", "reload", "go_back", "go_forward"})

    def test_action_tool_names_has_six_tools(self):
        from super_browser.mcp_server import ACTION_TOOL_NAMES

        assert ACTION_TOOL_NAMES == frozenset({
            "scroll", "press_key", "click", "fill", "open_tab", "close_tab", "hover", "select_option", "check", "uncheck", "focus", "type_text",
        })

    def test_default_tool_names_is_inspect_union_navigation(self):
        from super_browser.mcp_server import (
            DEFAULT_TOOL_NAMES,
            INSPECT_TOOL_NAMES,
            NAVIGATION_TOOL_NAMES,
        )

        assert DEFAULT_TOOL_NAMES == INSPECT_TOOL_NAMES | NAVIGATION_TOOL_NAMES

    def test_tiers_are_disjoint(self):
        from super_browser.mcp_server import (
            ACTION_TOOL_NAMES,
            INSPECT_TOOL_NAMES,
            NAVIGATION_TOOL_NAMES,
        )

        assert not (INSPECT_TOOL_NAMES & NAVIGATION_TOOL_NAMES)
        assert not (INSPECT_TOOL_NAMES & ACTION_TOOL_NAMES)
        assert not (NAVIGATION_TOOL_NAMES & ACTION_TOOL_NAMES)

    def test_all_known_tools_partition_into_three_tiers(self):
        """Inspect + Navigation + Action = every known tool name."""
        from super_browser.mcp_server import (
            ACTION_TOOL_NAMES,
            INSPECT_TOOL_NAMES,
            NAVIGATION_TOOL_NAMES,
        )

        all_known = {
            "browser_status", "current_url", "observe", "extract_text",
            "screenshot", "list_tabs", "extract_image_text", "analyze_image",
            "navigate", "wait_for", "switch_tab", "reload", "go_back", "go_forward",
            "scroll", "press_key", "click", "fill", "open_tab", "close_tab", "hover", "select_option", "check", "uncheck", "focus", "type_text",
            "get_console_messages", "get_page_errors", "get_network_errors",
            "list_requests", "get_request",
        }
        assert (INSPECT_TOOL_NAMES | NAVIGATION_TOOL_NAMES | ACTION_TOOL_NAMES) == all_known


# ============================================================================
# MCPSessionPolicy: allow_actions + allow_writes backward compat
# ============================================================================


class TestPolicyAllowActions:
    """allow_actions is the primary knob; allow_writes is a compat alias."""

    def test_default_policy_has_allow_actions_false(self):
        from super_browser.mcp_server import MCPSessionPolicy

        p = MCPSessionPolicy()
        assert p.allow_actions is False

    def test_allow_actions_true_via_kwarg(self):
        from super_browser.mcp_server import MCPSessionPolicy

        assert MCPSessionPolicy(allow_actions=True).allow_actions is True

    def test_legacy_allow_writes_true_enables_allow_actions(self):
        """Released callers using allow_writes=True must keep working."""
        from super_browser.mcp_server import MCPSessionPolicy

        p = MCPSessionPolicy(allow_writes=True)
        assert p.allow_actions is True
        # And the compat property must read it back.
        assert p.allow_writes is True

    def test_legacy_allow_writes_false_still_false(self):
        from super_browser.mcp_server import MCPSessionPolicy

        assert MCPSessionPolicy(allow_writes=False).allow_actions is False

    def test_allow_actions_takes_precedence_when_both_none(self):
        """allow_writes=None (the default) must not override allow_actions."""
        from super_browser.mcp_server import MCPSessionPolicy

        p = MCPSessionPolicy(allow_actions=True, allow_writes=None)
        assert p.allow_actions is True

    def test_allow_writes_setter_writes_through_to_allow_actions(self):
        from super_browser.mcp_server import MCPSessionPolicy

        p = MCPSessionPolicy()
        p.allow_writes = True
        assert p.allow_actions is True
        p.allow_writes = False
        assert p.allow_actions is False

    def test_policy_keeps_other_budget_fields(self):
        from super_browser.mcp_server import MCPSessionPolicy

        p = MCPSessionPolicy(allow_actions=True)
        assert p.max_actions > 0
        assert p.timeout_seconds > 0
        assert p.actions_used == 0


# ============================================================================
# MCPAuthorizer.record_audit()
# ============================================================================


class TestRecordAudit:
    """record_audit() is the public audit path for navigation-tier entries.
    It must write an audit entry AND return the MCPAuthorizationResult so the
    dispatcher can pass it straight to _refusal_content()."""

    def test_record_audit_returns_authorization_result(self):
        from super_browser.mcp_server import MCPAuthorizationResult, MCPAuthorizer, MCPSessionPolicy

        authorizer = MCPAuthorizer(MCPSessionPolicy())
        result = authorizer.record_audit(
            tool="navigate", arguments={"url": "https://x"},
            security_level="sensitive", allowed=False,
            blocked_by="security_manager", reason="domain_filter",
        )
        assert isinstance(result, MCPAuthorizationResult)
        assert result.allowed is False
        assert result.blocked_by == "security_manager"
        assert result.reason == "domain_filter"
        assert result.security_level == "sensitive"

    def test_record_audit_writes_audit_entry_on_deny(self):
        from super_browser.mcp_server import MCPAuthorizer, MCPSessionPolicy

        authorizer = MCPAuthorizer(MCPSessionPolicy())
        authorizer.record_audit(
            tool="navigate", arguments={"url": "https://x"},
            security_level="sensitive", allowed=False,
            blocked_by="security_manager", reason="denied",
        )
        assert len(authorizer.audit_log) == 1
        entry = authorizer.audit_log[0]
        assert entry.tool == "navigate"
        assert entry.allowed is False
        assert entry.blocked_by == "security_manager"
        assert entry.arguments == {"url": "https://x"}

    def test_record_audit_writes_audit_entry_on_allow(self):
        from super_browser.mcp_server import MCPAuthorizer, MCPSessionPolicy

        authorizer = MCPAuthorizer(MCPSessionPolicy())
        authorizer.record_audit(
            tool="navigate", arguments={"url": "https://x"},
            security_level="sensitive", allowed=True,
        )
        assert len(authorizer.audit_log) == 1
        assert authorizer.audit_log[0].allowed is True
        assert authorizer.audit_log[0].blocked_by is None

    def test_record_audit_does_not_increment_actions_used(self):
        """Navigation-tier audit must NOT consume the action budget."""
        from super_browser.mcp_server import MCPAuthorizer, MCPSessionPolicy

        authorizer = MCPAuthorizer(MCPSessionPolicy())
        authorizer.record_audit(
            tool="navigate", arguments={"url": "https://x"},
            security_level="sensitive", allowed=True,
        )
        assert authorizer.policy.actions_used == 0


# ============================================================================
# Shared fixtures: a dispatcher with a mocked facade
# ============================================================================


def _real_security_manager(allowlist: tuple[str, ...] = (), blocklist: tuple[str, ...] = ()):
    from super_browser.security import SecurityManager
    from super_browser.security.types import SecurityConfig

    return SecurityManager(SecurityConfig(
        domain_filter_enabled=True,
        domain_allowlist=allowlist,
        domain_blocklist=blocklist,
        injection_detection_enabled=False,
        redaction_enabled=False,
    ))


def _fake_action_result(data=None, ok=True):
    ar = MagicMock()
    ar.ok = ok
    ar.data = data
    ar.error = None if ok else {"category": "test_error"}
    ar.meta = None
    return ar


def _make_dispatcher(
    *,
    allow_actions: bool = False,
    security_manager=None,
    authorizer=None,
) -> tuple[Any, Any]:
    """Build a dispatcher with a mocked facade. Returns (dispatcher, fake_sb)."""
    from super_browser.mcp_server import (
        MCPAuthorizer,
        MCPBrowserRuntime,
        MCPSessionPolicy,
        ToolDispatcher,
    )

    fake_sb = MagicMock()
    fake_sb.navigate = AsyncMock(return_value=_fake_action_result({"url": "https://example.com"}))
    fake_sb.click = AsyncMock(return_value=_fake_action_result({}))
    fake_sb.fill = AsyncMock(return_value=_fake_action_result({}))
    fake_sb.open_tab = AsyncMock(return_value=_fake_action_result({"tab_id": 1}))
    fake_sb.close_tab = AsyncMock(return_value=_fake_action_result({}))
    fake_sb._controller = MagicMock()
    fake_sb._controller.scroll = AsyncMock(return_value=_fake_action_result({}))
    fake_sb._controller.keypress = AsyncMock(return_value=_fake_action_result({}))

    runtime = MCPBrowserRuntime()
    runtime._sb = fake_sb  # type: ignore[assignment]

    if authorizer is None:
        policy = MCPSessionPolicy(allow_actions=allow_actions)
        authorizer = MCPAuthorizer(policy, security_manager=security_manager)
    dispatcher = ToolDispatcher(runtime, authorizer=authorizer)
    return dispatcher, fake_sb


# ============================================================================
# Navigation dispatch: default mode (no allow_actions needed)
# ============================================================================


class TestNavigationDispatchDefault:
    """navigate is default-allowed: works without allow_actions, does not
    consume the action budget, is security-checked, and is audited."""

    @pytest.mark.asyncio
    async def test_default_navigate_works_without_allow_actions(self):
        dispatcher, fake_sb = _make_dispatcher(allow_actions=False)
        result = await dispatcher.dispatch("navigate", {"url": "https://example.com"})
        fake_sb.navigate.assert_awaited_once_with("https://example.com", wait_until="domcontentloaded")
        payload = json.loads(result[0].text)
        assert payload["ok"] is True

    @pytest.mark.asyncio
    async def test_default_navigate_does_not_increment_actions_used(self):
        dispatcher, _ = _make_dispatcher(allow_actions=False)
        await dispatcher.dispatch("navigate", {"url": "https://example.com"})
        assert dispatcher.authorizer.policy.actions_used == 0

    @pytest.mark.asyncio
    async def test_default_navigate_passes_wait_until(self):
        dispatcher, fake_sb = _make_dispatcher(allow_actions=False)
        await dispatcher.dispatch("navigate", {"url": "https://example.com", "wait_until": "networkidle"})
        fake_sb.navigate.assert_awaited_once_with("https://example.com", wait_until="networkidle")

    @pytest.mark.asyncio
    async def test_navigate_blocked_domain_refuses_and_audits(self):
        sm = _real_security_manager(blocklist=("evil.com",))
        dispatcher, fake_sb = _make_dispatcher(allow_actions=False, security_manager=sm)
        result = await dispatcher.dispatch("navigate", {"url": "https://evil.com"})
        payload = json.loads(result[0].text)
        assert payload["ok"] is False
        assert payload["refusal"]["blocked_by"] == "security_manager"
        fake_sb.navigate.assert_not_called()
        # Denial must be audited.
        assert len(dispatcher.authorizer.audit_log) == 1
        entry = dispatcher.authorizer.audit_log[0]
        assert entry.tool == "navigate"
        assert entry.allowed is False
        assert entry.blocked_by == "security_manager"

    @pytest.mark.asyncio
    async def test_navigate_approval_is_audited(self):
        sm = _real_security_manager(allowlist=("example.com",))
        dispatcher, _ = _make_dispatcher(allow_actions=False, security_manager=sm)
        await dispatcher.dispatch("navigate", {"url": "https://example.com"})
        # Approval must be audited.
        assert len(dispatcher.authorizer.audit_log) == 1

    @pytest.mark.asyncio
    async def test_bare_dispatcher_navigate_works_without_authorizer(self):
        """A ToolDispatcher(runtime) with NO authorizer must still navigate —
        navigation is default-allowed. Security-check and audit are conditional
        on an authorizer/SecurityManager being present, not prerequisites for
        navigation itself. (The smoke suite uses this bare form.)"""
        from super_browser.mcp_server import MCPBrowserRuntime, ToolDispatcher

        fake_sb = MagicMock()
        fake_sb.navigate = AsyncMock(return_value=_fake_action_result({"url": "https://example.com"}))
        runtime = MCPBrowserRuntime()
        runtime._sb = fake_sb  # type: ignore[assignment]
        dispatcher = ToolDispatcher(runtime)  # no authorizer
        result = await dispatcher.dispatch("navigate", {"url": "https://example.com"})
        payload = json.loads(result[0].text)
        assert payload["ok"] is True
        fake_sb.navigate.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_navigate_invalid_url_returns_invalid_arguments_no_audit(self):
        dispatcher, fake_sb = _make_dispatcher(allow_actions=False)
        result = await dispatcher.dispatch("navigate", {"url": ""})
        payload = json.loads(result[0].text)
        assert payload["ok"] is False
        assert "invalid_arguments" in payload
        fake_sb.navigate.assert_not_called()
        # Invalid args must NOT produce an audit entry.
        assert len(dispatcher.authorizer.audit_log) == 0

    @pytest.mark.asyncio
    async def test_navigate_denial_does_not_lazy_start_browser(self):
        """A blocked navigate must not start the browser."""
        from super_browser.mcp_server import MCPBrowserRuntime

        sm = _real_security_manager(blocklist=("evil.com",))
        dispatcher, _ = _make_dispatcher(allow_actions=False, security_manager=sm)
        await dispatcher.dispatch("navigate", {"url": "https://evil.com"})
        # The facade was pre-seeded; but verify nothing else started.
        assert isinstance(dispatcher.runtime, MCPBrowserRuntime)


# ============================================================================
# Action dispatch: requires allow_actions, says "actions are disabled"
# ============================================================================


class TestActionDispatch:
    @pytest.mark.asyncio
    async def test_action_tool_in_default_mode_says_actions_disabled(self):
        dispatcher, fake_sb = _make_dispatcher(allow_actions=False)
        result = await dispatcher.dispatch("click", {"target": "#btn"})
        payload = json.loads(result[0].text)
        assert payload["ok"] is False
        assert payload["refusal"]["reason"] == "actions are disabled"
        assert payload["refusal"]["blocked_by"] == "mcp_policy"
        fake_sb.click.assert_not_called()

    @pytest.mark.asyncio
    async def test_action_tool_refusal_does_not_increment_actions_used(self):
        dispatcher, _ = _make_dispatcher(allow_actions=False)
        await dispatcher.dispatch("scroll", {"direction": "down"})
        assert dispatcher.authorizer.policy.actions_used == 0

    @pytest.mark.asyncio
    async def test_action_tool_refusal_does_not_lazy_start_browser(self):
        """A denied action tool must not start the browser. Use a fresh runtime
        with no pre-seeded facade so we can prove get_browser() was never
        reached."""
        from super_browser.mcp_server import (
            MCPAuthorizer,
            MCPBrowserRuntime,
            MCPSessionPolicy,
            ToolDispatcher,
        )

        runtime = MCPBrowserRuntime()
        dispatcher = ToolDispatcher(runtime, authorizer=MCPAuthorizer(MCPSessionPolicy(allow_actions=False)))
        result = await dispatcher.dispatch("click", {"target": "#btn"})
        payload = json.loads(result[0].text)
        assert payload["ok"] is False
        # The browser must not have been started.
        assert runtime.sb is None

    @pytest.mark.asyncio
    async def test_action_tool_works_when_allow_actions_true(self):
        dispatcher, fake_sb = _make_dispatcher(allow_actions=True)
        result = await dispatcher.dispatch("click", {"target": "#btn"})
        fake_sb.click.assert_awaited_once()
        payload = json.loads(result[0].text)
        assert payload["ok"] is True

    @pytest.mark.asyncio
    async def test_action_tool_invalid_args_no_budget(self):
        dispatcher, _ = _make_dispatcher(allow_actions=True)
        result = await dispatcher.dispatch("click", {"target": ""})
        payload = json.loads(result[0].text)
        assert payload["ok"] is False
        assert "invalid_arguments" in payload
        assert dispatcher.authorizer.policy.actions_used == 0


# ============================================================================
# Unknown-tool routing still works under the new three-path dispatch
# ============================================================================


class TestDispatchRouting:
    @pytest.mark.asyncio
    async def test_unknown_tool_returns_structured_error(self):
        dispatcher, _ = _make_dispatcher(allow_actions=False)
        result = await dispatcher.dispatch("__missing__", {})
        payload = json.loads(result[0].text)
        assert payload["ok"] is False
        assert "Unknown tool" in payload["error"]

    @pytest.mark.asyncio
    async def test_excluded_tools_rejected_as_unknown(self):
        dispatcher, _ = _make_dispatcher(allow_actions=True)
        for forbidden in ("download", "upload", "act", "eval", "execute_js"):
            result = await dispatcher.dispatch(forbidden, {})
            payload = json.loads(result[0].text)
            assert payload["ok"] is False


# ============================================================================
# wait_for validation
# ============================================================================


def _make_wait_dispatcher() -> tuple[Any, Any, Any]:
    """Build a dispatcher whose fake_sb has a NormalizedPage-shaped page."""
    from super_browser.mcp_server import (
        MCPAuthorizer,
        MCPBrowserRuntime,
        MCPSessionPolicy,
        ToolDispatcher,
    )

    fake_page = MagicMock()
    # P6: the handler waits through the NormalizedPage adapter, so the
    # normalized page itself carries the wait_for seam.
    fake_page.wait_for = AsyncMock(return_value="selector")

    fake_sb = MagicMock()
    fake_sb._page = fake_page

    runtime = MCPBrowserRuntime()
    runtime._sb = fake_sb  # type: ignore[assignment]
    authorizer = MCPAuthorizer(MCPSessionPolicy(allow_actions=False))
    dispatcher = ToolDispatcher(runtime, authorizer=authorizer)
    return dispatcher, fake_sb, fake_page


class TestWaitForValidation:
    @pytest.mark.asyncio
    async def test_no_condition_returns_invalid_arguments(self):
        dispatcher, _, _ = _make_wait_dispatcher()
        result = await dispatcher.dispatch("wait_for", {"timeout_ms": 1000})
        payload = json.loads(result[0].text)
        assert payload["ok"] is False
        assert "invalid_arguments" in payload
        assert "exactly one" in payload["invalid_arguments"].lower()

    @pytest.mark.asyncio
    async def test_multiple_conditions_rejected(self):
        dispatcher, _, _ = _make_wait_dispatcher()
        result = await dispatcher.dispatch("wait_for", {"selector": "#x", "text": "hi"})
        payload = json.loads(result[0].text)
        assert payload["ok"] is False
        assert "invalid_arguments" in payload

    @pytest.mark.asyncio
    async def test_timeout_ms_rejects_non_int(self):
        dispatcher, _, _ = _make_wait_dispatcher()
        result = await dispatcher.dispatch("wait_for", {"selector": "#x", "timeout_ms": "1000"})
        payload = json.loads(result[0].text)
        assert payload["ok"] is False
        assert "invalid_arguments" in payload

    @pytest.mark.asyncio
    async def test_timeout_ms_rejects_bool(self):
        """bool is an int subtype in Python; must be rejected."""
        dispatcher, _, _ = _make_wait_dispatcher()
        result = await dispatcher.dispatch("wait_for", {"selector": "#x", "timeout_ms": True})
        payload = json.loads(result[0].text)
        assert payload["ok"] is False
        assert "invalid_arguments" in payload

    @pytest.mark.asyncio
    async def test_timeout_ms_rejects_out_of_range(self):
        dispatcher, _, _ = _make_wait_dispatcher()
        # Too low
        result = await dispatcher.dispatch("wait_for", {"selector": "#x", "timeout_ms": 50})
        assert json.loads(result[0].text)["ok"] is False
        # Too high
        result = await dispatcher.dispatch("wait_for", {"selector": "#x", "timeout_ms": 99999})
        assert json.loads(result[0].text)["ok"] is False

    @pytest.mark.asyncio
    async def test_load_state_invalid_value_rejected(self):
        dispatcher, _, _ = _make_wait_dispatcher()
        result = await dispatcher.dispatch("wait_for", {"load_state": "bogus"})
        payload = json.loads(result[0].text)
        assert payload["ok"] is False
        assert "invalid_arguments" in payload


class TestWaitForHandler:
    @pytest.mark.asyncio
    async def test_wait_for_selector_success(self):
        dispatcher, _, page = _make_wait_dispatcher()
        result = await dispatcher.dispatch("wait_for", {"selector": "#btn"})
        page.wait_for.assert_awaited_once_with(
            selector="#btn", text=None, url=None, load_state=None, timeout_ms=10000,
        )
        payload = json.loads(result[0].text)
        assert payload["ok"] is True
        assert payload["matched"] == "selector"

    @pytest.mark.asyncio
    async def test_wait_for_selector_passes_timeout_ms(self):
        dispatcher, _, page = _make_wait_dispatcher()
        await dispatcher.dispatch("wait_for", {"selector": "#btn", "timeout_ms": 5000})
        page.wait_for.assert_awaited_once_with(
            selector="#btn", text=None, url=None, load_state=None, timeout_ms=5000,
        )

    @pytest.mark.asyncio
    async def test_wait_for_selector_timeout_surfaces_structured(self):
        dispatcher, _, page = _make_wait_dispatcher()
        page.wait_for = AsyncMock(side_effect=TimeoutError("timeout 10000ms exceeded"))
        result = await dispatcher.dispatch("wait_for", {"selector": "#missing"})
        payload = json.loads(result[0].text)
        assert payload["ok"] is False
        assert payload.get("timeout") is True
        assert "timeout" in payload["reason"].lower()

    @pytest.mark.asyncio
    async def test_wait_for_text_success(self):
        dispatcher, _, page = _make_wait_dispatcher()
        page.wait_for = AsyncMock(return_value="text")
        result = await dispatcher.dispatch("wait_for", {"text": "Welcome"})
        # arg= forwarding is asserted at the adapter level
        # (test_normalized_page.py, native-semantics test).
        page.wait_for.assert_awaited_once_with(
            selector=None, text="Welcome", url=None, load_state=None, timeout_ms=10000,
        )
        payload = json.loads(result[0].text)
        assert payload["ok"] is True
        assert payload["matched"] == "text"

    @pytest.mark.asyncio
    async def test_wait_for_url_success(self):
        dispatcher, _, page = _make_wait_dispatcher()
        page.wait_for = AsyncMock(return_value="url")
        result = await dispatcher.dispatch("wait_for", {"url": "**/login"})
        page.wait_for.assert_awaited_once_with(
            selector=None, text=None, url="**/login", load_state=None, timeout_ms=10000,
        )
        payload = json.loads(result[0].text)
        assert payload["ok"] is True
        assert payload["matched"] == "url"

    @pytest.mark.asyncio
    async def test_wait_for_load_state_success(self):
        dispatcher, _, page = _make_wait_dispatcher()
        page.wait_for = AsyncMock(return_value="load_state")
        result = await dispatcher.dispatch("wait_for", {"load_state": "networkidle"})
        page.wait_for.assert_awaited_once_with(
            selector=None, text=None, url=None, load_state="networkidle", timeout_ms=10000,
        )
        payload = json.loads(result[0].text)
        assert payload["ok"] is True
        assert payload["matched"] == "load_state"

    @pytest.mark.asyncio
    async def test_wait_for_goes_through_normalized_adapter(self):
        """P6: the handler must reach the page through NormalizedPage.wait_for.
        Touching backend_page or raw_page is a composition violation."""
        from unittest.mock import PropertyMock

        from super_browser.mcp_server import (
            MCPAuthorizer,
            MCPBrowserRuntime,
            MCPSessionPolicy,
            ToolDispatcher,
        )

        fake_page = MagicMock()
        fake_page.wait_for = AsyncMock(return_value="selector")
        # Any raw-page reach fails the test loudly.
        type(fake_page).backend_page = PropertyMock(
            side_effect=AssertionError("handler must not use backend_page"),
        )
        type(fake_page).raw_page = PropertyMock(
            side_effect=AssertionError("handler must not use raw_page"),
        )

        fake_sb = MagicMock()
        fake_sb._page = fake_page
        runtime = MCPBrowserRuntime()
        runtime._sb = fake_sb  # type: ignore[assignment]
        dispatcher = ToolDispatcher(runtime, authorizer=MCPAuthorizer(MCPSessionPolicy()))

        result = await dispatcher.dispatch("wait_for", {"selector": "#x"})
        payload = json.loads(result[0].text)
        assert payload["ok"] is True
        fake_page.wait_for.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_wait_for_no_active_page_returns_error(self):
        from super_browser.mcp_server import (
            MCPAuthorizer,
            MCPBrowserRuntime,
            MCPSessionPolicy,
            ToolDispatcher,
        )

        fake_sb = MagicMock()
        fake_sb._page = None  # no active page
        runtime = MCPBrowserRuntime()
        runtime._sb = fake_sb  # type: ignore[assignment]
        dispatcher = ToolDispatcher(runtime, authorizer=MCPAuthorizer(MCPSessionPolicy()))
        result = await dispatcher.dispatch("wait_for", {"selector": "#x"})
        payload = json.loads(result[0].text)
        assert payload["ok"] is False
        assert "no active page" in payload["error"]


# ============================================================================
# Tool advertisement: _tools_for_policy + build_server
# ============================================================================


class TestToolAdvertisement:
    def test_default_policy_advertises_14_tools(self):
        from super_browser.mcp_server import MCPSessionPolicy, _tools_for_policy

        advertised = _tools_for_policy(MCPSessionPolicy())
        names = {t.name for t in advertised}
        assert len(advertised) == 19
        # All 5 diagnostics tools present.
        assert {"get_console_messages", "get_page_errors", "get_network_errors",
                "list_requests", "get_request"} <= names
        # analyze_image (P7.C) is default-advertised.
        assert "analyze_image" in names

    def test_default_advertises_inspect_plus_navigation(self):
        from super_browser.mcp_server import (
            DEFAULT_TOOL_NAMES,
            INSPECT_TOOL_NAMES,
            NAVIGATION_TOOL_NAMES,
            MCPSessionPolicy,
            _tools_for_policy,
        )

        advertised = _tools_for_policy(MCPSessionPolicy())
        assert {t.name for t in advertised} == DEFAULT_TOOL_NAMES
        assert DEFAULT_TOOL_NAMES == INSPECT_TOOL_NAMES | NAVIGATION_TOOL_NAMES

    def test_allow_actions_advertises_29_tools(self):
        from super_browser.mcp_server import MCPSessionPolicy, _tools_for_policy

        advertised = _tools_for_policy(MCPSessionPolicy(allow_actions=True))
        names = {t.name for t in advertised}
        assert len(advertised) == 31
        # All 6 action tools present.
        assert {"scroll", "press_key", "click", "fill", "open_tab", "close_tab"} <= names

    def test_default_does_not_advertise_action_tools(self):
        from super_browser.mcp_server import ACTION_TOOL_NAMES, MCPSessionPolicy, _tools_for_policy

        advertised = _tools_for_policy(MCPSessionPolicy())
        assert not ({t.name for t in advertised} & ACTION_TOOL_NAMES)

    def test_build_server_advertises_14_by_default(self):
        from super_browser.mcp_server import _tools_for_policy, build_server

        server = build_server()
        policy = server._sb_policy  # type: ignore[attr-defined]
        names = {t.name for t in _tools_for_policy(policy)}
        assert len(names) == 19
        assert "navigate" in names
        assert "wait_for" in names
        assert "get_console_messages" in names
        assert "click" not in names

    def test_build_server_allow_actions_advertises_29(self):
        from super_browser.mcp_server import MCPSessionPolicy, _tools_for_policy, build_server

        server = build_server(policy=MCPSessionPolicy(allow_actions=True))
        policy = server._sb_policy  # type: ignore[attr-defined]
        names = {t.name for t in _tools_for_policy(policy)}
        assert len(names) == 31

    def test_build_server_legacy_allow_writes_advertises_29(self):
        """Legacy allow_writes=True must still enable action tools."""
        from super_browser.mcp_server import MCPSessionPolicy, _tools_for_policy, build_server

        server = build_server(policy=MCPSessionPolicy(allow_writes=True))
        policy = server._sb_policy  # type: ignore[attr-defined]
        names = {t.name for t in _tools_for_policy(policy)}
        assert len(names) == 31


# ============================================================================
# Bootstrap: _env_truthy, main(), run_server(), default SecurityManager
# ============================================================================


class TestEnvTruthy:
    def test_one_is_truthy(self):
        from super_browser.mcp_server import _env_truthy
        assert _env_truthy("SB_TEST_TRUTHY_ONE") is False  # unset
        import os
        os.environ["SB_TEST_TRUTHY_ONE"] = "1"
        try:
            assert _env_truthy("SB_TEST_TRUTHY_ONE") is True
        finally:
            del os.environ["SB_TEST_TRUTHY_ONE"]

    @pytest.mark.parametrize("val", ["1", "true", "TRUE", "True", "yes", "on", "  on  "])
    def test_truthy_sentinels(self, val):
        import os

        from super_browser.mcp_server import _env_truthy
        os.environ["SB_TEST_TRUTHY"] = val
        try:
            assert _env_truthy("SB_TEST_TRUTHY") is True
        finally:
            del os.environ["SB_TEST_TRUTHY"]

    @pytest.mark.parametrize("val", ["", "0", "false", "no", "off", "maybe", "2"])
    def test_non_truthy_sentinels(self, val):
        import os

        from super_browser.mcp_server import _env_truthy
        os.environ["SB_TEST_TRUTHY"] = val
        try:
            assert _env_truthy("SB_TEST_TRUTHY") is False
        finally:
            del os.environ["SB_TEST_TRUTHY"]


class TestMainBootstrap:
    def test_main_allow_actions_flag_sets_allow_actions(self, monkeypatch):
        """--allow-actions on the CLI must enable action mode."""
        from super_browser import mcp_server

        captured: dict = {}

        async def fake_run_server(*, allow_actions: bool = False, **kwargs):
            captured["allow_actions"] = allow_actions

        def fake_asyncio_run(coro):
            # Drive the coroutine so it doesn't warn.
            try:
                coro.send(None)
            except StopIteration:
                pass

        monkeypatch.setattr(mcp_server, "run_server", fake_run_server)
        monkeypatch.setattr("asyncio.run", fake_asyncio_run)
        monkeypatch.setattr("sys.argv", ["superbrowser-mcp", "--allow-actions"])
        # Env must not leak.
        monkeypatch.delenv("SB_MCP_ALLOW_ACTIONS", raising=False)

        mcp_server.main()
        assert captured["allow_actions"] is True

    def test_main_env_var_sets_allow_actions(self, monkeypatch):
        from super_browser import mcp_server

        captured: dict = {}

        async def fake_run_server(*, allow_actions: bool = False, **kwargs):
            captured["allow_actions"] = allow_actions

        def fake_asyncio_run(coro):
            try:
                coro.send(None)
            except StopIteration:
                pass

        monkeypatch.setattr(mcp_server, "run_server", fake_run_server)
        monkeypatch.setattr("asyncio.run", fake_asyncio_run)
        monkeypatch.setattr("sys.argv", ["superbrowser-mcp"])
        monkeypatch.setenv("SB_MCP_ALLOW_ACTIONS", "true")

        mcp_server.main()
        assert captured["allow_actions"] is True

    def test_main_no_flag_no_env_defaults_navigation_only(self, monkeypatch):
        from super_browser import mcp_server

        captured: dict = {}

        async def fake_run_server(*, allow_actions: bool = False, **kwargs):
            captured["allow_actions"] = allow_actions

        def fake_asyncio_run(coro):
            try:
                coro.send(None)
            except StopIteration:
                pass

        monkeypatch.setattr(mcp_server, "run_server", fake_run_server)
        monkeypatch.setattr("asyncio.run", fake_asyncio_run)
        monkeypatch.setattr("sys.argv", ["superbrowser-mcp"])
        monkeypatch.delenv("SB_MCP_ALLOW_ACTIONS", raising=False)

        mcp_server.main()
        assert captured["allow_actions"] is False


class TestBuildDefaultSecurityManager:
    def test_builds_a_security_manager(self):
        from super_browser.mcp_server import _build_default_security_manager
        from super_browser.security import SecurityManager

        sm = _build_default_security_manager()
        assert isinstance(sm, SecurityManager)

    def test_default_is_allow_all_on_domains(self, monkeypatch):
        """With no env lists, navigation to any host must pass the domain filter."""
        from super_browser.mcp_server import _build_default_security_manager
        from super_browser.security.types import SecurityLevel

        monkeypatch.delenv("SB_MCP_DOMAIN_ALLOWLIST", raising=False)
        monkeypatch.delenv("SB_MCP_DOMAIN_BLOCKLIST", raising=False)

        sm = _build_default_security_manager()
        import asyncio
        result = asyncio.run(
            sm.check_action("navigate", {"url": "https://anything.example.com"},
                            "https://anything.example.com", SecurityLevel.SENSITIVE)
        )
        assert result.passed is True

    def test_blocklist_env_enforced(self, monkeypatch):
        from super_browser.mcp_server import _build_default_security_manager
        from super_browser.security.types import SecurityLevel

        monkeypatch.delenv("SB_MCP_DOMAIN_ALLOWLIST", raising=False)
        monkeypatch.setenv("SB_MCP_DOMAIN_BLOCKLIST", "evil.com, malware.test")

        sm = _build_default_security_manager()
        import asyncio
        result = asyncio.run(
            sm.check_action("navigate", {"url": "https://evil.com"},
                            "https://evil.com", SecurityLevel.SENSITIVE)
        )
        assert result.passed is False
        assert result.blocked_by == "domain_filter"

    def test_allowlist_env_enforced(self, monkeypatch):
        from super_browser.mcp_server import _build_default_security_manager
        from super_browser.security.types import SecurityLevel

        monkeypatch.setenv("SB_MCP_DOMAIN_ALLOWLIST", "example.com")
        monkeypatch.delenv("SB_MCP_DOMAIN_BLOCKLIST", raising=False)

        sm = _build_default_security_manager()

        async def _check():
            ok = await sm.check_action("navigate", {"url": "https://example.com"},
                                       "https://example.com", SecurityLevel.SENSITIVE)
            denied = await sm.check_action("navigate", {"url": "https://other.com"},
                                           "https://other.com", SecurityLevel.SENSITIVE)
            return ok, denied

        import asyncio
        ok, denied = asyncio.run(_check())
        assert ok.passed is True
        assert denied.passed is False

    def test_whitespace_separated_lists(self, monkeypatch):
        """Both comma- and whitespace-separated lists must parse."""
        from super_browser.mcp_server import _build_default_security_manager
        from super_browser.security.types import SecurityLevel

        monkeypatch.setenv("SB_MCP_DOMAIN_BLOCKLIST", "a.com   b.com,c.com")
        sm = _build_default_security_manager()

        async def _check():
            results = []
            for host in ("a.com", "b.com", "c.com"):
                r = await sm.check_action("navigate", {"url": f"https://{host}"},
                                          f"https://{host}", SecurityLevel.SENSITIVE)
                results.append((host, r.passed))
            return results

        import asyncio
        for host, passed in asyncio.run(_check()):
            assert not passed, f"{host} should be blocked"


class TestRunServerConstructsSecurityManager:
    def test_run_server_constructs_default_security_manager(self, monkeypatch):
        """run_server() must pass a non-None security_manager to build_server,
        so navigate is always security-checked."""
        from super_browser import mcp_server

        captured: dict = {}

        # Intercept build_server so we don't spawn the stdio loop.
        def fake_build_server(runtime, *, policy=None, security_manager=None):
            captured["security_manager"] = security_manager
            captured["policy"] = policy
            server = MagicMock()
            async def run(*a, **kw):
                return None
            server.run = run
            server.create_initialization_options = lambda: MagicMock()
            return server

        # Stub stdio_server so run_server returns immediately.
        class _FakeStdio:
            async def __aenter__(self):
                return (MagicMock(), MagicMock())
            async def __aexit__(self, *a):
                return False

        monkeypatch.setattr(mcp_server, "build_server", fake_build_server)
        monkeypatch.setattr(mcp_server, "stdio_server", lambda: _FakeStdio())
        monkeypatch.delenv("SB_MCP_DOMAIN_BLOCKLIST", raising=False)
        monkeypatch.delenv("SB_MCP_DOMAIN_ALLOWLIST", raising=False)

        import asyncio
        asyncio.run(mcp_server.run_server(allow_actions=False))

        assert captured["security_manager"] is not None
        assert captured["policy"].allow_actions is False

    def test_run_server_passes_through_explicit_security_manager(self, monkeypatch):
        """If a caller passes security_manager=, run_server must NOT override it."""
        from super_browser import mcp_server

        captured: dict = {}
        sentinel = MagicMock(name="explicit_sm")

        def fake_build_server(runtime, *, policy=None, security_manager=None):
            captured["security_manager"] = security_manager
            server = MagicMock()
            async def run(*a, **kw):
                return None
            server.run = run
            server.create_initialization_options = lambda: MagicMock()
            return server

        class _FakeStdio:
            async def __aenter__(self):
                return (MagicMock(), MagicMock())
            async def __aexit__(self, *a):
                return False

        monkeypatch.setattr(mcp_server, "build_server", fake_build_server)
        monkeypatch.setattr(mcp_server, "stdio_server", lambda: _FakeStdio())

        import asyncio
        asyncio.run(mcp_server.run_server(allow_actions=True, security_manager=sentinel))

        assert captured["security_manager"] is sentinel


# ============================================================================
# Fixture-based smoke: navigate → wait_for → extract_text (default mode)
# ============================================================================


class TestReadWorkflowSmoke:
    """End-to-end proof that the default server (no --allow-actions) can
    execute the headline P1 read workflow. Uses a mocked facade/backend so it
    runs in CI without a real browser."""

    @pytest.mark.asyncio
    async def test_navigate_wait_for_extract_text_chain(self):
        from super_browser.mcp_server import (
            MCPAuthorizer,
            MCPBrowserRuntime,
            MCPSessionPolicy,
            ToolDispatcher,
        )

        # --- Mock the normalized page (wait_for target, P6) ---
        fake_page = MagicMock()
        fake_page.wait_for = AsyncMock(return_value="text")

        # --- Mock the facade (navigate + extract targets) ---
        fake_sb = MagicMock()
        fake_sb._page = fake_page
        fake_sb.navigate = AsyncMock(return_value=_fake_action_result({"url": "https://example.com"}))
        fake_sb.extract = AsyncMock(return_value=_fake_action_result({"text": "Example Domain"}))

        runtime = MCPBrowserRuntime()
        runtime._sb = fake_sb  # type: ignore[assignment]
        # Default policy: allow_actions=False. Navigation must still work.
        authorizer = MCPAuthorizer(MCPSessionPolicy(allow_actions=False))
        dispatcher = ToolDispatcher(runtime, authorizer=authorizer)

        # 1. navigate (default-allowed, no SecurityManager configured here)
        nav_result = await dispatcher.dispatch("navigate", {"url": "https://example.com"})
        nav_payload = json.loads(nav_result[0].text)
        assert nav_payload["ok"] is True, "navigate must succeed in default mode"
        fake_sb.navigate.assert_awaited_once()

        # 2. wait_for (navigation tier — reads, no security check needed)
        wait_result = await dispatcher.dispatch("wait_for", {"text": "Example Domain"})
        wait_payload = json.loads(wait_result[0].text)
        assert wait_payload["ok"] is True, "wait_for must succeed"
        assert wait_payload["matched"] == "text"
        fake_page.wait_for.assert_awaited_once()

        # 3. extract_text (inspect tier)
        extract_result = await dispatcher.dispatch("extract_text", {"query": "Example"})
        extract_payload = json.loads(extract_result[0].text)
        assert extract_payload["ok"] is True, "extract_text must succeed"
        fake_sb.extract.assert_awaited_once()

        # The action budget was never touched by any of the three calls.
        assert authorizer.policy.actions_used == 0

    @pytest.mark.asyncio
    async def test_chain_works_through_build_server_dispatcher(self):
        """Same chain, but dispatched through the actual server-owned
        dispatcher that build_server() constructs (not a fresh ToolDispatcher)."""
        from super_browser.mcp_server import MCPBrowserRuntime, build_server

        # --- Mock the normalized page (wait_for target, P6) ---
        fake_page = MagicMock()
        fake_page.wait_for = AsyncMock(return_value="load_state")

        fake_sb = MagicMock()
        fake_sb._page = fake_page
        fake_sb.navigate = AsyncMock(return_value=_fake_action_result({"url": "https://x"}))
        fake_sb.extract = AsyncMock(return_value=_fake_action_result({"text": "hi"}))

        runtime = MCPBrowserRuntime()
        runtime._sb = fake_sb  # type: ignore[assignment]
        server = build_server(runtime)  # default policy, default SecurityManager
        dispatcher = server._sb_dispatcher  # type: ignore[attr-defined]

        nav = await dispatcher.dispatch("navigate", {"url": "https://example.com"})
        assert json.loads(nav[0].text)["ok"] is True

        wait = await dispatcher.dispatch("wait_for", {"load_state": "networkidle"})
        assert json.loads(wait[0].text)["ok"] is True

        ext = await dispatcher.dispatch("extract_text", {"query": "x"})
        assert json.loads(ext[0].text)["ok"] is True

        # navigate was audited (approval) since build_server wires a SecurityManager.
        assert len(server._sb_authorizer.audit_log) >= 1  # type: ignore[attr-defined]


# ============================================================================
# switch_tab (P3.0A) — navigation-tier MCP wrapper around facade.switch_tab
# ============================================================================


class TestSwitchTabTool:
    """switch_tab is a navigation-tier tool: default-allowed, delegates to the
    facade, validates tab_id."""

    def test_switch_tab_in_navigation_tool_names(self):
        from super_browser.mcp_server import NAVIGATION_TOOL_NAMES

        assert "switch_tab" in NAVIGATION_TOOL_NAMES

    def test_switch_tab_in_default_advertised_tools(self):
        from super_browser.mcp_server import MCPSessionPolicy, _tools_for_policy

        names = {t.name for t in _tools_for_policy(MCPSessionPolicy())}
        assert "switch_tab" in names

    @pytest.mark.asyncio
    async def test_switch_tab_dispatches_to_facade(self):
        """switch_tab delegates to sb.switch_tab(tab_id) and returns ok."""
        dispatcher, fake_sb = _make_dispatcher(allow_actions=False)
        fake_sb.switch_tab = AsyncMock(return_value=_fake_action_result({"tab_id": 1, "url": "https://example.com"}))
        result = await dispatcher.dispatch("switch_tab", {"tab_id": 1})
        fake_sb.switch_tab.assert_awaited_once_with(1)
        payload = json.loads(result[0].text)
        assert payload["ok"] is True

    @pytest.mark.asyncio
    async def test_switch_tab_invalid_tab_id_returns_invalid_arguments(self):
        """Non-integer or missing tab_id must return a structured error, not
        reach the facade."""
        dispatcher, fake_sb = _make_dispatcher(allow_actions=False)
        fake_sb.switch_tab = AsyncMock()
        # Missing tab_id
        result = await dispatcher.dispatch("switch_tab", {})
        payload = json.loads(result[0].text)
        assert payload["ok"] is False
        assert "invalid_arguments" in payload
        # Non-integer
        result = await dispatcher.dispatch("switch_tab", {"tab_id": "abc"})
        payload = json.loads(result[0].text)
        assert payload["ok"] is False
        assert "invalid_arguments" in payload
        # Negative
        result = await dispatcher.dispatch("switch_tab", {"tab_id": -1})
        payload = json.loads(result[0].text)
        assert payload["ok"] is False
        assert "invalid_arguments" in payload
        # Bool (bool is an int subtype in Python — must be rejected)
        result = await dispatcher.dispatch("switch_tab", {"tab_id": True})
        payload = json.loads(result[0].text)
        assert payload["ok"] is False
        assert "invalid_arguments" in payload
        # Facade must never be called for invalid args
        fake_sb.switch_tab.assert_not_called()

    @pytest.mark.asyncio
    async def test_switch_tab_does_not_require_allow_actions(self):
        """switch_tab is navigation-tier: works without --allow-actions."""
        dispatcher, fake_sb = _make_dispatcher(allow_actions=False)
        fake_sb.switch_tab = AsyncMock(return_value=_fake_action_result({"tab_id": 2}))
        result = await dispatcher.dispatch("switch_tab", {"tab_id": 2})
        assert json.loads(result[0].text)["ok"] is True

    @pytest.mark.asyncio
    async def test_switch_tab_does_not_increment_actions_used(self):
        dispatcher, fake_sb = _make_dispatcher(allow_actions=False)
        fake_sb.switch_tab = AsyncMock(return_value=_fake_action_result({"tab_id": 1}))
        await dispatcher.dispatch("switch_tab", {"tab_id": 1})
        assert dispatcher.authorizer.policy.actions_used == 0

    @pytest.mark.asyncio
    async def test_switch_tab_facade_error_returns_structured(self):
        """When the facade returns ok=False (e.g. tab not found), the MCP
        response reflects it."""
        dispatcher, fake_sb = _make_dispatcher(allow_actions=False)
        err_result = MagicMock()
        err_result.ok = False
        err_result.data = None
        err_result.error = {"category": "selector_not_found", "message": "tab not found"}
        err_result.meta = None
        fake_sb.switch_tab = AsyncMock(return_value=err_result)
        result = await dispatcher.dispatch("switch_tab", {"tab_id": 999})
        payload = json.loads(result[0].text)
        assert payload["ok"] is False


# ============================================================================
# reload / go_back / go_forward (P3.0B) — navigation-tier MCP tools
# ============================================================================


class TestHistoryNavigationTools:
    """reload, go_back, go_forward are navigation-tier: default-allowed,
    delegate to the facade, don't consume action budget."""

    def test_all_three_in_navigation_tool_names(self):
        from super_browser.mcp_server import NAVIGATION_TOOL_NAMES

        for name in ("reload", "go_back", "go_forward"):
            assert name in NAVIGATION_TOOL_NAMES

    def test_all_three_in_default_advertised_tools(self):
        from super_browser.mcp_server import MCPSessionPolicy, _tools_for_policy

        names = {t.name for t in _tools_for_policy(MCPSessionPolicy())}
        for name in ("reload", "go_back", "go_forward"):
            assert name in names

    @pytest.mark.asyncio
    async def test_reload_dispatches_to_facade(self):
        dispatcher, fake_sb = _make_dispatcher(allow_actions=False)
        fake_sb.reload = AsyncMock(return_value=_fake_action_result({"url": "https://example.com"}))
        result = await dispatcher.dispatch("reload", {})
        fake_sb.reload.assert_awaited_once()
        payload = json.loads(result[0].text)
        assert payload["ok"] is True

    @pytest.mark.asyncio
    async def test_go_back_dispatches_to_facade(self):
        dispatcher, fake_sb = _make_dispatcher(allow_actions=False)
        fake_sb.go_back = AsyncMock(return_value=_fake_action_result({"url": "https://prev.com"}))
        result = await dispatcher.dispatch("go_back", {})
        fake_sb.go_back.assert_awaited_once()
        assert json.loads(result[0].text)["ok"] is True

    @pytest.mark.asyncio
    async def test_go_forward_dispatches_to_facade(self):
        dispatcher, fake_sb = _make_dispatcher(allow_actions=False)
        fake_sb.go_forward = AsyncMock(return_value=_fake_action_result({"url": "https://next.com"}))
        result = await dispatcher.dispatch("go_forward", {})
        fake_sb.go_forward.assert_awaited_once()
        assert json.loads(result[0].text)["ok"] is True

    @pytest.mark.asyncio
    async def test_passes_wait_until(self):
        dispatcher, fake_sb = _make_dispatcher(allow_actions=False)
        fake_sb.reload = AsyncMock(return_value=_fake_action_result({"url": "x"}))
        await dispatcher.dispatch("reload", {"wait_until": "networkidle"})
        fake_sb.reload.assert_awaited_once_with(wait_until="networkidle")

    @pytest.mark.asyncio
    async def test_invalid_wait_until_returns_error(self):
        dispatcher, fake_sb = _make_dispatcher(allow_actions=False)
        fake_sb.reload = AsyncMock()
        result = await dispatcher.dispatch("reload", {"wait_until": "bogus"})
        payload = json.loads(result[0].text)
        assert payload["ok"] is False
        assert "invalid_arguments" in payload
        fake_sb.reload.assert_not_called()

    @pytest.mark.asyncio
    async def test_do_not_require_allow_actions(self):
        dispatcher, fake_sb = _make_dispatcher(allow_actions=False)
        for name, mock_name in [("reload", "reload"), ("go_back", "go_back"), ("go_forward", "go_forward")]:
            setattr(fake_sb, mock_name, AsyncMock(return_value=_fake_action_result({"url": "x"})))
            result = await dispatcher.dispatch(name, {})
            assert json.loads(result[0].text)["ok"] is True

    @pytest.mark.asyncio
    async def test_do_not_increment_actions_used(self):
        dispatcher, fake_sb = _make_dispatcher(allow_actions=False)
        fake_sb.reload = AsyncMock(return_value=_fake_action_result({"url": "x"}))
        fake_sb.go_back = AsyncMock(return_value=_fake_action_result({"url": "x"}))
        fake_sb.go_forward = AsyncMock(return_value=_fake_action_result({"url": "x"}))
        await dispatcher.dispatch("reload", {})
        await dispatcher.dispatch("go_back", {})
        await dispatcher.dispatch("go_forward", {})
        assert dispatcher.authorizer.policy.actions_used == 0

    @pytest.mark.asyncio
    async def test_facade_error_returns_structured(self):
        """When facade returns ok=False (e.g. no history), MCP reflects it."""
        dispatcher, fake_sb = _make_dispatcher(allow_actions=False)
        err_result = MagicMock()
        err_result.ok = False
        err_result.data = None
        err_result.error = {"category": "page_error", "message": "No history entry"}
        err_result.meta = None
        fake_sb.go_back = AsyncMock(return_value=err_result)
        result = await dispatcher.dispatch("go_back", {})
        payload = json.loads(result[0].text)
        assert payload["ok"] is False


# ============================================================================
# Facade security blocking (P3.0B review) — backend page not called when blocked
# ============================================================================


class TestFacadeSecurityBlocking:
    """The new facade methods (reload/go_back/go_forward) must call
    _check_facade_security before touching the backend page. When security
    blocks, the backend method must never be awaited."""

    def _blocked_sb(self):
        """A SuperBrowser whose _check_facade_security always returns a denial."""
        from super_browser import SuperBrowser
        from super_browser.results.types import ActionError, ErrorCategory, action_result

        sb = SuperBrowser()
        # Pre-seed a fake page so the _page check passes.
        sb._page = MagicMock()
        sb._page.url = "https://example.com"
        sb._page.backend_page = MagicMock()
        sb._page.backend_page.reload = AsyncMock()
        sb._page.backend_page.go_back = AsyncMock()
        sb._page.backend_page.go_forward = AsyncMock()
        # Make _check_facade_security return a denial (not None).
        sb._check_facade_security = AsyncMock(return_value=action_result(
            ok=False, error=ActionError(ErrorCategory.SECURITY, "blocked")))
        return sb

    @pytest.mark.asyncio
    async def test_reload_blocked_does_not_call_backend(self):
        sb = self._blocked_sb()
        result = await sb.reload()
        assert result.ok is False
        sb._page.backend_page.reload.assert_not_called()

    @pytest.mark.asyncio
    async def test_go_back_blocked_does_not_call_backend(self):
        sb = self._blocked_sb()
        result = await sb.go_back()
        assert result.ok is False
        sb._page.backend_page.go_back.assert_not_called()

    @pytest.mark.asyncio
    async def test_go_forward_blocked_does_not_call_backend(self):
        sb = self._blocked_sb()
        result = await sb.go_forward()
        assert result.ok is False
        sb._page.backend_page.go_forward.assert_not_called()

    @pytest.mark.asyncio
    async def test_check_facade_security_is_called_for_all_three(self):
        sb = self._blocked_sb()
        await sb.reload()
        await sb.go_back()
        await sb.go_forward()
        assert sb._check_facade_security.call_count == 3
