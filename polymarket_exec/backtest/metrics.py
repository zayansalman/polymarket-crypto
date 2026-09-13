"""Backtest metrics, friction models, and reporting data classes.

The :class:`FrictionModel` encodes realistic market assumptions (spread,
fill probability, stale-quote tolerance).  :class:`BacktestResult` captures
everything produced by a full-market backtest run.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class FrictionModel:
    """Market friction assumptions for realistic simulation.

    These parameters model the real-world slippage between theoretical
    signals and actual execution on the CLOB.

    Attributes:
        stale_quote_ms: Quotes older than this are considered stale and
            skipped (default 500 ms).
        spread_bps: Bid-ask spread in basis points.  Applied as a cost
            on both entry and exit (default 10 bps).
        fill_probability: Probability that a market order fills in full
            on the first attempt (default 0.95).
        partial_fill_probability: Probability of a partial fill that
            still allows the trade to proceed (default 0.05).
    """

    stale_quote_ms: int = 500
    spread_bps: float = 10.0
    fill_probability: float = 0.95
    partial_fill_probability: float = 0.05

    @property
    def total_fill_probability(self) -> float:
        """Combined probability of any fill (full + partial)."""
        return self.fill_probability + self.partial_fill_probability

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "FrictionModel":
        return cls(
            stale_quote_ms=d.get("stale_quote_ms", 500),
            spread_bps=d.get("spread_bps", 10.0),
            fill_probability=d.get("fill_probability", 0.95),
            partial_fill_probability=d.get("partial_fill_probability", 0.05),
        )


@dataclass
class BacktestResult:
    """Comprehensive result of a full-market backtest run.

    Fields:
        name: Human-readable identifier for this run.
        params: Strategy parameters used.
        start_ts: Backtest period start (Unix seconds).
        end_ts: Backtest period end (Unix seconds).
        total_windows: Total market windows in the period.
        windows_traded: Windows where at least one trade was entered.
        total_signals: Total signals generated across all ticks.
        signals_taken: Signals that passed friction and were "filled".
        wins: Number of winning trades.
        losses: Number of losing trades.
        total_pnl_usd: Net PnL across all closed trades.
        total_notional_usd: Total notional traded.
        roi: Return on investment (``total_pnl / total_notional``).
        win_rate: ``wins / (wins + losses)``.
        avg_pnl_usd: ``total_pnl / (wins + losses)``.
        max_drawdown_usd: Peak-to-trough drawdown in USD.
        friction_model: The friction model used (as dict).
        exit_reasons: Count of exits by reason (TARGET, STOP, TIME, ...).
    """

    name: str
    params: dict[str, Any]
    start_ts: int
    end_ts: int
    total_windows: int
    windows_traded: int
    total_signals: int
    signals_taken: int
    wins: int
    losses: int
    total_pnl_usd: float
    total_notional_usd: float
    roi: float
    win_rate: float
    avg_pnl_usd: float
    max_drawdown_usd: float
    friction_model: dict[str, Any]
    exit_reasons: dict[str, int]

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "BacktestResult":
        return cls(
            name=d["name"],
            params=d.get("params", {}),
            start_ts=d["start_ts"],
            end_ts=d["end_ts"],
            total_windows=d.get("total_windows", 0),
            windows_traded=d.get("windows_traded", 0),
            total_signals=d.get("total_signals", 0),
            signals_taken=d.get("signals_taken", 0),
            wins=d.get("wins", 0),
            losses=d.get("losses", 0),
            total_pnl_usd=d.get("total_pnl_usd", 0.0),
            total_notional_usd=d.get("total_notional_usd", 0.0),
            roi=d.get("roi", 0.0),
            win_rate=d.get("win_rate", 0.0),
            avg_pnl_usd=d.get("avg_pnl_usd", 0.0),
            max_drawdown_usd=d.get("max_drawdown_usd", 0.0),
            friction_model=d.get("friction_model", {}),
            exit_reasons=d.get("exit_reasons", {}),
        )

    # ------------------------------------------------------------------
    # Pretty-printing
    # ------------------------------------------------------------------

    def summary(self) -> str:
        """Return a concise human-readable summary."""
        trade_count = self.wins + self.losses
        return (
            f"Backtest: {self.name}\n"
            f"  Period:   {self.start_ts} .. {self.end_ts}\n"
            f"  Windows:  {self.total_windows} total, {self.windows_traded} traded\n"
            f"  Signals:  {self.total_signals} generated, {self.signals_taken} taken\n"
            f"  Trades:   {trade_count} ({self.wins} W / {self.losses} L)\n"
            f"  PnL:      ${self.total_pnl_usd:+.2f}  "
            f"(ROI={self.roi:+.2%}, WR={self.win_rate:.1%})\n"
            f"  Avg/trade:${self.avg_pnl_usd:+.3f}  Max DD=${self.max_drawdown_usd:.2f}\n"
            f"  Exits:    {self.exit_reasons}\n"
            f"  Friction: {self.friction_model}"
        )
