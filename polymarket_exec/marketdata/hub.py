"""Live Polymarket books, trades and reference prices for strategies (the module's public API).

``MarketDataHub.run`` holds the CLOB market-channel connections (order books, trades,
resolutions for the followed Up/Down windows), one RTDS connection (Chainlink, Chainlink
60 s TWAP and Binance prices) and the universe (which windows to follow). Every 2 s one
loop rolls windows over from the tokens already known and another looks up the tokens
still missing, so a slow Gamma lookup never holds up a window that starts. The
dashboard lifespan starts it and registers it with ``set_current``.

Markets are streamed on demand. The grid (assets x timeframes) is what is available;
nothing comes from the market channel until someone calls ``want(asset, timeframe,
owner)`` (any thread; idempotent per owner and market). The first owner starts the
market's sockets (current and next window); ``release`` (or ``Demand.release``) lets it
go, and a market nobody wants keeps streaming for ``demand_linger_s`` (60 s) before its
sockets stop and its books are dropped, so an owner that re-registers every tick causes
no churn. Reads for a market that is not streaming return None, never an old book.
``pinned`` demand is fixed at construction. The RTDS prices are always on (a few KiB/s,
and strategies need their history warm).

Each asset x timeframe has its own market-channel socket group (``clob_shard.ClobShard``).
Live on 2026-09-16, all 96 tokens on one socket (~2,100 frames/s, ~1.2 MiB/s) fell
seconds behind and the server dropped it with 1013 "slow consumer: send buffer full"
every 15-30 s, even with a reader that did no parsing; one socket per pair kept most
sockets near 110 ms. A busy market's single connection could still stall for seconds
while a second connection to the same tokens stayed current, so the busiest groups
(``DEFAULT_HEDGE``: btc 5m, 15m, 1h) run two connections and serve whichever is
freshest. A window's two tokens always share a group, because each ``price_change``
carries both outcomes.

Reads are safe from any thread (the BTC loop runs on its own thread and event loop):
they return frozen objects that are swapped in whole, never mutated. A top (and a
quote) says ``live=False`` while no connection that is up serves its token: the socket
dropped, or a new one has not delivered its snapshot yet. The values are then the last
ones seen, and ``levels`` returns the last levels seen.

Event-driven code calls ``listen()`` on its own event loop and iterates the listener:
``TopChanged`` (best bid/ask or their sizes moved), ``Trade``, ``PriceTick`` (a new
reference print), ``MarketResolved`` and ``WindowOpened``, each pushed once however many
connections deliver it. Delivery crosses threads with ``call_soon_threadsafe``; a
listener that falls ``maxsize`` events behind loses its oldest events, and the losses
are counted.

Observation only: nothing here decides or gates trades, or reads the trading mode.
"""
from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Union

import httpx

from logging_setup import get_logger
from polymarket_exec.marketdata.clob_messages import (
    BookEvent,
    ClobEvent,
    LastTradeEvent,
    MarketResolvedEvent,
    NewMarketEvent,
    PriceChangeEvent,
    TickSizeEvent,
)
from polymarket_exec.marketdata.clob_shard import ClobShard, ShardStatus
from polymarket_exec.marketdata.clob_stream import StreamStatus, run_until_stopped
from polymarket_exec.marketdata.order_book import STREAM, Level, TopOfBook
from polymarket_exec.marketdata.rtds_stream import PricePoint, RtdsPriceStream, SourceStatus
from polymarket_exec.marketdata.universe import (
    ASSETS,
    CURRENT,
    REFRESH_S,
    TIMEFRAMES,
    MarketRef,
    MarketUniverse,
    UniverseUpdate,
)

log = get_logger("marketdata.hub")

LISTENER_MAXSIZE = 2048
# Connections per asset x timeframe; anything not listed gets one. Keys may be
# (asset, timeframe), "asset-timeframe" or an asset (all its timeframes).
DEFAULT_HEDGE: dict[Any, int] = {"btc-5m": 2, "btc-15m": 2, "btc-1h": 2}
RESOLVED_MEMORY = 512
# A market nobody wants any more keeps streaming this long, so an owner that lets go
# and takes it again (e.g. every tick) does not open and close sockets each time.
DEMAND_LINGER_S = 60.0
READY_POLL_S = 0.02  # how often ``wait_ready`` looks
# How old a book a decision may use. A REST /book round trip is p50 201 ms on
# this machine, so anything under a couple of seconds still beats reading it.
BOOK_MAX_STALE_S = 2.0

Pair = tuple[str, str]  # (asset, timeframe)


@dataclass(frozen=True)
class Demand:
    """One owner's use of one market (``MarketDataHub.want``).

    ``release()``, or leaving a ``with`` block, gives it up; releasing twice is harmless.
    """

    asset: str
    timeframe: str
    owner: str
    hub: MarketDataHub = field(repr=False, compare=False)

    def release(self) -> None:
        self.hub.release(self.owner, self.asset, self.timeframe)

    def __enter__(self) -> Demand:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()


@dataclass(frozen=True)
class MarketQuote:
    market: MarketRef
    up: TopOfBook | None
    down: TopOfBook | None
    live: bool = False  # both tops are there and live


@dataclass(frozen=True)
class TopChanged:
    token_id: str
    top: TopOfBook


@dataclass(frozen=True)
class Trade:
    token_id: str
    price: float
    size: float
    side: str  # the taker's side
    ts_ms: int
    transaction_hash: str = ""  # tells apart trades that are otherwise alike


@dataclass(frozen=True)
class PriceTick:
    point: PricePoint


@dataclass(frozen=True)
class MarketResolved:
    market: MarketRef | None  # None if the window was no longer followed
    condition_id: str
    winning_token: str
    winning_outcome: str
    ts_ms: int


@dataclass(frozen=True)
class WindowOpened:
    market: MarketRef  # the (asset, timeframe)'s new current window


HubEvent = Union[TopChanged, Trade, PriceTick, MarketResolved, WindowOpened]

# A grid market's state.
STREAMING = "STREAMING"  # someone wants it
LINGERING = "LINGERING"  # nobody does any more; its sockets stop after demand_linger_s
AVAILABLE = "AVAILABLE"  # not streaming


@dataclass(frozen=True)
class GridMarket:
    """One available market (asset x timeframe): who uses it and how its feed is doing."""

    asset: str
    timeframe: str
    state: str  # STREAMING | LINGERING | AVAILABLE
    owners: tuple[str, ...]  # who wants it, sorted (empty unless STREAMING)
    since: float | None  # streaming since (not AVAILABLE)
    linger_left_s: float | None  # LINGERING: seconds until its sockets stop
    connections_up: int
    connections: int
    served_latency_ms_p50: float | None
    served_latency_ms_p90: float | None
    bytes_per_s: float  # over the last 10 whole seconds, every connection together
    tokens: int  # tokens wanted (0 until its windows are known)


@dataclass(frozen=True)
class ReadStats:
    """How ``book_top`` reads were served (one count per call)."""

    reads: int  # from_stream + from_rest + stale + missing
    from_stream: int  # the market-channel sockets
    from_rest: int  # a REST /book read that was ahead of the sockets
    stale: int  # what we held was older than the caller's max_age_s
    missing: int  # nothing to serve: the caller falls back to its own read


class _ReadCounter:
    """``book_top`` outcomes, counted from any thread (observation only)."""

    __slots__ = ("_lock", "_counts")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts = dict.fromkeys(("from_stream", "from_rest", "stale", "missing"), 0)

    def bump(self, outcome: str) -> None:
        with self._lock:
            self._counts[outcome] += 1

    def stats(self) -> ReadStats:
        with self._lock:
            counts = dict(self._counts)
        return ReadStats(reads=sum(counts.values()), **counts)


@dataclass(frozen=True)
class MarketDataSnapshot:
    """What the FEEDS card shows."""

    taken_at: float
    started_at: float
    clob: StreamStatus  # every market-channel connection together (merge_shard_status)
    clob_shards: dict[str, ShardStatus]  # by asset x timeframe, e.g. "btc-5m"
    slowest_shard: str | None  # the group with the highest served latency
    prices: dict[str, SourceStatus]  # by source
    price_ages: dict[str, float | None]  # seconds since the newest print, by source
    markets: int  # windows followed
    tokens: int  # tokens wanted
    subscribed: int  # tokens with at least one open subscription
    gamma_lookups: int
    gamma_errors: int
    gamma_last_error: str | None
    listeners: int
    listener_drops: int
    grid: dict[str, GridMarket] = field(default_factory=dict)  # every market, grid order
    assets: tuple[str, ...] = ()
    timeframes: tuple[str, ...] = ()
    clob_kib_s: float = 0.0  # market channel, every connection
    rtds_kib_s: float = 0.0  # reference prices (after decompression)
    demand_linger_s: float = DEMAND_LINGER_S
    reads: ReadStats = ReadStats(0, 0, 0, 0, 0)  # how strategy reads were served


def shard_name(asset: str, timeframe: str) -> str:
    return f"{asset}-{timeframe}"


def hedge_count(hedge: Mapping[Any, int], asset: str, timeframe: str) -> int:
    """Connections for one asset x timeframe (the most specific key wins; at least one)."""
    for key in ((asset, timeframe), shard_name(asset, timeframe), asset):
        if key in hedge:
            return max(1, int(hedge[key]))
    return 1


def merge_shard_status(shards: dict[str, ShardStatus]) -> StreamStatus:
    """One status for every market-channel connection.

    ``connected`` means every group that wants tokens has a connection up; ``last_error``
    names the groups with none. Counts and rates are summed over connections. The latency
    percentiles are the SERVED latency of the worst group (what readers actually get),
    not the worst single connection.
    """
    wanted = {name: st for name, st in shards.items() if st.desired > 0}
    down = [name for name, st in wanted.items() if st.connected == 0]
    conns = [c for st in shards.values() for c in st.connections]
    since = [c.connected_since for st in wanted.values() for c in st.connections
             if c.connected and c.connected_since is not None]
    frames = [c.last_frame_at for c in conns if c.last_frame_at is not None]
    pongs = [c.last_pong_at for c in conns if c.last_pong_at is not None]
    notices = [c.last_notice for c in conns if c.last_notice]
    p50s = [st.served_latency_ms_p50 for st in wanted.values()
            if st.served_latency_ms_p50 is not None]
    p90s = [st.served_latency_ms_p90 for st in wanted.values()
            if st.served_latency_ms_p90 is not None]
    maxes = [st.served_latency_ms_max for st in wanted.values()
             if st.served_latency_ms_max is not None]
    error = None
    if down:
        detail = next((c.last_error for c in wanted[down[0]].connections if c.last_error),
                      None)
        error = f"{down[0]}: {detail or 'connecting'}"
        if len(down) > 1:
            error += f" (+{len(down) - 1} more)"
    return StreamStatus(
        connected=bool(wanted) and not down,
        connected_since=min(since) if since else None,
        last_frame_at=max(frames) if frames else None,
        last_pong_at=max(pongs) if pongs else None,
        frames_total=sum(c.frames_total for c in conns),
        frames_per_s=sum(c.frames_per_s for c in conns),
        latency_ms_p50=max(p50s) if p50s else None,
        latency_ms_p90=max(p90s) if p90s else None,
        reconnects=sum(c.reconnects for c in conns),
        resyncs=sum(c.resyncs for c in conns),
        subscribed=sum(max((c.subscribed for c in st.connections), default=0)
                       for st in shards.values()),
        desired=sum(st.desired for st in shards.values()),
        unknown=sum(c.unknown for c in conns),
        handler_errors=sum(c.handler_errors for c in conns),
        last_error=error[:200] if error else None,
        last_notice=notices[0] if notices else None,
        bytes_total=sum(c.bytes_total for c in conns),
        latency_ms_max=max(maxes) if maxes else None,
        bytes_per_s=sum(c.bytes_per_s for c in conns),
    )


def slowest_shard(shards: dict[str, ShardStatus]) -> str | None:
    timed = [(st.served_latency_ms_p50, name) for name, st in shards.items()
             if st.desired > 0 and st.served_latency_ms_p50 is not None]
    return max(timed)[1] if timed else None


async def _nap(stop_event: asyncio.Event, wake: asyncio.Event, delay: float) -> None:
    """Sleep ``delay`` seconds, or less if ``stop_event`` or ``wake`` is set first."""
    if stop_event.is_set() or wake.is_set():
        return
    waiters = [asyncio.ensure_future(stop_event.wait()), asyncio.ensure_future(wake.wait())]
    try:
        await asyncio.wait(waiters, timeout=delay, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for waiter in waiters:
            waiter.cancel()


def _token_of(event: ClobEvent) -> str | None:
    if isinstance(event, (BookEvent, LastTradeEvent, TickSizeEvent)):
        return event.asset_id
    if isinstance(event, PriceChangeEvent):
        return event.changes[0].asset_id
    return None


class Listener:
    """Hub events as an async iterator, on the event loop that created the listener."""

    def __init__(self, hub: MarketDataHub, maxsize: int, loop: asyncio.AbstractEventLoop) -> None:
        self._hub = hub
        self._maxsize = max(1, maxsize)
        self._loop = loop
        self._items: deque[HubEvent] = deque()
        self._waiter: asyncio.Future[None] | None = None
        self._wake_pending = False
        self._closed = False
        self.dropped = 0  # written by the hub's thread only

    @property
    def closed(self) -> bool:
        return self._closed

    def __aiter__(self) -> Listener:
        return self

    async def __anext__(self) -> HubEvent:
        while True:
            try:
                return self._items.popleft()
            except IndexError:
                pass
            if self._closed:
                raise StopAsyncIteration
            waiter = self._loop.create_future()
            self._waiter = waiter
            if self._items or self._closed:  # arrived while the waiter was being armed
                self._waiter = None
                continue
            try:
                await waiter
            finally:
                self._waiter = None

    def get_nowait(self) -> HubEvent | None:
        """The oldest waiting event, or None (for polling consumers)."""
        try:
            return self._items.popleft()
        except IndexError:
            return None

    def close(self) -> None:
        """Stop receiving events; iteration ends once the waiting ones are consumed."""
        if self._closed:
            return
        self._closed = True
        self._hub._remove_listener(self)
        self._schedule_wake()

    def _deliver(self, event: HubEvent) -> None:
        """Called on the hub's thread."""
        if self._closed:
            return
        items = self._items
        if len(items) >= self._maxsize:
            try:
                items.popleft()
            except IndexError:  # the consumer just took it
                pass
            else:
                self.dropped += 1
        items.append(event)  # append before checking the flag: no lost wake-ups
        self._schedule_wake()

    def _schedule_wake(self) -> None:
        if self._wake_pending:
            return
        self._wake_pending = True
        try:
            self._loop.call_soon_threadsafe(self._wake)
        except RuntimeError:  # the consumer's loop has closed
            self._wake_pending = False
            if not self._closed:
                self._closed = True
                self._hub._remove_listener(self)

    def _wake(self) -> None:
        self._wake_pending = False
        waiter = self._waiter
        if waiter is not None and not waiter.done():
            waiter.set_result(None)


class MarketDataHub:
    def __init__(
        self,
        assets: Iterable[str] = ASSETS,
        timeframes: Iterable[str] = TIMEFRAMES,
        *,
        hedge: Mapping[Any, int] | None = None,
        clob_connect: Callable[[str], Any] | None = None,
        rtds_connect: Callable[[str], Any] | None = None,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
        time_fn: Callable[[], float] = time.time,
        refresh_s: float = REFRESH_S,
        pinned: Iterable[tuple[str, str, str]] = (),
        demand_linger_s: float = DEMAND_LINGER_S,
    ) -> None:
        """``pinned``: (asset, timeframe, owner) demand that is always there and cannot be
        released. ``demand_linger_s``: how long a market nobody wants keeps streaming."""
        self._time_fn = time_fn
        self._refresh_s = refresh_s
        self._universe = MarketUniverse(assets, timeframes, client_factory=client_factory,
                                        time_fn=time_fn, wanted=())
        self._grid = frozenset(self._universe.grid)
        self._linger_s = max(0.0, float(demand_linger_s))
        # Demand: written under the lock from any thread; _owners is swapped whole.
        self._demand_lock = threading.Lock()
        self._owners: dict[Pair, frozenset[str]] = {}
        self._released_at: dict[Pair, float] = {}  # nobody wants it since (lingering)
        self._since: dict[Pair, float] = {}  # streaming since (wanted or lingering)
        pins = [(asset, timeframe, owner) for asset, timeframe, owner in pinned]
        for asset, timeframe, owner in pins:
            self._check_market(asset, timeframe, owner)
        self._pinned = frozenset(pins)
        for asset, timeframe, owner in pins:
            self._add_owner((asset, timeframe), owner, time_fn())
        self._universe.set_wanted(self._owners)
        self._loop: asyncio.AbstractEventLoop | None = None  # the running hub's loop
        self._wakes: tuple[asyncio.Event, ...] = ()
        hedge = DEFAULT_HEDGE if hedge is None else hedge
        self._shards = {
            shard_name(asset, timeframe): ClobShard(
                shard_name(asset, timeframe), hedge_count(hedge, asset, timeframe),
                on_top=self._on_top, on_trade=self._on_trade, on_other=self._on_other_event,
                connect=clob_connect, time_fn=time_fn)
            for asset, timeframe in self._universe.grid
        }
        self._rtds = RtdsPriceStream(self._universe.assets, on_point=self._on_price,
                                     connect=rtds_connect, time_fn=time_fn)
        self._shard_by_token: dict[str, ClobShard] = {}
        self._resolved_seen: dict[str, None] = {}  # markets whose resolution was pushed
        self._listeners: tuple[Listener, ...] = ()
        self._listeners_lock = threading.Lock()
        self._closed_listener_drops = 0
        self._reads = _ReadCounter()
        self._started_at = time_fn()

    @property
    def assets(self) -> tuple[str, ...]:
        return self._universe.assets

    @property
    def timeframes(self) -> tuple[str, ...]:
        return self._universe.timeframes

    @property
    def grid(self) -> tuple[Pair, ...]:
        """Every market that can be wanted: (asset, timeframe), in grid order."""
        return self._universe.grid

    @property
    def demand_linger_s(self) -> float:
        return self._linger_s

    # --- demand (any thread) ----------------------------------------------------------

    def want(self, asset: str, timeframe: str, owner: str) -> Demand:
        """Stream ``asset``/``timeframe`` for ``owner`` until it is released.

        Idempotent per (owner, asset, timeframe). The market's first owner starts its
        sockets (current and next window); until they deliver, reads return None.
        """
        key = self._check_market(asset, timeframe, owner)
        with self._demand_lock:
            added = self._add_owner(key, owner, self._time_fn())
        if added:
            self._poke()
        return Demand(asset, timeframe, owner, self)

    def release(self, owner: str, asset: str | None = None, timeframe: str | None = None
                ) -> int:
        """Drop ``owner``'s demand: all of it, or only one asset, timeframe or market.

        Returns how many markets it let go (pinned demand stays). A market nobody wants
        keeps streaming for ``demand_linger_s``; then its sockets stop and its books go.
        """
        now = self._time_fn()
        dropped = 0
        with self._demand_lock:
            owners_by_market = dict(self._owners)
            for key, owners in self._owners.items():
                if owner not in owners or (*key, owner) in self._pinned:
                    continue
                if (asset is not None and key[0] != asset) or (
                        timeframe is not None and key[1] != timeframe):
                    continue
                dropped += 1
                rest = owners - {owner}
                if rest:
                    owners_by_market[key] = rest
                else:
                    del owners_by_market[key]
                    self._released_at[key] = now
            if dropped:
                self._owners = owners_by_market
        if dropped:
            self._poke()
        return dropped

    def wanted(self) -> Mapping[Pair, frozenset[str]]:
        """Owners by (asset, timeframe) for the markets wanted now (a read-only view)."""
        return MappingProxyType(self._owners)

    def _check_market(self, asset: str, timeframe: str, owner: str) -> Pair:
        if not isinstance(owner, str) or not owner.strip():
            raise ValueError("a demand needs an owner name")
        key = (asset, timeframe)
        if key not in self._grid:
            raise ValueError(f"{asset} {timeframe} is not an available market")
        return key

    def _add_owner(self, key: Pair, owner: str, now: float) -> bool:
        """Add ``owner`` to a market's demand (lock held); False if it was there."""
        owners = self._owners.get(key, frozenset())
        if owner in owners:
            return False
        if not owners:
            released = self._released_at.pop(key, None)
            if released is None or now - released >= self._linger_s:
                self._since[key] = now  # it was not streaming: a new period starts
        self._owners = {**self._owners, key: owners | {owner}}
        return True

    def _poke(self) -> None:
        """Have the hub's loops apply a demand change now (callable from any thread)."""
        loop = self._loop
        if loop is None:
            return  # not running: run() applies the demand when it starts
        try:
            loop.call_soon_threadsafe(self._wake)
        except RuntimeError:  # the hub's loop has closed
            pass

    def _wake(self) -> None:
        for event in self._wakes:
            event.set()

    def _apply_demand(self, now: float | None = None) -> frozenset[Pair]:
        """Tell the universe which markets to stream: the wanted ones, and those released
        less than ``demand_linger_s`` ago (the hub's loop)."""
        now = self._time_fn() if now is None else now
        with self._demand_lock:
            for key, released in list(self._released_at.items()):
                if now - released >= self._linger_s:
                    del self._released_at[key]
                    self._since.pop(key, None)
            streaming = frozenset(self._owners).union(self._released_at)
        self._universe.set_wanted(streaming)
        return streaming

    # --- reads (any thread) ---------------------------------------------------------

    def top(self, token_id: str) -> TopOfBook | None:
        """The top of the freshest connection's book for a streamed token (``live=False``:
        no connection that is up serves it; the last values seen). None for a token of a
        market that is not streaming, or before its book arrives."""
        shard = self._shard_by_token.get(token_id)
        return shard.top(token_id) if shard is not None else None

    def levels(self, token_id: str, side: str = "bid", n: int = 10
               ) -> tuple[Level, ...] | None:
        """The best ``n`` levels of ``side`` ("bid" | "ask"), from the freshest connection;
        None for a token of a market that is not streaming."""
        shard = self._shard_by_token.get(token_id)
        return shard.levels(token_id, side, n) if shard is not None else None

    def market(self, asset: str, timeframe: str, which: str = CURRENT) -> MarketRef | None:
        """The ``"current"`` or ``"next"`` window of a streaming ``asset``/``timeframe``."""
        return self._universe.market(asset, timeframe, which)

    def quote(self, asset: str, timeframe: str, which: str = CURRENT) -> MarketQuote | None:
        ref = self._universe.market(asset, timeframe, which)
        if ref is None:
            return None
        up, down = self.top(ref.up_token), self.top(ref.down_token)
        live = up is not None and down is not None and up.live and down.live
        return MarketQuote(ref, up, down, live)

    def book_top(self, token_id: str, max_age_s: float | None = None) -> TopOfBook | None:
        """The freshest book we hold for ``token_id`` — never an old one instead.

        What a strategy calls per decision. It serves the streamed top while a
        connection that is up is serving that token, and the fresh REST poll's read
        whenever that one is ahead of the sockets. ``max_age_s`` is how old the read
        may be, measured on OUR clock (when it arrived): this machine runs ~66 ms
        behind the venue's event stamps, so "now minus the event time" is not
        staleness. None means the caller should read the book itself.
        """
        streamed = self.top(token_id)
        if streamed is not None and not streamed.live:
            streamed = None  # the last values seen, no longer updating
        best = self._freshest(streamed, self._rest_top(token_id))
        if best is None:
            self._reads.bump("missing")
            return None
        if max_age_s is not None and self._age_s(best) > max_age_s:
            self._reads.bump("stale")
            return None
        self._reads.bump("from_stream" if best.source == STREAM else "from_rest")
        return best

    def _rest_top(self, token_id: str) -> TopOfBook | None:
        """The fresh REST poll's last read for ``token_id``, when it beat the sockets."""
        return None

    @staticmethod
    def _freshest(streamed: TopOfBook | None, rest: TopOfBook | None) -> TopOfBook | None:
        """The one holding the newer book; the stream wins a tie (it keeps updating)."""
        if rest is None:
            return streamed
        if streamed is None:
            return rest
        streamed_ts = -1 if streamed.server_ts_ms is None else streamed.server_ts_ms
        rest_ts = -1 if rest.server_ts_ms is None else rest.server_ts_ms
        return rest if rest_ts > streamed_ts else streamed

    def _age_s(self, top: TopOfBook) -> float:
        if top.received_ms is None:
            return float("inf")
        return max(0.0, self._time_fn() - top.received_ms / 1000)

    async def wait_ready(self, asset: str, timeframe: str, timeout_s: float = 10.0,
                         which: str = CURRENT) -> bool:
        """Wait until both of a market's books are live, or ``timeout_s`` passes.

        What a caller that has just wanted a market uses before its first read: the
        sockets need to connect and deliver their snapshot first.
        """
        loop = asyncio.get_running_loop()
        end = loop.time() + max(0.0, timeout_s)
        while True:
            quote = self.quote(asset, timeframe, which)
            if quote is not None and quote.live:
                return True
            if loop.time() >= end:
                return False
            await asyncio.sleep(READY_POLL_S)

    def price(self, source: str, asset: str) -> PricePoint | None:
        """Newest print from ``source`` (chainlink | chainlink_twap60 | binance)."""
        return self._rtds.latest(source, asset)

    def prices(self, source: str, asset: str, seconds: float | None = None
               ) -> tuple[PricePoint, ...]:
        """Held prints, oldest first (up to 900); ``seconds`` keeps only the recent ones."""
        return self._rtds.history(source, asset, seconds)

    def listen(self, maxsize: int = LISTENER_MAXSIZE) -> Listener:
        """A listener bound to the calling thread's running event loop."""
        listener = Listener(self, maxsize, asyncio.get_running_loop())
        with self._listeners_lock:
            self._listeners = (*self._listeners, listener)
        return listener

    def snapshot(self) -> MarketDataSnapshot:
        now = self._time_fn()
        shards = {name: shard.status() for name, shard in self._shards.items()}
        clob = merge_shard_status(shards)
        prices = self._rtds.status()
        universe = self._universe.status()
        listeners = self._listeners
        return MarketDataSnapshot(
            taken_at=now,
            started_at=self._started_at,
            clob=clob,
            clob_shards=shards,
            slowest_shard=slowest_shard(shards),
            prices=prices,
            price_ages={
                source: (None if st.newest_obs_ms is None
                         else max(0.0, now - st.newest_obs_ms / 1000))
                for source, st in prices.items()
            },
            markets=universe.markets,
            tokens=universe.tokens,
            subscribed=clob.subscribed,
            gamma_lookups=universe.lookups,
            gamma_errors=universe.lookup_errors,
            gamma_last_error=universe.last_error,
            listeners=len(listeners),
            listener_drops=self._closed_listener_drops + sum(li.dropped for li in listeners),
            grid=self._grid_markets(now, shards),
            assets=self.assets,
            timeframes=self.timeframes,
            clob_kib_s=clob.bytes_per_s / 1024,
            rtds_kib_s=self._rtds.bytes_per_s() / 1024,
            demand_linger_s=self._linger_s,
            reads=self._reads.stats(),
        )

    def _grid_markets(self, now: float, shards: dict[str, ShardStatus]
                      ) -> dict[str, GridMarket]:
        with self._demand_lock:
            owners_by_market = self._owners
            released = dict(self._released_at)
            since = dict(self._since)
        out: dict[str, GridMarket] = {}
        for asset, timeframe in self._universe.grid:
            key = (asset, timeframe)
            name = shard_name(asset, timeframe)
            st = shards[name]
            owners = owners_by_market.get(key, frozenset())
            linger_left = None
            if owners:
                state = STREAMING
            elif key in released and now - released[key] < self._linger_s:
                state = LINGERING
                linger_left = self._linger_s - (now - released[key])
            else:
                state = AVAILABLE
            started = since.get(key) if state != AVAILABLE else None
            out[name] = GridMarket(
                asset=asset,
                timeframe=timeframe,
                state=state,
                owners=tuple(sorted(owners)),
                since=None if started is None else max(started, self._started_at),
                linger_left_s=linger_left,
                connections_up=st.connected,
                connections=len(st.connections),
                served_latency_ms_p50=st.served_latency_ms_p50,
                served_latency_ms_p90=st.served_latency_ms_p90,
                bytes_per_s=st.bytes_per_s,
                tokens=st.desired,
            )
        return out

    # --- the hub's loop ----------------------------------------------------------------

    async def run(self, stop_event: asyncio.Event) -> None:
        """Stream the wanted markets and the prices until ``stop_event``."""
        self._started_at = self._time_fn()
        roll_wake, lookup_wake = asyncio.Event(), asyncio.Event()
        self._wakes = (roll_wake, lookup_wake)
        self._loop = asyncio.get_running_loop()
        tasks = [asyncio.ensure_future(shard.run(stop_event))
                 for shard in self._shards.values()]
        tasks.append(asyncio.ensure_future(self._rtds.run(stop_event)))
        tasks.append(asyncio.ensure_future(self._roll_forever(stop_event, roll_wake)))
        tasks.append(asyncio.ensure_future(self._lookup_forever(stop_event, lookup_wake)))
        try:
            await asyncio.gather(*tasks)
        finally:
            self._loop = None
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self._universe.aclose()
            for listener in self._listeners:
                listener.close()

    async def _roll_forever(self, stop_event: asyncio.Event, wake: asyncio.Event) -> None:
        """Apply demand changes and switch windows on time, from the tokens already known
        (no I/O): every ``refresh_s``, and at once when demand changes."""
        while not stop_event.is_set():
            wake.clear()
            try:
                self._roll_once()
            except Exception as exc:  # noqa: BLE001 — keep following what we have
                log.warning("marketdata.refresh_failed", error=f"{type(exc).__name__}: {exc}")
            await _nap(stop_event, wake, self._refresh_s)

    async def _lookup_forever(self, stop_event: asyncio.Event, wake: asyncio.Event) -> None:
        """Look up the tokens of wanted windows not announced yet, then follow them. A
        stop cancels the lookups in flight (Gamma can take up to 10 s a read)."""
        while not stop_event.is_set():
            wake.clear()
            try:
                await run_until_stopped(self._lookup_once(), stop_event)
            except Exception as exc:  # noqa: BLE001 — keep following what we have
                log.warning("marketdata.refresh_failed", error=f"{type(exc).__name__}: {exc}")
            await _nap(stop_event, wake, self._refresh_s)

    def _roll_once(self) -> None:
        self._apply_demand()
        self._apply_update(self._universe.select())

    async def _lookup_once(self) -> None:
        self._apply_demand()
        self._apply_update(await self._universe.refresh())

    def _apply_update(self, update: UniverseUpdate) -> None:
        self._apply_groups(update.groups)
        for ref in update.opened:
            self._emit(WindowOpened(ref))

    def _apply_groups(self, groups: dict[tuple[str, str], frozenset[str]]) -> None:
        """Give each group its tokens; each group drops the books it no longer follows."""
        by_token: dict[str, ClobShard] = {}
        for asset, timeframe in self._universe.grid:
            shard = self._shards[shard_name(asset, timeframe)]
            tokens = groups.get((asset, timeframe), frozenset())
            shard.set_tokens(tokens)
            for token in tokens:
                by_token[token] = shard
        self._shard_by_token = by_token

    def handle_clob_event(self, event: ClobEvent, received_ms: int) -> None:
        """Apply one event as if its group's first connection delivered it (replays, tests)."""
        token = _token_of(event)
        if token is None:
            self._on_other_event(event)
            return
        shard = self._shard_by_token.get(token)
        if shard is not None:
            shard.handle_event(0, event, received_ms)

    def _on_top(self, token_id: str, top: TopOfBook) -> None:
        self._emit(TopChanged(token_id, top))

    def _on_trade(self, event: LastTradeEvent) -> None:
        self._emit(Trade(event.asset_id, event.price, event.size, event.side, event.ts_ms,
                         event.transaction_hash))

    def _on_other_event(self, event: ClobEvent) -> None:
        if isinstance(event, NewMarketEvent):
            self._universe.observe(event)
        elif isinstance(event, MarketResolvedEvent):
            if event.market in self._resolved_seen:
                return  # another connection delivered it first
            self._resolved_seen[event.market] = None
            while len(self._resolved_seen) > RESOLVED_MEMORY:
                del self._resolved_seen[next(iter(self._resolved_seen))]
            self._on_resolved(event)
        # best_bid_ask repeats what the rebuilt books already show.

    def _on_resolved(self, event: MarketResolvedEvent) -> None:
        ref = None
        for token in (event.winning_asset_id, *event.token_ids):
            ref = self._universe.mark_resolved(token)
            if ref is not None:
                break
        log.info("marketdata.market_resolved", slug=ref.slug if ref else None,
                 winner=event.winning_outcome)
        self._emit(MarketResolved(ref, event.market, event.winning_asset_id,
                                  event.winning_outcome, event.ts_ms))
        self._apply_groups(self._universe.groups())

    def _on_price(self, point: PricePoint) -> None:
        self._emit(PriceTick(point))

    def _emit(self, event: HubEvent) -> None:
        for listener in self._listeners:
            listener._deliver(event)

    def _remove_listener(self, listener: Listener) -> None:
        with self._listeners_lock:
            if listener in self._listeners:
                self._listeners = tuple(li for li in self._listeners if li is not listener)
                self._closed_listener_drops += listener.dropped


# Process-wide hub, set by the dashboard lifespan. None outside the app.
_current: MarketDataHub | None = None


def set_current(hub: MarketDataHub | None) -> None:
    global _current
    _current = hub


def current() -> MarketDataHub | None:
    return _current


# The same calls against whichever hub this process runs, for code that must work with
# and without one (the BTC loop runs outside the dashboard too): no hub -> no data, and
# the caller falls back to its own REST read.

def want(asset: str, timeframe: str, owner: str) -> Demand | None:
    """Stream a market for ``owner`` if a hub is running; None when there is none."""
    hub = _current
    return None if hub is None else hub.want(asset, timeframe, owner)


def release(owner: str, asset: str | None = None, timeframe: str | None = None) -> int:
    hub = _current
    return 0 if hub is None else hub.release(owner, asset, timeframe)


def book_top(token_id: str, max_age_s: float | None = None) -> TopOfBook | None:
    hub = _current
    return None if hub is None else hub.book_top(token_id, max_age_s)


async def wait_ready(asset: str, timeframe: str, timeout_s: float = 10.0) -> bool:
    hub = _current
    return False if hub is None else await hub.wait_ready(asset, timeframe, timeout_s)
