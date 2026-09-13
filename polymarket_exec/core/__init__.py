"""Core domain types, interfaces, and exceptions."""

from .types import (
    BacktestMetrics,
    BacktestParams,
    BtcBotStatus,
    BtcHistoryStats,
    BuyOpportunity,
    ExitReason,
    MarketWindow,
    OrderState,
    PaperOrder,
    PaperPosition,
    PaperSnapshot,
    PaperSummary,
    Side,
    Signal,
    SignalAction,
    StrategyParams,
    Tick,
)

from .exceptions import (
    BtcBotError,
    ConfigurationError,
    FeedError,
    MarketDiscoveryError,
    RiskBreach,
)

__all__ = [
    "BacktestMetrics",
    "BacktestParams",
    "BtcBotStatus",
    "BtcHistoryStats",
    "BuyOpportunity",
    "BtcBotError",
    "ConfigurationError",
    "ExitReason",
    "FeedError",
    "MarketDiscoveryError",
    "MarketWindow",
    "OrderState",
    "PaperOrder",
    "PaperPosition",
    "PaperSnapshot",
    "PaperSummary",
    "RiskBreach",
    "Side",
    "Signal",
    "SignalAction",
    "StrategyParams",
    "Tick",
]
