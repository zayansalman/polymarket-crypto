"""Tsinghua-Kronos BTC 24h input: 383 closed Binance spot 1h candles before the window's noon."""
from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

import config as _config
from polymarket_bot.daily_btc import forecast_input as fi
from polymarket_bot.daily_btc import market as dbm

S = int(datetime(2026, 9, 16, 16, tzinfo=UTC).timestamp())
WINDOW = dbm.current_window(S)


def _kline(open_s: int) -> list:
    return [open_s * 1000, "100", "101", "99", "100.5", "10", open_s * 1000 + 3_599_999,
            "1000", 5, "5", "0", "0"]


def _client(rows: list[list], seen: list | None = None) -> httpx.AsyncClient:
    def handle(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(200, json=rows)
    return httpx.AsyncClient(transport=httpx.MockTransport(handle))


@pytest.mark.asyncio
async def test_returns_the_383_candles_ending_an_hour_before_noon() -> None:
    rows = [_kline(S - k * 3600) for k in range(383, -1, -1)]  # ... S-1h, then forming S
    seen: list[httpx.Request] = []
    async with _client(rows, seen) as client:
        candles = await fi.fetch_forecast_candles(client, WINDOW, S + 5)
    assert candles is not None and len(candles) == fi.INPUT_CANDLES == 383
    assert candles[-1].open_time_ms == (S - 3600) * 1000
    assert candles[0].open_time_ms == (S - 383 * 3600) * 1000
    assert candles[-1].quote_volume == 1000.0
    req = seen[0]
    assert str(req.url).startswith(f"{_config.BINANCE_API_BASE}/api/v3/klines")
    assert req.url.params["interval"] == "1h" and req.url.params["limit"] == "384"


@pytest.mark.asyncio
async def test_none_while_the_hour_before_noon_is_not_closed_on_binance() -> None:
    rows = [_kline(S - k * 3600) for k in range(384, 0, -1)]  # Binance has not opened S yet
    async with _client(rows) as client:
        assert await fi.fetch_forecast_candles(client, WINDOW, S + 2) is None


@pytest.mark.asyncio
async def test_none_when_history_is_short() -> None:
    rows = [_kline(S - k * 3600) for k in range(10, -1, -1)]
    async with _client(rows) as client:
        assert await fi.fetch_forecast_candles(client, WINDOW, S + 5) is None
