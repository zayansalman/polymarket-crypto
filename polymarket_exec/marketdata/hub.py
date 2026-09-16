"""Live Polymarket books, trades and reference prices for strategies (the module's public API).

``MarketDataHub.run`` holds the CLOB market-channel connections (order books, trades,
resolutions for the followed Up/Down windows), one RTDS connection (Chainlink, Chainlink
60 s TWAP and Binance prices) and the universe refresher (which windows to follow, every
2 s). The dashboard lifespan starts it and registers it with ``set_current``.

There is one market-channel socket per asset x timeframe (24 for the default grid). Live
on 2026-09-16, all 96 tokens on one socket (~2,100 frames/s, ~1.2 MiB/s) fell seconds
behind and the server dropped it with 1013 "slow consumer: send buffer full" every
15-30 s, even with a reader that did no parsing; one socket per pair kept every socket
near 110 ms. A window's two tokens always share a socket, because each ``price_change``
carries both outcomes.

Reads are safe from any thread (the BTC loop runs on its own thread and event loop):
they return frozen objects that are swapped in whole, never mutated.

Event-driven code calls ``listen()`` on its own event loop and iterates the listener:
``TopChanged`` (best bid/ask or their sizes moved), ``Trade``, ``PriceTick`` (a new
reference print), ``MarketResolved`` and ``WindowOpened``. Delivery crosses threads with
``call_soon_threadsafe``; a listener that falls ``maxsize`` events behind loses its
oldest events, and the losses are counted.

Observation only: nothing here decides or gates trades, or reads the trading mode.
"""
from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
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
from polymarket_exec.marketdata.clob_stream import (
    ClobMarketStream,
    StreamStatus,
    pause,
    percentile,
)
from polymarket_exec.marketdata.order_book import Level, OrderBook, TopOfBook
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
    clob: StreamStatus  # all market-channel sockets together (see merge_shard_status)
    clob_shards: dict[str, StreamStatus]  # by socket, e.g. "btc-5m"
    prices: dict[str, SourceStatus]  # by source
    price_ages: dict[str, float | None]  # seconds since the newest print, by source
    markets: int  # windows followed
    tokens: int  # tokens wanted
    subscribed: int  # tokens on the open socket
    gamma_lookups: int
    gamma_errors: int
    gamma_last_error: str | None
    listeners: int
    listener_drops: int


def shard_name(asset: str, timeframe: str) -> str:
    return f"{asset}-{timeframe}"


def merge_shard_status(
    shards: dict[str, StreamStatus], samples: dict[str, tuple[int, ...]]
) -> StreamStatus:
    """One status for the per-pair sockets.

    ``connected`` means every socket that wants tokens is up; ``last_error`` names the
    ones that are down. Counts and rates are summed; latency percentiles are taken over
    every socket's samples.
    """
    wanted = {name: st for name, st in shards.items() if st.desired > 0}
    down = [name for name, st in wanted.items() if not st.connected]
    since = [st.connected_since for st in wanted.values()
             if st.connected and st.connected_since is not None]
    frames = [st.last_frame_at for st in shards.values() if st.last_frame_at is not None]
    pongs = [st.last_pong_at for st in shards.values() if st.last_pong_at is not None]
    notices = [st.last_notice for st in shards.values() if st.last_notice]
    merged = sorted(x for name in shards for x in samples.get(name, ()))
    error = None
    if down:
        error = f"{down[0]}: {wanted[down[0]].last_error or 'connecting'}"
        if len(down) > 1:
            error += f" (+{len(down) - 1} more)"
    return StreamStatus(
        connected=bool(wanted) and not down,
        connected_since=min(since) if since else None,
        last_frame_at=max(frames) if frames else None,
        last_pong_at=max(pongs) if pongs else None,
        frames_total=sum(st.frames_total for st in shards.values()),
        frames_per_s=sum(st.frames_per_s for st in shards.values()),
        latency_ms_p50=percentile(merged, 0.5),
        latency_ms_p90=percentile(merged, 0.9),
        reconnects=sum(st.reconnects for st in shards.values()),
        resyncs=sum(st.resyncs for st in shards.values()),
        subscribed=sum(st.subscribed for st in shards.values()),
        desired=sum(st.desired for st in shards.values()),
        unknown=sum(st.unknown for st in shards.values()),
        handler_errors=sum(st.handler_errors for st in shards.values()),
        last_error=error[:200] if error else None,
        last_notice=notices[0] if notices else None,
    )


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
        self._clob_streams = {
            shard_name(asset, timeframe): ClobMarketStream(
                self.handle_clob_event, connect=clob_connect, time_fn=time_fn)
            for asset, timeframe in self._universe.grid
        }
        self._rtds = RtdsPriceStream(self._universe.assets, on_point=self._on_price,
                                     connect=rtds_connect, time_fn=time_fn)
        self._desired: frozenset[str] = frozenset()
        self._books: dict[str, OrderBook] = {}
        self._tops: dict[str, TopOfBook] = {}
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
        return self._tops.get(token_id)

    def levels(self, token_id: str, side: str = "bid", n: int = 10) -> tuple[Level, ...]:
        """The best ``n`` levels of ``side`` ("bid" | "ask") for a followed token."""
        book = self._books.get(token_id)
        return book.levels(side, n) if book is not None else ()

    def market(self, asset: str, timeframe: str, which: str = CURRENT) -> MarketRef | None:
        """The followed ``"current"`` or ``"next"`` window of ``asset``/``timeframe``."""
        return self._universe.market(asset, timeframe, which)

    def quote(self, asset: str, timeframe: str, which: str = CURRENT) -> MarketQuote | None:
        ref = self._universe.market(asset, timeframe, which)
        if ref is None:
            return None
        return MarketQuote(ref, self._tops.get(ref.up_token), self._tops.get(ref.down_token))

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
        shards = {name: stream.status() for name, stream in self._clob_streams.items()}
        clob = merge_shard_status(
            shards, {name: s.latency_samples() for name, s in self._clob_streams.items()})
        prices = self._rtds.status()
        universe = self._universe.status()
        listeners = self._listeners
        return MarketDataSnapshot(
            taken_at=now,
            started_at=self._started_at,
            clob=clob,
            clob_shards=shards,
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
        tasks = [asyncio.ensure_future(stream.run(stop_event))
                 for stream in self._clob_streams.values()]
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
        """Give each pair's socket its tokens; forget books no socket follows any more."""
        desired: set[str] = set()
        for asset, timeframe in self._universe.grid:
            tokens = groups.get((asset, timeframe), frozenset())
            self._clob_streams[shard_name(asset, timeframe)].set_tokens(tokens)
            desired |= tokens
        self._desired = frozenset(desired)
        for token in [t for t in self._books if t not in self._desired]:
            del self._books[token]
            self._tops.pop(token, None)

    def handle_clob_event(self, event: ClobEvent, received_ms: int) -> None:
        """Apply one market-channel event (the stream's callback; synchronous)."""
        if isinstance(event, PriceChangeEvent):
            for change in event.changes:
                book = self._book(change.asset_id)
                if book is not None:
                    book.apply(change.side, change.price, change.size, event.ts_ms, received_ms)
                    self._publish_top(book)
        elif isinstance(event, BookEvent):
            book = self._book(event.asset_id)
            if book is not None:
                book.reset(event.bids, event.asks, event.tick_size, event.last_trade_price,
                           event.ts_ms, received_ms)
                self._publish_top(book)
        elif isinstance(event, LastTradeEvent):
            book = self._book(event.asset_id)
            if book is not None:
                book.record_trade(event.price, event.size, event.side, event.ts_ms,
                                  received_ms)
                self._publish_top(book)
                self._emit(Trade(event.asset_id, event.price, event.size, event.side,
                                 event.ts_ms))
        elif isinstance(event, TickSizeEvent):
            book = self._book(event.asset_id)
            if book is not None and book.set_tick_size(event.new_tick_size):
                self._publish_top(book)
        elif isinstance(event, NewMarketEvent):
            self._universe.observe(event)
        elif isinstance(event, MarketResolvedEvent):
            self._on_resolved(event)
        # best_bid_ask repeats what the rebuilt book already shows.

    def _book(self, token_id: str) -> OrderBook | None:
        book = self._books.get(token_id)
        if book is None and token_id in self._desired:
            book = self._books[token_id] = OrderBook(token_id)
        return book

    def _publish_top(self, book: OrderBook) -> None:
        top = book.top()
        previous = self._tops.get(book.token_id)
        self._tops[book.token_id] = top
        if (previous is None
                or previous.best_bid != top.best_bid or previous.best_ask != top.best_ask
                or previous.bid_size != top.bid_size or previous.ask_size != top.ask_size):
            self._emit(TopChanged(book.token_id, top))

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
