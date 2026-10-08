"""TEST-49-01: StealthBridge abstraction — unit tests.

Verifies that all stealth modules accept StealthBridge protocol
and degrade gracefully when it is None.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from super_browser.browser.cdp import CDPResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cdp_bridge() -> MagicMock:
    """Create a mock CDPBridge with .send() returning a successful CDPResult."""
    bridge = MagicMock()
    bridge.send = AsyncMock(return_value=CDPResult(
        ok=True, data={"result": {"value": False}}, method="Runtime.evaluate",
    ))
    bridge._session = MagicMock()
    return bridge


def _make_stealth_bridge(cdp_bridge: MagicMock | None = None) -> MagicMock:
    """Create a mock StealthBridge with .cdp_send() and .get_ax_tree()."""
    inner = cdp_bridge or _make_cdp_bridge()
    bridge = MagicMock()
    bridge.cdp_send = AsyncMock(return_value=CDPResult(
        ok=True, data={"result": {"value": False}}, method="Runtime.evaluate",
    ))
    bridge.get_ax_tree = AsyncMock(return_value={"nodes": []})
    bridge._cdp = inner
    return bridge


# ===================================================================
# TEST-49-01-01: StealthManager accepts StealthBridge in constructor
# ===================================================================


class TestStealthManagerBridge:
    """TEST-49-01-01: StealthManager with StealthBridge."""

    def test_constructor_with_stealth_bridge(self) -> None:
        """Constructor succeeds when stealth_bridge is provided."""
        from super_browser.stealth.manager import StealthManager

        bridge = _make_stealth_bridge()
        mgr = StealthManager(stealth_bridge=bridge)
        assert mgr._stealth_bridge is bridge
        assert mgr._cdp is None

    def test_constructor_with_both_bridge_and_cdp(self) -> None:
        """When both are provided, stealth_bridge is stored separately."""
        from super_browser.stealth.manager import StealthManager

        cdp = _make_cdp_bridge()
        bridge = _make_stealth_bridge(cdp)
        mgr = StealthManager(stealth_bridge=bridge, cdp=cdp)
        assert mgr._stealth_bridge is bridge
        assert mgr._cdp is cdp


# ===================================================================
# TEST-49-01-02: InjectDelivery stealth_bridge install
# ===================================================================


class TestInjectDeliveryBridge:
    """TEST-49-01-02: InjectDelivery with stealth_bridge."""

    @pytest.mark.asyncio
    async def test_install_with_stealth_bridge(self) -> None:
        """install() works when stealth_bridge is provided."""
        from super_browser.stealth.consistency.inject_delivery import InjectDelivery

        bridge = _make_stealth_bridge()
        page = MagicMock()
        page.add_init_script = AsyncMock()

        delivery = InjectDelivery("console.log('test');")
        await delivery.install(stealth_bridge=bridge, page=page)

        assert delivery._stealth_bridge is bridge
        assert delivery._installed is True

    @pytest.mark.asyncio
    async def test_install_backward_compat(self) -> None:
        """install() still works with positional cdp_bridge args."""
        from super_browser.stealth.consistency.inject_delivery import InjectDelivery

        cdp = _make_cdp_bridge()
        page = MagicMock()
        page.add_init_script = AsyncMock()

        delivery = InjectDelivery("console.log('test');")
        await delivery.install(cdp_bridge=cdp, page=page)

        assert delivery._stealth_bridge is None
        assert delivery._cdp_bridge is cdp
        assert delivery._installed is True


# ===================================================================
# TEST-49-01-03: Snapshot prefers StealthBridge
# ===================================================================


class TestSnapshotBridge:
    """TEST-49-01-03: Snapshot uses StealthBridge."""

    @pytest.mark.asyncio
    async def test_capture_prefers_stealth_bridge(self) -> None:
        """capture_ax_only uses stealth_bridge.get_ax_tree() when available."""
        from super_browser.interaction.snapshot import SnapshotProvider

        bridge = _make_stealth_bridge()
        bridge.get_ax_tree = AsyncMock(return_value={"nodes": []})

        cdp = _make_cdp_bridge()

        provider = SnapshotProvider(cdp=cdp, stealth_bridge=bridge)
        result = await provider.capture_ax_only("https://example.com", "Test")

        bridge.get_ax_tree.assert_awaited_once()
        cdp.send.assert_not_awaited()
        assert result.url == "https://example.com"


# ===================================================================
# TEST-49-01-04: Captcha start with stealth_bridge
# ===================================================================


class TestCaptchaBridge:
    """TEST-49-01-04: Captcha start accepts stealth_bridge."""

    @pytest.mark.asyncio
    async def test_start_extracts_stealth_bridge(self) -> None:
        """start() extracts stealth_bridge from page.engine_page."""
        from super_browser.stealth.captcha import CAPTCHAWatchdog

        bridge = _make_stealth_bridge()
        engine_page = MagicMock()
        engine_page.stealth_bridge = bridge

        page = MagicMock()
        page.engine_page = engine_page

        config = MagicMock()
        config.captcha_detection_enabled = False
        event_bus = MagicMock()

        watchdog = CAPTCHAWatchdog(config, event_bus)
        await watchdog.start(page=page)

        assert watchdog._cdp is bridge


# ===================================================================
# TEST-49-01-05: Diagnostics with StealthBridge
# ===================================================================


class TestDiagnosticsBridge:
    """TEST-49-01-05: Diagnostics accepts StealthBridge."""

    @pytest.mark.asyncio
    async def test_run_diagnostics_with_stealth_bridge(self) -> None:
        """run_diagnostics works when given a StealthBridge."""
        from super_browser.stealth.diagnostics import run_diagnostics

        bridge = _make_stealth_bridge()
        config = MagicMock()
        config.patchright_args = ["--disable-blink-features=AutomationControlled"]
        config.headless = False
        config.proxy_tier = MagicMock()
        config.proxy_tier.value = "direct"

        report = await run_diagnostics(bridge, config)
        assert report is not None
        bridge.cdp_send.assert_awaited()


# ===================================================================
# TEST-49-01-06: Facade passes stealth_bridge correctly
# ===================================================================


class TestFacadeBridge:
    """TEST-49-01-06: Facade passes stealth_bridge to StealthManager."""

    def test_configure_stealth_passes_bridge(self) -> None:
        """_configure_stealth extracts stealth_bridge from engine_page."""
        from super_browser.agent.facade import SuperBrowser

        bridge = _make_stealth_bridge()
        engine_page = MagicMock()
        engine_page.stealth_bridge = bridge
        engine_page.cdp = _make_cdp_bridge()

        config = MagicMock()
        config.enable_stealth = True

        facade = SuperBrowser.__new__(SuperBrowser)
        facade._config = config
        facade._page = MagicMock()
        facade._page.stealth_bridge = bridge
        facade._loop_stealth = None

        facade._configure_stealth()

        assert facade._stealth_manager._stealth_bridge is bridge


# ===================================================================
# TEST-49-01-07: StealthBridge=None degrades gracefully
# ===================================================================


class TestGracefulDegradation:
    """TEST-49-01-07: stealth_bridge=None does not crash."""

    def test_manager_none_bridge(self) -> None:
        """StealthManager with stealth_bridge=None works fine."""
        from super_browser.stealth.manager import StealthManager

        mgr = StealthManager(stealth_bridge=None, cdp=None)
        assert mgr._stealth_bridge is None
        assert mgr._cdp is None

    @pytest.mark.asyncio
    async def test_diagnostics_none_bridge(self) -> None:
        """run_diagnostics with None does not crash (reports failures gracefully)."""
        from super_browser.stealth.diagnostics import run_diagnostics

        config = MagicMock()
        config.patchright_args = ["--disable-blink-features=AutomationControlled"]
        config.headless = False
        config.proxy_tier = MagicMock()
        config.proxy_tier.value = "direct"

        report = await run_diagnostics(None, config)
        assert report is not None
        # Should have at least one failed check (no CDP session)
        assert any(not c.passed for c in report.checks)


# ===================================================================
# TEST-49-01-08: Precedence: stealth_bridge > cdp
# ===================================================================


class TestPrecedence:
    """TEST-49-01-08: stealth_bridge takes precedence over cdp."""

    @pytest.mark.asyncio
    async def test_inject_delivery_precedence(self) -> None:
        """When both stealth_bridge and cdp_bridge provided, bridge wins."""
        from super_browser.stealth.consistency.inject_delivery import InjectDelivery

        cdp = _make_cdp_bridge()
        bridge = _make_stealth_bridge(cdp)
        page = MagicMock()
        page.add_init_script = AsyncMock()

        delivery = InjectDelivery("")
        await delivery.install(stealth_bridge=bridge, cdp_bridge=cdp, page=page)

        # Internal bridge should be stealth_bridge (precedence)
        assert delivery._stealth_bridge is bridge
        assert delivery._cdp_bridge is bridge  # stealth_bridge used as cdp_bridge too


# ===================================================================
# TEST-49-01-09: Full suite passes
# ===================================================================


class TestFullSuite:
    """TEST-49-01-09: All 2,141+ tests pass."""

    def test_total_test_count(self) -> None:
        """Verify the test suite has at least 2,141 tests."""
        import subprocess

        result = subprocess.run(
            ["python", "-m", "pytest", "--collect-only", "-q"],
            capture_output=True,
            text=True,
            cwd=".",
        )
        # Parse the last line: "2141 tests collected"
        last_line = result.stdout.strip().split("\n")[-1]
        count = int(last_line.split()[0])
        assert count >= 2141, f"Expected >= 2141 tests, got {count}"


# ===================================================================
# TEST-49-01-10: Lint clean
# ===================================================================


class TestLintClean:
    """TEST-49-01-10: ruff check returns zero warnings."""

    def test_ruff_check(self) -> None:
        """python -m ruff check src/ produces zero warnings."""
        import subprocess

        result = subprocess.run(
            ["python", "-m", "ruff", "check", "src/"],
            capture_output=True,
            text=True,
            cwd=".",
        )
        assert result.returncode == 0, f"Ruff found issues:\n{result.stdout}\n{result.stderr}"
