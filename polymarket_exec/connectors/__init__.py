"""Exchange and data connectors.

Public API
----------
* :class:`ChainlinkSettlementConnector` — settlement-aligned reference open /
  close prints via Polymarket's crypto-price REST API (issue #21)
* :class:`ChainlinkWsFeed` — live Chainlink BTC/USD 1s prints + sigma series
  via wss://ws-live-data.polymarket.com (issue #21)
* Exception: ``FeedError``

The ABC connectors (base/binance/chainlink/polymarket) and the registry that
sat behind them were built but never wired into the live trading path — the
live feed is `chainlink_settlement` only, reached directly, never through the
registry/ABC layer. Removed 2026-09-21.
"""

from __future__ import annotations

from polymarket_exec.core.exceptions import FeedError

from .chainlink_settlement import (
    ChainlinkSettlementConnector,
    ChainlinkWsFeed,
    WindowPrices,
    build_subscribe_message,
)

__all__ = [
    "ChainlinkSettlementConnector",
    "ChainlinkWsFeed",
    "FeedError",
    "WindowPrices",
    "build_subscribe_message",
]
