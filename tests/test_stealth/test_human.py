"""Tests for BATCH-28/TASK-01 — Human Behavior Adapter.

Test IDs: TEST-28-01-01 through TEST-28-01-05
"""

from unittest.mock import AsyncMock, patch

import pytest

from super_browser.stealth.human import HumanBehaviorAdapter
from super_browser.stealth.human_config import HumanConfig

# -- TEST-28-01-01: HumanConfig defaults are correct ------------------------


class TestHumanConfigDefaults:
    """TEST-28-01-01: HumanConfig defaults match spec."""

    def test_typing_delay_ms_default(self):
        cfg = HumanConfig()
        assert cfg.typing_delay_ms == (50, 150)

    def test_mouse_jitter_px_default(self):
        cfg = HumanConfig()
        assert cfg.mouse_jitter_px == 3.0

    def test_click_hold_ms_default(self):
        cfg = HumanConfig()
        assert cfg.click_hold_ms == (50, 200)

    def test_scroll_step_px_default(self):
        cfg = HumanConfig()
        assert cfg.scroll_step_px == 300

    def test_pause_between_actions_default(self):
        cfg = HumanConfig()
        assert cfg.pause_between_actions == (0.3, 1.5)

    def test_typo_chance_default(self):
        cfg = HumanConfig()
        assert cfg.typo_chance == 0.02

    def test_preset_default(self):
        cfg = HumanConfig()
        assert cfg.preset == "default"


# -- TEST-28-01-02: preset="careful" sets slower timings -------------------


class TestCarefulPreset:
    """TEST-28-01-02: preset="careful" sets slower timings than default."""

    def test_careful_typing_delay_greater_than_default(self):
        default = HumanConfig()
        careful = HumanConfig(preset="careful")
        assert careful.typing_delay_ms[0] > default.typing_delay_ms[0]
        assert careful.typing_delay_ms[1] > default.typing_delay_ms[1]

    def test_careful_pause_greater_than_default(self):
        default = HumanConfig()
        careful = HumanConfig(preset="careful")
        assert careful.pause_between_actions[0] > default.pause_between_actions[0]

    def test_careful_typo_chance_lower(self):
        default = HumanConfig()
        careful = HumanConfig(preset="careful")
        assert careful.typo_chance < default.typo_chance


# -- TEST-28-01-03: adapter delegates to cloak when available ---------------


class TestCloakDelegation:
    """TEST-28-01-03: adapter delegates to CloakBrowser's humanize."""

    @pytest.mark.asyncio
    async def test_cloak_click_uses_mouse_click(self):
        adapter = HumanBehaviorAdapter(backend="cloak")
        page = AsyncMock()
        el = AsyncMock()
        el.bounding_box.return_value = {"x": 100, "y": 200, "width": 80, "height": 30}
        page.query_selector.return_value = el

        with patch.object(adapter, "random_pause", new_callable=AsyncMock):
            await adapter.humanize_click(page, "#btn")

        page.mouse.click.assert_called_once()

    @pytest.mark.asyncio
    async def test_cloak_type_types_characters(self):
        adapter = HumanBehaviorAdapter(
            config=HumanConfig(typing_delay_ms=(1, 2)),
            backend="cloak",
        )
        page = AsyncMock()
        with patch.object(adapter, "random_pause", new_callable=AsyncMock):
            await adapter.humanize_type(page, "#input", "ab")

        assert page.keyboard.type.call_count == 2


# -- TEST-28-01-04: adapter uses basic sim with patchright ------------------


class TestPatchrightSimulation:
    """TEST-28-01-04: adapter uses basic sim with Patchright."""

    @pytest.mark.asyncio
    async def test_patchright_click_moves_mouse_with_jitter(self):
        adapter = HumanBehaviorAdapter(backend="patchright")
        page = AsyncMock()
        el = AsyncMock()
        el.bounding_box.return_value = {"x": 100, "y": 200, "width": 80, "height": 30}
        page.query_selector.return_value = el

        with patch.object(adapter, "random_pause", new_callable=AsyncMock):
            await adapter.humanize_click(page, "#btn")

        # v2: behavioral synthesis dispatches many mouse.move calls
        assert page.mouse.move.call_count >= 1
        page.mouse.down.assert_called()
        page.mouse.up.assert_called()

    @pytest.mark.asyncio
    async def test_patchright_type_types_per_character(self):
        adapter = HumanBehaviorAdapter(
            config=HumanConfig(typing_delay_ms=(1, 2)),
            backend="patchright",
        )
        page = AsyncMock()

        with patch.object(adapter, "random_pause", new_callable=AsyncMock):
            await adapter.humanize_type(page, "#input", "hi")

        # At least 2 keyboard.type calls (one per char)
        assert page.keyboard.type.call_count >= 2


# -- TEST-28-01-05: random_pause produces delay in range -------------------


class TestRandomPause:
    """TEST-28-01-05: random_pause produces delay in range."""

    @pytest.mark.asyncio
    async def test_pause_is_within_range(self):
        cfg = HumanConfig(pause_between_actions=(0.01, 0.05))
        adapter = HumanBehaviorAdapter(config=cfg, backend="patchright")

        import time

        start = time.monotonic()
        await adapter.random_pause()
        elapsed = time.monotonic() - start

        assert elapsed >= 0.008  # at least ~the min pause (tolerance for Windows timer)

    @pytest.mark.asyncio
    async def test_fast_preset_pause_is_quick(self):
        cfg = HumanConfig(preset="fast")
        adapter = HumanBehaviorAdapter(config=cfg, backend="patchright")

        import time

        start = time.monotonic()
        await adapter.random_pause()
        elapsed = time.monotonic() - start

        # fast preset max pause is 0.5s
        assert elapsed <= 1.0
