"""Tests for BudgetAwareLLMClient and enhanced factory (BATCH-04 / TASK-02).

Test IDs:
  TEST-04-02-01  BudgetAwareLLMClient records cost in governor
  TEST-04-02-02  create_llm auto-detects from SB_LLM_PROVIDER env var
  TEST-04-02-03  pip install -e .[openai] resolves (check pyproject.toml has the extra)
  TEST-04-02-04  BudgetAwareLLMClient delegates all methods correctly
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from super_browser.agent.llm.budget_aware import (
    BudgetAwareLLMClient,
    _estimate_cost_usd,
    _extract_tokens,
)
from super_browser.budget.governor import TokenBudgetGovernor
from super_browser.budget.types import BudgetConfig, TokenUsageRecord

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_governor() -> TokenBudgetGovernor:
    """Create a fresh TokenBudgetGovernor with default config."""
    return TokenBudgetGovernor(config=BudgetConfig())


def _make_stub_client() -> AsyncMock:
    """Create an async mock that satisfies the LLMClient protocol."""
    client = AsyncMock()
    client.propose_action = AsyncMock(return_value={
        "action": "click",
        "params": {"target": "#btn"},
        "tokens": {"input": 100, "output": 50},
    })
    client.create_plan = AsyncMock(return_value=[
        {"step": "Open page", "tool": "navigate", "params": {"url": "https://example.com"}},
    ])
    client.replan = AsyncMock(return_value=[
        {"step": "Retry with different selector"},
    ])
    return client


# ===================================================================
# TEST-04-02-01 — BudgetAwareLLMClient records cost in governor
# ===================================================================

class TestCostRecording:
    """Verify that every LLM call creates a TokenUsageRecord in the governor."""

    @pytest.mark.asyncio()
    async def test_propose_action_records_cost(self) -> None:
        """TEST-04-02-01a: propose_action records a usage record."""
        governor = _make_governor()
        stub = _make_stub_client()
        wrapper = BudgetAwareLLMClient(stub, governor, model="claude-sonnet-4-20250514")

        result = await wrapper.propose_action("Click the button", tools=None)

        # Result is delegated unchanged.
        assert result["action"] == "click"
        assert result["params"]["target"] == "#btn"

        # Governor should have exactly 1 record.
        records = governor.records
        assert len(records) == 1

        rec = records[0]
        assert isinstance(rec, TokenUsageRecord)
        assert rec.model == "claude-sonnet-4-20250514"
        assert rec.input_tokens == 100
        assert rec.output_tokens == 50
        assert rec.action_name == "propose_action"
        # Cost should be non-zero for a known model.
        assert rec.estimated_cost_usd > 0

    @pytest.mark.asyncio()
    async def test_create_plan_records_cost(self) -> None:
        """TEST-04-02-01b: create_plan records a conservative ESTIMATED usage
        record (plans carry no provider usage metadata, so the wrapper
        estimates: prompt-length input + bounded output allowance)."""
        governor = _make_governor()
        stub = _make_stub_client()
        wrapper = BudgetAwareLLMClient(stub, governor, model="gpt-4o")

        result = await wrapper.create_plan("Open Google", tools=[{"name": "navigate"}])

        assert len(result) == 1
        assert result[0]["step"] == "Open page"

        records = governor.records
        assert len(records) == 1
        rec = records[0]
        assert rec.action_name == "create_plan (estimated)"
        assert rec.model == "gpt-4o"
        # "Open Google" is 11 chars → 2 estimated input tokens; output is the
        # bounded planning allowance (1000).
        assert rec.input_tokens == 2
        assert rec.output_tokens == 1000

    @pytest.mark.asyncio()
    async def test_replan_records_cost(self) -> None:
        """TEST-04-02-01c: replan records a usage record."""
        governor = _make_governor()
        stub = _make_stub_client()
        wrapper = BudgetAwareLLMClient(stub, governor, model="gpt-4o")

        result = await wrapper.replan(
            instruction="Open Google",
            original_plan=[{"step": "click"}],
            failed_step=0,
            error="Element not found",
        )

        assert len(result) == 1

        records = governor.records
        assert len(records) == 1
        assert records[0].action_name == "replan (estimated)"

    @pytest.mark.asyncio()
    async def test_multiple_calls_accumulate_in_governor(self) -> None:
        """TEST-04-02-01d: Multiple calls accumulate records."""
        governor = _make_governor()
        stub = _make_stub_client()
        wrapper = BudgetAwareLLMClient(stub, governor, model="claude-sonnet-4-20250514")

        await wrapper.propose_action("step 1")
        await wrapper.propose_action("step 2")
        await wrapper.propose_action("step 3")

        assert len(governor.records) == 3
        # Daily spend should have accumulated.
        assert governor.daily_spend > 0

    @pytest.mark.asyncio()
    async def test_unknown_model_priced_at_conservative_fallback(self) -> None:
        """TEST-04-02-01e: Unknown models are priced at the most expensive
        known rate — unknown pricing must not silently bypass enforcement.
        The stub reports 100 in / 50 out → (100 x 0.015 + 50 x 0.075) / 1000
        = 0.00525 USD."""
        governor = _make_governor()
        stub = _make_stub_client()
        wrapper = BudgetAwareLLMClient(stub, governor, model="unknown-model-xyz")

        await wrapper.propose_action("test")

        records = governor.records
        assert len(records) == 1
        assert records[0].estimated_cost_usd == pytest.approx(0.00525)

    def test_estimate_cost_known_model(self) -> None:
        """TEST-04-02-01f: Cost estimation returns non-zero for known models."""
        cost = _estimate_cost_usd("claude-sonnet-4-20250514", 1000, 500)
        assert cost > 0
        # Expected: (1000 * 0.003 + 500 * 0.015) / 1000 = (3 + 7.5) / 1000 = 0.0105
        assert abs(cost - 0.0105) < 1e-9

    def test_estimate_cost_unknown_model(self) -> None:
        """TEST-04-02-01g: Unknown models fall back to the most expensive
        known rate (0.015 in / 0.075 out per 1k) so enforcement cannot be
        bypassed by an unrecognized model name."""
        cost = _estimate_cost_usd("nonexistent-model", 1000, 500)
        assert cost == pytest.approx(0.0525)


# ===================================================================
# TEST-04-02-02 — create_llm auto-detects from SB_LLM_PROVIDER env var
# ===================================================================

class TestFactoryAutoDetection:
    """Verify create_llm() reads env vars when args are not given."""

    def test_auto_detect_anthropic_from_env(self) -> None:
        """TEST-04-02-02a: create_llm uses SB_LLM_PROVIDER=anthropic."""
        mock_anthropic = MagicMock()
        mock_client = MagicMock()
        mock_anthropic.AsyncAnthropic = MagicMock(return_value=mock_client)

        with patch.dict("sys.modules", {"anthropic": mock_anthropic}):
            with patch.dict(os.environ, {
                "SB_LLM_PROVIDER": "anthropic",
                "SB_LLM_MODEL": "claude-sonnet-4",
                "SB_LLM_API_KEY": "sk-test-123",
            }):
                # Force reimport to pick up patched sys.modules
                import importlib

                import super_browser.agent.llm.factory as fmod
                importlib.reload(fmod)

                client = fmod.create_llm()  # noqa: F841

                # Verify Anthropic was instantiated.
                mock_anthropic.AsyncAnthropic.assert_called_once_with(api_key="sk-test-123")

    def test_auto_detect_openai_from_env(self) -> None:
        """TEST-04-02-02b: create_llm uses SB_LLM_PROVIDER=openai."""
        mock_openai = MagicMock()
        mock_client = MagicMock()
        mock_openai.AsyncOpenAI = MagicMock(return_value=mock_client)

        with patch.dict("sys.modules", {"openai": mock_openai}):
            with patch.dict(os.environ, {
                "SB_LLM_PROVIDER": "openai",
                "SB_LLM_MODEL": "gpt-4o",
                "SB_LLM_API_KEY": "sk-openai-456",
            }):
                import importlib

                import super_browser.agent.llm.factory as fmod
                importlib.reload(fmod)

                client = fmod.create_llm()  # noqa: F841

                mock_openai.AsyncOpenAI.assert_called_once_with(api_key="sk-openai-456")

    def test_explicit_args_override_env(self) -> None:
        """TEST-04-02-02c: Explicit arguments take precedence over env vars."""
        mock_openai = MagicMock()
        mock_client = MagicMock()
        mock_openai.AsyncOpenAI = MagicMock(return_value=mock_client)

        with patch.dict("sys.modules", {"openai": mock_openai}):
            with patch.dict(os.environ, {
                "SB_LLM_PROVIDER": "anthropic",  # should be ignored
                "SB_LLM_MODEL": "wrong-model",   # should be ignored
                "SB_LLM_API_KEY": "wrong-key",    # should be ignored
            }):
                import importlib

                import super_browser.agent.llm.factory as fmod
                importlib.reload(fmod)

                client = fmod.create_llm(  # noqa: F841
                    provider="openai",
                    model="gpt-4o",
                    api_key="explicit-key",
                )

                mock_openai.AsyncOpenAI.assert_called_once_with(api_key="explicit-key")

    def test_raises_when_no_provider(self) -> None:
        """TEST-04-02-02d: EnvironmentError when provider is missing entirely."""
        with patch.dict(os.environ, {}, clear=False):
            # Make sure SB_LLM_PROVIDER is not set
            env = dict(os.environ)
            env.pop("SB_LLM_PROVIDER", None)
            with patch.dict(os.environ, env, clear=True):
                import importlib

                import super_browser.agent.llm.factory as fmod
                importlib.reload(fmod)

                with pytest.raises(EnvironmentError, match="SB_LLM_PROVIDER"):
                    fmod.create_llm()

    def test_raises_when_no_model(self) -> None:
        """TEST-04-02-02e: EnvironmentError when model is missing entirely."""
        env = dict(os.environ)
        env.pop("SB_LLM_MODEL", None)
        env["SB_LLM_PROVIDER"] = "openai"
        with patch.dict(os.environ, env, clear=True):
            import importlib

            import super_browser.agent.llm.factory as fmod
            importlib.reload(fmod)

            with pytest.raises(EnvironmentError, match="SB_LLM_MODEL"):
                fmod.create_llm()

    def test_raises_when_no_api_key(self) -> None:
        """TEST-04-02-02f: EnvironmentError when api_key is missing entirely."""
        env = dict(os.environ)
        env.pop("SB_LLM_API_KEY", None)
        env["SB_LLM_PROVIDER"] = "openai"
        env["SB_LLM_MODEL"] = "gpt-4o"
        with patch.dict(os.environ, env, clear=True):
            import importlib

            import super_browser.agent.llm.factory as fmod
            importlib.reload(fmod)

            with pytest.raises(EnvironmentError, match="SB_LLM_API_KEY"):
                fmod.create_llm()


# ===================================================================
# TEST-04-02-03 — pip install -e .[openai] resolves
# ===================================================================

class TestPyprojectOpenAIExtra:
    """Verify pyproject.toml has the [openai] optional dependency group."""

    def test_openai_extra_exists_in_pyproject(self) -> None:
        """TEST-04-02-03: pyproject.toml contains an [openai] extra."""
        pyproject = Path("./pyproject.toml")
        content = pyproject.read_text(encoding="utf-8")
        assert "openai = [" in content or '"openai"' in content, (
            "pyproject.toml is missing the [openai] optional-dependencies entry"
        )
        # Verify the openai package is listed.
        assert "openai" in content


# ===================================================================
# TEST-04-02-04 — BudgetAwareLLMClient delegates all methods correctly
# ===================================================================

class TestDelegation:
    """Verify BudgetAwareLLMClient delegates every method to the wrapped client."""

    @pytest.mark.asyncio()
    async def test_propose_action_delegates(self) -> None:
        """TEST-04-02-04a: propose_action delegates to wrapped client."""
        governor = _make_governor()
        stub = _make_stub_client()
        wrapper = BudgetAwareLLMClient(stub, governor, model="gpt-4o")

        result = await wrapper.propose_action(
            "test prompt", tools=[{"name": "click"}]
        )

        stub.propose_action.assert_awaited_once_with(
            "test prompt", tools=[{"name": "click"}]
        )
        # Result is passed through unchanged.
        assert result == stub.propose_action.return_value

    @pytest.mark.asyncio()
    async def test_create_plan_delegates(self) -> None:
        """TEST-04-02-04b: create_plan delegates to wrapped client."""
        governor = _make_governor()
        stub = _make_stub_client()
        wrapper = BudgetAwareLLMClient(stub, governor, model="gpt-4o")

        tools = [{"name": "navigate"}]
        result = await wrapper.create_plan("Open page", tools=tools)

        stub.create_plan.assert_awaited_once_with("Open page", tools=tools)
        assert result == stub.create_plan.return_value

    @pytest.mark.asyncio()
    async def test_replan_delegates(self) -> None:
        """TEST-04-02-04c: replan delegates to wrapped client with all kwargs."""
        governor = _make_governor()
        stub = _make_stub_client()
        wrapper = BudgetAwareLLMClient(stub, governor, model="gpt-4o")

        result = await wrapper.replan(
            instruction="Test",
            original_plan=[{"step": "A"}],
            failed_step=0,
            error="boom",
        )

        stub.replan.assert_awaited_once_with(
            instruction="Test",
            original_plan=[{"step": "A"}],
            failed_step=0,
            error="boom",
        )
        assert result == stub.replan.return_value

    @pytest.mark.asyncio()
    async def test_propose_action_with_none_tools(self) -> None:
        """TEST-04-02-04d: propose_action correctly passes tools=None."""
        governor = _make_governor()
        stub = _make_stub_client()
        wrapper = BudgetAwareLLMClient(stub, governor, model="gpt-4o")

        await wrapper.propose_action("prompt")
        stub.propose_action.assert_awaited_once_with("prompt", tools=None)

    @pytest.mark.asyncio()
    async def test_result_not_mutated(self) -> None:
        """TEST-04-02-04e: BudgetAwareLLMClient does not mutate the result."""
        governor = _make_governor()
        original_result = {
            "action": "click",
            "params": {"x": 1},
            "tokens": {"input": 50, "output": 25},
        }
        stub = AsyncMock()
        stub.propose_action = AsyncMock(return_value=original_result)
        wrapper = BudgetAwareLLMClient(stub, governor, model="gpt-4o")

        result = await wrapper.propose_action("test")

        # The returned dict should be the exact same object.
        assert result is original_result
        # The tokens should still be there.
        assert result["tokens"] == {"input": 50, "output": 25}


# ===================================================================
# Bonus: _extract_tokens edge cases
# ===================================================================

class TestExtractTokens:
    """Edge cases for the _extract_tokens helper."""

    def test_dict_with_tokens(self) -> None:
        result = _extract_tokens({"tokens": {"input": 10, "output": 5}})
        assert result == (10, 5)

    def test_dict_without_tokens(self) -> None:
        result = _extract_tokens({"action": "click"})
        assert result == (0, 0)

    def test_list_returns_zeros(self) -> None:
        result = _extract_tokens([{"step": "do stuff"}])
        assert result == (0, 0)

    def test_missing_input_key(self) -> None:
        result = _extract_tokens({"tokens": {"output": 5}})
        assert result == (0, 5)

    def test_missing_output_key(self) -> None:
        result = _extract_tokens({"tokens": {"input": 10}})
        assert result == (10, 0)
