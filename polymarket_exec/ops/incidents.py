"""Incident state machine and operator runbooks for the BTC 5m pricing-model system.

Usage::

    mgr = IncidentManager()
    await mgr.transition_to(IncidentState.STALE_FEED, "No tick for 90s")
    state, detail = await mgr.get_current()
    actions = RunbookActions.action_for(state)

    await mgr.resolve("Feed recovered")
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum, auto


# ---------------------------------------------------------------------------
# Incident states
# ---------------------------------------------------------------------------


class IncidentState(Enum):
    """All possible incident states for the trading system."""

    HEALTHY = auto()
    STALE_FEED = auto()
    MARKET_DISCOVERY_FAILED = auto()
    API_ERROR = auto()
    UNEXPECTED_POSITION_COUNT = auto()
    DB_WRITE_ERROR = auto()
    FORCE_CLOSE_FAILED = auto()
    RISK_BREACH = auto()
    HIGH_LATENCY = auto()


# ---------------------------------------------------------------------------
# Incident manager
# ---------------------------------------------------------------------------


class IncidentManager:
    """Simple state-machine that tracks incident transitions and history.

    The manager starts in :data:`IncidentState.HEALTHY` and transitions
    to other states as problems are detected.  Each transition (including
    resolutions) is recorded in an in-memory history list.
    """

    def __init__(self) -> None:
        self._current: IncidentState = IncidentState.HEALTHY
        self._detail: str = "System nominal"
        self._history: list[dict] = []
        self._enter_state(IncidentState.HEALTHY, "System nominal")

    # -- Transitions --------------------------------------------------------

    async def transition_to(self, state: IncidentState, detail: str = "") -> None:
        """Transition to *state* with optional *detail*.

        If the new state is the same as the current one, only the detail
        is updated (no duplicate history entry).
        """
        if state == self._current:
            if detail:
                self._detail = detail
                # Update the last history entry's detail
                if self._history:
                    self._history[-1]["detail"] = detail
            return
        self._enter_state(state, detail)

    async def resolve(self, detail: str = "") -> None:
        """Resolve the current incident, returning to ``HEALTHY``."""
        if self._current is IncidentState.HEALTHY:
            if detail:
                self._detail = detail
                if self._history:
                    self._history[-1]["detail"] = detail
            return
        self._enter_state(IncidentState.HEALTHY, detail or "Incident resolved")

    # -- Queries ------------------------------------------------------------

    async def get_current(self) -> tuple[IncidentState, str]:
        """Return ``(current_state, detail)``."""
        return (self._current, self._detail)

    async def get_history(self, limit: int = 50) -> list[dict]:
        """Return the last *limit* state transitions (newest first).

        Each entry is a dict with keys:
        ``timestamp``, ``from_state``, ``to_state``, ``detail``.
        """
        return list(reversed(self._history[-limit:]))

    # -- Internal -----------------------------------------------------------

    def _enter_state(self, state: IncidentState, detail: str) -> None:
        from_state = self._current
        self._current = state
        self._detail = detail
        self._history.append(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "from_state": from_state.name,
                "to_state": state.name,
                "detail": detail,
            }
        )


# ---------------------------------------------------------------------------
# Runbook actions
# ---------------------------------------------------------------------------


class RunbookActions:
    """Operator guidance for every incident state.

    Access actions via :meth:`action_for` or enumerate all of them
    with :meth:`all_actions`.
    """

    ACTIONS: dict[IncidentState, str] = {
        IncidentState.HEALTHY: (
            "No action required. System nominal."
        ),
        IncidentState.STALE_FEED: (
            "1. Check feed source health in telemetry. "
            "2. Verify network connectivity. "
            "3. If persistent > 60s, stop bot and investigate."
        ),
        IncidentState.MARKET_DISCOVERY_FAILED: (
            "1. Verify Polymarket API status. "
            "2. Check if market window has rolled. "
            "3. If API down, wait for recovery; bot will auto-retry."
        ),
        IncidentState.API_ERROR: (
            "1. Check rate limits (Binance: 1200 weight/min). "
            "2. Review error_log table. "
            "3. If persistent, stop bot to prevent IP ban."
        ),
        IncidentState.UNEXPECTED_POSITION_COUNT: (
            "1. Review open positions in paper_positions. "
            "2. Run force_close_all via controller. "
            "3. Investigate race condition in tick loop."
        ),
        IncidentState.DB_WRITE_ERROR: (
            "1. Check disk space. "
            "2. Verify SQLite file permissions. "
            "3. If WAL mode issue, restart process to release locks."
        ),
        IncidentState.FORCE_CLOSE_FAILED: (
            "1. Check execution manager state. "
            "2. Review logs for close errors. "
            "3. Manual position cleanup via SQL if needed."
        ),
        IncidentState.RISK_BREACH: (
            "1. Review risk_service state. "
            "2. Check drawdown and exposure. "
            "3. Stop bot immediately if breach persists."
        ),
        IncidentState.HIGH_LATENCY: (
            "1. Check network latency to exchanges. "
            "2. Review loop_duration_log. "
            "3. If p95 > 5s, consider increasing tick interval."
        ),
    }

    @classmethod
    def action_for(cls, state: IncidentState) -> str:
        """Return the operator action string for *state*."""
        return cls.ACTIONS.get(
            state, "No runbook entry for this state — escalate to on-call."
        )

    @classmethod
    def all_actions(cls) -> dict[str, str]:
        """Return a mapping of state name -> action string for all states."""
        return {state.name: action for state, action in cls.ACTIONS.items()}
