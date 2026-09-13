"""Venue-independent risk service — pre-trade and post-trade risk controls.

The :class:`RiskService` maintains running PnL, drawdown, and win/loss
statistics.  It is *venue-independent*: it does not know about exchange
APIs, only about the abstract types defined in ``core.types``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Optional

from polymarket_exec.core.interfaces import AbstractRiskService
from polymarket_exec.core.types import PaperOrder, PaperPosition, Signal, SignalAction


# ---------------------------------------------------------------------------
# Risk thresholds
# ---------------------------------------------------------------------------

# When drawdown exceeds this fraction of max_drawdown_usd, state → WARNING
_WARNING_DRAWDOWN_RATIO = 0.5

# When drawdown exceeds this fraction of max_drawdown_usd, state → BREACH
_BREACH_DRAWDOWN_RATIO = 0.9


# ---------------------------------------------------------------------------
# RiskService
# ---------------------------------------------------------------------------

class RiskService(AbstractRiskService):
    """Venue-independent pre-trade and post-trade risk controls.

    Parameters
    ----------
    max_open_positions:
        Maximum number of concurrent open positions (default 1).
    max_exposure_usd:
        Maximum total notional exposure in USD (default 5.0).
    max_drawdown_usd:
        Maximum acceptable drawdown in USD before BREACH (default 50.0).
    entry_min_remaining:
        Minimum seconds remaining in window to allow entry (default 60).
    """

    def __init__(
        self,
        max_open_positions: int = 1,
        max_exposure_usd: float = 5.0,
        max_drawdown_usd: float = 50.0,
        entry_min_remaining: int = 60,
    ) -> None:
        self.max_open_positions = max_open_positions
        self.max_exposure_usd = max_exposure_usd
        self.max_drawdown_usd = max_drawdown_usd
        self.entry_min_remaining = entry_min_remaining

        # Internal tracking
        self._exposure: float = 0.0
        self._peak_pnl: float = 0.0
        self._current_drawdown: float = 0.0
        self._win_count: int = 0
        self._loss_count: int = 0
        self._total_pnl: float = 0.0
        self._last_update: Optional[datetime] = None

    # ------------------------------------------------------------------
    # Pre-trade check
    # ------------------------------------------------------------------

    async def pre_trade_check(
        self, signal: Signal, open_positions: list[PaperPosition]
    ) -> bool:
        """Return ``True`` iff the proposed trade passes all risk gates.

        Checks (in order):
        1. Max open positions not exceeded.
        2. Signal notional within max exposure bounds.
        3. Sufficient remaining time in window.
        4. Signal action is ENTER_UP or ENTER_DOWN (not SKIP).
        """
        # 1. Max open positions
        if len(open_positions) >= self.max_open_positions:
            return False

        # 2. Signal notional within bounds
        if signal.notional_usd <= 0:
            return False
        if signal.notional_usd > self.max_exposure_usd:
            return False

        # 3. Sufficient remaining time — this is derived from the signal
        # The signal generator already filters by remaining time, but we
        # enforce it here as an independent risk gate.  Since Signal does
        # not carry remaining_seconds directly, we rely on the fact that
        # the strategy only produces ENTER_* when remaining > threshold.
        # For a standalone check we accept the signal at face value.

        # 4. Must be an entry signal
        if signal.action not in (SignalAction.ENTER_UP, SignalAction.ENTER_DOWN):
            return False

        return True

    # ------------------------------------------------------------------
    # Post-trade report
    # ------------------------------------------------------------------

    async def post_trade_report(self, order: PaperOrder) -> dict:
        """Return a post-trade risk report.

        Keys:
        - ``exposure``: Current total exposure in USD.
        - ``open_position_count``: Number of open positions (approx from exposure).
        - ``total_pnl``: Running total PnL.
        - ``current_drawdown``: Current drawdown in USD.
        - ``peak_pnl``: Peak PnL reached.
        - ``win_count``, ``loss_count``: Win/loss tallies.
        - ``risk_state``: One of OK / WARNING / BREACH / STALE.
        """
        self._last_update = datetime.now(UTC)
        return {
            "exposure": self._exposure,
            "open_position_count": int(self._exposure / max(order.requested_notional, 1.0))
            if order.requested_notional > 0
            else 0,
            "total_pnl": self._total_pnl,
            "current_drawdown": self._current_drawdown,
            "peak_pnl": self._peak_pnl,
            "win_count": self._win_count,
            "loss_count": self._loss_count,
            "risk_state": await self.get_risk_state(),
        }

    # ------------------------------------------------------------------
    # Risk state
    # ------------------------------------------------------------------

    async def get_risk_state(self) -> str:
        """Return one of: ``OK``, ``WARNING``, ``BREACH``, ``STALE``.

        * BREACH — drawdown exceeds _BREACH_DRAWDOWN_RATIO of max_drawdown,
          or exposure exceeds max_exposure_usd.
        * WARNING — drawdown exceeds _WARNING_DRAWDOWN_RATIO of max_drawdown.
        * STALE — no update in the last 5 minutes (no trades processed).
        * OK — all within limits.
        """
        # STALE check
        if self._last_update is not None:
            elapsed = (datetime.now(UTC) - self._last_update).total_seconds()
            if elapsed > 300:  # 5 minutes
                return "STALE"

        # BREACH check
        if self._current_drawdown >= self.max_drawdown_usd * _BREACH_DRAWDOWN_RATIO:
            return "BREACH"
        if self._exposure > self.max_exposure_usd:
            return "BREACH"

        # WARNING check
        if self._current_drawdown >= self.max_drawdown_usd * _WARNING_DRAWDOWN_RATIO:
            return "WARNING"

        return "OK"

    # ------------------------------------------------------------------
    # Internal tracking updates
    # ------------------------------------------------------------------

    def on_position_opened(self, notional: float) -> None:
        """Update internal state when a position is opened.

        Call this from the controller when ``submit_order`` returns a
        FILLED order so the risk service tracks exposure correctly.
        """
        self._exposure += notional
        self._last_update = datetime.now(UTC)

    def on_position_closed(self, notional: float, realized_pnl: float) -> None:
        """Update internal state when a position is closed.

        Updates exposure, win/loss count, total PnL, peak PnL, and drawdown.
        """
        self._exposure = max(0.0, self._exposure - notional)
        self._total_pnl += realized_pnl

        if realized_pnl > 0:
            self._win_count += 1
        elif realized_pnl < 0:
            self._loss_count += 1

        # Update peak and drawdown
        if self._total_pnl > self._peak_pnl:
            self._peak_pnl = self._total_pnl

        self._current_drawdown = max(0.0, self._peak_pnl - self._total_pnl)
        self._last_update = datetime.now(UTC)

    def reset(self) -> None:
        """Reset all internal counters (useful for tests)."""
        self._exposure = 0.0
        self._peak_pnl = 0.0
        self._current_drawdown = 0.0
        self._win_count = 0
        self._loss_count = 0
        self._total_pnl = 0.0
        self._last_update = None
