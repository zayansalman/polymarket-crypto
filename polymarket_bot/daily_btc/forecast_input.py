"""Tsinghua-Kronos BTC 24h input: the 383 closed Binance spot BTCUSDT 1h candles before noon ET.

Same as the Kronos team's demo (github.com/shiyu-coder/Kronos-demo update_predictions.py at
eba16695): it fetched 384 candles and dropped the still-forming one. Here the last input
candle must be the one that opens an hour before the window's reference noon, so its close
is the price at noon.
"""
from __future__ import annotations

import httpx

from polymarket_bot.daily_btc.market import DayWindow
from polymarket_bot.hourly.market import Candle, fetch_closed_candles

INPUT_CANDLES = 383
_HOUR_S = 3600


async def fetch_forecast_candles(
    client: httpx.AsyncClient, window: DayWindow, now_ts: int
) -> list[Candle] | None:
    candles = await fetch_closed_candles(
        client, market="spot", symbol="BTCUSDT", now_ms=now_ts * 1000, limit=INPUT_CANDLES + 1
    )
    if len(candles) < INPUT_CANDLES:
        return None
    if candles[-1].open_time_ms != (window.reference_ts - _HOUR_S) * 1000:
        return None
    return candles[-INPUT_CANDLES:]
