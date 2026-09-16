"""One asset x timeframe's market-channel connections: N redundant sockets, the freshest served.

Live on 2026-09-16, one connection to a busy market sometimes fell seconds behind while
a second connection to the same tokens stayed current, and the stalls were independent.
So busy markets run more than one connection:

* every connection keeps its own books, and events are never applied across connections;
* reads serve the connection whose book is furthest along the event stream (newest
  server time, then events applied at that millisecond); an exact tie goes to a
  connection that is clearly faster (recent latency more than 25 ms lower);
* top changes and trades are pushed once, by whichever connection delivers them first.
  Nothing behind what was already pushed goes out, and a trade is known by (token,
  time, price, size, side);
* a connection more than 3 s behind the freshest one is replaced while another fresh
  connection serves. If every connection is 10 s behind its own best, one is replaced
  at a time. A connection is never replaced while no other one is up.

With one connection nothing changes: the stream's own rule (replace when 10 s behind
its best) applies and every top change and trade is pushed.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import partial
from typing import Any

from logging_setup import get_logger
from polymarket_exec.marketdata.clob_messages import (
    BookEvent,
    ClobEvent,
    LastTradeEvent,
    PriceChangeEvent,
    TickSizeEvent,
)
from polymarket_exec.marketdata.clob_stream import (
    MAX_LAG_S,
    ClobMarketStream,
    StreamStatus,
    copy_deque,
    describe_error,
    pause,
    percentile,
)
from polymarket_exec.marketdata.order_book import Level, OrderBook, TopOfBook

log = get_logger("marketdata.shard")

RECYCLE_LAG_S = 3.0  # behind the freshest connection
STALL_S = 1.0  # a connection this far behind the freshest one is stalled
SUPERVISE_S = 0.5
TIE_MARGIN_MS = 25.0
LATENCY_WEIGHT = 0.05  # recent-latency smoothing per event
SERVED_SAMPLES = 512
TRADE_MEMORY = 512  # recent trade keys remembered per token

TopKey = tuple[Any, Any, Any, Any]  # best bid, best ask, bid size, ask size
Freshness = tuple[int, int]


@dataclass(frozen=True)
class ShardStatus:
    name: str  # e.g. "btc-5m"
    connections: tuple[StreamStatus, ...]
    connected: int  # connections up
    desired: int  # tokens wanted
    served_latency_ms_p50: float | None  # receive minus event time, first deliveries only
    served_latency_ms_p90: float | None
    served_latency_ms_max: float | None
    served_staleness_s: float | None  # now minus the newest served event's server time
    leader_switches: int  # a token started being served from another connection
    stalls_avoided: int  # a connection fell >1 s behind while another one kept serving
    recycles: int
    stall_episodes: tuple[int, ...]  # per connection: times it fell >1 s behind


class _Conn:
    __slots__ = ("index", "stream", "books", "newest_ms", "session_seen", "latency_ms",
                 "stalled", "stall_episodes", "recycles", "pending_session")

    def __init__(self, index: int, stream: ClobMarketStream) -> None:
        self.index = index
        self.stream = stream
        self.books: dict[str, OrderBook] = {}
        self.newest_ms: int | None = None  # newest server time applied, this session
        self.session_seen = stream.session
        self.latency_ms: float | None = None  # smoothed receive-minus-event latency
        self.stalled = False
        self.stall_episodes = 0
        self.recycles = 0
        self.pending_session: int | None = None  # replacement requested for this session


class ClobShard:
    """The connections for one asset x timeframe; the hub's callbacks get each event once.

    ``set_tokens``, ``handle_event`` and ``run`` belong to one event loop; ``top`` and
    ``levels`` are safe from any thread.
    """

    def __init__(
        self,
        name: str,
        connections: int = 1,
        *,
        on_top: Callable[[str, TopOfBook], None],
        on_trade: Callable[[LastTradeEvent], None],
        on_other: Callable[[ClobEvent], None],
        connect: Callable[[str], Any] | None = None,
        time_fn: Callable[[], float] = time.time,
        recycle_lag_s: float = RECYCLE_LAG_S,
        stall_s: float = STALL_S,
        own_lag_s: float = MAX_LAG_S,
        supervise_s: float = SUPERVISE_S,
        trade_memory: int = TRADE_MEMORY,
    ) -> None:
        count = max(1, int(connections))
        self.name = name
        self._on_top = on_top
        self._on_trade = on_trade
        self._on_other = on_other
        self._time_fn = time_fn
        self._recycle_lag_ms = recycle_lag_s * 1000
        self._stall_ms = stall_s * 1000
        self._own_lag_ms = own_lag_s * 1000
        self._supervise_s = supervise_s
        self._trade_memory = max(1, trade_memory)
        # One connection keeps the stream's own lag rule; with more, the shard decides.
        stream_lag_s = own_lag_s if count == 1 else 0.0
        self._conns = [
            _Conn(i, ClobMarketStream(partial(self.handle_event, i), connect=connect,
                                      time_fn=time_fn, max_lag_s=stream_lag_s))
            for i in range(count)
        ]
        self._wanted: frozenset[str] = frozenset()
        self._leader: dict[str, _Conn] = {}
        self._tops: dict[str, TopOfBook] = {}
        self._served_books: dict[str, OrderBook] = {}
        self._pushed: dict[str, tuple[Freshness, TopKey]] = {}
        self._trade_keys: dict[str, set[tuple]] = {}
        self._trade_order: dict[str, deque[tuple]] = {}
        self._served_latency: deque[int] = deque(maxlen=SERVED_SAMPLES)
        self._newest_served_ms: int | None = None
        self._leader_switches = 0
        self._stalls_avoided = 0

    @property
    def connections(self) -> int:
        return len(self._conns)

    @property
    def desired(self) -> frozenset[str]:
        return self._wanted

    # --- reads (any thread) ------------------------------------------------------------

    def top(self, token_id: str) -> TopOfBook | None:
        return self._tops.get(token_id)

    def levels(self, token_id: str, side: str, n: int) -> tuple[Level, ...]:
        book = self._served_books.get(token_id)
        return book.levels(side, n) if book is not None else ()

    def leader(self, token_id: str) -> int | None:
        """Index of the connection serving ``token_id``."""
        conn = self._leader.get(token_id)
        return None if conn is None else conn.index

    def book(self, index: int, token_id: str) -> OrderBook | None:
        """One connection's own book (for inspection)."""
        return self._conns[index].books.get(token_id)

    def status(self) -> ShardStatus:
        conns = tuple(c.stream.status() for c in self._conns)
        samples = sorted(copy_deque(self._served_latency))
        newest = self._newest_served_ms
        return ShardStatus(
            name=self.name,
            connections=conns,
            connected=sum(1 for st in conns if st.connected),
            desired=len(self._wanted),
            served_latency_ms_p50=percentile(samples, 0.5),
            served_latency_ms_p90=percentile(samples, 0.9),
            served_latency_ms_max=float(samples[-1]) if samples else None,
            served_staleness_s=(None if newest is None
                                else max(0.0, self._time_fn() - newest / 1000)),
            leader_switches=self._leader_switches,
            stalls_avoided=self._stalls_avoided,
            recycles=sum(c.recycles for c in self._conns),
            stall_episodes=tuple(c.stall_episodes for c in self._conns),
        )

    # --- the hub's loop ------------------------------------------------------------------

    def set_tokens(self, tokens: Iterable[str]) -> None:
        wanted = frozenset(tokens)
        self._wanted = wanted
        for conn in self._conns:
            conn.stream.set_tokens(wanted)
            for token in [t for t in conn.books if t not in wanted]:
                del conn.books[token]
        for served in (self._leader, self._tops, self._served_books, self._pushed,
                       self._trade_keys, self._trade_order):
            for token in [t for t in served if t not in wanted]:
                served.pop(token, None)

    async def run(self, stop_event: asyncio.Event) -> None:
        tasks = [asyncio.ensure_future(c.stream.run(stop_event)) for c in self._conns]
        if len(self._conns) > 1:
            tasks.append(asyncio.ensure_future(self._supervise(stop_event)))
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def handle_event(self, index: int, event: ClobEvent, received_ms: int) -> None:
        """Apply one event delivered by connection ``index`` (its stream's callback)."""
        conn = self._conns[index]
        if conn.session_seen != conn.stream.session:  # a replacement connection's data
            conn.session_seen = conn.stream.session
            conn.newest_ms = None
        if isinstance(event, PriceChangeEvent):
            self._observe_latency(conn, received_ms - event.ts_ms)
            for change in event.changes:
                book = self._book_for(conn, change.asset_id)
                if book is not None:
                    book.apply(change.side, change.price, change.size, event.ts_ms, received_ms)
                    self._after_apply(conn, book, event.ts_ms, received_ms)
        elif isinstance(event, BookEvent):
            live = not event.snapshot
            if live:
                self._observe_latency(conn, received_ms - event.ts_ms)
            book = self._book_for(conn, event.asset_id)
            if book is not None:
                book.reset(event.bids, event.asks, event.tick_size, event.last_trade_price,
                           event.ts_ms, received_ms)
                self._after_apply(conn, book, event.ts_ms if live else None, received_ms)
        elif isinstance(event, LastTradeEvent):
            self._observe_latency(conn, received_ms - event.ts_ms)
            book = self._book_for(conn, event.asset_id)
            if book is not None:
                book.record_trade(event.price, event.size, event.side, event.ts_ms, received_ms)
                self._after_apply(conn, book, event.ts_ms, received_ms)
                self._push_trade(event)
        elif isinstance(event, TickSizeEvent):
            book = self._book_for(conn, event.asset_id)
            if (book is not None and book.set_tick_size(event.new_tick_size)
                    and self._leader.get(event.asset_id) is conn):
                self._tops[event.asset_id] = book.top()
        else:
            self._on_other(event)

    def _book_for(self, conn: _Conn, token: str) -> OrderBook | None:
        book = conn.books.get(token)
        if book is None and token in self._wanted:
            book = conn.books[token] = OrderBook(token)
        return book

    @staticmethod
    def _observe_latency(conn: _Conn, latency_ms: int) -> None:
        if conn.latency_ms is None:
            conn.latency_ms = float(latency_ms)
        else:
            conn.latency_ms += LATENCY_WEIGHT * (latency_ms - conn.latency_ms)

    @staticmethod
    def _clearly_faster(challenger: _Conn, leader: _Conn) -> bool:
        return challenger.latency_ms is not None and (
            leader.latency_ms is None
            or challenger.latency_ms + TIE_MARGIN_MS < leader.latency_ms)

    def _after_apply(self, conn: _Conn, book: OrderBook, sample_ts: int | None,
                     received_ms: int) -> None:
        """Serve ``book`` if its connection is the freshest; push what is new."""
        token = book.token_id
        ts = book.server_ts_ms
        if ts is not None and (conn.newest_ms is None or ts > conn.newest_ms):
            conn.newest_ms = ts
        fresh = book.freshness
        leader = self._leader.get(token)
        if leader is not conn:
            held = leader.books.get(token) if leader is not None else None
            if held is not None:
                ahead = held.freshness
                if fresh < ahead or (fresh == ahead and not self._clearly_faster(conn, leader)):
                    return
            if leader is not None:
                self._leader_switches += 1
            self._leader[token] = conn
            self._served_books[token] = book
        top = book.top()
        self._tops[token] = top
        key = (top.best_bid, top.best_ask, top.bid_size, top.ask_size)
        pushed = self._pushed.get(token)
        if pushed is not None and (fresh < pushed[0] or (fresh == pushed[0] and key == pushed[1])):
            return  # already served, possibly by another connection
        self._pushed[token] = (fresh, key)
        if ts is not None and (self._newest_served_ms is None or ts > self._newest_served_ms):
            self._newest_served_ms = ts
        if sample_ts is not None:
            self._served_latency.append(received_ms - sample_ts)
        if pushed is None or key != pushed[1]:
            self._on_top(token, top)

    def _push_trade(self, event: LastTradeEvent) -> None:
        token = event.asset_id
        key = (event.ts_ms, event.price, event.size, event.side)
        keys = self._trade_keys.get(token)
        if keys is None:
            keys = self._trade_keys[token] = set()
            self._trade_order[token] = deque()
        if key in keys:
            return
        order = self._trade_order[token]
        keys.add(key)
        order.append(key)
        if len(order) > self._trade_memory:
            keys.discard(order.popleft())
        self._on_trade(event)

    # --- supervision (more than one connection) ------------------------------------------

    async def _supervise(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            await pause(stop_event, self._supervise_s)
            try:
                self._check()
            except Exception as exc:  # noqa: BLE001 — supervision must keep running
                log.warning("marketdata.shard_check_failed", shard=self.name,
                            error=describe_error(exc))

    def _check(self) -> None:
        conns = self._conns
        for conn in conns:  # a replaced connection is back once its stream reopened
            if conn.pending_session is not None and conn.stream.session != conn.pending_session:
                conn.pending_session = None
        if len(conns) < 2:
            return
        known = [c.newest_ms for c in conns if c.newest_ms is not None]
        front = max(known) if known else None
        current = [c for c in conns
                   if c.newest_ms is not None and c.session_seen == c.stream.session]
        fresh = [c for c in current
                 if c.stream.connected and front - c.newest_ms <= self._stall_ms]
        pending = any(c.pending_session is not None for c in conns)
        for conn in conns:
            if conn.pending_session is not None or not conn.stream.connected:
                continue
            others_fresh = any(other is not conn for other in fresh)
            if conn in current:
                lag = front - conn.newest_ms
                if lag > self._stall_ms:
                    if not conn.stalled:
                        conn.stalled = True
                        conn.stall_episodes += 1
                        if others_fresh:
                            self._stalls_avoided += 1
                elif lag <= self._stall_ms / 2:
                    conn.stalled = False
                if lag > self._recycle_lag_ms and others_fresh:
                    self._recycle(conn, lag, "behind the freshest connection")
                    pending = True
                    continue
            if pending:
                continue
            behind = conn.stream.behind_best_ms()
            if (behind is not None and behind > self._own_lag_ms
                    and any(o is not conn and o.stream.connected for o in conns)):
                self._recycle(conn, behind, "behind its own best")
                pending = True

    def _recycle(self, conn: _Conn, lag_ms: float, reason: str) -> None:
        conn.pending_session = conn.stream.session
        conn.recycles += 1
        log.warning("marketdata.clob_recycle", shard=self.name, connection=conn.index,
                    lag_ms=int(round(lag_ms)), reason=reason)
        conn.stream.request_reconnect(f"recycled: {lag_ms / 1000:.1f}s {reason}")
