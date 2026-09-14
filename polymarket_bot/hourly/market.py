"""Hourly BTC Up/Down market: window timing, Gamma discovery, Binance candles and settlement.

The market resolves Up iff the Binance BTCUSDT 1h candle that starts at the market's
``eventStartTime`` closes at or above its open (ties go Up). Settlement is read from
Binance directly: Gamma stops listing a resolved hourly market about 12 minutes after
close, so nothing here depends on Polymarket still returning a past window.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

import config as _config
from logging_setup import get_logger
from polymarket_exec.connectors.updown_quote import window_slug

log = get_logger("hourly_market")

HOUR_S = 3600
BINANCE_FAPI = "https://fapi.binance.com"


@dataclass(frozen=True)
class HourMarket:
    slug: str
    question: str
    window_start_ts: int
    up_token_id: str
    down_token_id: str


@dataclass(frozen=True)
class Candle:
    open_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float
    taker_buy_volume: float


@dataclass(frozen=True)
class HourCandle:
    open: float
    close: float
    closed: bool


def hour_start(now: int) -> int:
    return now - now % HOUR_S


def slug_for(start_ts: int) -> str:
    return window_slug("btc", "1h", datetime.fromtimestamp(start_ts, UTC))


def up_won(open_price: float, close_price: float) -> bool:
    return close_price >= open_price


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    return value if isinstance(value, list) else []


def _tokens(row: dict[str, Any]) -> tuple[str, str] | None:
    tokens = _json_list(row.get("clobTokenIds"))
    if len(tokens) != 2:
        return None
    labels = [str(o).lower() for o in _json_list(row.get("outcomes"))]
    up = labels.index("up") if "up" in labels else 0
    return str(tokens[up]), str(tokens[1 - up])


async def discover(client: httpx.AsyncClient, start_ts: int) -> HourMarket | None:
    """The hourly BTC market for the hour starting at ``start_ts``, or None."""
    slug = slug_for(start_ts)
    resp = await client.get(f"{_config.POLYMARKET_GAMMA_API}/markets", params={"slug": slug})
    resp.raise_for_status()
    rows = resp.json()
    row = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else None
    if row is None:
        return None
    event_start = row.get("eventStartTime")
    if event_start:
        started = int(datetime.fromisoformat(str(event_start).replace("Z", "+00:00")).timestamp())
        if started != start_ts:
            log.warning("hourly_market.event_start_mismatch", slug=slug,
                        event_start=event_start, expected=start_ts)
            return None
    tokens = _tokens(row)
    if tokens is None:
        return None
    return HourMarket(slug, str(row.get("question") or slug), start_ts, tokens[0], tokens[1])


def _klines_url(market: str) -> str:
    if market == "spot":
        return f"{_config.BINANCE_API_BASE}/api/v3/klines"
    return f"{BINANCE_FAPI}/fapi/v1/klines"


async def fetch_closed_candles(
    client: httpx.AsyncClient, *, market: str, symbol: str, now_ms: int, limit: int
) -> list[Candle]:
    """Most recent closed 1h candles, oldest first (the forming candle is dropped).

    Without ``startTime`` Binance always returns its own current (forming) candle last, so
    that row is dropped by position, on Binance's clock. The ``close_time < now_ms`` check
    stays as a second guard. A local clock running ahead of Binance cannot then hand the
    strategy a previous hour that is still forming.
    """
    resp = await client.get(
        _klines_url(market), params={"symbol": symbol, "interval": "1h", "limit": limit}
    )
    resp.raise_for_status()
    return [
        Candle(
            open_time_ms=int(r[0]),
            open=float(r[1]),
            high=float(r[2]),
            low=float(r[3]),
            close=float(r[4]),
            volume=float(r[5]),
            quote_volume=float(r[7]),
            taker_buy_volume=float(r[9]),
        )
        for r in resp.json()[:-1]
        if int(r[6]) < now_ms
    ]


async def fetch_hour_candle(
    client: httpx.AsyncClient, start_ts: int, now_ms: int
) -> HourCandle | None:
    """The Binance spot 1h candle that opens at ``start_ts`` (forming or closed), or None.

    ``closed`` needs Binance's own clock as well as ours: the next hour's candle must already
    exist, because Binance only opens it after processing every trade of this one. A local
    clock running ahead of Binance cannot then settle on a candle that is still forming.
    """
    resp = await client.get(
        _klines_url("spot"),
        params={"symbol": "BTCUSDT", "interval": "1h", "startTime": start_ts * 1000, "limit": 2},
    )
    resp.raise_for_status()
    rows = resp.json()
    if not rows or int(rows[0][0]) != start_ts * 1000:
        return None
    row = rows[0]
    nxt = (start_ts + HOUR_S) * 1000
    closed = int(row[6]) < now_ms and len(rows) > 1 and int(rows[1][0]) == nxt
    return HourCandle(open=float(row[1]), close=float(row[4]), closed=closed)


async def fetch_spot(client: httpx.AsyncClient) -> float | None:
    try:
        resp = await client.get(
            f"{_config.BINANCE_API_BASE}/api/v3/ticker/price", params={"symbol": "BTCUSDT"}
        )
        resp.raise_for_status()
        return float(resp.json()["price"])
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        log.warning("hourly_market.spot_read_failed", error=str(exc))
        return None
