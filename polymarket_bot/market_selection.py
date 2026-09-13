"""Operator market selection: which crypto asset + window timeframe to trade.

One global selection shared by paper AND live — mode is a railroad switch over
the same pipeline, never a different market. Persisted in the config table.

``STRATEGY_MARKETS`` names the combos that have a strategy; every other combo
is greyed out in the dashboard and refused here. ``LOOP_SUPPORTED`` names the
combos the loop (``paper.py``) actually trades today, so the dashboard can flag
a selection the loop is not yet wired for.
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

# Combos that have a strategy. Only these are selectable; add a combo here when
# its strategy lands (e.g. ``("btc", "1h")``). Empty today: the v0 strategy was
# archived 2026-09-13 and nothing has replaced it yet, so every button is grey.
STRATEGY_MARKETS: frozenset[tuple[str, str]] = frozenset()

# Shown when no selectable market exists — the market the loop runs on today.
DEFAULT_ASSET = "btc"
DEFAULT_TIMEFRAME = "5m"

# Combos the trading loop actually trades today.
LOOP_SUPPORTED: frozenset[tuple[str, str]] = frozenset({("btc", "5m")})

ASSET_KEY = "polymarket_bot.market_asset"
TIMEFRAME_KEY = "polymarket_bot.market_timeframe"


@dataclass(frozen=True)
class MarketSelection:
    asset: str
    timeframe: str

    @property
    def loop_supported(self) -> bool:
        return (self.asset, self.timeframe) in LOOP_SUPPORTED


def has_strategy(asset: str, timeframe: str) -> bool:
    return (asset, timeframe) in STRATEGY_MARKETS


def timeframe_for(asset: str, current: str) -> str | None:
    """Timeframe to land on when switching to ``asset``.

    Keeps ``current`` when that combo has a strategy, else the first timeframe
    (in display order) that does; None when the asset has no strategy at all.
    """
    if has_strategy(asset, current):
        return current
    return next((tf for tf in TIMEFRAMES if has_strategy(asset, tf)), None)


def default_selection() -> MarketSelection:
    """First market with a strategy (display order), else the loop's market."""
    for asset in ASSETS:
        for tf in TIMEFRAMES:
            if has_strategy(asset, tf):
                return MarketSelection(asset, tf)
    return MarketSelection(DEFAULT_ASSET, DEFAULT_TIMEFRAME)


def validate(asset: str, timeframe: str) -> str | None:
    """Return an error message, or None when the combo is valid."""
    if asset not in ASSETS:
        return f"unknown asset {asset!r}"
    if timeframe not in TIMEFRAMES:
        return f"unknown timeframe {timeframe!r}"
    if not has_strategy(asset, timeframe):
        return f"no strategy for {ASSETS[asset]} {TIMEFRAMES[timeframe]} yet"
    return None


async def get_selection() -> MarketSelection:
    from db import get_config

    asset = await get_config(ASSET_KEY, None)
    timeframe = await get_config(TIMEFRAME_KEY, None)
    if not asset or not timeframe or validate(asset, timeframe) is not None:
        return default_selection()
    return MarketSelection(asset, timeframe)


async def set_selection(asset: str, timeframe: str) -> MarketSelection:
    from db import set_config

    err = validate(asset, timeframe)
    if err is not None:
        raise ValueError(err)
    await set_config(ASSET_KEY, asset)
    await set_config(TIMEFRAME_KEY, timeframe)
    return MarketSelection(asset, timeframe)
