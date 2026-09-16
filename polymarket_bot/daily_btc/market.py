"""Daily BTC Up/Down market: noon-ET window timing, Gamma discovery, Binance reads.

The market dated D resolves Up iff the Binance BTCUSDT 1m candle labeled 12:00 ET on D
closes above the one labeled 12:00 ET on D-1; an exact tie pays 50-50. Gamma reports
those two instants as ``eventStartTime`` (reference) and ``endDate`` (settlement). A day
across a DST change is 23h or 25h, so every instant is built from America/New_York noon.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

import config as _config
from logging_setup import get_logger
from polymarket_exec.connectors.updown_quote import window_slug

log = get_logger("daily_btc_market")

ET = ZoneInfo("America/New_York")
SERIES_ID = 41
MINUTE_S = 60


@dataclass(frozen=True)
class DayWindow:
    market_date: date
    reference_ts: int
    settle_ts: int


@dataclass(frozen=True)
class DayMarket:
    slug: str
    question: str
    window: DayWindow
    up_token_id: str
    down_token_id: str
    fee_rate: float
    fee_exponent: float


def noon_et(day: date) -> int:
    return int(datetime.combine(day, time(12), tzinfo=ET).timestamp())


def window_for(market_date: date) -> DayWindow:
    return DayWindow(market_date, noon_et(market_date - timedelta(days=1)), noon_et(market_date))


def next_window(now_ts: int) -> DayWindow:
    """The market whose reference candle (noon ET) is the next one at or after ``now_ts``."""
    et = datetime.fromtimestamp(now_ts, ET)
    reference_day = et.date() if et.time() < time(12) else et.date() + timedelta(days=1)
    return window_for(reference_day + timedelta(days=1))


def current_window(now_ts: int) -> DayWindow:
    """The market whose reference noon is the latest one at or before ``now_ts``.

    Tsinghua-Kronos BTC 24h decides just after noon ET, once the 1h candle ending at noon
    has closed (Claude, 2026-09-16), so it needs the window that has just started.
    """
    et = datetime.fromtimestamp(now_ts, ET)
    reference_day = et.date() if et.time() >= time(12) else et.date() - timedelta(days=1)
    return window_for(reference_day + timedelta(days=1))


def outcome(reference_close: float, settle_close: float) -> str:
    if settle_close > reference_close:
        return "Up"
    if settle_close < reference_close:
        return "Down"
    return "tie"


def payout(side: str, result: str) -> float:
    """Per-share payout at resolution: an exact tie pays 0.50 to both sides.

    Source: market rules, Gamma, 2026-09-16.
    """
    if result == "tie":
        return 0.5
    return 1.0 if side == result else 0.0


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    return value if isinstance(value, list) else []


def _epoch(value: Any) -> int | None:
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def parse_market(row: dict[str, Any], window: DayWindow) -> DayMarket | None:
    """A Gamma market row as the market for ``window``, or None if it is a different one."""
    if _epoch(row.get("eventStartTime")) != window.reference_ts:
        return None
    if _epoch(row.get("endDate")) != window.settle_ts:
        return None
    tokens = _json_list(row.get("clobTokenIds"))
    labels = [str(o).lower() for o in _json_list(row.get("outcomes"))]
    if len(tokens) != 2 or sorted(labels) != ["down", "up"]:
        return None
    up = labels.index("up")
    rate, exponent = 0.0, 1.0
    if row.get("feesEnabled"):
        schedule = row.get("feeSchedule") or {}
        if "rate" not in schedule or "exponent" not in schedule:
            log.warning("daily_btc_market.fee_schedule_missing", slug=row.get("slug"))
            return None
        rate, exponent = float(schedule["rate"]), float(schedule["exponent"])
    slug = str(row.get("slug") or "")
    return DayMarket(slug, str(row.get("question") or slug), window,
                     str(tokens[up]), str(tokens[1 - up]), rate, exponent)


async def discover(client: httpx.AsyncClient, window: DayWindow) -> DayMarket | None:
    """The BTC daily market for ``window``: direct slug first, then the series listing."""
    start_of_day = datetime.combine(window.market_date, time(0), tzinfo=ET)
    slug = window_slug("btc", "1d", start_of_day)
    resp = await client.get(f"{_config.POLYMARKET_GAMMA_API}/markets", params={"slug": slug})
    resp.raise_for_status()
    for row in resp.json() or []:
        market = parse_market(row, window) if isinstance(row, dict) else None
        if market is not None:
            return market
    resp = await client.get(f"{_config.POLYMARKET_GAMMA_API}/events", params={
        "series_id": SERIES_ID, "closed": "false", "order": "endDate", "ascending": "true",
        "limit": 20,
    })
    resp.raise_for_status()
    for event in resp.json() or []:
        for row in event.get("markets") or []:
            market = parse_market(row, window)
            if market is not None:
                return market
    log.warning("daily_btc_market.not_found", slug=slug, reference_ts=window.reference_ts)
    return None


async def fetch_minute_close(client: httpx.AsyncClient, minute_ts: int,
                             now_ts: int) -> float | None:
    """Close of the Binance spot BTCUSDT 1m candle opening at ``minute_ts``, once it has closed."""
    if minute_ts % MINUTE_S or now_ts < minute_ts + MINUTE_S:
        return None
    resp = await client.get(f"{_config.BINANCE_API_BASE}/api/v3/klines", params={
        "symbol": "BTCUSDT", "interval": "1m", "startTime": minute_ts * 1000, "limit": 1,
    })
    resp.raise_for_status()
    rows = resp.json()
    if not rows or int(rows[0][0]) != minute_ts * 1000 or int(rows[0][6]) >= now_ts * 1000:
        return None
    return float(rows[0][4])


async def fetch_price_at(client: httpx.AsyncClient, ts: int, now_ts: int) -> float | None:
    """Last traded price at ``ts``: the close of the closed 1m candle that ends at ``ts``."""
    return await fetch_minute_close(client, ts - ts % MINUTE_S - MINUTE_S, now_ts)
