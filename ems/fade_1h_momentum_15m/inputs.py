"""Live inputs for Fade 1h Momentum on 15m: one read of everything the maths needs, per coin.

``gather(hub, client, now)`` returns ``{asset: Inputs | Problem}`` for btc, eth, sol and xrp.
It never raises (a cancel still propagates): anything missing, stale or inconsistent becomes a
``Problem`` with a stable ``code`` and a plain-English ``message`` for the card. The runner
records either one every pass.

Where each input comes from
---------------------------
- Demand. Every pass asks the market-data hub (``ems/marketdata/hub.py``) for the
  coin's 15m market (hot: its books also get the fresh REST poll) and its 1h market, as owner
  ``OWNER``. Asking again is free. The runner releases ``OWNER`` when it stops.
- The 15m window: ``hub.market(asset, "15m")``, with its start and end, Up/Down tokens and
  condition id. Bounds guard: the hub switches windows up to ~2 s after :00/:15/:30/:45, so a
  window with ``now`` outside ``[start, end)`` is a Problem, never used. A missing condition id
  is looked up once on Gamma (``/markets?slug=``); fills and settlement need it.
- The 15m books, for both tokens. A book is used only while a live connection serves it
  (``hub.top(token).live``). Best bid/ask and their sizes come from ``hub.book_top`` (the fresher
  of the stream and the REST poll), depth from ``hub.levels`` (the stream book, best first,
  ``DEPTH_LEVELS`` a side). Both sides must be there; the tick size is the book's own (the
  coarser of the two tokens'), else the venue's standard 0.01, which is always a valid step.
- The 1h market: ``hub.quote(asset, "1h")``. Hour-match guard: its window must start at this
  15m window's hour (``window_start - window_start % 3600``; ET offsets are whole hours). Its
  Up price is the Up book's mid while both tops are live. No age limit: a quiet 1h book is
  still a live book (a max age would read it as missing).
- Prices now: Chainlink TWAP-60s, Chainlink and Binance from the hub's RTDS stream. Each must
  have been observed within ``PRICE_MAX_AGE_S`` (5 s; Chainlink prints arrive ~2 s late).
  The price now is the live Chainlink price (``chainlink``; Binance to compare). ``twap60`` is
  the current TWAP-60s print: the stream the 15m market settles on, and a 60 s average that
  runs about 30 s behind the price, so it is recorded for information and never used as the
  price now. Only the settlement's two ends come from it: the start reference below, and the
  closing minute's average.
- How a 15m window settles (verified on the live markets 2026-09-22): Up iff the TWAP-60s
  print at the window's close (the average of the Chainlink price over the last 60 s) is >=
  the TWAP-60s print at the open. Gamma's ``priceToBeat`` for the window (its event's
  ``eventMetadata``) equals that opening print to every digit. It is not an average over the
  whole window.
- The start reference: that opening print, the window's ``priceToBeat``. Taken, in order,
  from (1) what this process already holds, (2) ``fade_windows`` (an earlier pass or process
  stored it), (3) the hub's in-memory prints (~15 minutes): the print whose observation second
  is the open second, which is the priceToBeat itself, (4) Gamma's ``priceToBeat``, looked up
  only when that print is not held (the open second was skipped, the feed stalled, or the app
  restarted after the open), and again every ``GAMMA_RETRY_S`` while Gamma has none (checked
  live 2026-09-22: Gamma publishes it only several minutes into the window, when the window
  before it is finalised), (5) as the named fallback while Gamma has none, a stand-in print:
  the newest up to ``START_MATCH_S`` seconds before the open second, else the first up to
  ``START_MATCH_S`` after it. The source text says which, and how many seconds after the open
  it was read. Only a final value (the opening print or Gamma's) is persisted with
  ``ledger.upsert_window``, where the first stored value wins; a stand-in is used but not
  stored, so Gamma's number replaces it once published. After a restart only ~68 s of prints
  are backfilled, so a window whose open is gone waits for Gamma (a Problem until then).
- The closing average known so far (``close_avg``), in the last 60 s of the window only: the
  step-path average of the Chainlink prints from ``window_end - 60`` to the newest one (the
  TWAP-60s print at the close averages that minute), held from the print at or before
  ``window_end - 60``. None earlier in the window.
- The TWAP-60s stream's realised average over the window so far (``window_avg``; recorded for
  information only: it is not what settles and nothing decides on it), kept from the stream's
  prints across passes
  in ``InputMemory`` (the hub only holds ~15 minutes). Exactly: v(s) for each whole second s
  from the window start ``s0`` to ``s_last``, the observation second of the newest print held
  for this window, is the print observed at s if there is one, else v(s - 1) (the stream's
  value is held until the next print). v(s0) is the print at s0, or the start reference when
  there is none. ``value`` = mean of v(s) over those N = s_last - s0 + 1 seconds, i.e. the
  time-weighted average of the stream's step path over [s0, s_last + 1); ``log_value`` = mean
  of ln v(s). A hole in the prints (a late start or a long feed drop; RTDS reconnects after
  30 s of silence and backfills ~68 s) is recorded as ``longest_gap_s`` and never blocks a
  decision.
- Binance history, over REST at ``config.BINANCE_API_BASE`` ``/api/v3/klines``. Each request
  asks for completed candles only (``startTime`` + ``limit``) and any row that is still forming
  (closes less than ``KLINE_SETTLE_S`` ago, which allows for clock skew) is dropped as well:
  - ``minute_returns``: the 60 close-to-close log returns of the 61 one-minute candles ending at
    the newest completed minute, newest first. sigma^2 per hour is their sum of squares.
  - ``r15``: ln(close / open) of the 12 completed 15m candles before the window, newest first;
    ``r15[0]`` is the candle that ends at the window start (Binance 15m candles align with the
    windows).
  - ``hour_open``: the open of the Binance 1h candle starting at the window's hour. The 1h
    market settles on that candle (close >= open), so this is its settlement open.
  The 15m returns and the hour open are cached for their window and hour; the minute returns
  for their minute.

Problem codes
-------------
no_hub, market_unavailable, window_unknown, window_rolling, condition_id_unknown,
book_not_live, book_one_sided, book_crossed, hour_market_unknown, hour_mismatch,
hour_book_not_live, hour_book_one_sided, price_missing, price_invalid, price_stale,
start_ref_pending, start_ref_missing, average_incomplete (the closing minute's prints only),
klines_failed, klines_incomplete, hour_open_missing, internal_error. A coin with several
problems reports the first as its code and the rest in ``also``. Trouble that does not stop the
coin comes in two lists, both recorded and shown on the card: ``warnings`` for a failure (a
database read or write that failed: an unsaved window row means that window cannot be settled
or learned from until a later pass saves it), which the runner also reports as an error of the
pass; ``notes`` for plain facts (the books carried no tick size, so the standard one is used).

Stdlib only; nothing here decides, sizes or places anything.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, NamedTuple

import httpx

from ems import config as _config
from ems.logging_setup import get_logger
from ems.fade_1h_momentum_15m import ledger as _ledger

log = get_logger("fade_1h.inputs")

ASSETS: tuple[str, ...] = ("btc", "eth", "sol", "xrp")
OWNER = "fade 1h (15m)"  # shown on the FEEDS card; unique, because release(OWNER) drops it all
SYMBOLS: Mapping[str, str] = MappingProxyType(
    {"btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT", "xrp": "XRPUSDT"}
)

TWAP60, CHAINLINK, BINANCE = "chainlink_twap60", "chainlink", "binance"
PRICE_SOURCES: tuple[str, ...] = (TWAP60, CHAINLINK, BINANCE)
SOURCE_NAMES: Mapping[str, str] = MappingProxyType(
    {TWAP60: "Chainlink TWAP-60s", CHAINLINK: "Chainlink", BINANCE: "Binance"}
)

PRICE_MAX_AGE_S = 5.0  # a price older than this (by observation time) is stale
START_MATCH_S = 5  # how far from the open second a stand-in start print may be
MAX_AVG_GAP_S = 30  # longest hole in the closing minute's Chainlink prints its average takes
KLINE_SETTLE_S = 2.0  # a candle counts as complete this long after it closes (clock skew)
MINUTE_RETURNS = 60
R15_CANDLES = 12
DEPTH_LEVELS = 50  # levels read per book side
RECORD_DEPTH_LEVELS = 10  # levels per side kept in a decision record
WAIT_READY_S = 3.0  # a cold market gets this long to deliver its books
DEFAULT_TICK = 0.01
GAMMA_API = "https://gamma-api.polymarket.com"
GAMMA_HEADERS: Mapping[str, str] = MappingProxyType({
    # Gamma rejects some non-browser clients.
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
    "Accept": "application/json",
})
GAMMA_RETRY_S = 30.0
HTTP_TIMEOUT_S = 10.0
CLOSE_AVG_S = 60  # the TWAP-60s print at the close averages the window's last 60 s
GAMMA_PTB = "Gamma priceToBeat"  # start reference source text for Gamma's number
QUARTER_S = 900
HOUR_S = 3600
_EPS = 1e-9

Level = tuple[float, float]  # (price, shares)


# --------------------------------------------------------------------------- types


@dataclass(frozen=True)
class PriceNow:
    """The newest print of one reference source."""

    source: str  # chainlink_twap60 | chainlink | binance
    value: float
    obs_s: float  # observation time, epoch seconds
    age_s: float  # now - obs_s, at least 0


@dataclass(frozen=True)
class Book:
    """One 15m token's book at the pass: both sides present, best levels first."""

    side: str  # "Up" | "Down"
    token_id: str
    best_bid: float
    best_ask: float
    bid_size: float | None
    ask_size: float | None
    bids: tuple[Level, ...]  # the stream book, best first, up to DEPTH_LEVELS
    asks: tuple[Level, ...]
    source: str  # where the top came from: "stream" | "rest"
    age_s: float | None  # since that top arrived, on our clock

    @property
    def mid(self) -> float:
        return 0.5 * (self.best_bid + self.best_ask)

    @property
    def spread(self) -> float:
        return self.best_ask - self.best_bid

    def depth_ahead(self, price: float) -> float:
        """Shares bid at ``price`` or better: the depth a new buy order there joins behind."""
        return sum(size for px, size in self.bids if px >= price - _EPS)

    def ask_depth_ahead(self, price: float) -> float:
        """Shares offered at ``price`` or better: the depth a new sell order there joins
        behind."""
        return sum(size for px, size in self.asks if px <= price + _EPS)


@dataclass(frozen=True)
class WindowAverage:
    """The TWAP-60s stream's realised average over the window so far (module docstring)."""

    value: float  # mean of v(s), s = start .. through_s
    log_value: float  # mean of ln v(s)
    through_s: int  # the last second included: the newest print's observation second
    seconds: int  # N = through_s - window start + 1
    printed: int  # seconds that had a print of their own
    longest_gap_s: int  # longest run of seconds that held the previous value


@dataclass(frozen=True)
class Inputs:
    """Everything one coin's decision reads, from one pass. Built only when nothing is missing."""

    asset: str
    ts: float  # the pass time, epoch seconds
    # The 15m window being traded.
    window_slug: str
    window_start: float
    window_end: float
    condition_id: str
    up_token: str
    down_token: str
    tick_size: float
    up_book: Book
    down_book: Book
    # The 1h market this window sits in.
    hour_start: float
    hour_slug: str
    hour_condition_id: str | None
    hour_up_bid: float
    hour_up_ask: float
    hour_book_age_s: float | None
    hour_open: float  # Binance 1h candle open at hour_start: the 1h market's settlement open
    # Reference prices now.
    twap60: PriceNow
    chainlink: PriceNow
    binance: PriceNow
    # The window's start reference (its priceToBeat) and, for information only, the settlement
    # stream's average over the window so far.
    start_ref: float
    start_ref_source: str
    window_avg: WindowAverage
    # Binance history.
    minute_returns: tuple[float, ...]  # 60 close-to-close 1m log returns, newest first
    minute_returns_end: float  # when the newest of those minutes closed, epoch seconds
    r15: tuple[float, ...]  # 12 completed 15m candles, ln(close/open), newest first
    notes: tuple[str, ...] = ()
    # The part of the closing 60 s average already known (Chainlink prints from
    # window_end - 60 on); None before the window's last 60 s.
    close_avg: WindowAverage | None = None
    # Failures that did not stop this coin (a database read or write); module docstring.
    warnings: tuple[str, ...] = ()

    # Definitions from the research doc's notation (tasks/2026-09-21-fade-1h-momentum-on-15m.md).

    @property
    def market_up(self) -> float:
        """m: the 15m market's Up price, the Up book's mid."""
        return self.up_book.mid

    @property
    def hour_up(self) -> float:
        """m_H: the 1h market's Up price, the Up book's mid."""
        return 0.5 * (self.hour_up_bid + self.hour_up_ask)

    @property
    def quarter(self) -> int:
        """k = 1..4: which quarter of the hour this window is."""
        return int((self.window_start - self.hour_start) // QUARTER_S) + 1

    @property
    def t(self) -> float:
        """Hour-time of the decision, 0..1."""
        return (self.ts - self.hour_start) / HOUR_S

    @property
    def tau(self) -> float:
        """Fraction of the window already gone, 0..1."""
        return (self.ts - self.window_start) / (self.window_end - self.window_start)

    @property
    def h(self) -> float:
        """Hours left in the window."""
        return (self.window_end - self.ts) / HOUR_S

    @property
    def x(self) -> float:
        """The hour's log move so far on Binance, the 1h market's settlement basis."""
        return math.log(self.binance.value / self.hour_open)

    @property
    def d(self) -> float:
        """The price now (the live Chainlink price), in log terms, measured from the start
        reference. Not the TWAP-60s print now, which lags the price by about 30 s."""
        return math.log(self.chainlink.value / self.start_ref)

    @property
    def d_binance(self) -> float:
        """The same on Binance's price: what the model reads as the price now when the operator
        picks Binance (the ``fade1h_spot_feed`` Setting); recorded to compare either way."""
        return math.log(self.binance.value / self.start_ref)

    @property
    def abar(self) -> float:
        """The realised average of the log settlement stream so far, minus ln(start_ref)
        (information only)."""
        return self.window_avg.log_value - math.log(self.start_ref)

    @property
    def close_abar(self) -> float | None:
        """The known part of the closing average, in log terms from the start reference: the
        mean of ln(Chainlink) over the closing minute so far, minus ln(start_ref). None before
        the window's last 60 s."""
        if self.close_avg is None:
            return None
        return self.close_avg.log_value - math.log(self.start_ref)

    @property
    def sigma(self) -> float:
        """Volatility per sqrt(hour): root of the sum of the 60 squared 1m log returns."""
        return math.sqrt(sum(r * r for r in self.minute_returns))

    @property
    def mu_l(self) -> float:
        """The trailing one-hour log return (L = 1): the sum of the 60 minute returns."""
        return sum(self.minute_returns)

    def as_record(self, depth_levels: int = RECORD_DEPTH_LEVELS) -> dict[str, Any]:
        """A JSON-safe dict for ``ledger.record_decision(inputs=...)``: raw inputs plus the
        derived values above; book depth trimmed to ``depth_levels`` a side."""
        def book(b: Book) -> dict[str, Any]:
            return {
                "token_id": b.token_id, "best_bid": b.best_bid, "best_ask": b.best_ask,
                "bid_size": b.bid_size, "ask_size": b.ask_size, "source": b.source,
                "age_s": b.age_s, "bids": [list(lv) for lv in b.bids[:depth_levels]],
                "asks": [list(lv) for lv in b.asks[:depth_levels]],
            }

        def price(p: PriceNow) -> dict[str, Any]:
            return {"value": p.value, "obs_s": p.obs_s, "age_s": p.age_s}

        derived: dict[str, Any] = {}
        for name in ("market_up", "hour_up", "quarter", "t", "tau", "h", "x", "d", "d_binance",
                     "abar", "close_abar", "sigma", "mu_l"):
            try:
                derived[name] = getattr(self, name)
            except (ValueError, ZeroDivisionError, OverflowError):
                derived[name] = None
        avg, close = self.window_avg, self.close_avg
        return {
            "status": "ok",
            "asset": self.asset, "ts": self.ts,
            "window_slug": self.window_slug, "window_start": self.window_start,
            "window_end": self.window_end, "condition_id": self.condition_id,
            "up_token": self.up_token, "down_token": self.down_token,
            "tick_size": self.tick_size,
            "up_book": book(self.up_book), "down_book": book(self.down_book),
            "hour_start": self.hour_start, "hour_slug": self.hour_slug,
            "hour_condition_id": self.hour_condition_id,
            "hour_up_bid": self.hour_up_bid, "hour_up_ask": self.hour_up_ask,
            "hour_book_age_s": self.hour_book_age_s, "hour_open": self.hour_open,
            "twap60": price(self.twap60), "chainlink": price(self.chainlink),
            "binance": price(self.binance),
            "start_ref": self.start_ref, "start_ref_source": self.start_ref_source,
            "window_avg": {
                "value": avg.value, "log_value": avg.log_value, "through_s": avg.through_s,
                "seconds": avg.seconds, "printed": avg.printed,
                "longest_gap_s": avg.longest_gap_s,
            },
            "close_avg": None if close is None else {
                "value": close.value, "log_value": close.log_value,
                "through_s": close.through_s, "seconds": close.seconds,
                "printed": close.printed, "longest_gap_s": close.longest_gap_s,
            },
            "minute_returns": list(self.minute_returns),
            "minute_returns_end": self.minute_returns_end,
            "r15": list(self.r15),
            "derived": derived,
            "notes": list(self.notes),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class Problem:
    """Why a coin has no inputs this pass. ``message`` is for the card."""

    asset: str
    code: str  # stable name, see the module docstring
    message: str
    ts: float
    window_slug: str | None = None
    also: tuple[tuple[str, str], ...] = ()  # (code, message) of further problems this pass
    known: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    notes: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()  # failures that did not decide the problem (a database write)

    @property
    def codes(self) -> tuple[str, ...]:
        return (self.code, *(code for code, _ in self.also))

    def as_record(self) -> dict[str, Any]:
        """A JSON-safe dict for ``ledger.record_decision(inputs=...)``."""
        return {
            "status": "problem", "asset": self.asset, "ts": self.ts,
            "window_slug": self.window_slug, "code": self.code, "message": self.message,
            "also": [{"code": c, "message": m} for c, m in self.also],
            "known": dict(self.known), "notes": list(self.notes),
            "warnings": list(self.warnings),
        }


class InputMemory:
    """What ``gather`` keeps between passes. One per runner; ``gather`` uses a module default.

    - ``prints``: per coin, the current window's TWAP-60s prints by observation second.
    - ``start_refs``: the start reference per window slug, once known.
    - ``saved``: the facts last written to ``fade_windows`` per slug (a write only when they
      change); ``db_checked``: slugs whose stored row has been read.
    - ``cids`` / ``cid_retry_at``: condition ids looked up on Gamma.
    - ``ptb_retry_at``: when Gamma is next asked for a window's priceToBeat after it had none.
    - ``minute`` / ``r15`` / ``hour_open``: Binance results per coin, keyed by the minute,
      window or hour they belong to.
    """

    def __init__(self) -> None:
        self.prints: dict[str, tuple[int, dict[int, float]]] = {}
        self.start_refs: dict[str, tuple[float, str]] = {}
        self.saved: dict[str, tuple[Any, ...]] = {}
        self.db_checked: set[str] = set()
        self.cids: dict[str, str] = {}
        self.cid_retry_at: dict[str, float] = {}
        self.ptb_retry_at: dict[str, float] = {}
        self.minute: dict[str, tuple[int, tuple[float, ...]]] = {}
        self.r15: dict[str, tuple[int, tuple[float, ...]]] = {}
        self.hour_open: dict[str, tuple[int, float]] = {}
        self._ends: dict[str, float] = {}

    def window_prints(self, asset: str, window_start: int) -> dict[int, float]:
        """The coin's held prints for this window (a new window starts an empty record)."""
        held = self.prints.get(asset)
        if held is None or held[0] != window_start:
            held = (window_start, {})
            self.prints[asset] = held
        return held[1]

    def seen(self, slug: str, window_end: float) -> None:
        self._ends[slug] = window_end

    def prune(self, now: float, keep_s: float = HOUR_S) -> None:
        """Forget windows that ended more than ``keep_s`` ago."""
        old = [slug for slug, end in self._ends.items() if end < now - keep_s]
        for slug in old:
            del self._ends[slug]
            self.start_refs.pop(slug, None)
            self.saved.pop(slug, None)
            self.db_checked.discard(slug)
            self.cids.pop(slug, None)
            self.cid_retry_at.pop(slug, None)
            self.ptb_retry_at.pop(slug, None)


_DEFAULT_MEMORY = InputMemory()


# --------------------------------------------------------------------------- pure helpers


class StartRef(NamedTuple):
    status: str  # "found" | "pending" | "missing"
    value: float | None = None
    source: str | None = None


def start_reference(points: Iterable[Any], window_start: int, now: float) -> StartRef:
    """The TWAP-60s value at the window start, from held prints (``PricePoint``-like objects
    with ``obs_ms`` and ``value``). See the module docstring for the order of preference.

    "pending": no print at the open second yet and none after it (it arrives ~2 s late).
    "missing": the stream moved past the open and nothing is held near it.
    """
    s0 = int(window_start)
    by_s: dict[int, float] = {}
    for p in points:
        value = _positive(getattr(p, "value", None))
        if value is not None:
            by_s[int(p.obs_ms // 1000)] = value
    read = f"read {max(0.0, now - s0):.0f} s after the open"
    if s0 in by_s:
        return StartRef("found", by_s[s0], f"TWAP-60s print at the open, {read}")
    if not any(s > s0 for s in by_s):
        return StartRef("pending")
    before = [s for s in by_s if s0 - START_MATCH_S <= s < s0]
    if before:
        s = max(before)
        return StartRef("found", by_s[s], f"TWAP-60s print {s0 - s} s before the open "
                                          f"(none at the open second), {read}")
    after = [s for s in by_s if s0 < s <= s0 + START_MATCH_S]
    if after:
        s = min(after)
        return StartRef("found", by_s[s], f"TWAP-60s print {s - s0} s after the open "
                                          f"(none at or just before it), {read}")
    return StartRef("missing")


def window_average(prints: Mapping[int, float], window_start: int,
                   start_value: float) -> WindowAverage:
    """The step-path average of the held prints from the window start (module docstring)."""
    s0 = int(window_start)
    last = max((s for s in prints if s >= s0), default=s0)
    value = start_value
    total = log_total = 0.0
    printed = gap = longest = 0
    for s in range(s0, last + 1):
        v = prints.get(s)
        if v is not None:
            value = v
            printed += 1
            gap = 0
        elif s > s0:
            gap += 1
            longest = max(longest, gap)
        total += value
        log_total += math.log(value)
    n = last - s0 + 1
    return WindowAverage(value=total / n, log_value=log_total / n, through_s=last, seconds=n,
                         printed=printed, longest_gap_s=longest)


def _positive(value: Any) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) and v > 0 else None


def _clock(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%H:%M:%S UTC")


# --------------------------------------------------------------------------- one pass


class _Missing(Exception):
    """An input that could not be read: becomes a Problem."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class _Pass:
    """One coin's pass: the problems, notes, warnings and facts found so far."""

    def __init__(self, asset: str, now: float) -> None:
        self.asset = asset
        self.now = now
        self.problems: list[tuple[str, str]] = []
        self.notes: list[str] = []
        self.warnings: list[str] = []
        self.known: dict[str, Any] = {}
        self.window_slug: str | None = None

    def problem(self, code: str, message: str) -> None:
        self.problems.append((code, message))

    def failed(self) -> Problem:
        (code, message), *rest = self.problems
        return Problem(asset=self.asset, code=code, message=message, ts=self.now,
                       window_slug=self.window_slug, also=tuple(rest),
                       known=MappingProxyType(dict(self.known)), notes=tuple(self.notes),
                       warnings=tuple(self.warnings))


class _Hour(NamedTuple):
    slug: str
    condition_id: str | None
    bid: float | None
    ask: float | None
    age_s: float | None


async def gather(hub: Any, client: httpx.AsyncClient | None, now: float, *,
                 memory: InputMemory | None = None) -> dict[str, Inputs | Problem]:
    """This pass's inputs for every coin in ``ASSETS``; never raises (a cancel propagates).

    ``hub``: the process's ``MarketDataHub`` (``hub.current()``), None when there is none.
    ``client``: an httpx client for Binance and Gamma. ``now``: the pass time, epoch seconds.
    ``memory``: state kept between passes; a module default when not given.
    """
    memory = _DEFAULT_MEMORY if memory is None else memory
    if hub is None:
        return {
            asset: Problem(asset=asset, code="no_hub", ts=now,
                           message="No live market data: the market-data hub is not running "
                                   "in this process.")
            for asset in ASSETS
        }
    try:
        memory.prune(now)
    except Exception as exc:  # noqa: BLE001 — housekeeping only
        log.warning("fade_1h.inputs_prune_failed", error=f"{type(exc).__name__}: {exc}")
    results = await asyncio.gather(*(_guarded(hub, client, a, now, memory) for a in ASSETS))
    return dict(zip(ASSETS, results, strict=True))


async def _guarded(hub: Any, client: httpx.AsyncClient | None, asset: str, now: float,
                   memory: InputMemory) -> Inputs | Problem:
    try:
        return await _gather_one(hub, client, asset, now, memory)
    except Exception as exc:  # noqa: BLE001 — a pass must never raise; shown on the card
        log.warning("fade_1h.inputs_failed", asset=asset, error=f"{type(exc).__name__}: {exc}")
        return Problem(asset=asset, code="internal_error", ts=now,
                       message=f"Reading the inputs failed unexpectedly: "
                               f"{type(exc).__name__}: {exc}")


async def _gather_one(hub: Any, client: httpx.AsyncClient | None, asset: str, now: float,
                      memory: InputMemory) -> Inputs | Problem:
    run = _Pass(asset, now)
    coin = asset.upper()
    try:
        hub.want(asset, "15m", OWNER, hot=True)
        hub.want(asset, "1h", OWNER)
    except ValueError as exc:
        run.problem("market_unavailable", f"The hub does not offer {coin}'s 15m or 1h market: "
                                          f"{exc}.")
        return run.failed()
    await _wait_ready(hub, asset)

    ref = hub.market(asset, "15m")
    if ref is None:
        run.problem("window_unknown", f"The hub has not found the tokens of {coin}'s current "
                                      f"15m window yet.")
        return run.failed()
    start, end = int(ref.window_start), int(ref.window_end)
    if not ref.window_start <= now < ref.window_end:
        if now >= ref.window_end:
            run.problem("window_rolling", f"The hub still shows the {coin} 15m window that "
                                          f"ended at {_clock(end)}; it moves to the new one "
                                          f"within about 2 s.")
        else:
            run.problem("window_rolling", f"The hub shows a {coin} 15m window that starts at "
                                          f"{_clock(start)}, after now.")
        return run.failed()
    run.window_slug = ref.slug
    memory.seen(ref.slug, end)
    hour_start = start - start % HOUR_S
    run.known.update(window_slug=ref.slug, window_start=start, window_end=end,
                     up_token=ref.up_token, down_token=ref.down_token, hour_start=hour_start)

    cid = await _condition_id(client, ref, now, memory, run)
    up_book = _book(hub, "Up", ref.up_token, now, run)
    down_book = _book(hub, "Down", ref.down_token, now, run)
    tick = _tick_size(hub, (ref.up_token, ref.down_token), run)
    hour = _hour_market(hub, asset, hour_start, now, run)
    prices = {src: _price_now(hub, src, asset, now, run) for src in PRICE_SOURCES}
    start_ref = await _start_ref(hub, client, asset, ref, now, memory, run)
    avg = _window_avg(hub, asset, ref, start_ref, memory, run)
    close = _close_avg(hub, asset, ref, now, run)
    final_ref = None if start_ref is None or not start_ref.final else start_ref[:2]
    await _save_window(ref, hour_start, cid, final_ref, hour, now, memory, run)
    minute, r15, hour_open = await _binance_history(client, asset, start, hour_start, now,
                                                    memory, run)

    if run.problems:
        return run.failed()
    # Every piece is present when nothing was flagged; the asserts document it for type checks.
    assert cid is not None and up_book is not None and down_book is not None
    assert hour is not None and hour.bid is not None and hour.ask is not None
    assert start_ref is not None and avg is not None
    assert minute is not None and r15 is not None and hour_open is not None
    twap60, chainlink, binance = (prices[src] for src in PRICE_SOURCES)
    assert twap60 is not None and chainlink is not None and binance is not None
    return Inputs(
        asset=asset, ts=now, window_slug=ref.slug, window_start=float(start),
        window_end=float(end), condition_id=cid, up_token=ref.up_token,
        down_token=ref.down_token, tick_size=tick, up_book=up_book, down_book=down_book,
        hour_start=float(hour_start), hour_slug=hour.slug, hour_condition_id=hour.condition_id,
        hour_up_bid=hour.bid, hour_up_ask=hour.ask, hour_book_age_s=hour.age_s,
        hour_open=hour_open, twap60=twap60, chainlink=chainlink, binance=binance,
        start_ref=start_ref[0], start_ref_source=start_ref[1], window_avg=avg,
        minute_returns=minute[1], minute_returns_end=float(minute[0]), r15=r15,
        notes=tuple(run.notes), close_avg=close, warnings=tuple(run.warnings),
    )


async def _wait_ready(hub: Any, asset: str) -> None:
    """Give a market that has just been wanted a moment to deliver its books."""
    waits = []
    for timeframe in ("15m", "1h"):
        quote = hub.quote(asset, timeframe)
        if quote is None or not quote.live:
            waits.append(hub.wait_ready(asset, timeframe, WAIT_READY_S))
    if waits:
        await asyncio.gather(*waits, return_exceptions=True)


async def _condition_id(client: httpx.AsyncClient | None, ref: Any, now: float,
                        memory: InputMemory, run: _Pass) -> str | None:
    """The hub's condition id, else one Gamma lookup per slug (retried every 30 s)."""
    cid = str(ref.condition_id) if ref.condition_id else memory.cids.get(ref.slug)
    if cid is None:
        cid, why = await _lookup_condition_id(client, ref.slug, now, memory)
        if cid is None:
            run.problem("condition_id_unknown", f"The condition id of {ref.slug} is unknown "
                                                f"({why}); fills and settlement need it.")
            return None
    run.known["condition_id"] = cid
    return cid


async def _lookup_condition_id(client: httpx.AsyncClient | None, slug: str, now: float,
                               memory: InputMemory) -> tuple[str | None, str]:
    if client is None:
        return None, "there is no HTTP client to look it up on Gamma"
    retry_at = memory.cid_retry_at.get(slug)
    if retry_at is not None and retry_at > now:
        return None, f"the last Gamma lookup failed; it is retried in {retry_at - now:.0f} s"
    try:
        cid = await _gamma_condition_id(client, slug)
    except Exception as exc:  # noqa: BLE001 — becomes a Problem
        memory.cid_retry_at[slug] = now + GAMMA_RETRY_S
        return None, f"the Gamma lookup failed: {type(exc).__name__}: {exc}"
    if cid is None:
        memory.cid_retry_at[slug] = now + GAMMA_RETRY_S
        return None, "Gamma does not list it"
    memory.cids[slug] = cid
    return cid, ""


async def _gamma_condition_id(client: httpx.AsyncClient, slug: str) -> str | None:
    resp = await client.get(f"{GAMMA_API}/markets", params={"slug": slug},
                            headers=dict(GAMMA_HEADERS), timeout=HTTP_TIMEOUT_S)
    resp.raise_for_status()
    rows = resp.json()
    if not isinstance(rows, list):
        return None
    for row in rows:
        if isinstance(row, dict) and row.get("slug") == slug and row.get("conditionId"):
            return str(row["conditionId"])
    return None


def _book(hub: Any, side: str, token: str, now: float, run: _Pass) -> Book | None:
    stream = hub.top(token)
    if stream is None or not stream.live:
        run.problem("book_not_live", f"No live connection serves the 15m {side} book right "
                                     f"now.")
        return None
    best = hub.book_top(token) or stream
    bids = hub.levels(token, "bid", DEPTH_LEVELS)
    asks = hub.levels(token, "ask", DEPTH_LEVELS)
    if bids is None or asks is None:
        run.problem("book_not_live", f"The 15m {side} book is not streaming.")
        return None
    key = side.lower()
    run.known[f"{key}_bid"], run.known[f"{key}_ask"] = best.best_bid, best.best_ask
    missing = [name for name, px in (("bids", best.best_bid), ("asks", best.best_ask))
               if px is None]
    if missing:
        run.problem("book_one_sided", f"The 15m {side} book has no {' or '.join(missing)}, "
                                      f"so the market's price is undefined.")
        return None
    if best.best_bid > best.best_ask:
        run.problem("book_crossed", f"The 15m {side} book is crossed (bid {best.best_bid:g} "
                                    f"above ask {best.best_ask:g}); it is mid-update.")
        return None
    age = None if best.received_ms is None else max(0.0, now - best.received_ms / 1000)
    return Book(
        side=side, token_id=token, best_bid=float(best.best_bid),
        best_ask=float(best.best_ask), bid_size=best.bid_size, ask_size=best.ask_size,
        bids=tuple((float(px), float(sz)) for px, sz in bids),
        asks=tuple((float(px), float(sz)) for px, sz in asks),
        source=str(getattr(best, "source", "stream")), age_s=age,
    )


def _tick_size(hub: Any, tokens: Iterable[str], run: _Pass) -> float:
    """The coarser of the two books' ticks (a coarser step is always valid on a finer grid).

    The stream book's tick comes from its snapshot and later tick-size changes."""
    ticks = []
    for token in tokens:
        top = hub.top(token)
        tick = None if top is None else _positive(top.tick_size)
        if tick is not None:
            ticks.append(tick)
    if ticks:
        return max(ticks)
    run.notes.append(f"The books carry no tick size; using the venue's standard "
                     f"{DEFAULT_TICK:g}, which is always a valid price step.")
    return DEFAULT_TICK


def _hour_market(hub: Any, asset: str, hour_start: int, now: float, run: _Pass) -> _Hour | None:
    coin = asset.upper()
    quote = hub.quote(asset, "1h")
    if quote is None:
        run.problem("hour_market_unknown", f"The hub has not found the tokens of {coin}'s "
                                           f"current 1h market yet.")
        return None
    if int(quote.market.window_start) != hour_start:
        run.problem("hour_mismatch", f"The hub's {coin} 1h market starts at "
                                     f"{_clock(quote.market.window_start)}, not at this "
                                     f"window's hour {_clock(hour_start)} (it is rolling over, "
                                     f"or this is the repeated daylight-saving hour).")
        return None
    slug, cid = quote.market.slug, quote.market.condition_id
    run.known.update(hour_slug=slug, hour_condition_id=cid)
    up = quote.up
    if not quote.live or up is None:
        run.problem("hour_book_not_live", f"No live connection serves {coin}'s 1h books right "
                                          f"now.")
        return _Hour(slug, cid, None, None, None)
    run.known.update(hour_up_bid=up.best_bid, hour_up_ask=up.best_ask)
    missing = [name for name, px in (("bids", up.best_bid), ("asks", up.best_ask))
               if px is None]
    if missing:
        run.problem("hour_book_one_sided", f"{coin}'s 1h Up book has no "
                                           f"{' or '.join(missing)}, so the hour's price is "
                                           f"undefined.")
        return _Hour(slug, cid, None, None, None)
    age = None if up.received_ms is None else max(0.0, now - up.received_ms / 1000)
    return _Hour(slug, cid, float(up.best_bid), float(up.best_ask), age)


def _price_now(hub: Any, source: str, asset: str, now: float, run: _Pass) -> PriceNow | None:
    name, coin = SOURCE_NAMES[source], asset.upper()
    point = hub.price(source, asset)
    if point is None:
        run.problem("price_missing", f"No {name} print for {coin} yet.")
        return None
    value = _positive(point.value)
    obs_s = point.obs_ms / 1000
    age = max(0.0, now - obs_s)
    run.known[source] = {"value": point.value, "age_s": round(age, 3)}
    if value is None:
        run.problem("price_invalid", f"The newest {name} print for {coin} is not a usable "
                                     f"price ({point.value!r}).")
        return None
    if age > PRICE_MAX_AGE_S:
        run.problem("price_stale", f"The newest {name} print for {coin} is {age:.0f} s old; a "
                                   f"decision needs one under {PRICE_MAX_AGE_S:.0f} s.")
        return None
    return PriceNow(source=source, value=value, obs_s=obs_s, age_s=age)


class _Ref(NamedTuple):
    value: float
    source: str
    final: bool  # the opening print itself (or Gamma's copy of it), not a stand-in


async def _start_ref(hub: Any, client: httpx.AsyncClient | None, asset: str, ref: Any,
                     now: float, memory: InputMemory, run: _Pass) -> _Ref | None:
    """The window's priceToBeat: held, stored, the opening print, Gamma's, else a named
    stand-in (module docstring)."""
    slug, s0 = ref.slug, int(ref.window_start)
    got = memory.start_refs.get(slug)
    if got is None and slug not in memory.db_checked:
        try:
            row = await _ledger.get_window(slug)
        except Exception as exc:  # noqa: BLE001 — the hub may still have it
            run.warnings.append(f"Could not read this window's stored start reference: "
                                f"{type(exc).__name__}: {exc}")
        else:
            memory.db_checked.add(slug)
            if row is not None and _positive(row.get("start_ref_price")) is not None:
                got = (float(row["start_ref_price"]), str(row.get("start_ref_source") or
                                                          "stored start reference"))
                memory.start_refs[slug] = got
    if got is not None:
        run.known.update(start_ref=got[0], start_ref_source=got[1])
        return _Ref(got[0], got[1], True)

    points = tuple(hub.prices(TWAP60, asset))
    found = start_reference(points, s0, now)
    exact = found.status == "found" and any(
        int(p.obs_ms // 1000) == s0 and _positive(p.value) is not None for p in points)
    if exact:
        assert found.value is not None and found.source is not None
        memory.start_refs[slug] = (found.value, found.source)
        run.known.update(start_ref=found.value, start_ref_source=found.source)
        return _Ref(found.value, found.source, True)

    # The opening print is not held. Gamma publishes the same number as the window's
    # priceToBeat, several minutes into the window. Not asked while the print is simply on
    # its way (it arrives ~2 s after the open).
    on_its_way = found.status == "pending" and now - s0 <= 2 * START_MATCH_S
    gamma_why = "not asked yet"
    if not on_its_way:
        value, gamma_why = await _lookup_price_to_beat(client, slug, now, memory)
        if value is not None:
            source = f"{GAMMA_PTB} (the TWAP-60s print at the open), read {now - s0:.0f} s " \
                     f"after the open"
            memory.start_refs[slug] = (value, source)
            run.known.update(start_ref=value, start_ref_source=source)
            return _Ref(value, source, True)

    if found.status == "pending":
        if on_its_way:
            why = "it arrives about 2 s after the open"
        else:
            why = f"none has arrived {now - s0:.0f} s after it, so the feed looks stalled"
        run.problem("start_ref_pending", f"Waiting for the TWAP-60s print at the window "
                                         f"open ({_clock(s0)}); {why}.")
        return None
    if found.status == "missing":
        run.problem("start_ref_missing", f"Missed the window open ({_clock(s0)}): no "
                                         f"TWAP-60s print within {START_MATCH_S} s of it is "
                                         f"held (the app started or the feed reconnected "
                                         f"after the open), and Gamma has no priceToBeat for "
                                         f"it yet ({gamma_why}).")
        return None
    assert found.value is not None and found.source is not None
    source = f"{found.source} (fallback: Gamma has no priceToBeat for this window yet, " \
             f"{gamma_why})"
    run.known.update(start_ref=found.value, start_ref_source=source)
    return _Ref(found.value, source, False)


async def _lookup_price_to_beat(client: httpx.AsyncClient | None, slug: str, now: float,
                                memory: InputMemory) -> tuple[float | None, str]:
    """Gamma's priceToBeat for a window, at most once per ``GAMMA_RETRY_S`` while missing."""
    if client is None:
        return None, "there is no HTTP client to ask Gamma"
    retry_at = memory.ptb_retry_at.get(slug)
    if retry_at is not None and retry_at > now:
        return None, f"asked again in {retry_at - now:.0f} s"
    memory.ptb_retry_at[slug] = now + GAMMA_RETRY_S
    try:
        resp = await client.get(f"{GAMMA_API}/events", params={"slug": slug},
                                headers=dict(GAMMA_HEADERS), timeout=HTTP_TIMEOUT_S)
        resp.raise_for_status()
        rows = resp.json()
    except Exception as exc:  # noqa: BLE001 — the fallback applies; retried later
        return None, f"the lookup failed: {type(exc).__name__}: {exc}"
    for row in rows if isinstance(rows, list) else [rows]:
        if not isinstance(row, Mapping) or row.get("slug") not in (None, slug):
            continue
        meta = row.get("eventMetadata")
        value = _positive(meta.get("priceToBeat")) if isinstance(meta, Mapping) else None
        if value is not None:
            memory.ptb_retry_at.pop(slug, None)
            return value, ""
    return None, "not published yet"


def _close_avg(hub: Any, asset: str, ref: Any, now: float, run: _Pass) -> WindowAverage | None:
    """The part of the closing 60 s average already known: Chainlink prints from
    ``window_end - CLOSE_AVG_S`` to the newest one. None before the last 60 s."""
    s0 = int(ref.window_end) - CLOSE_AVG_S
    if now < s0:
        return None
    by_s: dict[int, float] = {}
    for point in hub.prices(CHAINLINK, asset):
        value = _positive(point.value)
        if value is not None:
            by_s[int(point.obs_ms // 1000)] = value
    held_before = [s for s in by_s if s <= s0]
    after = sorted(s for s in by_s if s0 < s <= now)
    if held_before:
        start_value = by_s[max(held_before)]
    elif after:
        start_value = by_s[after[0]]
    else:
        run.problem("average_incomplete", f"No Chainlink print is held for the closing minute "
                                          f"(from {_clock(s0)}), so the known part of the "
                                          f"closing average is not known.")
        return None
    close = window_average({s: v for s, v in by_s.items() if s >= s0}, s0, start_value)
    run.known["close_avg"] = {"value": close.value, "seconds": close.seconds,
                              "longest_gap_s": close.longest_gap_s}
    if not held_before and after and after[0] - s0 > MAX_AVG_GAP_S:
        run.problem("average_incomplete", f"The Chainlink prints held for the closing minute "
                                          f"start {after[0] - s0} s into it, so its average so "
                                          f"far is not known.")
    elif close.longest_gap_s > MAX_AVG_GAP_S:
        run.problem("average_incomplete", f"The Chainlink prints held for the closing minute "
                                          f"have a {close.longest_gap_s} s hole, so its average "
                                          f"so far is not known.")
    return close


def _window_avg(hub: Any, asset: str, ref: Any, start_ref: tuple[float, str] | None,
                memory: InputMemory, run: _Pass) -> WindowAverage | None:
    s0, s_end = int(ref.window_start), int(ref.window_end)
    held = memory.window_prints(asset, s0)
    for point in hub.prices(TWAP60, asset):
        s = int(point.obs_ms // 1000)
        value = _positive(point.value)
        if s0 <= s < s_end and value is not None:
            held[s] = value
    if start_ref is None:
        return None
    avg = window_average(held, s0, start_ref[0])
    # Information only: the window settles on the print at its close, not on this average, so
    # a hole here (after a restart, or a feed drop) is recorded, never a reason to stop.
    run.known["window_avg"] = {"value": avg.value, "seconds": avg.seconds,
                               "printed": avg.printed, "longest_gap_s": avg.longest_gap_s}
    return avg


async def _save_window(ref: Any, hour_start: int, cid: str | None,
                       start_ref: tuple[float, str] | None, hour: _Hour | None, now: float,
                       memory: InputMemory, run: _Pass) -> None:
    """Record the window in ``fade_windows`` whenever this pass knows something new about it."""
    facts = (cid, ref.up_token, ref.down_token, start_ref,
             None if hour is None else hour.slug, None if hour is None else hour.condition_id)
    if memory.saved.get(ref.slug) == facts:
        return
    try:
        await _ledger.upsert_window(
            window_slug=ref.slug, asset=ref.asset, window_start=ref.window_start,
            window_end=ref.window_end, ts=now, hour_start=hour_start, condition_id=cid,
            up_token=ref.up_token, down_token=ref.down_token,
            start_ref_price=None if start_ref is None else start_ref[0],
            start_ref_source=None if start_ref is None else start_ref[1],
            hour_slug=facts[4], hour_condition_id=facts[5],
        )
    except Exception as exc:  # noqa: BLE001 — retried next pass; shown on the card
        run.warnings.append(f"Could not save this window's row, so it cannot be settled or "
                            f"learned from until a later pass saves it: "
                            f"{type(exc).__name__}: {exc}")
        return
    memory.saved[ref.slug] = facts


# --------------------------------------------------------------------------- Binance


async def _binance_history(
    client: httpx.AsyncClient | None, asset: str, window_start: int, hour_start: int,
    now: float, memory: InputMemory, run: _Pass,
) -> tuple[tuple[int, tuple[float, ...]] | None, tuple[float, ...] | None, float | None]:
    """(minute returns with their end, 15m returns, hour open); None for each that failed."""
    symbol = SYMBOLS[asset]
    if client is None:
        run.problem("klines_failed", f"Binance candles for {symbol} cannot be read: there is "
                                     f"no HTTP client.")
        return None, None, None
    results = await asyncio.gather(
        _minute_returns(client, asset, symbol, now, memory),
        _r15(client, asset, symbol, window_start, now, memory),
        _hour_open(client, asset, symbol, hour_start, memory),
        return_exceptions=True,
    )
    out: list[Any] = []
    for result in results:
        if isinstance(result, _Missing):
            run.problem(result.code, result.message)
            out.append(None)
        elif isinstance(result, Exception):
            run.problem("klines_failed", f"Binance candles for {symbol} could not be read: "
                                         f"{type(result).__name__}: {result}")
            out.append(None)
        elif isinstance(result, BaseException):
            raise result  # a cancel
        else:
            out.append(result)
    minute, r15, hour_open = out
    if minute is not None:
        run.known["sigma"] = math.sqrt(sum(r * r for r in minute[1]))
    if hour_open is not None:
        run.known["hour_open"] = hour_open
    return minute, r15, hour_open


async def _klines(client: httpx.AsyncClient, symbol: str, interval: str, start_s: int,
                  limit: int) -> list[Any]:
    try:
        resp = await client.get(
            f"{_config.BINANCE_API_BASE}/api/v3/klines",
            params={"symbol": symbol, "interval": interval, "startTime": int(start_s) * 1000,
                    "limit": limit},
            timeout=HTTP_TIMEOUT_S,
        )
        resp.raise_for_status()
        rows = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise _Missing("klines_failed", f"Binance {interval} candles for {symbol} could not be "
                                        f"read: {type(exc).__name__}: {exc}") from exc
    if not isinstance(rows, list):
        raise _Missing("klines_failed", f"Binance {interval} candles for {symbol}: the reply "
                                        f"was not a list of candles.")
    return rows


def _completed(rows: list[Any], interval_s: int, now: float) -> dict[int, tuple[float, float]]:
    """{open second: (open, close)} of the rows that have closed; a forming row is dropped."""
    out: dict[int, tuple[float, float]] = {}
    for row in rows:
        try:
            open_s = int(row[0]) // 1000
            o, c = _positive(row[1]), _positive(row[4])
        except (TypeError, ValueError, IndexError):
            continue
        if o is None or c is None or open_s + interval_s > now - KLINE_SETTLE_S:
            continue
        out[open_s] = (o, c)
    return out


async def _minute_returns(client: httpx.AsyncClient, asset: str, symbol: str, now: float,
                          memory: InputMemory) -> tuple[int, tuple[float, ...]]:
    end = int((now - KLINE_SETTLE_S) // 60) * 60  # when the newest completed minute closed
    cached = memory.minute.get(asset)
    if cached is not None and cached[0] == end:
        return cached
    count = MINUTE_RETURNS + 1
    first = end - 60 * count
    bars = _completed(await _klines(client, symbol, "1m", first, count), 60, now)
    opens = [first + 60 * i for i in range(count)]  # oldest first
    have = [o for o in opens if o in bars]
    if len(have) < count:
        raise _Missing("klines_incomplete", f"Binance returned {len(have)} of the {count} "
                                            f"completed one-minute {symbol} candles needed (the "
                                            f"newest closing at {_clock(end)}).")
    closes = [bars[o][1] for o in opens]
    returns = tuple(math.log(closes[i] / closes[i - 1]) for i in range(count - 1, 0, -1))
    memory.minute[asset] = (end, returns)
    return end, returns


async def _r15(client: httpx.AsyncClient, asset: str, symbol: str, window_start: int,
               now: float, memory: InputMemory) -> tuple[float, ...]:
    cached = memory.r15.get(asset)
    if cached is not None and cached[0] == window_start:
        return cached[1]
    if window_start > now - KLINE_SETTLE_S:
        raise _Missing("klines_incomplete", f"The 15m {symbol} candle that ends at the window "
                                            f"open closed under {KLINE_SETTLE_S:.0f} s ago; it "
                                            f"is read on the next pass.")
    first = window_start - QUARTER_S * R15_CANDLES
    bars = _completed(await _klines(client, symbol, "15m", first, R15_CANDLES), QUARTER_S, now)
    opens = [window_start - QUARTER_S * j for j in range(1, R15_CANDLES + 1)]  # newest first
    have = [o for o in opens if o in bars]
    if len(have) < R15_CANDLES:
        raise _Missing("klines_incomplete", f"Binance returned {len(have)} of the "
                                            f"{R15_CANDLES} completed 15m {symbol} candles "
                                            f"before the window (the newest closing at "
                                            f"{_clock(window_start)}).")
    returns = tuple(math.log(bars[o][1] / bars[o][0]) for o in opens)
    memory.r15[asset] = (window_start, returns)
    return returns


async def _hour_open(client: httpx.AsyncClient, asset: str, symbol: str, hour_start: int,
                     memory: InputMemory) -> float:
    """The open of the 1h candle starting at ``hour_start`` (still forming; its open is final)."""
    cached = memory.hour_open.get(asset)
    if cached is not None and cached[0] == hour_start:
        return cached[1]
    for row in await _klines(client, symbol, "1h", hour_start, 1):
        try:
            open_s, value = int(row[0]) // 1000, _positive(row[1])
        except (TypeError, ValueError, IndexError):
            continue
        if open_s == hour_start and value is not None:
            memory.hour_open[asset] = (hour_start, value)
            return value
    raise _Missing("hour_open_missing", f"Binance has no 1h {symbol} candle opening at "
                                        f"{_clock(hour_start)} yet.")
