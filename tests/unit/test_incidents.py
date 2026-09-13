"""Unit tests for IncidentManager and RunbookActions."""

from __future__ import annotations

import pytest

from polymarket_exec.ops.incidents import IncidentManager, IncidentState, RunbookActions


# ---------------------------------------------------------------------------
# IncidentManager tests
# ---------------------------------------------------------------------------


class TestIncidentManager:
    """Test suite for the incident state machine."""

    @pytest.mark.asyncio
    async def test_starts_healthy(self) -> None:
        """Manager must start in HEALTHY state."""
        mgr = IncidentManager()
        state, detail = await mgr.get_current()
        assert state is IncidentState.HEALTHY
        assert detail == "System nominal"

    @pytest.mark.asyncio
    async def test_transition_to_error_state(self) -> None:
        """Transitioning to an error state should update current."""
        mgr = IncidentManager()
        await mgr.transition_to(IncidentState.STALE_FEED, "No tick for 90s")

        state, detail = await mgr.get_current()
        assert state is IncidentState.STALE_FEED
        assert detail == "No tick for 90s"

    @pytest.mark.asyncio
    async def test_resolve_returns_to_healthy(self) -> None:
        """Resolving should return state to HEALTHY."""
        mgr = IncidentManager()
        await mgr.transition_to(IncidentState.API_ERROR, "Rate limited")
        await mgr.resolve("API recovered")

        state, detail = await mgr.get_current()
        assert state is IncidentState.HEALTHY
        assert detail == "API recovered"

    @pytest.mark.asyncio
    async def resolve_without_detail_uses_default(self) -> None:
        """Resolving with no detail should use a default message."""
        mgr = IncidentManager()
        await mgr.transition_to(IncidentState.DB_WRITE_ERROR, "Disk full")
        await mgr.resolve()

        state, detail = await mgr.get_current()
        assert state is IncidentState.HEALTHY
        assert detail == "Incident resolved"

    @pytest.mark.asyncio
    async def test_history_accumulates(self) -> None:
        """Each transition should create a history entry."""
        mgr = IncidentManager()
        # Initial HEALTHY entry + transitions
        await mgr.transition_to(IncidentState.STALE_FEED, "No tick")
        await mgr.transition_to(IncidentState.API_ERROR, "Rate limit")
        await mgr.resolve("All clear")

        history = await mgr.get_history(limit=10)
        # Should have entries: HEALTHY->STALE_FEED->API_ERROR->HEALTHY
        assert len(history) == 4  # initial + 3 transitions

    @pytest.mark.asyncio
    async def test_history_limit(self) -> None:
        """get_history limit should restrict returned entries."""
        mgr = IncidentManager()
        await mgr.transition_to(IncidentState.STALE_FEED)
        await mgr.resolve()
        await mgr.transition_to(IncidentState.API_ERROR)
        await mgr.resolve()
        await mgr.transition_to(IncidentState.HIGH_LATENCY)
        await mgr.resolve()

        full = await mgr.get_history(limit=10)
        limited = await mgr.get_history(limit=2)
        assert len(limited) == 2
        assert len(limited) < len(full)

    @pytest.mark.asyncio
    async def test_history_entry_format(self) -> None:
        """History entries should have the expected keys."""
        mgr = IncidentManager()
        await mgr.transition_to(IncidentState.RISK_BREACH, "Drawdown exceeded")

        history = await mgr.get_history(limit=2)
        entry = history[0]  # most recent
        assert "timestamp" in entry
        assert "from_state" in entry
        assert "to_state" in entry
        assert "detail" in entry
        assert entry["to_state"] == "RISK_BREACH"
        assert entry["detail"] == "Drawdown exceeded"

    @pytest.mark.asyncio
    async def test_duplicate_transition_noop(self) -> None:
        """Transitioning to the same state should not create duplicate entries."""
        mgr = IncidentManager()
        await mgr.transition_to(IncidentState.STALE_FEED, "First")
        await mgr.transition_to(IncidentState.STALE_FEED, "Second")

        history = await mgr.get_history(limit=10)
        # Should be: initial HEALTHY + one transition to STALE_FEED
        stale_entries = [h for h in history if h["to_state"] == "STALE_FEED"]
        assert len(stale_entries) == 1
        assert stale_entries[0]["detail"] == "Second"

    @pytest.mark.asyncio
    async def test_all_states_reachable(self) -> None:
        """Every IncidentState should be transitionable and resolvable."""
        for state in IncidentState:
            if state is IncidentState.HEALTHY:
                continue
            mgr = IncidentManager()
            await mgr.transition_to(state, f"Test {state.name}")
            current, _ = await mgr.get_current()
            assert current is state
            await mgr.resolve()
            current, _ = await mgr.get_current()
            assert current is IncidentState.HEALTHY


# ---------------------------------------------------------------------------
# RunbookActions tests
# ---------------------------------------------------------------------------


class TestRunbookActions:
    """Test suite for operator runbook guidance."""

    @pytest.mark.asyncio
    async def test_action_for_every_state(self) -> None:
        """Every IncidentState should have a non-empty action string."""
        for state in IncidentState:
            action = RunbookActions.action_for(state)
            assert isinstance(action, str)
            assert len(action) > 0

    @pytest.mark.asyncio
    async def test_action_for_healthy(self) -> None:
        """HEALTHY state should indicate no action needed."""
        action = RunbookActions.action_for(IncidentState.HEALTHY)
        assert "No action required" in action
        assert "nominal" in action

    @pytest.mark.asyncio
    async def test_action_for_stale_feed(self) -> None:
        """STALE_FEED should reference feed health and telemetry."""
        action = RunbookActions.action_for(IncidentState.STALE_FEED)
        assert "telemetry" in action
        assert "60s" in action

    @pytest.mark.asyncio
    async def test_action_for_api_error(self) -> None:
        """API_ERROR should reference rate limits and error_log."""
        action = RunbookActions.action_for(IncidentState.API_ERROR)
        assert "1200" in action
        assert "error_log" in action

    @pytest.mark.asyncio
    async def test_action_for_risk_breach(self) -> None:
        """RISK_BREACH should reference drawdown and stopping."""
        action = RunbookActions.action_for(IncidentState.RISK_BREACH)
        assert "drawdown" in action
        assert "Stop" in action

    @pytest.mark.asyncio
    async def test_action_for_high_latency(self) -> None:
        """HIGH_LATENCY should reference loop_duration_log."""
        action = RunbookActions.action_for(IncidentState.HIGH_LATENCY)
        assert "loop_duration_log" in action
        assert "5s" in action

    @pytest.mark.asyncio
    async def test_action_for_market_discovery_failed(self) -> None:
        """MARKET_DISCOVERY_FAILED should reference Polymarket API."""
        action = RunbookActions.action_for(IncidentState.MARKET_DISCOVERY_FAILED)
        assert "Polymarket" in action

    @pytest.mark.asyncio
    async def test_action_for_unexpected_position_count(self) -> None:
        """UNEXPECTED_POSITION_COUNT should reference positions table."""
        action = RunbookActions.action_for(IncidentState.UNEXPECTED_POSITION_COUNT)
        assert "paper_positions" in action

    @pytest.mark.asyncio
    async def test_action_for_db_write_error(self) -> None:
        """DB_WRITE_ERROR should reference disk space and WAL mode."""
        action = RunbookActions.action_for(IncidentState.DB_WRITE_ERROR)
        assert "disk space" in action
        assert "WAL" in action

    @pytest.mark.asyncio
    async def test_action_for_force_close_failed(self) -> None:
        """FORCE_CLOSE_FAILED should reference execution manager and logs."""
        action = RunbookActions.action_for(IncidentState.FORCE_CLOSE_FAILED)
        assert "execution manager" in action

    @pytest.mark.asyncio
    async def test_all_actions_returns_dict(self) -> None:
        """all_actions should return a mapping of all state names."""
        actions = RunbookActions.all_actions()
        assert isinstance(actions, dict)
        for state in IncidentState:
            assert state.name in actions
            assert len(actions[state.name]) > 0

    @pytest.mark.asyncio
    async def test_unknown_state_fallback(self) -> None:
        """An unknown state should return the fallback message."""
        # Create a fake state not in ACTIONS
        class FakeState:
            pass

        action = RunbookActions.action_for(FakeState())  # type: ignore[arg-type]
        assert "No runbook entry" in action
