"""I/O fetchers for the regime monitor: Binance spot klines, Gamma markets, tick book.

Every fetcher returns an empty / ``None`` result on failure and logs a
structured warning — the monitor turns that into a ``quality`` entry on the
snapshot rather than crashing the scan. The same ``BINANCE_API_BASE`` mirror
and ``POLYMARKET_GAMMA_API`` base the rest of the lab uses are reused, so
this package adds no new endpoint to the network surface.
"""
from __future__ import annotations

import json
import statistics
from datetime import UTC, datetime
from typing import Any

import httpx

import config as _config
from db import connect as _connect
from logging_setup import get_logger
from polymarket_bot.regime.types import Bar, BookState, VenueMarket

log = get_logger("regime_sources")

# Short asset key (market_selection.ASSETS) -> Binance spot symbol. Same map
# tools/venue_recorder.py:SPOT_SYMBOL uses; duplicated as a plain constant so
# this live package does not import a CLI tool.
SPOT_SYMBOL: dict[str, str] = {
    "btc": "BTCUSDT",
    "eth": "ETHUSDT",
    "sol": "SOLUSDT",
    "xrp": "XRPUSDT",
    "doge": "DOGEUSDT",
    "bnb": "BNBUSDT",
}

# Window length in seconds per clock-derived Up/Down rung (the #181 slug
# scheme ``{asset}-updown-{rung}-{floor(now/len)*len}``). The daily family
# uses a different, date-named slug and is not clock-derivable.
WINDOW_SECONDS: dict[str, int] = {"5m": 300, "15m": 900, "1h": 3600}

# The loop's own safety floor (strategy.sigma_per_second): a tick whose sigma
# sits exactly on it carries no volatility information.
SIGMA_FLOOR = 0.00002

# Quotable phase of an Up/Down window, as fractions of its length remaining:
# 0.2 → 0.9 is 60–270 s for the 5m family the loop trades. Outside it the book
# widens / skews for reasons tied to the window clock, not the regime.
BOOK_PHASE_MIN_FRACTION = 0.2
BOOK_PHASE_MAX_FRACTION = 0.9
LOOP_WINDOW_SECONDS = 300   # the loop only journals the 5m family


def phase_bounds(window_seconds: int) -> tuple[float, float]:
    """``(min, max)`` remaining seconds for the quotable phase of a window."""
    return (
        window_seconds * BOOK_PHASE_MIN_FRACTION,
        window_seconds * BOOK_PHASE_MAX_FRACTION,
    )


def in_quotable_phase(remaining_seconds: float, window_seconds: int) -> bool:
    lo, hi = phase_bounds(window_seconds)
    return lo <= remaining_seconds <= hi

# Binance kline row layout (documented, stable):
# [open_time, open, high, low, close, volume, close_time, quote_volume,
#  trades, taker_buy_base, taker_buy_quote, ignore]
_KLINE_MIN_LEN = 11


def parse_kline(row: Any) -> Bar | None:
    """Turn one raw kline row into a :class:`Bar`; ``None`` if malformed."""
    if not isinstance(row, (list, tuple)) or len(row) < _KLINE_MIN_LEN:
        return None
    try:
        return Bar(
            open_time_ms=int(row[0]),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
            quote_volume=float(row[7]),
            trades=int(row[8]),
            taker_buy_quote=float(row[10]),
        )
    except (TypeError, ValueError):
        return None


def parse_klines(rows: Any) -> list[Bar]:
    """Parse a klines response, dropping malformed rows, oldest-first."""
    if not isinstance(rows, list):
        return []
    bars = [parse_kline(r) for r in rows]
    return [b for b in bars if b is not None]


async def fetch_bars(
    client: httpx.AsyncClient,
    symbol: str,
    interval: str,
    limit: int,
    *,
    completed_only: bool = True,
) -> list[Bar]:
    """The last ``limit`` COMPLETED klines for ``symbol`` at ``interval``, oldest-first.

    Binance returns the still-forming candle as the final row of an
    open-ended request; with ``completed_only`` (the default) one extra row
    is requested and that partial bar is dropped, so sums and ranges are over
    whole bars. Empty list on any HTTP / decode failure (logged). Binance
    caps ``limit`` at 1000 per call; every horizon this package uses fits.
    """
    want = limit + 1 if completed_only else limit
    try:
        resp = await client.get(
            f"{_config.BINANCE_API_BASE}/api/v3/klines",
            params={"symbol": symbol, "interval": interval, "limit": want},
        )
        resp.raise_for_status()
        rows = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning(
            "regime.bars_unavailable", symbol=symbol, interval=interval, error=str(exc)
        )
        return []
    bars = parse_klines(rows)
    if completed_only and bars:
        bars = bars[:-1]
    return bars[-limit:] if limit > 0 else bars


# --- Venue (Gamma) --------------------------------------------------------------


def window_slug(asset: str, timeframe: str, window_start_ts: int) -> str | None:
    """Slug of the clock-derived Up/Down window starting at ``window_start_ts``.

    Mirrors :func:`polymarket_bot.pairarb.market_index.window_slug` (the
    #181 discovery) for every clock-derivable rung. ``None`` for the daily
    family, whose slug is date-named.
    """
    if timeframe not in WINDOW_SECONDS:
        return None
    return f"{asset}-updown-{timeframe}-{window_start_ts}"


def window_starts(timeframe: str, now_ts: int, completed: int) -> tuple[int | None, list[int]]:
    """``(current window start, [starts of the last ``completed`` windows])``.

    Completed windows are oldest-first and exclude the current one, whose
    volume is still accruing and therefore not comparable.
    """
    length = WINDOW_SECONDS.get(timeframe)
    if length is None:
        return None, []
    current = now_ts - (now_ts % length)
    return current, [current - length * k for k in range(completed, 0, -1)]


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def outcome_token_ids(market: dict[str, Any]) -> tuple[str, str]:
    """``(up_token_id, down_token_id)`` from a Gamma market's ``clobTokenIds``.

    Same convention as the loop (``paper.py:_outcome_token_ids``): the id list
    is aligned with ``outcomes``; empty strings when unavailable.
    """
    token_ids = _json_list(market.get("clobTokenIds"))
    if len(token_ids) != 2:
        return "", ""
    outcomes = _json_list(market.get("outcomes"))
    up_idx = 0
    if len(outcomes) == 2:
        labels = [str(x).lower() for x in outcomes]
        if "up" in labels:
            up_idx = labels.index("up")
    return str(token_ids[up_idx]), str(token_ids[1 - up_idx])


def parse_venue_market(market: dict[str, Any]) -> VenueMarket:
    """Volume / liquidity / token ids from a Gamma market record (``*Num`` preferred)."""
    up, down = outcome_token_ids(market)
    return VenueMarket(
        slug=str(market.get("slug") or ""),
        volume_usd=_float_or_none(market.get("volumeNum") or market.get("volume")),
        liquidity_usd=_float_or_none(
            market.get("liquidityNum") or market.get("liquidity")
        ),
        up_token=up,
        down_token=down,
    )


async def fetch_venue_market(
    client: httpx.AsyncClient, slug: str
) -> VenueMarket | None:
    """The Gamma market record for ``slug``; ``None`` when absent or failing."""
    try:
        resp = await client.get(
            f"{_config.POLYMARKET_GAMMA_API}/markets", params={"slug": slug}
        )
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("regime.venue_unavailable", slug=slug, error=str(exc))
        return None
    market = data[0] if isinstance(data, list) and data else None
    if not isinstance(market, dict):
        return None
    return parse_venue_market(market)


# --- Book (the loop's tick journal) --------------------------------------------


def age_seconds(ts: str | None, now: datetime | None = None) -> int | None:
    """Whole seconds since ISO-8601 ``ts`` (naive = UTC); ``None`` if unparseable."""
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    now = now or datetime.now(UTC)
    return max(0, int((now - parsed).total_seconds()))


def _vol_source(feed_source: Any) -> str | None:
    """The ``vol=`` label out of the tick's ``feed_source`` provenance string."""
    if not isinstance(feed_source, str):
        return None
    for chunk in feed_source.split(";"):
        if chunk.startswith("vol="):
            return chunk[4:].strip() or None
    return None


def _in_phase(tick: dict[str, Any]) -> bool:
    rem = tick.get("remaining_seconds")
    return isinstance(rem, (int, float)) and in_quotable_phase(rem, LOOP_WINDOW_SECONDS)


def _crossed(bid: float | None, ask: float | None) -> bool:
    """A side whose bid is above its ask is not a quotable market (the loop's own
    ``BookTop.crossed`` predicate)."""
    return bid is not None and ask is not None and bid > ask


def book_from_ticks(
    ticks: list[dict[str, Any]],
    now: datetime | None = None,
    window_prefix: str | None = None,
) -> BookState | None:
    """Phase-conditioned book averages over recent tick rows (newest first).

    Only ticks inside the quotable phase contribute to the averages. A tick
    with a crossed side (bid above ask) contributes to no cost measure; a
    one-sided tick contributes only the measures it has both legs for.
    ``ticks_used`` counts in-phase reads. The newest in-phase row supplies
    the sigma / feed provenance. ``None`` when
    no row is in phase — a book measured at the window edges is not the
    market's book. ``window_prefix`` (e.g. ``"btc-updown-5m-"``) restricts
    the rows to one market family so another selection is never scored on
    this family's book.
    """
    if window_prefix is not None:
        ticks = [t for t in ticks if str(t.get("window_slug") or "").startswith(window_prefix)]
    in_phase = [t for t in ticks if _in_phase(t)]
    if not in_phase:
        return None
    overrounds: list[float] = []
    captures: list[float] = []
    depths: list[float] = []
    for t in in_phase:
        ua, da = _float_or_none(t.get("up_best_ask")), _float_or_none(t.get("down_best_ask"))
        ub, db = _float_or_none(t.get("up_best_bid")), _float_or_none(t.get("down_best_bid"))
        if _crossed(ub, ua) or _crossed(db, da):
            continue
        if ua is not None and da is not None:
            overrounds.append(ua + da - 1.0)
            uas, das = _float_or_none(t.get("up_ask_size")), _float_or_none(t.get("down_ask_size"))
            legs = [
                sz * px for sz, px in ((uas, ua), (das, da)) if sz is not None and sz >= 0
            ]
            if legs:
                depths.append(min(legs))
        if ub is not None and db is not None:
            captures.append(1.0 - (ub + db))
    newest = in_phase[0]
    sigma = _float_or_none(newest.get("sigma_per_second"))
    source = _vol_source(newest.get("feed_source"))
    if sigma is not None and (sigma <= SIGMA_FLOOR or source == "floor"):
        sigma = None
    reason = str(newest.get("reason") or "")
    return BookState(
        overround=statistics.fmean(overrounds) if overrounds else None,
        maker_capture=statistics.fmean(captures) if captures else None,
        executable_depth_usd=statistics.fmean(depths) if depths else None,
        ticks_used=len(in_phase),
        newest_age_seconds=age_seconds(newest.get("created_at"), now),
        sigma_per_second=sigma,
        vol_source=source,
        feed_degraded="feed degraded" in reason,
        source="paper_ticks",
    )


# --- Book (direct CLOB read, for when the loop is not running) -----------------


def _best_level(levels: Any) -> tuple[float | None, float | None]:
    """(price, size) of the best level — the LAST element of a CLOB array."""
    if not isinstance(levels, list) or not levels:
        return None, None
    best = levels[-1]
    if not isinstance(best, dict):
        return None, None
    try:
        return float(best["price"]), float(best["size"])
    except (KeyError, TypeError, ValueError):
        return None, None


async def fetch_clob_top(
    client: httpx.AsyncClient, token_id: str
) -> tuple[float | None, float | None, float | None, float | None]:
    """``(best_bid, best_ask, bid_size, ask_size)`` from the public CLOB ``/book``.

    Mirrors the loop's ``paper.py:_fetch_clob_book`` (levels are listed
    worst-to-best, so the best is the LAST element). All ``None`` on any
    error or an empty token id.
    """
    if not token_id:
        return None, None, None, None
    try:
        resp = await client.get(
            f"{_config.POLYMARKET_CLOB_API}/book", params={"token_id": token_id}
        )
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("regime.clob_book_unavailable", token_id=token_id[:16], error=str(exc))
        return None, None, None, None
    if not isinstance(data, dict):
        return None, None, None, None
    bid, bid_size = _best_level(data.get("bids"))
    ask, ask_size = _best_level(data.get("asks"))
    return bid, ask, bid_size, ask_size


def book_from_clob(
    up: tuple[float | None, float | None, float | None, float | None],
    down: tuple[float | None, float | None, float | None, float | None],
    remaining_seconds: float,
    window_seconds: int = LOOP_WINDOW_SECONDS,
) -> BookState | None:
    """A single direct book read as a :class:`BookState` (``source="clob_direct"``).

    ``None`` when the window is outside its quotable phase (the same fraction
    rule :func:`book_from_ticks` applies, scaled to ``window_seconds``), when
    either side is crossed, or when neither side has a usable two-sided
    quote. No loop sigma exists on this path.
    """
    if not in_quotable_phase(remaining_seconds, window_seconds):
        return None
    ub, ua, _ubs, uas = up
    db_, da, _dbs, das = down
    if _crossed(ub, ua) or _crossed(db_, da):
        return None
    overround = ua + da - 1.0 if ua is not None and da is not None else None
    capture = 1.0 - (ub + db_) if ub is not None and db_ is not None else None
    depth = None
    if ua is not None and da is not None:
        legs = [sz * px for sz, px in ((uas, ua), (das, da)) if sz is not None and sz >= 0]
        depth = min(legs) if legs else None
    if overround is None and capture is None:
        return None
    return BookState(
        overround=overround,
        maker_capture=capture,
        executable_depth_usd=depth,
        ticks_used=1,
        newest_age_seconds=0,
        sigma_per_second=None,
        vol_source=None,
        feed_degraded=False,
        source="clob_direct",
    )


async def recent_ticks(limit: int = 12) -> list[dict[str, Any]]:
    """The loop's most recent ``limit`` tick rows, newest first (read-only)."""
    async with _connect() as db:
        async with db.execute(
            "SELECT created_at, window_slug, reason, feed_source, remaining_seconds, "
            "sigma_per_second, up_best_bid, up_best_ask, up_bid_size, up_ask_size, "
            "down_best_bid, down_best_ask, down_bid_size, down_ask_size "
            "FROM paper_ticks ORDER BY id DESC LIMIT ?",
            (limit,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]
