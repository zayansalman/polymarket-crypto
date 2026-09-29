"""The regime monitor's scan loop: fetch → features → bands → fits → journal.

Always-on and independent of the BTC loop's Start/Stop, exactly like the
daily altcoin scanner (started from the dashboard's lifespan in
``polymarket_exec/ops/dashboard/app.py``): the operator wants to read the
regime *before* deciding what to start. Follows the operator's selected
asset and timeframe (:mod:`polymarket_bot.market_selection`) so the
overview is for the market they are looking at.

Every scan journals a snapshot even when inputs are missing — the snapshot
carries an enumerated ``quality`` list naming each gap and a ``grade``
roll-up, so a partial read is visible as partial rather than silently wrong
(AGENTS.md: no silent failures). Nothing here places orders or is read by
any trading path.

Book source: the loop's own in-phase ticks when the loop is running on the
selected market (a ~60s average), otherwise one direct CLOB ``/book`` read
for the selected window — the "before pressing Start" case the overview
exists for. Fetch cadence: the last-hour 1m bars, the venue's current
window and the book are read every scan; the 24h (5m) and 7-day (1h) bars
and completed venue windows change slowly and are cached.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import httpx

import config as _config
from logging_setup import get_logger
from polymarket_bot import market_selection
from polymarket_bot import runtime_knobs as _knobs
from polymarket_bot.daily.market import daily_slug
from polymarket_bot.regime import classify, features as _features, ledger, sources
from polymarket_bot.regime.types import (
    Bar,
    BookState,
    QualityFlag,
    RegimeSnapshot,
    VenueMarket,
)

log = get_logger("regime_monitor")

BARS_1M_LIMIT = 60      # the "last hour" window
BARS_5M_LIMIT = 288     # the "last 24h" baseline
BARS_1H_LIMIT = 672     # 28 days of hourly bars for same-hour, same-day-class baselines
BOOK_TICKS = 12         # ~60s of the loop's 5s ticks
VENUE_COMPLETED_WINDOWS = 6
SLOW_TTL_SECONDS = 300.0
_MIN_1M_BARS = 30       # below this the 1h estimates are too thin to band on
_MIN_5M_BARS = 144      # 12h — below this the 24h baseline is not a baseline
_MIN_1H_BARS = 336      # 14 days — below this a weekend same-hour median has <3 points


class _SlowCache:
    """Per-(symbol, key) cache of slowly-changing fetches with a TTL."""

    def __init__(self, ttl_seconds: float = SLOW_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._items: dict[tuple[str, str], tuple[float, object]] = {}

    def get(self, symbol: str, key: str, now: float) -> object | None:
        hit = self._items.get((symbol, key))
        if hit is None or now - hit[0] > self._ttl:
            return None
        return hit[1]

    def put(self, symbol: str, key: str, now: float, value: object) -> None:
        self._items[(symbol, key)] = (now, value)


_slow = _SlowCache()
# Completed windows never change (their volume is final) — cache by slug.
_venue_completed: dict[str, VenueMarket] = {}
_VENUE_CACHE_MAX = 64

# Run identity: one id per monitor process boot, a monotonic scan counter.
_run_id: str = uuid.uuid4().hex[:12]
_scan_seq: int = 0
_thresholds_recorded: set[str] = set()


def build_snapshot(
    *,
    asset: str,
    symbol: str,
    timeframe: str,
    created_at: str,
    created_ts: int,
    bars_1m: Sequence[Bar],
    bars_5m: Sequence[Bar],
    bars_1h: Sequence[Bar],
    book: BookState | None,
    venue_current: VenueMarket | None,
    venue_completed: Sequence[VenueMarket],
    edge_gate: float,
    daily_assets: Sequence[str],
    window_slug: str | None = None,
    run_id: str = "",
    scan_seq: int = 0,
    thresholds: classify.RegimeThresholds = classify.DEFAULT_THRESHOLDS,
    sources_used: dict[str, str] | None = None,
    extra_quality: Sequence[QualityFlag] = (),
) -> RegimeSnapshot:
    """Pure assembly: inputs → :class:`RegimeSnapshot`. No I/O, no clock."""
    quality: list[QualityFlag] = list(extra_quality)
    if not bars_1m and not bars_5m:
        quality.append(QualityFlag("bars_unavailable", "no 1m or 5m bars"))
    else:
        if len(bars_1m) < _MIN_1M_BARS:
            quality.append(QualityFlag("bars_1m_short", f"{len(bars_1m)} of {BARS_1M_LIMIT}"))
        if len(bars_5m) < _MIN_5M_BARS:
            quality.append(QualityFlag("bars_5m_short", f"{len(bars_5m)} of {BARS_5M_LIMIT}"))
    if len(bars_1h) < _MIN_1H_BARS:
        quality.append(
            QualityFlag("bars_1h_short", f"{len(bars_1h)} of {BARS_1H_LIMIT}: seasonal baselines thin")
        )
    if book is None:
        if not any(q.code.startswith("book_") for q in quality):
            quality.append(QualityFlag("book_absent", "no in-phase book read"))
    else:
        if book.newest_age_seconds is None or book.newest_age_seconds > thresholds.book_stale_seconds:
            quality.append(QualityFlag("book_stale", f"{book.newest_age_seconds}s"))
        if book.feed_degraded:
            quality.append(QualityFlag("loop_feed_degraded", "loop tick journaled with a degraded feed"))
        if book.source == "paper_ticks" and book.sigma_per_second is None:
            quality.append(QualityFlag("loop_sigma_unusable", book.vol_source or "absent"))
    if venue_current is None and not any(q.code == "venue_slug_unknown" for q in quality):
        quality.append(QualityFlag("venue_market_absent", f"{asset}/{timeframe}"))

    at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    feats = _features.compute_features(
        bars_1m=bars_1m, bars_5m=bars_5m, bars_1h=bars_1h, at=at, book=book,
        venue_current=venue_current, venue_completed=venue_completed,
    )
    bands = classify.bands_for(feats, book, created_at, thresholds)
    fits = classify.strategy_fits(
        feats, bands, edge_gate=edge_gate, asset=asset, daily_assets=daily_assets, t=thresholds
    )
    used = dict(sources_used or {})
    used.update({f"estimator.{axis}": est for axis, est in classify.BAND_ESTIMATORS.items()})
    if book is not None:
        used["book"] = book.source
        used["vol_1s"] = book.vol_source or "absent"
    return RegimeSnapshot(
        created_at=created_at,
        created_ts=created_ts,
        asset=asset,
        symbol=symbol,
        timeframe=timeframe,
        window_slug=window_slug,
        run_id=run_id,
        scan_seq=scan_seq,
        features=feats,
        bands=bands,
        headline=classify.headline(feats, bands, degraded=bool(quality)),
        fits=fits,
        recommendation=classify.recommendation(fits),
        quality=tuple(quality),
        grade=classify.grade_for(quality),
        thresholds_version=thresholds.version,
        sources=used,
    )


async def _slow_bars(
    client: httpx.AsyncClient, symbol: str, interval: str, limit: int, now: float
) -> list[Bar]:
    cached = _slow.get(symbol, interval, now)
    if isinstance(cached, list):
        return cached
    bars = await sources.fetch_bars(client, symbol, interval, limit)
    if bars:  # never cache a failure — retry next scan
        _slow.put(symbol, interval, now, bars)
    return bars


async def _venue_blocks(
    client: httpx.AsyncClient, asset: str, timeframe: str, now_ts: int
) -> tuple[VenueMarket | None, list[VenueMarket], str | None, QualityFlag | None]:
    """``(current window, completed windows, current window slug, quality flag)``."""
    if timeframe == "1d":
        slug = daily_slug(asset, datetime.fromtimestamp(now_ts, tz=UTC))
        if slug is None:
            return None, [], None, QualityFlag("venue_slug_unknown", f"{asset}/{timeframe}")
        return await sources.fetch_venue_market(client, slug), [], None, None
    current_start, completed_starts = sources.window_starts(
        timeframe, now_ts, VENUE_COMPLETED_WINDOWS
    )
    length = sources.WINDOW_SECONDS.get(timeframe)
    if current_start is None or length is None:
        return None, [], None, QualityFlag("venue_slug_unknown", f"{asset}/{timeframe}")
    current_slug = sources.window_slug(asset, timeframe, current_start)

    async def completed(start: int) -> VenueMarket | None:
        slug = sources.window_slug(asset, timeframe, start)
        if slug is None:
            return None
        hit = _venue_completed.get(slug)
        if hit is not None:
            return hit
        hit = await sources.fetch_venue_market(client, slug)
        # Cache only once the window has been closed for a full window length:
        # a just-closed window's Gamma volume may still be settling, and a
        # cached early read would freeze a partial number for the process's life.
        if hit is not None and now_ts - (start + length) >= length:
            if len(_venue_completed) >= _VENUE_CACHE_MAX:
                _venue_completed.pop(next(iter(_venue_completed)))
            _venue_completed[slug] = hit
        return hit

    results = await asyncio.gather(
        sources.fetch_venue_market(client, current_slug or ""),
        *(completed(st) for st in completed_starts),
    )
    return results[0], [h for h in results[1:] if h is not None], current_slug, None


async def _book(
    client: httpx.AsyncClient,
    selection: market_selection.MarketSelection,
    venue_current: VenueMarket | None,
    book_ts: int,
    window_start: int | None,
) -> tuple[BookState | None, QualityFlag | None]:
    """The selected market's phase-conditioned book, loop ticks first, else direct.

    Loop ticks are only ever for the loop's own market
    (``market_selection.LOOP_SUPPORTED``); any other selection reads the
    CLOB directly so it is never scored on the BTC 5m book. ``book_ts`` is the
    clock at the book stage (not scan start — the fetches before it can take
    seconds), and the phase is measured against the venue window's own end.
    """
    now = datetime.fromtimestamp(book_ts, tz=UTC)
    if selection.loop_supported:
        ticks = await sources.recent_ticks(BOOK_TICKS)
        book = sources.book_from_ticks(
            ticks, now, window_prefix=f"{selection.asset}-updown-{selection.timeframe}-"
        )
        if book is not None and book.newest_age_seconds is not None and (
            book.newest_age_seconds <= classify.DEFAULT_THRESHOLDS.book_stale_seconds
        ):
            return book, None
    length = sources.WINDOW_SECONDS.get(selection.timeframe)
    if venue_current is None or length is None or window_start is None or not venue_current.up_token:
        return None, QualityFlag("book_absent", "no loop ticks and no venue tokens to read the book")
    remaining = window_start + length - book_ts
    if not sources.in_quotable_phase(remaining, length):
        return None, QualityFlag("book_out_of_phase", f"{remaining}s remaining in window")
    up, down = await asyncio.gather(
        sources.fetch_clob_top(client, venue_current.up_token),
        sources.fetch_clob_top(client, venue_current.down_token),
    )
    book = sources.book_from_clob(up, down, remaining, length)
    if book is None:
        return None, QualityFlag("book_absent", "direct CLOB read returned no two-sided quote")
    return book, None


async def scan_once(
    client: httpx.AsyncClient, now_ts: int | None = None
) -> RegimeSnapshot | None:
    """One scan for the operator's selected market; journals and returns it.

    Returns ``None`` (and logs) only when the selected asset has no Binance
    spot symbol — there is nothing to measure.
    """
    global _scan_seq
    selection = await market_selection.get_selection()
    symbol = sources.SPOT_SYMBOL.get(selection.asset)
    if symbol is None:
        log.warning("regime.no_symbol", asset=selection.asset)
        return None
    # Consume the sequence number BEFORE any fetch that can fail, so an
    # aborted scan leaves a visible gap in scan_seq rather than no trace.
    _scan_seq += 1
    scan_seq = _scan_seq
    clock_injected = now_ts is not None
    now_ts = int(time.time()) if now_ts is None else now_ts
    now = float(now_ts)
    created_at = datetime.fromtimestamp(now_ts, tz=UTC).isoformat(timespec="seconds")

    thresholds = classify.DEFAULT_THRESHOLDS
    if thresholds.version not in _thresholds_recorded:
        await ledger.record_thresholds(thresholds.version, thresholds.as_dict(), created_at)
        _thresholds_recorded.add(thresholds.version)

    bars_1m, bars_5m, bars_1h = await asyncio.gather(
        sources.fetch_bars(client, symbol, "1m", BARS_1M_LIMIT),
        _slow_bars(client, symbol, "5m", BARS_5M_LIMIT, now),
        _slow_bars(client, symbol, "1h", BARS_1H_LIMIT, now),
    )
    venue_current, venue_completed, window_slug, venue_flag = await _venue_blocks(
        client, selection.asset, selection.timeframe, now_ts
    )
    window_start, _ = sources.window_starts(selection.timeframe, now_ts, 0)
    book_ts = now_ts if clock_injected else int(time.time())
    book, book_flag = await _book(client, selection, venue_current, book_ts, window_start)

    extra = [q for q in (venue_flag, book_flag) if q is not None]
    snapshot = build_snapshot(
        asset=selection.asset,
        symbol=symbol,
        timeframe=selection.timeframe,
        created_at=created_at,
        created_ts=now_ts,
        bars_1m=bars_1m,
        bars_5m=bars_5m,
        bars_1h=bars_1h,
        book=book,
        venue_current=venue_current,
        venue_completed=venue_completed,
        edge_gate=_config.PAPER_ENTRY_EDGE_MIN,
        daily_assets=_config.DAILY_ASSETS,
        window_slug=window_slug,
        run_id=_run_id,
        scan_seq=scan_seq,
        thresholds=thresholds,
        sources_used={
            "bars": "binance_spot_klines",
            "venue": "gamma" if venue_current or venue_completed else "absent",
            "book": "absent",
        },
        extra_quality=extra,
    )
    await ledger.record_snapshot(snapshot)
    log.info(
        "regime.scan",
        asset=snapshot.asset,
        grade=snapshot.grade,
        headline=snapshot.headline,
        recommendation=snapshot.recommendation,
        quality=[q.code for q in snapshot.quality],
    )
    return snapshot


async def run_forever(stop_event: asyncio.Event | None = None) -> None:
    """Run the scan loop until ``stop_event`` is set (or forever if ``None``).

    The ``regime_monitor_enabled`` knob is re-read every cycle: switching it
    off from the SETTINGS card pauses the polling without a restart. Every
    await that can raise (the scan AND the knob reads, which hit SQLite) sits
    inside a guard, so a transient DB error costs one cycle, never the task.
    """
    default_interval = _knobs.KNOBS["regime_scan_interval_seconds"].default
    async with httpx.AsyncClient(timeout=15.0) as client:
        while stop_event is None or not stop_event.is_set():
            try:
                if await _knobs.get("regime_monitor_enabled"):
                    await scan_once(client)
            except Exception:  # noqa: BLE001 — a failed scan must never kill the monitor
                log.exception("regime.scan_failed")
            try:
                interval = await _knobs.get("regime_scan_interval_seconds")
            except Exception:  # noqa: BLE001
                log.exception("regime.interval_unreadable")
                interval = default_interval
            await asyncio.sleep(interval)
