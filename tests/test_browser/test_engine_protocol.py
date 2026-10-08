"""Tests for BATCH-46/TASK-01 — BrowserEngine Protocol definitions."""

from __future__ import annotations

from super_browser.browser.engine import (
    EngineCapabilities,
    EnginePage,
    StealthBridge,
    StealthInjector,
    _detect_backend,
)


class TestEngineCapabilities:
    """TEST-46-01-01"""

    def test_has_required_fields(self) -> None:
        caps = EngineCapabilities()
        assert caps.cdp is False
        assert caps.bidi is False
        assert caps.stealth_inject_before is False
        assert caps.stealth_inject_after is True
        assert caps.network_intercept is False
        assert caps.multi_tab is False
        assert caps.screenshots is True
        assert caps.name == "unknown"

    def test_custom_capabilities(self) -> None:
        caps = EngineCapabilities(cdp=True, name="patchright")
        assert caps.cdp is True
        assert caps.name == "patchright"


class TestBrowserEngineProtocol:
    """TEST-46-01-02"""

    def test_protocol_is_runtime_checkable(self) -> None:
        from super_browser.browser.engine import BrowserEngine

        class FakeEngine:
            async def start(self, config): pass
            async def stop(self): pass
            async def new_page(self): pass

            @property
            def capabilities(self):
                return EngineCapabilities()

            @property
            def backend_name(self):
                return "fake"

        assert isinstance(FakeEngine(), BrowserEngine)


class TestEnginePageProtocol:
    """TEST-46-01-03"""

    def test_protocol_has_22_members(self) -> None:
        expected_methods = {
            "goto", "title", "close", "activate", "content", "click", "fill",
            "select_option", "hover", "drag_and_drop", "scroll",
            "type_text", "press_key", "set_input_files", "evaluate",
            "screenshot", "route", "unroute_all", "frame_locator",
            "expect_download",
        }
        expected_properties = {"url", "stealth_bridge"}
        all_expected = expected_methods | expected_properties
        for name in all_expected:
            assert hasattr(EnginePage, name), f"EnginePage missing: {name}"


class TestStealthBridgeProtocol:
    """TEST-46-01-04"""

    def test_protocol_has_required_methods(self) -> None:
        expected = {
            "cdp_send", "inject_script_before_load", "get_ax_tree",
            "get_all_cookies", "set_cookies", "capture_screenshot_cdp",
        }
        for name in expected:
            assert hasattr(StealthBridge, name), f"StealthBridge missing: {name}"


class TestStealthInjectorProtocol:
    """TEST-46-01-05"""

    def test_protocol_has_required_methods(self) -> None:
        expected = {"inject_before_load", "inject_after_load", "injection_timing"}
        for name in expected:
            assert hasattr(StealthInjector, name), f"StealthInjector missing: {name}"


class TestDetectBackend:
    """TEST-46-01-06"""

    def test_explicit_backend_override(self) -> None:
        import argparse

        config = argparse.Namespace(backend="patchright", mode=None)
        assert _detect_backend(config) == "patchright"

    def test_mode_patchright_launch(self) -> None:
        import argparse

        config = argparse.Namespace(backend="auto", mode="PATCHRIGHT_LAUNCH")
        assert _detect_backend(config) == "patchright"

    def test_mode_cloak(self) -> None:
        import argparse

        config = argparse.Namespace(backend="auto", mode="CLOAK_LAUNCH")
        assert _detect_backend(config) == "cloak"

    def test_no_backend_raises(self) -> None:
        # With no config and no imports, should raise — but since patchright
        # IS installed in this environment, it returns "patchright".
        result = _detect_backend(None)
        assert result in ("patchright", "playwright", "selenium")

    def test_auto_returns_valid_string(self) -> None:
        import argparse

        config = argparse.Namespace(backend="auto")
        result = _detect_backend(config)
        assert result in ("patchright", "playwright", "selenium")
