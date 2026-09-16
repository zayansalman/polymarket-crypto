"""Hourly BTC book record: the Polymarket book 0, 10, 30, 60 and 120 s after the hour opens.

Every hour the hourly loop runs, bet or no bet, this stores both tokens' top three levels, a
spot-based fair value for Up and the previous hour's move. It answers whether the opening
price already leans against the previous hour. Observation only: nothing here changes a
decision or an entry.

Sources: approved by Zayan (operator), 2026-09-15, from the alphaXiv sweep by Claude,
2026-09-15 (research/hourly_btc_2026_09/literature/). Fair value is the digital-option value
with zero rate, arXiv 2606.19517 eq. 3.3:
    P(up) = Phi( ln(spot / open) / (sigma * sqrt(tau)) - sigma * sqrt(tau) / 2 )
with tau the hours left and sigma the sample stdev of the last 168 closed hourly log returns.
Rows are keyed by the hour's UTC start (branch-review finding dst-fallback-slug-collision).
"""
from __future__ import annotations

import json
import math
import statistics
import time
from dataclasses import dataclass
from typing import Any

import httpx

import config as _config
import db as _db
from logging_setup import get_logger
from polymarket_bot.hourly import market
from polymarket_bot.hourly.market import HOUR_S, Candle, HourMarket

log = get_logger("hourly_book_record")

# Each offset owns the window up to the next one; the last owns 120-179 s.
OFFSETS_S: tuple[int, ...] = (0, 10, 30, 60, 120)
_LAST_WINDOW_END_S = 180
SIGMA_WINDOW = 168
MIRROR_GAP_LOG_THRESHOLD = 0.011

_recorded: set[tuple[int, int]] = set()
_candles_by_hour: dict[int, list[Candle]] = {}


@dataclass(frozen=True)
class BookLevels:
    bids: list[tuple[float, float]]  # best first
    asks: list[tuple[float, float]]  # best first
    book_ts_ms: int | None


def reset_caches() -> None:
    _recorded.clear()
    _candles_by_hour.clear()


def offset_for(elapsed_s: int) -> int | None:
    if elapsed_s < 0 or elapsed_s >= _LAST_WINDOW_END_S:
        return None
    current = None
    for offset in OFFSETS_S:
        if elapsed_s >= offset:
            current = offset
    return current


def fair_up(
    spot: float, hour_open: float, sigma_1h: float | None, seconds_left: float
) -> float | None:
    if spot <= 0 or hour_open <= 0:
        return None
    tau = seconds_left / HOUR_S
    if tau <= 0:
        return 1.0 if spot >= hour_open else 0.0  # the market resolves ties Up
    if sigma_1h is None or sigma_1h <= 0:
        return None
    width = sigma_1h * math.sqrt(tau)
    x = math.log(spot / hour_open) / width - width / 2
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def hour_sigma(candles: list[Candle]) -> float | None:
    returns = [math.log(c.close / c.open) for c in candles[-SIGMA_WINDOW:]
               if c.open > 0 and c.close > 0]
    if len(returns) < 2:
        return None
    sd = statistics.stdev(returns)
    return sd if sd > 0 else None


def _levels(raw: Any, depth: int) -> list[tuple[float, float]]:
    if not isinstance(raw, list):
        return []
    # CLOB arrays list levels worst to best, so the best levels are the tail.
    return [(float(level["price"]), float(level["size"])) for level in reversed(raw[-depth:])]


async def fetch_levels(client: httpx.AsyncClient, token_id: str, depth: int = 3) -> BookLevels | None:
    try:
        resp = await client.get(f"{_config.POLYMARKET_CLOB_API}/book", params={"token_id": token_id})
        resp.raise_for_status()
        data = resp.json()
        stamp = data.get("timestamp")
        return BookLevels(
            bids=_levels(data.get("bids"), depth),
            asks=_levels(data.get("asks"), depth),
            book_ts_ms=int(stamp) if isinstance(stamp, (int, str)) and str(stamp).isdigit() else None,
        )
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as exc:
        log.warning("hourly_book_record.book_failed", token_id=token_id[:16], error=str(exc))
        return None


def _best(levels: BookLevels | None, side: str) -> float | None:
    if levels is None:
        return None
    rows = levels.bids if side == "bid" else levels.asks
    return rows[0][0] if rows else None


def _gap(a: float | None, b: float | None) -> float | None:
    return None if a is None or b is None else a - (1 - b)


async def _closed_candles(client: httpx.AsyncClient, start: int, now: int) -> list[Candle]:
    if start not in _candles_by_hour:
        try:
            candles = await market.fetch_closed_candles(
                client, market="spot", symbol="BTCUSDT", now_ms=now * 1000, limit=SIGMA_WINDOW + 2
            )
        except httpx.HTTPError as exc:
            log.warning("hourly_book_record.candles_failed", error=str(exc))
            return []
        _candles_by_hour.clear()
        _candles_by_hour[start] = candles
    return _candles_by_hour[start]


async def maybe_record(
    client: httpx.AsyncClient, *, snapshot: Any, market: HourMarket, now: int
) -> bool:
    """Write this hour's row for the current offset, once. Never raises."""
    try:
        return await _record(client, snapshot=snapshot, hour=market, now=now)
    except Exception as exc:  # noqa: BLE001 - an observation must never break the tick
        log.warning("hourly_book_record.failed", error=f"{type(exc).__name__}: {exc}")
        return False


async def _record(client: httpx.AsyncClient, *, snapshot: Any, hour: HourMarket, now: int) -> bool:
    start = hour.window_start_ts
    elapsed = now - start
    offset = offset_for(elapsed)
    if offset is None or (start, offset) in _recorded:
        return False
    up = await fetch_levels(client, hour.up_token_id)
    down = await fetch_levels(client, hour.down_token_id)
    if up is None and down is None:
        return False  # nothing to record; retry on the next tick inside this window
    candles = await _closed_candles(client, start, now)
    sigma = hour_sigma(candles)
    prev = next((c for c in candles if c.open_time_ms == (start - HOUR_S) * 1000), None)
    prev_return = math.log(prev.close / prev.open) if prev and prev.open > 0 else None
    seconds_left = max(0, start + HOUR_S - now)
    gap_ask = _gap(_best(up, "ask"), _best(down, "bid"))
    gap_bid = _gap(_best(up, "bid"), _best(down, "ask"))
    for name, gap in (("ask", gap_ask), ("bid", gap_bid)):
        if gap is not None and abs(gap) > MIRROR_GAP_LOG_THRESHOLD:
            log.info("hourly_book_record.mirror_mismatch", side=name, gap=round(gap, 4),
                     window_start_ts=start, offset_s=offset)
    async with _db.connect() as conn:
        await conn.execute(
            """
            INSERT OR IGNORE INTO hourly_book_snapshots(
              created_at, window_slug, window_start_ts, offset_s, elapsed_s, fetched_at_ms,
              up_best_bid, up_best_ask, down_best_bid, down_best_ask,
              up_bids_json, up_asks_json, down_bids_json, down_asks_json,
              up_book_ts_ms, down_book_ts_ms, mirror_gap_ask, mirror_gap_bid,
              spot, hour_open, sigma_1h, seconds_left, fair_up,
              prev_hour_return, prev_hour_vol_units
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _db.utc_now_iso(), hour.slug, start, offset, elapsed, int(time.time() * 1000),
                _best(up, "bid"), _best(up, "ask"), _best(down, "bid"), _best(down, "ask"),
                json.dumps(up.bids) if up else None, json.dumps(up.asks) if up else None,
                json.dumps(down.bids) if down else None, json.dumps(down.asks) if down else None,
                up.book_ts_ms if up else None, down.book_ts_ms if down else None,
                gap_ask, gap_bid,
                snapshot.spot_price or None, snapshot.reference_price or None, sigma, seconds_left,
                fair_up(snapshot.spot_price or 0.0, snapshot.reference_price or 0.0, sigma,
                        seconds_left),
                prev_return,
                prev_return / sigma if prev_return is not None and sigma else None,
            ),
        )
        await conn.commit()
    for key in [k for k in _recorded if k[0] != start]:
        _recorded.discard(key)
    _recorded.add((start, offset))
    return True
