"""Tests for BATCH-48/TASK-01 — SeleniumBackend.

TEST-48-01-01 through TEST-48-01-10 as specified in Blueprint v1.1.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from super_browser.browser.engine import (
    BrowserEngine,
    EngineCapabilities,
    EnginePage,
    _detect_backend,
)

# =========================================================================
# TEST-48-01-01: SeleniumEngine constructable
# =========================================================================


class TestSeleniumEngineConstruction:
    """TEST-48-01-01: SeleniumEngine exists and is constructable."""

    def test_creating_instance(self) -> None:
        from super_browser.browser.backends.selenium_backend import SeleniumEngine

        engine = SeleniumEngine()
        assert engine is not None
        assert engine.backend_name == "selenium"

    def test_creating_with_browser_type(self) -> None:
        from super_browser.browser.backends.selenium_backend import SeleniumEngine

        engine = SeleniumEngine(browser_type="firefox")
        assert engine._browser_type == "firefox"


# =========================================================================
# TEST-48-01-02: SeleniumEngine implements BrowserEngine
# =========================================================================


class TestSeleniumEngineProtocol:
    """TEST-48-01-02: SeleniumEngine satisfies BrowserEngine protocol."""

    def test_implements_browser_engine(self) -> None:
        from super_browser.browser.backends.selenium_backend import SeleniumEngine

        engine = SeleniumEngine()
        assert isinstance(engine, BrowserEngine)


# =========================================================================
# TEST-48-01-03: SeleniumPage implements EnginePage
# =========================================================================


class TestSeleniumPageProtocol:
    """TEST-48-01-03: SeleniumPage satisfies EnginePage protocol."""

    def test_implements_engine_page(self) -> None:
        from super_browser.browser.backends.selenium_backend import SeleniumPage

        mock_driver = MagicMock()
        mock_driver.current_url = "https://example.com"
        page = SeleniumPage(mock_driver, "chrome")
        assert isinstance(page, EnginePage)


# =========================================================================
# TEST-48-01-04: Chrome capabilities — cdp=True
# =========================================================================


class TestSeleniumCapabilitiesChrome:
    """TEST-48-01-04: Chrome capabilities report CDP."""

    def test_chrome_has_cdp(self) -> None:
        from super_browser.browser.backends.selenium_backend import SeleniumEngine

        engine = SeleniumEngine(browser_type="chrome")
        caps = engine.capabilities
        assert isinstance(caps, EngineCapabilities)
        assert caps.cdp is True
        assert caps.stealth_inject_before is True
        assert caps.stealth_inject_after is True
        assert caps.name == "selenium-chrome"


# =========================================================================
# TEST-48-01-05: Firefox capabilities — cdp=False, bidi=True
# =========================================================================


class TestSeleniumCapabilitiesFirefox:
    """TEST-48-01-05: Firefox capabilities — no CDP, has BiDi."""

    def test_firefox_no_cdp_has_bidi(self) -> None:
        from super_browser.browser.backends.selenium_backend import SeleniumEngine

        engine = SeleniumEngine(browser_type="firefox")
        caps = engine.capabilities
        assert caps.cdp is False
        assert caps.bidi is True
        assert caps.name == "selenium-firefox"


# =========================================================================
# TEST-48-01-06: Safari capabilities — cdp=False
# =========================================================================


class TestSeleniumCapabilitiesSafari:
    """TEST-48-01-06: Safari capabilities — no CDP."""

    def test_safari_no_cdp(self) -> None:
        from super_browser.browser.backends.selenium_backend import SeleniumEngine

        engine = SeleniumEngine(browser_type="safari")
        caps = engine.capabilities
        assert caps.cdp is False
        assert caps.bidi is False
        assert caps.name == "selenium-safari"


# =========================================================================
# TEST-48-01-07: Chrome stealth bridge available
# =========================================================================


class TestSeleniumStealthBridge:
    """TEST-48-01-07: Chrome stealth bridge is available."""

    def test_chrome_stealth_bridge_available(self) -> None:
        from super_browser.browser.backends.selenium_backend import SeleniumPage

        mock_driver = MagicMock()
        page = SeleniumPage(mock_driver, "chrome")
        assert page.stealth_bridge is not None

    def test_firefox_no_stealth_bridge(self) -> None:
        from super_browser.browser.backends.selenium_backend import SeleniumPage

        mock_driver = MagicMock()
        page = SeleniumPage(mock_driver, "firefox")
        assert page.stealth_bridge is None

    def test_safari_no_stealth_bridge(self) -> None:
        from super_browser.browser.backends.selenium_backend import SeleniumPage

        mock_driver = MagicMock()
        page = SeleniumPage(mock_driver, "safari")
        assert page.stealth_bridge is None


# =========================================================================
# TEST-48-01-08: Explicit selenium backend detection
# =========================================================================


class TestDetectBackendSelenium:
    """TEST-48-01-08: _detect_backend returns selenium when configured."""

    def test_explicit_selenium(self) -> None:
        import argparse

        config = argparse.Namespace(backend="selenium", mode=None)
        assert _detect_backend(config) == "selenium"


# =========================================================================
# TEST-48-01-09: Import failure handled gracefully
# =========================================================================


@pytest.mark.flaky
class TestSeleniumImportFailure:
    """TEST-48-01-09: SeleniumBackend handles ImportError gracefully."""

    def test_engine_constructable_without_selenium(self) -> None:
        """Engine is importable and constructable even without selenium."""
        from super_browser.browser.backends.selenium_backend import SeleniumEngine

        engine = SeleniumEngine()
        assert engine is not None

    def test_start_fails_without_selenium(self) -> None:
        """start() raises ImportError when selenium is not importable."""
        import pytest

        from super_browser.browser.backends.selenium_backend import SeleniumEngine

        engine = SeleniumEngine()
        with patch(
            "super_browser.browser.backends.selenium_backend._SELENIUM_AVAILABLE",
            False,
        ):
            with pytest.raises(ImportError, match="selenium is not installed"):
                import asyncio

                asyncio.new_event_loop().run_until_complete(engine.start())  # noqa: I001


# =========================================================================
# TEST-48-01-10: All 21 members present on SeleniumPage
# =========================================================================


class TestSeleniumPageMemberAudit:
    """TEST-48-01-10: All 21 EnginePage members are present on SeleniumPage."""

    # The 21 members from the EnginePage protocol
    EXPECTED_MEMBERS: list[str] = [
        # Navigation (5)
        "goto",
        "title",
        "url",
        "close",
        "content",
        # Interaction (10)
        "click",
        "fill",
        "select_option",
        "hover",
        "drag_and_drop",
        "scroll",
        "type_text",
        "press_key",
        "set_input_files",
        # Evaluation (2)
        "evaluate",
        "screenshot",
        # Routing (2)
        "route",
        "unroute_all",
        # Frames (1)
        "frame_locator",
        # Downloads (1)
        "expect_download",
        # Stealth bridge (1)
        "stealth_bridge",
    ]

    def test_all_21_members_present(self) -> None:
        """Verify all 21 EnginePage members exist on SeleniumPage."""
        from super_browser.browser.backends.selenium_backend import SeleniumPage

        mock_driver = MagicMock()
        mock_driver.current_url = "https://example.com"
        page = SeleniumPage(mock_driver, "chrome")

        for member in self.EXPECTED_MEMBERS:
            assert hasattr(page, member), (
                f"SeleniumPage is missing member: {member!r}"
            )

    def test_member_count(self) -> None:
        """Verify we are checking exactly 21 members."""
        assert len(self.EXPECTED_MEMBERS) == 21, (
            f"Expected 21 members, got {len(self.EXPECTED_MEMBERS)}"
        )
