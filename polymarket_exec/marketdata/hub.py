"""Live Polymarket books, trades and reference prices for strategies (the module's public API).

``MarketDataHub.run`` holds the CLOB market-channel connections (order books, trades,
resolutions for the followed Up/Down windows), one RTDS connection (Chainlink, Chainlink
60 s TWAP and Binance prices) and the universe refresher (which windows to follow, every
2 s). The dashboard lifespan starts it and registers it with ``set_current``.

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
they return frozen objects that are swapped in whole, never mutated.

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
from dataclasses import dataclass
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
from polymarket_exec.marketdata.clob_stream import StreamStatus, pause
from polymarket_exec.marketdata.order_book import Level, TopOfBook
from polymarket_exec.marketdata.rtds_stream import PricePoint, RtdsPriceStream, SourceStatus
from polymarket_exec.marketdata.universe import (
    ASSETS,
    CURRENT,
    REFRESH_S,
    TIMEFRAMES,
    MarketRef,
    MarketUniverse,
)

log = get_logger("marketdata.hub")

LISTENER_MAXSIZE = 2048
# Connections per asset x timeframe; anything not listed gets one. Keys may be
# (asset, timeframe), "asset-timeframe" or an asset (all its timeframes).
DEFAULT_HEDGE: dict[Any, int] = {"btc-5m": 2, "btc-15m": 2, "btc-1h": 2}
RESOLVED_MEMORY = 512


@dataclass(frozen=True)
class MarketQuote:
    market: MarketRef
    up: TopOfBook | None
    down: TopOfBook | None


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
    )


def slowest_shard(shards: dict[str, ShardStatus]) -> str | None:
    timed = [(st.served_latency_ms_p50, name) for name, st in shards.items()
             if st.desired > 0 and st.served_latency_ms_p50 is not None]
    return max(timed)[1] if timed else None


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
    ) -> None:
        self._time_fn = time_fn
        self._refresh_s = refresh_s
        self._universe = MarketUniverse(assets, timeframes, client_factory=client_factory,
                                        time_fn=time_fn)
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
        self._started_at = time_fn()

    @property
    def assets(self) -> tuple[str, ...]:
        return self._universe.assets

    @property
    def timeframes(self) -> tuple[str, ...]:
        return self._universe.timeframes

    # --- reads (any thread) ---------------------------------------------------------

    def top(self, token_id: str) -> TopOfBook | None:
        """The top of the freshest connection's book for a followed token."""
        shard = self._shard_by_token.get(token_id)
        return shard.top(token_id) if shard is not None else None

    def levels(self, token_id: str, side: str = "bid", n: int = 10) -> tuple[Level, ...]:
        """The best ``n`` levels of ``side`` ("bid" | "ask"), from the freshest connection."""
        shard = self._shard_by_token.get(token_id)
        return shard.levels(token_id, side, n) if shard is not None else ()

    def market(self, asset: str, timeframe: str, which: str = CURRENT) -> MarketRef | None:
        """The followed ``"current"`` or ``"next"`` window of ``asset``/``timeframe``."""
        return self._universe.market(asset, timeframe, which)

    def quote(self, asset: str, timeframe: str, which: str = CURRENT) -> MarketQuote | None:
        ref = self._universe.market(asset, timeframe, which)
        if ref is None:
            return None
        return MarketQuote(ref, self.top(ref.up_token), self.top(ref.down_token))

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
        )

    # --- the hub's loop ----------------------------------------------------------------

    async def run(self, stop_event: asyncio.Event) -> None:
        """Follow the markets and prices until ``stop_event``."""
        self._started_at = self._time_fn()
        tasks = [asyncio.ensure_future(shard.run(stop_event))
                 for shard in self._shards.values()]
        tasks.append(asyncio.ensure_future(self._rtds.run(stop_event)))
        tasks.append(asyncio.ensure_future(self._refresh_forever(stop_event)))
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self._universe.aclose()
            for listener in self._listeners:
                listener.close()

    async def _refresh_forever(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                update = await self._universe.refresh()
            except Exception as exc:  # noqa: BLE001 — keep following what we have
                log.warning("marketdata.refresh_failed", error=f"{type(exc).__name__}: {exc}")
            else:
                self._apply_groups(update.groups)
                for ref in update.opened:
                    self._emit(WindowOpened(ref))
            await pause(stop_event, self._refresh_s)

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
