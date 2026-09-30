"""The daily BTC Up/Down market: noon-ET window, Gamma discovery, Binance candles, CLOB book.

Polymarket's daily BTC Up/Down market (Gamma series 41, ``btc-up-or-down-daily``, slug
``bitcoin-up-or-down-on-<month>-<day>-<year>`` for the resolution date). Its window runs from
12:00 ET on day D to 12:00 ET on D+1. That is 23 or 25 hours across a clock change, so every
instant is built from America/New_York noon and 86400 is never added. The strike is the close
of the Binance BTCUSDT 1m candle that opens at 12:00 ET on D, and the settle is the same candle
at 12:00 ET on D+1. The market resolves Up if settle > strike and Down if settle < strike. An
exact tie pays 0.50 a share to both outcomes.

Live facts this relies on (read-only public GETs, 2026-09-21, and checked again 2026-09-29 on
``bitcoin-up-or-down-on-september-30-2026``):

* The slug includes the year. Without it Gamma returns ``[]``.
* Gamma's ``eventStartTime`` is the window start (noon ET on D) and ``endDate`` is the window
  end. ``startDate`` is when the market was created, about two days earlier. It is not the
  window start.
* ``clobTokenIds`` and ``outcomes`` are JSON strings, with Up listed first. Also present:
  ``conditionId``.
* CLOB ``GET /book`` lists ``bids`` and ``asks`` worst to best (the best level comes last).

Read-only: every call here is a public GET. Nothing places, cancels or simulates an order.

Ported by Claude, 2026-09-22, from the unmerged
``origin/feature/tsinghua-kronos-btc-24h-daily-btc-market`` modules. Moved onto ``ems`` by
Claude, 2026-09-29, for the display-only forecast (Zayan (operator), 2026-09-29): the window
comes from ``ems.marketdata.universe.window_bounds`` and the browser-like User-Agent is local.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from ems import config as _config
from ems.logging_setup import get_logger
from ems.marketdata.universe import window_bounds

log = get_logger("lc2004_kronos_btc_24h.market")

# Gamma series "btc-up-or-down-daily".
SERIES_ID = 41
# Polymarket's public REST endpoints answer a bare client with a Cloudflare 403.
UA = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
    "Accept": "application/json",
}

HOUR_MS = 3_600_000
MINUTE_S = 60
# Binance /api/v3/klines returns at most this many rows per request.
_BINANCE_MAX_LIMIT = 1000


# ---------------------------------------------------------------------------------------------
# Window timing
# ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class DayWindow:
    """One daily window. ``slug`` names the resolution date. Times are epoch seconds."""

    slug: str
    start_ts: int
    end_ts: int


def window_at(now_ts: float) -> DayWindow:
    """The window with ``start_ts <= now_ts < end_ts``.

    Exactly 12:00:00 ET belongs to the window that starts then. Start and end are both
    wall-clock noon ET, so the window is 23h (March spring-forward) or 25h (November
    fall-back) on a clock-change day.
    """
    slug, start, end = window_bounds("btc", "1d", datetime.fromtimestamp(now_ts, UTC))
    return DayWindow(slug=slug, start_ts=int(start), end_ts=int(end))


# ---------------------------------------------------------------------------------------------
# Gamma discovery
# ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class BtcDailyMarket:
    """The daily BTC market for one window: its ids and its two outcome tokens."""

    slug: str
    condition_id: str
    question: str
    start_ts: int
    end_ts: int
    up_token: str
    down_token: str


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
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:  # Gamma times are UTC; never read a bare time as local time
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp())


def _parse_market(row: Any, window: DayWindow) -> BtcDailyMarket | None:
    """A Gamma market row as the market for ``window``, or None if it is any other market."""
    if not isinstance(row, dict):
        return None
    if _epoch(row.get("eventStartTime")) != window.start_ts:
        return None
    if _epoch(row.get("endDate")) != window.end_ts:
        return None
    condition_id = str(row.get("conditionId") or "")
    tokens = _json_list(row.get("clobTokenIds"))
    labels = [str(o).strip().lower() for o in _json_list(row.get("outcomes"))]
    if not condition_id or len(tokens) != 2 or sorted(labels) != ["down", "up"]:
        return None
    up = labels.index("up")
    slug = str(row.get("slug") or window.slug)
    return BtcDailyMarket(
        slug=slug,
        condition_id=condition_id,
        question=str(row.get("question") or slug),
        start_ts=window.start_ts,
        end_ts=window.end_ts,
        up_token=str(tokens[up]),
        down_token=str(tokens[1 - up]),
    )


async def _gamma_get(client: httpx.AsyncClient, path: str, params: dict[str, Any]) -> Any:
    resp = await client.get(f"{_config.POLYMARKET_GAMMA_API}{path}", params=params, headers=UA)
    resp.raise_for_status()
    return resp.json()


def _request_error(exc: Exception) -> str:
    """A short, one-line reason for a failed request, for the card."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    return f"{type(exc).__name__}: {exc}".splitlines()[0][:120]


async def discover(client: httpx.AsyncClient, window: DayWindow) -> BtcDailyMarket | None:
    """The daily BTC market for ``window``, or None when Gamma answered and does not list it.

    It tries the direct slug first, then the open markets of series 41. A row is accepted only
    if its ``eventStartTime`` and ``endDate`` equal the window's start and end. If a request
    fails and the other one does not find the market, MarketDataError is raised with the
    reason, so the caller shows a failed lookup rather than "not listed yet" (Claude,
    2026-09-29, for a review finding). Gamma lists the daily market about two days ahead.
    """
    failures: list[str] = []
    try:
        rows = await _gamma_get(client, "/markets", {"slug": window.slug})
        for row in rows if isinstance(rows, list) else []:
            market = _parse_market(row, window)
            if market is not None:
                return market
        if rows:
            log.warning("lc2004_market.slug_row_mismatch", slug=window.slug,
                        start_ts=window.start_ts, end_ts=window.end_ts)
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("lc2004_market.slug_lookup_failed", slug=window.slug, error=str(exc))
        failures.append(f"the slug lookup failed ({_request_error(exc)})")
    try:
        events = await _gamma_get(client, "/events", {
            "series_id": SERIES_ID, "closed": "false", "order": "endDate",
            "ascending": "true", "limit": 20,
        })
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("lc2004_market.series_lookup_failed", slug=window.slug, error=str(exc))
        failures.append(f"the series lookup failed ({_request_error(exc)})")
        events = []
    for event in events if isinstance(events, list) else []:
        if not isinstance(event, dict):
            continue
        for row in event.get("markets") or []:
            market = _parse_market(row, window)
            if market is not None:
                log.info("lc2004_market.found_in_series", slug=market.slug)
                return market
    if failures:
        raise MarketDataError(f"Gamma: {'; '.join(failures)}")
    log.warning("lc2004_market.not_found", slug=window.slug, start_ts=window.start_ts)
    return None


# ---------------------------------------------------------------------------------------------
# Binance candles
# ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Candle:
    """One Binance spot BTCUSDT 1h candle. ``quote_volume`` is Kronos's ``amount`` column."""

    open_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float


class MarketDataError(Exception):
    """The market data needed for a forecast is missing, stale or malformed."""


async def fetch_closed_1h_candles(client: httpx.AsyncClient, *, now_ms: int,
                                  count: int = 512) -> list[Candle]:
    """The ``count`` most recent closed Binance BTCUSDT 1h candles, oldest first.

    Asks for ``count + 1`` rows and keeps only those with ``close_time < now_ms``. Normally
    Binance's last row is the hour still forming, so that leaves exactly ``count`` rows. The
    result must end with the last closed hour (open = floor(now, 1h) - 1h), be consecutive and
    be exactly ``count`` long, or MarketDataError is raised. If ``count + 1`` rows all look
    closed, Binance has not opened the new hour on its own clock yet. Our clock may be ahead of
    Binance and the "last" hour may still be taking trades, so that is refused too, and the
    caller retries a few seconds later. Request and parse failures also raise MarketDataError.
    """
    if not 1 <= count < _BINANCE_MAX_LIMIT:
        raise ValueError(f"count must be in [1, {_BINANCE_MAX_LIMIT - 1}], got {count}")
    try:
        resp = await client.get(f"{_config.BINANCE_API_BASE}/api/v3/klines", params={
            "symbol": "BTCUSDT", "interval": "1h", "limit": count + 1,
        })
        resp.raise_for_status()
        rows = resp.json()
        candles = [
            Candle(
                open_time_ms=int(r[0]),
                open=float(r[1]),
                high=float(r[2]),
                low=float(r[3]),
                close=float(r[4]),
                volume=float(r[5]),
                quote_volume=float(r[7]),
            )
            for r in rows
            if int(r[6]) < now_ms
        ]
    except httpx.HTTPError as exc:
        raise MarketDataError(f"binance 1h klines request failed: {exc}") from exc
    except (ValueError, TypeError, IndexError, KeyError) as exc:
        raise MarketDataError(f"binance 1h klines response malformed: {exc}") from exc

    expected_last = (now_ms // HOUR_MS) * HOUR_MS - HOUR_MS
    if not candles or candles[-1].open_time_ms != expected_last:
        got = candles[-1].open_time_ms if candles else None
        raise MarketDataError(
            f"last closed 1h candle opens at {got}, expected {expected_last}")
    if len(candles) != count:
        raise MarketDataError(f"got {len(candles)} closed 1h candles, expected exactly {count}")
    for prev, cur in zip(candles, candles[1:], strict=False):
        if cur.open_time_ms - prev.open_time_ms != HOUR_MS:
            raise MarketDataError(
                f"1h candles not consecutive: {prev.open_time_ms} then {cur.open_time_ms}")
    return candles


async def fetch_minute_close(client: httpx.AsyncClient, minute_ts: int,
                             now_ts: float) -> float | None:
    """Close of the Binance BTCUSDT 1m candle that opens at ``minute_ts``, once it has closed.

    None means not closed yet: ``minute_ts`` is not minute-aligned or ``now_ts < minute_ts +
    60`` (no request is made), or Binance still shows the candle open on our clock. Once the
    minute is over, anything else that gives no close raises MarketDataError: a request error,
    a malformed reply, or a reply without the candle opening at exactly ``minute_ts``. The
    caller shows that as a failure, not as waiting (Claude, 2026-09-29, for a review finding).
    Use it for the strike: the close of the 1m candle at 12:00 ET on D.
    """
    if minute_ts % MINUTE_S or now_ts < minute_ts + MINUTE_S:
        return None
    try:
        resp = await client.get(f"{_config.BINANCE_API_BASE}/api/v3/klines", params={
            "symbol": "BTCUSDT", "interval": "1m", "startTime": minute_ts * 1000, "limit": 1,
        })
        resp.raise_for_status()
        rows = resp.json()
        first = rows[0] if isinstance(rows, list) and rows else None
        if first is not None:
            opened, close_time, close = int(first[0]), int(first[6]), float(first[4])
    except httpx.HTTPError as exc:
        log.warning("lc2004_market.minute_close_failed", minute_ts=minute_ts, error=str(exc))
        raise MarketDataError(f"binance 1m klines request failed ({_request_error(exc)})") from exc
    except (ValueError, TypeError, IndexError, KeyError) as exc:
        log.warning("lc2004_market.minute_close_malformed", minute_ts=minute_ts, error=str(exc))
        raise MarketDataError(f"binance 1m klines response malformed: {exc}") from exc
    if first is None:
        raise MarketDataError(f"binance returned no 1m candle from {minute_ts * 1000}")
    if opened != minute_ts * 1000:
        raise MarketDataError(
            f"binance returned the 1m candle opening at {opened}, not {minute_ts * 1000}")
    if close_time >= now_ts * 1000:
        return None
    return close


# ---------------------------------------------------------------------------------------------
# CLOB book
# ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Book:
    """One token's order book, best level first on both sides: ((price, size), ...)."""

    bids: tuple[tuple[float, float], ...]
    asks: tuple[tuple[float, float], ...]

    @property
    def best_bid(self) -> float | None:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0][0] if self.asks else None


def _book_side(raw: Any, *, best_first_descending: bool) -> tuple[tuple[float, float], ...]:
    levels: list[tuple[float, float]] = []
    for item in raw if isinstance(raw, list) else []:
        price, size = float(item["price"]), float(item["size"])
        if size > 0:
            levels.append((price, size))
    # The CLOB sends worst to best. Sorting explicitly gives best first, and stays right if
    # that order ever changes.
    levels.sort(key=lambda lv: lv[0], reverse=best_first_descending)
    return tuple(levels)


async def fetch_book(client: httpx.AsyncClient, token_id: str) -> Book | None:
    """The CLOB REST book for ``token_id``, best first, or None when it cannot be read."""
    try:
        resp = await client.get(f"{_config.POLYMARKET_CLOB_API}/book",
                                params={"token_id": token_id}, headers=UA)
        resp.raise_for_status()
        raw = resp.json()
        if not isinstance(raw, dict):
            raise ValueError("book response is not an object")
        return Book(
            bids=_book_side(raw.get("bids"), best_first_descending=True),
            asks=_book_side(raw.get("asks"), best_first_descending=False),
        )
    except httpx.HTTPError as exc:
        log.warning("lc2004_market.book_failed", token_id=token_id, error=str(exc))
    except (ValueError, TypeError, KeyError) as exc:
        log.warning("lc2004_market.book_malformed", token_id=token_id, error=str(exc))
    return None
