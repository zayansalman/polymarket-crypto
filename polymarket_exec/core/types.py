"""All domain types and enums for the BTC 5m Binary Pricing Model trading system."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class Side(str, Enum):
    """Binary outcome side for BTC 5-minute markets."""

    UP = "Up"
    DOWN = "Down"


class SignalAction(Enum):
    """Action produced by the signal generator for a given tick."""

    SKIP = auto()
    ENTER_UP = auto()
    ENTER_DOWN = auto()


class OrderState(Enum):
    """Lifecycle states for a paper (or live) order."""

    PENDING = auto()
    ACKNOWLEDGED = auto()
    FILLED = auto()
    PARTIAL_FILL = auto()
    CANCELLED = auto()
    REJECTED = auto()


class ExitReason(str, Enum):
    """Reason a position was closed."""

    TARGET = "TARGET"
    STOP = "STOP"
    TIME = "TIME"
    WINDOW_ROLL = "WINDOW_ROLL"
    BAND_REENTRY = "BAND_REENTRY"
    STOP_REQUEST = "STOP_REQUEST"


# ---------------------------------------------------------------------------
# Frozen dataclasses — value objects, hashable, immutable
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MarketWindow:
    """A single BTC 5-minute binary market window."""

    slug: str
    question: str
    start_ts: int
    end_ts: int
    up_price: float
    down_price: float


@dataclass(frozen=True)
class Signal:
    """Signal produced by the strategy for a single tick."""

    action: SignalAction
    side: Optional[Side]
    confidence: float
    notional_usd: float
    edge: float
    fair_up_prob: float
    reason: str


@dataclass(frozen=True)
class Tick:
    """A single market tick combining price data and generated signal."""

    ts: datetime
    window: MarketWindow
    spot_price: float
    reference_price: float
    sigma_per_second: float
    fair_up_prob: float
    signal: Signal
    feed_source: str


@dataclass(frozen=True)
class StrategyParams:
    """Runtime parameters for the signal-generation strategy."""

    min_trade_usd: float
    max_trade_usd: float
    entry_edge_min: float
    min_confidence: float
    entry_min_remaining_seconds: int = 90
    max_entry_price: float = 0.95
    min_entry_price: float = 0.05


@dataclass(frozen=True)
class BacktestParams:
    """Parameter grid used when optimising or evaluating the strategy."""

    entry_edge_min: float
    min_confidence: float
    min_remaining_seconds: int
    max_entry_price: float
    min_trade_usd: float = 1.0
    max_trade_usd: float = 5.0
    min_entry_price: float = 0.05


# ---------------------------------------------------------------------------
# Mutable dataclasses — entities with identity that evolve over time
# ---------------------------------------------------------------------------


@dataclass
class PaperOrder:
    """A paper order at a point in its lifecycle."""

    order_id: int
    created_at: datetime
    window_slug: str
    side: Side
    state: OrderState
    requested_notional: float
    filled_notional: float
    entry_price: float
    confidence: float
    edge: float
    feed_source: str


@dataclass
class PaperPosition:
    """A paper position, open or closed, linked to its originating order."""

    position_id: int
    order: PaperOrder
    opened_at: datetime
    closed_at: Optional[datetime] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[ExitReason] = None
    realized_pnl_usd: Optional[float] = None


@dataclass
class PaperSnapshot:
    """Per-tick snapshot used by the paper trader loop and dashboard."""

    created_at: str
    window_slug: str
    market_question: str
    remaining_seconds: int
    spot_price: float
    reference_price: float
    sigma_per_second: float
    market_up_price: float
    market_down_price: float
    fair_up_prob: float
    edge: float
    signal_side: str | None
    confidence: float
    notional_usd: float
    reason: str
    feed_source: str


@dataclass
class PaperSummary:
    """Aggregated summary of the paper trading session for the dashboard."""

    running_state: str
    open_positions: int
    closed_positions: int
    total_pnl_usd: float
    open_exposure_usd: float
    closed_notional_usd: float
    win_rate: float | None
    avg_pnl_usd: float | None
    avg_hold_seconds: float | None
    risk_state: str
    last_signal: str
    last_tick_at: str | None
    last_window_slug: str | None
    last_spot_price: float | None
    last_fair_up_prob: float | None
    last_up_price: float | None
    last_edge: float | None
    last_feed_source: str | None
    recent_positions: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class BtcBotStatus:
    """Controller-reported status of the BTC bot."""

    state: str
    mode: str
    updated_at: str | None
    detail: str


@dataclass
class BtcHistoryStats:
    """Statistics extracted from the user's Polymarket trade-history CSV."""

    path: str
    found: bool
    total_rows: int = 0
    btc_rows: int = 0
    buys: int = 0
    sells: int = 0
    redeems: int = 0
    buy_usdc_total: float = 0.0
    buy_usdc_avg: float = 0.0
    buy_usdc_median: float = 0.0
    buy_usdc_min: float = 0.0
    buy_usdc_max: float = 0.0
    one_to_five_buy_share: float = 0.0


@dataclass
class BuyOpportunity:
    """A single historical buy opportunity enriched with pricing-model data."""

    market_name: str
    side: str
    trade_ts: int
    window_start_ts: int
    window_end_ts: int
    remaining_seconds: int
    entry_price: float
    actual_notional_usd: float
    actual_shares: float
    reference_price: float
    trade_spot_price: float
    settlement_price: float
    outcome: str
    fair_side_prob: float
    edge: float
    confidence: float
    settlement_pnl_usd: float


@dataclass
class BacktestMetrics:
    """Aggregated metrics produced by a backtest run."""

    name: str
    params: dict[str, Any]
    opportunities: int
    trades: int
    wins: int
    losses: int
    skipped: int
    total_notional_usd: float
    total_pnl_usd: float
    roi: float
    win_rate: float
    avg_pnl_usd: float
    max_drawdown_usd: float
    score: float
