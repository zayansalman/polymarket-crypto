"""Operator market selection: which crypto asset + window timeframe to trade.

One global selection shared by paper AND live — mode is a railroad switch over
the same pipeline, never a different market. Persisted in the config table.

For now this is UI + persistence only: the loop (``paper.py``) still trades the
BTC 5m family. ``LOOP_SUPPORTED`` names the combos the loop actually honours so
the dashboard can flag a selection the loop is not yet wired for.
"""
from __future__ import annotations

from dataclasses import dataclass

# Polymarket Up/Down crypto family (asset slug prefix → display label).
ASSETS: dict[str, str] = {
    "btc": "BTC",
    "eth": "ETH",
    "sol": "SOL",
    "xrp": "XRP",
    "doge": "DOGE",
    "bnb": "BNB",
}

# Window timeframe → display label.
TIMEFRAMES: dict[str, str] = {
    "5m": "5m",
    "15m": "15m",
    "1h": "1h",
    "1d": "1d",
}

DEFAULT_ASSET = "btc"
DEFAULT_TIMEFRAME = "5m"

# Combos the trading loop actually trades today.
LOOP_SUPPORTED: frozenset[tuple[str, str]] = frozenset(
    {("btc", "5m"), ("btc", "1h"), ("btc", "1d")}
)

ASSET_KEY = "polymarket_bot.market_asset"
TIMEFRAME_KEY = "polymarket_bot.market_timeframe"


@dataclass(frozen=True)
class MarketSelection:
    asset: str
    timeframe: str

    @property
    def loop_supported(self) -> bool:
        return (self.asset, self.timeframe) in LOOP_SUPPORTED


def validate(asset: str, timeframe: str) -> str | None:
    """Return an error message, or None when the combo is valid."""
    if asset not in ASSETS:
        return f"unknown asset {asset!r}"
    if timeframe not in TIMEFRAMES:
        return f"unknown timeframe {timeframe!r}"
    return None


async def get_selection() -> MarketSelection:
    from db import get_config

    asset = (await get_config(ASSET_KEY, DEFAULT_ASSET)) or DEFAULT_ASSET
    timeframe = (await get_config(TIMEFRAME_KEY, DEFAULT_TIMEFRAME)) or DEFAULT_TIMEFRAME
    if validate(asset, timeframe) is not None:
        return MarketSelection(DEFAULT_ASSET, DEFAULT_TIMEFRAME)
    return MarketSelection(asset, timeframe)


async def set_selection(asset: str, timeframe: str) -> MarketSelection:
    from db import set_config

    err = validate(asset, timeframe)
    if err is not None:
        raise ValueError(err)
    await set_config(ASSET_KEY, asset)
    await set_config(TIMEFRAME_KEY, timeframe)
    return MarketSelection(asset, timeframe)
