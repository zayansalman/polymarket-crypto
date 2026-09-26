"""One asset x timeframe's market-channel connections: N redundant sockets, the freshest served.

Live on 2026-09-16, one connection to a busy market sometimes fell seconds behind while
a second connection to the same tokens stayed current, and the stalls were independent.
So busy markets run more than one connection:

* every connection keeps its own books, and events are never applied across connections;
* reads serve the connection whose book is furthest along the event stream (newest
  server time, then events applied at that millisecond); an exact tie goes to a
  connection that is clearly faster (recent latency more than 25 ms lower);
* a new connection starts with no books (it rebuilds from its snapshot). When the
  serving connection drops or is replaced, its tokens move to the freshest connection
  that is still up; with none, reads keep the last values with ``live=False``;
* top changes and trades are pushed once, by whichever connection delivers them first.
  Nothing behind what was already pushed goes out. A trade is known by (token, time,
  price, size, side, transaction hash), and alike trades are counted per connection
  (one transaction can fill several makers at the same price and size);
* a connection more than 3 s behind the freshest one is replaced while another fresh
  connection serves. So is a new connection that has applied no book data (not even its
  snapshot) after 3 s, once the freshest one has moved more than 3 s on. If every
  connection is 10 s behind its own best, one is replaced at a time. A connection is
  never replaced while no other one is up.

With one connection nothing changes: the stream's own rule (replace when 10 s behind
its best) applies and every top change and trade is pushed.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from functools import partial
from typing import Any

from ems.logging_setup import get_logger
from ems.marketdata.clob_messages import (
    BookEvent,
    ClobEvent,
    LastTradeEvent,
    PriceChangeEvent,
    TickSizeEvent,
)
from ems.marketdata.clob_stream import (
    MAX_LAG_S,
    ClobMarketStream,
    StreamStatus,
    copy_deque,
    describe_error,
    pause,
    percentile,
)
from ems.marketdata.order_book import Level, OrderBook, TopOfBook

log = get_logger("marketdata.shard")

RECYCLE_LAG_S = 3.0  # behind the freshest connection
STALL_S = 1.0  # a connection this far behind the freshest one is stalled
SUPERVISE_S = 0.5
TIE_MARGIN_MS = 25.0
LATENCY_WEIGHT = 0.05  # recent-latency smoothing per event
SERVED_SAMPLES = 512
TRADE_MEMORY = 512  # recent trade keys remembered per token (and per connection)

TopKey = tuple[Any, Any, Any, Any]  # best bid, best ask, bid size, ask size
Freshness = tuple[int, int]
NOTHING_PUSHED: Freshness = (-1, 0)
TradeKey = tuple[int, float, float, str, str]  # time, price, size, side, transaction hash


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
    bytes_per_s: float = 0.0  # every connection together, over the last 10 whole seconds


class _Conn:
    __slots__ = ("index", "stream", "books", "newest_ms", "session_seen", "latency_ms",
                 "stalled", "stall_episodes", "recycles", "pending_session", "opened_at",
                 "opened_front", "trades_seen")

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
        self.opened_at: float | None = None  # when this session was first noticed
        self.opened_front: int | None = None  # the freshest connection's time by then
        # token -> trade key -> times this session delivered it
        self.trades_seen: dict[str, dict[TradeKey, int]] = {}


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
        self._trades_pushed: dict[str, dict[TradeKey, int]] = {}  # token -> key -> pushes
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
        """The served top; ``live`` is False while no connection that is up serves it."""
        top = self._tops.get(token_id)
        if top is None or self.live(token_id):
            return top
        return replace(top, live=False)

    def live(self, token_id: str) -> bool:
        """The token is served by a connection that is up, from data it received."""
        conn = self._leader.get(token_id)
        if conn is None or not conn.stream.connected or conn.session_seen != conn.stream.session:
            return False
        book = conn.books.get(token_id)
        return book is not None and book is self._served_books.get(token_id)

    def levels(self, token_id: str, side: str, n: int) -> tuple[Level, ...]:
        """The served book's levels (the last ones seen when ``live`` is False)."""
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
            bytes_per_s=sum(st.bytes_per_s for st in conns),
        )

    # --- the hub's loop ------------------------------------------------------------------

    def set_tokens(self, tokens: Iterable[str]) -> None:
        """Follow ``tokens``; books of the others are dropped. With none, the connections
        close and the served-latency record starts over for the next time."""
        wanted = frozenset(tokens)
        self._wanted = wanted
        if not wanted:
            self._served_latency.clear()
            self._newest_served_ms = None
        for conn in self._conns:
            conn.stream.set_tokens(wanted)
            for held in (conn.books, conn.trades_seen):
                for token in [t for t in held if t not in wanted]:
                    del held[token]
        for served in (self._leader, self._tops, self._served_books, self._pushed,
                       self._trades_pushed):
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
            self._start_session(conn)
        if isinstance(event, PriceChangeEvent):
            self._observe_latency(conn, received_ms - event.ts_ms)
            for change in event.changes:
                book = self._book_for(conn, change.asset_id)
                if book is not None:
                    book.apply(change.side, change.price, change.size, event.ts_ms,
                               received_ms, change.hash)
                    self._after_apply(conn, book, event.ts_ms, received_ms)
        elif isinstance(event, BookEvent):
            live = not event.snapshot
            if live:
                self._observe_latency(conn, received_ms - event.ts_ms)
            book = self._book_for(conn, event.asset_id)
            if book is not None:
                book.reset(event.bids, event.asks, event.tick_size, event.last_trade_price,
                           event.ts_ms, received_ms, event.hash)
                self._after_apply(conn, book, event.ts_ms if live else None, received_ms)
        elif isinstance(event, LastTradeEvent):
            self._observe_latency(conn, received_ms - event.ts_ms)
            book = self._book_for(conn, event.asset_id)
            if book is not None:
                book.record_trade(event.price, event.size, event.side, event.ts_ms, received_ms)
                self._after_apply(conn, book, event.ts_ms, received_ms)
                self._push_trade(conn, event)
        elif isinstance(event, TickSizeEvent):
            book = self._book_for(conn, event.asset_id)
            if (book is not None and book.set_tick_size(event.new_tick_size)
                    and self._leader.get(event.asset_id) is conn):
                self._tops[event.asset_id] = book.top()
        else:
            self._on_other(event)

    def _start_session(self, conn: _Conn) -> None:
        """``conn`` is a new connection: forget what the previous one applied."""
        conn.session_seen = conn.stream.session
        conn.newest_ms = None
        conn.books = {}  # swapped whole: it rebuilds from its snapshot
        conn.trades_seen = {}  # there is no replay: its trades are all new
        conn.opened_at = self._time_fn()
        conn.opened_front = self._front()
        for token in [t for t, leader in self._leader.items() if leader is conn]:
            self._hand_over(token)

    @staticmethod
    def _serves(conn: _Conn, token: str) -> bool:
        """``conn`` is up and holds a book for ``token`` from its current connection."""
        return (conn.stream.connected and conn.session_seen == conn.stream.session
                and token in conn.books)

    def _hand_over(self, token: str) -> None:
        """The serving connection dropped or was replaced: serve the freshest connection
        that is still up. With none, the last values stay (reads say they are not live)
        and the next connection with data serves; its first top counts as news."""
        best: tuple[_Conn, OrderBook] | None = None
        for conn in self._conns:
            book = conn.books.get(token) if self._serves(conn, token) else None
            if book is not None and (best is None or book.freshness > best[1].freshness):
                best = (conn, book)
        if best is None:
            self._leader.pop(token, None)
            pushed = self._pushed.get(token)
            if pushed is not None:
                self._pushed[token] = (NOTHING_PUSHED, pushed[1])
            return
        self._take_over(token, *best)
        self._publish(best[1], None, None)

    def _front(self) -> int | None:
        """The newest server time applied by any connection, in its current session."""
        known = [c.newest_ms for c in self._conns
                 if c.newest_ms is not None and c.session_seen == c.stream.session]
        return max(known) if known else None

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
        leader = self._leader.get(token)
        if leader is not conn:
            held = (leader.books.get(token)
                    if leader is not None and leader.session_seen == leader.stream.session
                    else None)
            if held is not None:
                fresh, ahead = book.freshness, held.freshness
                if fresh < ahead or (fresh == ahead and not self._clearly_faster(conn, leader)):
                    return
            self._take_over(token, conn, book)
        self._publish(book, sample_ts, received_ms)

    def _take_over(self, token: str, conn: _Conn, book: OrderBook) -> None:
        if token in self._leader:
            self._leader_switches += 1
        self._leader[token] = conn
        self._served_books[token] = book

    def _publish(self, book: OrderBook, sample_ts: int | None, received_ms: int | None) -> None:
        """Serve ``book``'s top; push it if it is new."""
        token = book.token_id
        ts = book.server_ts_ms
        fresh = book.freshness
        top = book.top()
        self._tops[token] = top
        key = (top.best_bid, top.best_ask, top.bid_size, top.ask_size)
        pushed = self._pushed.get(token)
        if pushed is not None and (fresh < pushed[0] or (fresh == pushed[0] and key == pushed[1])):
            return  # already served, possibly by another connection
        self._pushed[token] = (fresh, key)
        if ts is not None and (self._newest_served_ms is None or ts > self._newest_served_ms):
            self._newest_served_ms = ts
        if sample_ts is not None and received_ms is not None:
            self._served_latency.append(received_ms - sample_ts)
        if pushed is None or key != pushed[1]:
            self._on_top(token, top)

    def _push_trade(self, conn: _Conn, event: LastTradeEvent) -> None:
        """Push the n-th delivery of a trade key by ``conn`` unless some connection
        already pushed that key n times."""
        token = event.asset_id
        key = (event.ts_ms, event.price, event.size, event.side, event.transaction_hash)
        seen = conn.trades_seen.get(token)
        if seen is None:
            seen = conn.trades_seen[token] = {}
        count = seen.get(key, 0) + 1
        self._remember(seen, key, count)
        pushed = self._trades_pushed.get(token)
        if pushed is None:
            pushed = self._trades_pushed[token] = {}
        if count <= pushed.get(key, 0):
            return
        self._remember(pushed, key, count)
        self._on_trade(event)

    def _remember(self, counts: dict[TradeKey, int], key: TradeKey, count: int) -> None:
        counts[key] = count  # keys keep their first-seen order
        if len(counts) > self._trade_memory:
            del counts[next(iter(counts))]

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
        now = self._time_fn()
        for conn in conns:  # notice a new connection before (or without) its first event
            if conn.session_seen != conn.stream.session:
                self._start_session(conn)
        for token in [t for t, leader in self._leader.items() if not self._serves(leader, t)]:
            self._hand_over(token)  # its connection dropped
        front = self._front()
        for conn in conns:
            if conn.opened_at is None:
                conn.opened_at = now
            if conn.opened_front is None:
                conn.opened_front = front
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
            else:
                moved = self._moved_on_without(conn, front, now)
                if moved is not None and others_fresh:
                    self._recycle(conn, moved, "without book data since it connected")
                    pending = True
                    continue
            if pending:
                continue
            behind = conn.stream.behind_best_ms()
            if (behind is not None and behind > self._own_lag_ms
                    and any(o is not conn and o.stream.connected for o in conns)):
                self._recycle(conn, behind, "behind its own best")
                pending = True

    def _moved_on_without(self, conn: _Conn, front: int | None, now: float) -> int | None:
        """How far (ms) the freshest connection moved on while ``conn``, a connection with
        no book data yet, waited: only once that and the wait itself exceed the recycle
        lag. Our clock gives the snapshot time to arrive; a jump of the front (a stale
        snapshot stamp followed by a live event) alone is not enough."""
        if front is None or conn.opened_front is None or conn.opened_at is None:
            return None
        moved = front - conn.opened_front
        waited_ms = (now - conn.opened_at) * 1000
        if moved > self._recycle_lag_ms and waited_ms > self._recycle_lag_ms:
            return moved
        return None

    def _recycle(self, conn: _Conn, lag_ms: float, reason: str) -> None:
        conn.pending_session = conn.stream.session
        conn.recycles += 1
        log.warning("marketdata.clob_recycle", shard=self.name, connection=conn.index,
                    lag_ms=int(round(lag_ms)), reason=reason)
        conn.stream.request_reconnect(f"recycled: {lag_ms / 1000:.1f}s {reason}")
