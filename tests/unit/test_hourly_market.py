"""Hourly BTC market helpers: slug/timing, Gamma discovery, Binance candles and settlement."""
from __future__ import annotations

import json

import httpx
import pytest

import config as _config
from polymarket_bot.hourly import market as hm

H = 1_789_326_000  # 2026-09-13 19:00:00 UTC = 3PM ET (EDT)


def _row(slug: str, start_iso: str | None = "2026-09-13T19:00:00Z") -> dict:
    row = {
        "slug": slug,
        "question": "Bitcoin Up or Down - September 13, 3PM ET",
        "outcomes": json.dumps(["Up", "Down"]),
        "clobTokenIds": json.dumps(["111", "222"]),
    }
    if start_iso is not None:
        row["eventStartTime"] = start_iso
    return row


def _kline(open_s: int, o: float, c: float, *, hi: float | None = None,
           lo: float | None = None, vol: float = 10.0, tb: float = 5.0) -> list:
    return [open_s * 1000, str(o), str(hi if hi is not None else max(o, c)),
            str(lo if lo is not None else min(o, c)), str(c), str(vol),
            open_s * 1000 + 3_600_000 - 1, "0", 7, str(tb), "0", "0"]


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_hour_start_and_slug() -> None:
    assert hm.hour_start(H + 1799) == H
    assert hm.slug_for(H) == "bitcoin-up-or-down-september-13-2026-3pm-et"
    assert hm.slug_for(H - 15 * 3600) == "bitcoin-up-or-down-september-13-2026-12am-et"


def test_up_won_tie_goes_up() -> None:
    assert hm.up_won(100.0, 100.0) is True
    assert hm.up_won(100.0, 99.99) is False


@pytest.mark.asyncio
async def test_discover_returns_tokens_and_checks_event_start() -> None:
    slug = hm.slug_for(H)

    def handle(request: httpx.Request) -> httpx.Response:
        assert str(request.url).startswith(f"{_config.POLYMARKET_GAMMA_API}/markets")
        assert request.url.params["slug"] == slug
        return httpx.Response(200, json=[_row(slug)])

    async with _client(handle) as client:
        m = await hm.discover(client, H)
    assert m == hm.HourMarket(slug, "Bitcoin Up or Down - September 13, 3PM ET", H, "111", "222")


@pytest.mark.asyncio
async def test_discover_rejects_wrong_event_start_and_missing_market() -> None:
    slug = hm.slug_for(H)
    async with _client(lambda r: httpx.Response(
            200, json=[_row(slug, "2026-09-13T20:00:00Z")])) as client:
        assert await hm.discover(client, H) is None
    async with _client(lambda r: httpx.Response(200, json=[])) as client:
        assert await hm.discover(client, H) is None
    async with _client(lambda r: httpx.Response(200, json=[_row(slug, None)])) as client:
        assert (await hm.discover(client, H)).up_token_id == "111"  # field absent: slug is enough


@pytest.mark.asyncio
async def test_fetch_closed_candles_drops_forming_and_routes_perp() -> None:
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json=[_kline(H - 3600, 1, 2, tb=8.0), _kline(H, 2, 3)])

    async with _client(handle) as client:
        spot = await hm.fetch_closed_candles(client, market="spot", symbol="BTCUSDT",
                                             now_ms=(H + 30) * 1000, limit=170)
        perp = await hm.fetch_closed_candles(client, market="perp", symbol="BTCUSDT",
                                             now_ms=(H + 30) * 1000, limit=170)
    assert [c.open_time_ms for c in spot] == [(H - 3600) * 1000]
    assert spot[0].taker_buy_volume == 8.0 and spot[0].close == 2.0
    assert len(perp) == 1
    assert seen[0].startswith(f"{_config.BINANCE_API_BASE}/api/v3/klines")
    assert seen[1].startswith(f"{hm.BINANCE_FAPI}/fapi/v1/klines")


@pytest.mark.asyncio
async def test_fetch_hour_candle_open_and_closed_flag() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.params["startTime"] == str(H * 1000)
        return httpx.Response(200, json=[_kline(H, 100.0, 99.0)])

    async with _client(handle) as client:
        forming = await hm.fetch_hour_candle(client, H, (H + 60) * 1000)
        done = await hm.fetch_hour_candle(client, H, (H + 3600) * 1000)
    assert forming == hm.HourCandle(open=100.0, close=99.0, closed=False)
    assert done is not None and done.closed is True
    async with _client(lambda r: httpx.Response(200, json=[_kline(H + 3600, 1, 1)])) as client:
        assert await hm.fetch_hour_candle(client, H, (H + 7200) * 1000) is None  # wrong hour


@pytest.mark.asyncio
async def test_fetch_spot() -> None:
    async with _client(lambda r: httpx.Response(200, json={"price": "77123.5"})) as client:
        assert await hm.fetch_spot(client) == 77123.5
    async with _client(lambda r: httpx.Response(500)) as client:
        assert await hm.fetch_spot(client) is None
