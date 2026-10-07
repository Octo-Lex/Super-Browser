"""Gate 1 tests — Config composition and compatibility (v1.11.0).

1-A: Config.browser returns SessionConfig instance, not class.
1-B: TracingConfig.output_dir routes file tracing through Config.
1-C: Config() path with _legacy_core is None — features enable correctly.
1-D: SuperBrowserConfig (legacy) path — features still work.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from super_browser.browser.config import SessionConfig
from super_browser.config import AgentConfig, Config, TracingConfig


def _mock_engine_and_page() -> tuple[MagicMock, MagicMock]:
    """Build a (mock_engine, mock_page) pair for facade start().

    Matches the pattern from test_performance_smoke.py.
    """
    mock_page = MagicMock()
    mock_page.url = "about:blank"
    mock_page.title = AsyncMock(return_value="Blank")
    mock_page.engine_page = MagicMock()
    mock_page.engine_page.cdp = MagicMock()
    mock_page.raw_page = MagicMock()

    mock_session = AsyncMock()
    mock_session._context = MagicMock()

    mock_engine = AsyncMock()
    mock_engine.session = mock_session
    mock_engine.new_page = AsyncMock(return_value=mock_page)
    mock_engine.start = AsyncMock()
    mock_engine.stop = AsyncMock()

    return mock_engine, mock_page


# ── 1-A: Config.browser is an instance ──────────────────────────────────


class TestConfigBrowserInstance:
    """1-A — Config().browser must be a SessionConfig instance, not the class."""

    def test_default_returns_instance(self) -> None:
        cfg = Config()
        assert isinstance(cfg.browser, SessionConfig), (
            f"Config().browser is {type(cfg.browser)}, expected SessionConfig instance"
        )

    def test_default_backend_is_auto(self) -> None:
        cfg = Config()
        assert cfg.browser.backend == "auto"

    def test_custom_backend_preserved(self) -> None:
        cfg = Config(browser=SessionConfig(backend="patchright"))
        assert cfg.browser.backend == "patchright"

    def test_separate_configs_have_separate_browser(self) -> None:
        """Two Config() instances must not share a mutable browser object."""
        c1 = Config()
        c2 = Config()
        assert c1.browser is not c2.browser


# ── 1-B: TracingConfig.output_dir ───────────────────────────────────────


class TestTracingOutputDir:
    """1-B — TracingConfig.output_dir exists and routes file tracing."""

    def test_default_empty(self) -> None:
        cfg = TracingConfig()
        assert cfg.output_dir == ""

    def test_custom_output_dir(self) -> None:
        cfg = TracingConfig(enabled=True, output_dir="/tmp/trace")
        assert cfg.output_dir == "/tmp/trace"

    def test_config_composition(self) -> None:
        cfg = Config(tracing=TracingConfig(enabled=True, output_dir="/tmp/sb"))
        assert cfg.tracing.enabled is True
        assert cfg.tracing.output_dir == "/tmp/sb"

    def test_legacy_bridge_populates_output_dir(self) -> None:
        """AgentConfig trace fields map to tracing sub-config."""
        cfg = Config(agent=AgentConfig(trace_enabled=True, trace_output_dir="/tmp/leg"))
        assert cfg.agent.trace_output_dir == "/tmp/leg"
        assert cfg.agent.trace_enabled is True


# ── 1-C: Config() path features (no _legacy_core) ───────────────────────


class TestConfigPathFeatures:
    """1-C — Config() constructor enables features through cfg.agent."""

    def _make_config(
        self,
        *,
        enable_stealth: bool = False,
        enable_vision: bool = False,
        enable_skills: bool = False,
        enable_budget: bool = False,
        enable_recovery: bool = False,
        enable_security: bool = False,
    ) -> Config:
        return Config(
            agent=AgentConfig(
                enable_stealth=enable_stealth,
                enable_vision=enable_vision,
                enable_skills=enable_skills,
                enable_budget=enable_budget,
                enable_recovery=enable_recovery,
                enable_security=enable_security,
            ),
        )

    def test_no_legacy_core(self) -> None:
        """Config() path must not have _legacy_core attribute."""
        from super_browser.agent.facade import SuperBrowser

        sb = SuperBrowser(Config())
        assert not hasattr(sb, "_legacy_core") or sb._legacy_core is None

    @pytest.mark.asyncio
    async def test_stealth_via_config(self) -> None:
        """StealthManager activates through Config.agent.enable_stealth."""
        from super_browser.agent.facade import SuperBrowser

        cfg = self._make_config(enable_stealth=True)
        sb = SuperBrowser(cfg)
        mock_engine, _ = _mock_engine_and_page()
        with patch("super_browser.browser.factory._detect_backend", return_value="patchright"), \
             patch("super_browser.browser.backends.patchright_backend.PatchrightEngine", return_value=mock_engine), \
             patch("super_browser.stealth.StealthManager") as MockStealth:
            MockStealth.return_value = MagicMock()
            await sb.start()
            assert sb._stealth_manager is not None
            await sb.stop()

    @pytest.mark.asyncio
    async def test_budget_via_config(self) -> None:
        """BudgetAwareLLMClient activates through Config.agent.enable_budget."""
        from super_browser.agent.facade import SuperBrowser

        cfg = self._make_config(enable_budget=True)
        sb = SuperBrowser(cfg)
        mock_engine, _ = _mock_engine_and_page()
        with patch("super_browser.browser.factory._detect_backend", return_value="patchright"), \
             patch("super_browser.browser.backends.patchright_backend.PatchrightEngine", return_value=mock_engine):
            await sb.start()
            assert sb._budget_client is not None
            await sb.stop()

    @pytest.mark.asyncio
    async def test_tracing_file_via_config(self, tmp_path: Path) -> None:
        """FileSink activates through Config.tracing.output_dir (no legacy)."""
        from super_browser.agent.facade import SuperBrowser

        trace_dir = str(tmp_path / "traces")
        cfg = Config(
            tracing=TracingConfig(enabled=True, output_dir=trace_dir),
        )
        sb = SuperBrowser(cfg)
        mock_engine, _ = _mock_engine_and_page()
        with patch("super_browser.browser.factory._detect_backend", return_value="patchright"), \
             patch("super_browser.browser.backends.patchright_backend.PatchrightEngine", return_value=mock_engine):
            await sb.start()
            assert sb._flow_logger is not None
            # 2 sinks = console + file
            assert len(sb._flow_logger._sinks) == 2
            await sb.stop()

    @pytest.mark.asyncio
    async def test_tracing_console_via_config(self) -> None:
        """ConsoleSink activates through Config.tracing.enabled (no output_dir)."""
        from super_browser.agent.facade import SuperBrowser

        cfg = Config(tracing=TracingConfig(enabled=True))
        sb = SuperBrowser(cfg)
        mock_engine, _ = _mock_engine_and_page()
        with patch("super_browser.browser.factory._detect_backend", return_value="patchright"), \
             patch("super_browser.browser.backends.patchright_backend.PatchrightEngine", return_value=mock_engine):
            await sb.start()
            assert sb._flow_logger is not None
            # Console only = 1 sink
            assert len(sb._flow_logger._sinks) == 1
            await sb.stop()

    @pytest.mark.asyncio
    async def test_security_via_config(self) -> None:
        """SecurityManager activates through Config.agent.enable_security."""
        from super_browser.agent.facade import SuperBrowser

        cfg = self._make_config(enable_security=True)
        sb = SuperBrowser(cfg)
        mock_engine, _ = _mock_engine_and_page()
        with patch("super_browser.browser.factory._detect_backend", return_value="patchright"), \
             patch("super_browser.browser.backends.patchright_backend.PatchrightEngine", return_value=mock_engine):
            await sb.start()
            assert sb._security_manager is not None
            await sb.stop()

    @pytest.mark.asyncio
    async def test_recovery_via_config(self) -> None:
        """Recovery activates through Config.agent.enable_recovery."""
        from super_browser.agent.facade import SuperBrowser

        cfg = self._make_config(enable_recovery=True)
        sb = SuperBrowser(cfg)
        mock_engine, _ = _mock_engine_and_page()
        with patch("super_browser.browser.factory._detect_backend", return_value="patchright"), \
             patch("super_browser.browser.backends.patchright_backend.PatchrightEngine", return_value=mock_engine):
            await sb.start()
            assert sb._coordinator is not None
            await sb.stop()


# ── 1-D: SuperBrowserConfig (legacy) path compatibility ─────────────────

# ── Flat config path compatibility (formerly 1-D: SuperBrowserConfig legacy) ──


class TestFlatConfigCompat:
    """v2.0 — flat AgentConfig enables all features when used directly."""

    @pytest.mark.asyncio
    async def test_flat_stealth(self) -> None:
        from super_browser.agent.facade import SuperBrowser

        cfg = Config(agent=AgentConfig(enable_stealth=True))
        sb = SuperBrowser(cfg)
        mock_engine, _ = _mock_engine_and_page()
        with patch("super_browser.browser.factory._detect_backend", return_value="patchright"), \
             patch("super_browser.browser.backends.patchright_backend.PatchrightEngine", return_value=mock_engine), \
             patch("super_browser.stealth.StealthManager") as MockStealth:
            MockStealth.return_value = MagicMock()
            await sb.start()
            assert sb._stealth_manager is not None
            await sb.stop()

    @pytest.mark.asyncio
    async def test_flat_budget(self) -> None:
        from super_browser.agent.facade import SuperBrowser

        cfg = Config(agent=AgentConfig(enable_budget=True))
        sb = SuperBrowser(cfg)
        mock_engine, _ = _mock_engine_and_page()
        with patch("super_browser.browser.factory._detect_backend", return_value="patchright"), \
             patch("super_browser.browser.backends.patchright_backend.PatchrightEngine", return_value=mock_engine):
            await sb.start()
            assert sb._budget_client is not None
            await sb.stop()

    @pytest.mark.asyncio
    async def test_flat_tracing_file(self, tmp_path: Path) -> None:
        from super_browser.agent.facade import SuperBrowser

        trace_dir = str(tmp_path / "traces")
        cfg = Config(
            agent=AgentConfig(trace_enabled=True, trace_output_dir=trace_dir),
        )
        sb = SuperBrowser(cfg)
        mock_engine, _ = _mock_engine_and_page()
        with patch("super_browser.browser.factory._detect_backend", return_value="patchright"), \
             patch("super_browser.browser.backends.patchright_backend.PatchrightEngine", return_value=mock_engine):
            await sb.start()
            assert sb._flow_logger is not None
            assert len(sb._flow_logger._sinks) == 2  # console + file
            await sb.stop()

    @pytest.mark.asyncio
    async def test_flat_security(self) -> None:
        from super_browser.agent.facade import SuperBrowser

        cfg = Config(agent=AgentConfig(enable_security=True))
        sb = SuperBrowser(cfg)
        mock_engine, _ = _mock_engine_and_page()
        with patch("super_browser.browser.factory._detect_backend", return_value="patchright"), \
             patch("super_browser.browser.backends.patchright_backend.PatchrightEngine", return_value=mock_engine):
            await sb.start()
            assert sb._security_manager is not None
            await sb.stop()

    @pytest.mark.asyncio
    async def test_flat_recovery(self) -> None:
        from super_browser.agent.facade import SuperBrowser

        cfg = Config(agent=AgentConfig(enable_recovery=True))
        sb = SuperBrowser(cfg)
        mock_engine, _ = _mock_engine_and_page()
        with patch("super_browser.browser.factory._detect_backend", return_value="patchright"), \
             patch("super_browser.browser.backends.patchright_backend.PatchrightEngine", return_value=mock_engine):
            await sb.start()
            assert sb._coordinator is not None
            await sb.stop()
