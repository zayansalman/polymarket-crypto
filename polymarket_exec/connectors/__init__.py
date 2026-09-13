"""Exchange and data connectors.

Public API
----------
* :class:`PolymarketConnector` — discovers BTC 5m binary markets on Polymarket
* :class:`BinanceConnector` — BTC spot price and recent close history
  (volatility-shape fallback and backtest tooling ONLY — never settlement levels)
* :class:`ChainlinkSettlementConnector` — settlement-aligned reference open /
  close prints via Polymarket's crypto-price REST API (issue #21)
* :class:`ChainlinkWsFeed` — live Chainlink BTC/USD 1s prints + sigma series
  via wss://ws-live-data.polymarket.com (issue #21)
* :class:`ChainlinkConnectorStub` — placeholder for Data Streams integration (#9)
* :class:`ConnectorRegistry` — registration, lookup, and health aggregation
* Base ABCs: ``AbstractPriceConnector``, ``AbstractMarketConnector``
* Exceptions: ``FeedError``, ``MarketDiscoveryError``
"""

from __future__ import annotations

from .base import (
    AbstractMarketConnector,
    AbstractPriceConnector,
    FeedError,
    MarketDiscoveryError,
)
from .binance import BinanceConnector
from .chainlink import ChainlinkConnectorStub
from .chainlink_settlement import (
    ChainlinkSettlementConnector,
    ChainlinkWsFeed,
    WindowPrices,
    build_subscribe_message,
)
from .polymarket import PolymarketConnector
from .registry import ConnectorRegistry

__all__ = [
    "AbstractMarketConnector",
    "AbstractPriceConnector",
    "BinanceConnector",
    "ChainlinkConnectorStub",
    "ChainlinkSettlementConnector",
    "ChainlinkWsFeed",
    "ConnectorRegistry",
    "FeedError",
    "MarketDiscoveryError",
    "PolymarketConnector",
    "WindowPrices",
    "build_subscribe_message",
]
