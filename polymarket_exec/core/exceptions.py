"""Custom exception hierarchy for the BTC 5m Binary Pricing Model trading system."""

from __future__ import annotations


class BtcBotError(Exception):
    """Base exception for all BTC bot errors."""

    pass


class FeedError(BtcBotError):
    """Raised when a price or market data feed fails or returns stale data."""

    pass


class MarketDiscoveryError(BtcBotError):
    """Raised when the current market window cannot be discovered."""

    pass


class RiskBreach(BtcBotError):
    """Raised when a risk limit is exceeded (pre-trade or post-trade)."""

    pass


class ConfigurationError(BtcBotError):
    """Raised when configuration is missing, malformed, or inconsistent."""

    pass
