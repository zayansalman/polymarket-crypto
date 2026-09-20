"""Custom exception hierarchy for the BTC 5m Binary Pricing Model trading system."""

from __future__ import annotations


class BtcBotError(Exception):
    """Base exception for all BTC bot errors."""

    pass


class FeedError(BtcBotError):
    """Raised when a price or market data feed fails or returns stale data."""

    pass
