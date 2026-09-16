"""Reconnecting connection to the Polymarket CLOB market channel (books, trades, lifecycle).

Protocol rules this client follows (live-checked 2026-09-16):

* No auth, no compression. The first frame is ``{"assets_ids": [...], "type": "market",
  "custom_feature_enabled": true}``; later changes go out as ``operation`` frames. A second
  ``type: market`` frame is rejected with ``INVALID OPERATION``.
* ``custom_feature_enabled`` is last-write-wins for the whole connection, so EVERY
  subscribe frame carries it (otherwise ``best_bid_ask``, ``new_market`` and
  ``market_resolved`` silently stop). Unsubscribe frames don't reset it and don't carry it.
* A subscribe frame naming more than 750 tokens gets no snapshot, so frames hold at most
  500. A 600-token snapshot is over 1 MiB, hence the 16 MiB ``max_size``.
* Heartbeat: text ``PING`` every 10 s (``PONG`` comes back). There is no replay: after a
  reconnect the whole subscription is sent again and books rebuild from the snapshots.
* The server closes with 1000 once every subscribed token has resolved. Any close while
  tokens are wanted means reconnect (backoff 1 s doubling to 30 s, with jitter; reset
  once a connection has delivered data).
* A subscription can go silent. No data for 45 s means unsubscribe and subscribe again
  (fresh snapshots); still nothing 45 s later means a new connection.
* A connection can also fall behind when the network can't carry the flow; the server
  only drops it (1013 "slow consumer", or a reset) 20-30 s later. When the median
  latency of the last 64 events is more than 10 s above this connection's best, the
  connection is replaced, which throws the backlog away (a constant clock offset
  raises the best as well, so it never triggers this).
* At 350-900 frames/s the websockets default ``max_queue`` (16) stalls, so the queue is
  large and each frame is handled synchronously and quickly.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import random
import time
from collections import deque
from collections.abc import Callable, Coroutine, Iterable, Iterator
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from logging_setup import get_logger
from polymarket_exec.marketdata.clob_messages import (
    EMPTY_SNAPSHOT,
    NO_NEW_ASSETS,
    PONG,
    BestBidAskEvent,
    BookEvent,
    ClobEvent,
    LastTradeEvent,
    PriceChangeEvent,
    TextFrame,
    TickSizeEvent,
    Unknown,
    parse_frame,
)

log = get_logger("marketdata.clob")

CLOB_MARKET_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
PING_INTERVAL_S = 10.0
SILENCE_RESUBSCRIBE_S = 45.0
MAX_TOKENS_PER_FRAME = 500
INITIAL_BACKOFF_S = 1.0
MAX_BACKOFF_S = 30.0
# A handshake refused with HTTP 429 waits at least this long before the next try.
RATE_LIMITED_BACKOFF_S = 60.0
TICK_S = 0.5  # heartbeat / watchdog check cadence
RATE_WINDOW_S = 10
LATENCY_SAMPLES = 512
MAX_LAG_S = 10.0
LAG_WINDOW = 64  # events in the lag check's median
LAG_FRESH_S = 5.0  # the lag check only runs while events are flowing

# (event, received_ms) — called synchronously for every event in every frame.
EventHandler = Callable[[ClobEvent, int], None]


@dataclass(frozen=True)
class StreamStatus:
    connected: bool
    connected_since: float | None  # start of the current connection
    last_frame_at: float | None  # newest data frame (not PONG / notices)
    last_pong_at: float | None
    frames_total: int
    frames_per_s: float  # data frames over the last 10 whole seconds
    latency_ms_p50: float | None  # receive time minus event time, last 512 frames
    latency_ms_p90: float | None
    reconnects: int
    resyncs: int
    subscribed: int  # tokens on the open socket
    desired: int
    unknown: int  # objects we could not use
    handler_errors: int
    last_error: str | None  # why the last connection ended (kept after reconnecting)
    last_notice: str | None  # INVALID OPERATION / INVALID MESSAGE text from the server


class StreamSilent(Exception):
    """No data even after a resubscribe: the connection is replaced."""


class StreamLagging(Exception):
    """The connection is far behind the market: it is replaced to drop the backlog."""


def _default_connect(url: str) -> Any:
    import websockets

    return websockets.connect(
        url,
        open_timeout=15,
        ping_interval=None,  # text PING/PONG is the channel's heartbeat
        max_size=16 * 2**20,
        max_queue=8192,
        compression=None,
        close_timeout=1,
    )


def _compact(body: dict[str, Any]) -> str:
    return json.dumps(body, separators=(",", ":"))


def subscribe_frame(tokens: Iterable[str], *, first: bool) -> str:
    body: dict[str, Any] = {"assets_ids": list(tokens)}
    if first:
        body["type"] = "market"
    else:
        body["operation"] = "subscribe"
    body["custom_feature_enabled"] = True  # last write wins for the whole connection
    return _compact(body)


def unsubscribe_frame(tokens: Iterable[str]) -> str:
    return _compact({"assets_ids": list(tokens), "operation": "unsubscribe"})


def _chunks(items: list[str], size: int) -> Iterator[list[str]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


def describe_error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:200]


def is_rate_limited(exc: BaseException) -> bool:
    """A handshake refused with HTTP 429 (websockets ``InvalidStatus`` carries the response)."""
    return getattr(getattr(exc, "response", None), "status_code", None) == 429


def percentile(sorted_values: list[float] | list[int], q: float) -> float | None:
    if not sorted_values:
        return None
    return float(sorted_values[min(len(sorted_values) - 1, int(q * len(sorted_values)))])


def copy_deque(values: deque) -> tuple:
    """A copy that is safe to take while the event loop keeps appending."""
    for _ in range(10):
        try:
            return tuple(values)
        except RuntimeError:  # deque mutated during iteration
            continue
    return ()


def _live_ts(event: ClobEvent) -> int | None:
    """Server time of an update (snapshots and announcements are not latency samples)."""
    if isinstance(event, BookEvent):
        return None if event.snapshot else event.ts_ms
    if isinstance(event, (PriceChangeEvent, LastTradeEvent, BestBidAskEvent, TickSizeEvent)):
        return event.ts_ms
    return None


async def run_until_stopped(coro: Coroutine[Any, Any, None], stop_event: asyncio.Event) -> None:
    """Run ``coro``; cancel it (and wait for its cleanup) as soon as ``stop_event`` is set.

    Re-raises the exception that ended ``coro`` on its own.
    """
    task = asyncio.ensure_future(coro)
    stopper = asyncio.ensure_future(stop_event.wait())
    try:
        await asyncio.wait({task, stopper}, return_when=asyncio.FIRST_COMPLETED)
    except BaseException:
        task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task
        raise
    finally:
        stopper.cancel()
    if not task.done():
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        return
    task.result()


async def first_exit(*coros: Coroutine[Any, Any, None]) -> None:
    """Run ``coros`` together until one ends; cancel the rest and re-raise why it ended."""
    tasks = [asyncio.ensure_future(coro) for coro in coros]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    for task in done:
        task.result()


async def pause(stop_event: asyncio.Event, delay: float) -> None:
    """Sleep ``delay`` seconds, or less if ``stop_event`` is set first."""
    with suppress(asyncio.TimeoutError):
        await asyncio.wait_for(stop_event.wait(), timeout=delay)


class Backoff:
    """Reconnect delays: doubling from ``initial_s`` to ``max_s`` with +/-20% jitter.

    A connection that delivered data starts the sequence over; an HTTP 429 handshake
    waits at least ``RATE_LIMITED_BACKOFF_S``.
    """

    def __init__(self, initial_s: float, max_s: float, rng: Callable[[], float]) -> None:
        self._initial_s = initial_s
        self._max_s = max_s
        self._rng = rng
        self._next_s = initial_s

    def delay(self, *, had_data: bool, rate_limited: bool) -> float:
        if had_data:
            self._next_s = self._initial_s
        if rate_limited:
            self._next_s = max(self._next_s, RATE_LIMITED_BACKOFF_S)
        delay = self._next_s * (0.8 + 0.4 * self._rng())
        self._next_s = min(self._next_s * 2, self._max_s)
        return delay


class ClobMarketStream:
    """One connection to the market channel, following whatever token set it is given.

    ``set_tokens`` and ``request_resync`` must be called from the loop running ``run``.
    """

    def __init__(
        self,
        on_event: EventHandler,
        *,
        url: str = CLOB_MARKET_WS,
        connect: Callable[[str], Any] | None = None,
        time_fn: Callable[[], float] = time.time,
        ping_interval_s: float = PING_INTERVAL_S,
        silence_resubscribe_s: float = SILENCE_RESUBSCRIBE_S,
        max_lag_s: float = MAX_LAG_S,
        max_tokens_per_frame: int = MAX_TOKENS_PER_FRAME,
        initial_backoff_s: float = INITIAL_BACKOFF_S,
        max_backoff_s: float = MAX_BACKOFF_S,
        tick_s: float = TICK_S,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self.url = url
        self._on_event = on_event
        self._connect = connect or _default_connect
        self._time_fn = time_fn
        self._ping_interval_s = ping_interval_s
        self._silence_s = silence_resubscribe_s
        self._max_lag_ms = max_lag_s * 1000
        self._max_per_frame = max_tokens_per_frame
        self._backoff = Backoff(initial_backoff_s, max_backoff_s, rng)
        self._tick_s = tick_s
        self._pause = pause
        self._desired: frozenset[str] = frozenset()
        self._changed = asyncio.Event()
        self._resync_requested = False
        # Current connection.
        self._connected = False
        self._connected_since: float | None = None
        self._subscribed: set[str] = set()
        self._opened = False  # the type:market frame has been sent on this socket
        self._quiet_since = 0.0  # last data frame, or when a subscription began
        self._resync_at: float | None = None
        self._last_ping_at = 0.0
        self._session_had_data = False
        self._best_latency_ms: int | None = None  # lowest latency on this connection
        # Lifetime counters.
        self._last_frame_at: float | None = None
        self._last_pong_at: float | None = None
        self._frames_total = 0
        self._buckets: deque[list[int]] = deque(maxlen=RATE_WINDOW_S + 2)
        self._latency: deque[int] = deque(maxlen=LATENCY_SAMPLES)
        self._reconnects = 0
        self._resyncs = 0
        self._unknown = 0
        self._handler_errors = 0
        self._last_error: str | None = None
        self._last_notice: str | None = None

    @property
    def desired(self) -> frozenset[str]:
        return self._desired

    def set_tokens(self, tokens: Iterable[str]) -> None:
        """The tokens to follow; applied to the open socket as subscribe/unsubscribe diffs."""
        desired = frozenset(tokens)
        if desired != self._desired:
            self._desired = desired
            self._changed.set()

    def request_resync(self) -> None:
        """Unsubscribe and subscribe everything again on the next check (fresh snapshots)."""
        self._resync_requested = True

    def latency_samples(self) -> tuple[int, ...]:
        """The recent receive-minus-event latencies (ms), for merging several streams."""
        return copy_deque(self._latency)

    def status(self) -> StreamStatus:
        now_s = int(self._time_fn())
        recent = sum(
            count for second, count in copy_deque(self._buckets)
            if now_s - RATE_WINDOW_S <= second < now_s
        )
        samples = sorted(copy_deque(self._latency))
        return StreamStatus(
            connected=self._connected,
            connected_since=self._connected_since,
            last_frame_at=self._last_frame_at,
            last_pong_at=self._last_pong_at,
            frames_total=self._frames_total,
            frames_per_s=recent / RATE_WINDOW_S,
            latency_ms_p50=percentile(samples, 0.5),
            latency_ms_p90=percentile(samples, 0.9),
            reconnects=self._reconnects,
            resyncs=self._resyncs,
            subscribed=len(self._subscribed) if self._connected else 0,
            desired=len(self._desired),
            unknown=self._unknown,
            handler_errors=self._handler_errors,
            last_error=self._last_error,
            last_notice=self._last_notice,
        )

    async def run(self, stop_event: asyncio.Event) -> None:
        """Hold the connection until ``stop_event``; never raises (except cancellation)."""
        while not stop_event.is_set():
            if not self._desired:
                await self._idle(stop_event)
                continue
            self._session_had_data = False
            rate_limited = False
            try:
                await run_until_stopped(self._serve(), stop_event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — any socket error means reconnect
                self._last_error = describe_error(exc)
                rate_limited = is_rate_limited(exc)
                log.warning("marketdata.clob_disconnected", error=self._last_error)
            finally:
                self._mark_down()
            if stop_event.is_set():
                break
            self._reconnects += 1
            delay = self._backoff.delay(had_data=self._session_had_data,
                                        rate_limited=rate_limited)
            await self._pause(stop_event, delay)

    async def _idle(self, stop_event: asyncio.Event) -> None:
        """Nothing to follow: wait for tokens (or stop) without holding a connection."""
        self._changed.clear()
        if self._desired:
            return
        waiters = [asyncio.ensure_future(self._changed.wait()),
                   asyncio.ensure_future(stop_event.wait())]
        try:
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in waiters:
                waiter.cancel()

    async def _serve(self) -> None:
        async with self._connect(self.url) as ws:
            self._on_open()
            await self._sync(ws)
            await first_exit(self._read(ws), self._housekeep(ws))

    def _on_open(self) -> None:
        now = self._time_fn()
        self._connected, self._connected_since = True, now
        self._subscribed = set()
        self._opened = False
        self._quiet_since = now
        self._resync_at = None
        self._resync_requested = False  # a fresh connection sends everything anyway
        self._last_ping_at = now
        self._latency.clear()  # latency is judged per connection
        self._best_latency_ms = None
        log.info("marketdata.clob_connected", tokens=len(self._desired))

    def _mark_down(self) -> None:
        self._connected, self._connected_since = False, None
        self._subscribed = set()

    async def _sync(self, ws: Any) -> None:
        """Bring the socket's subscription in line with the wanted token set."""
        self._changed.clear()
        desired = self._desired
        removed = sorted(self._subscribed - desired)
        added = sorted(desired - self._subscribed)
        for chunk in _chunks(removed, self._max_per_frame):
            await ws.send(unsubscribe_frame(chunk))
            self._subscribed.difference_update(chunk)
        if added and not self._subscribed:
            self._quiet_since = self._time_fn()  # silence is timed from the subscribe
        for chunk in _chunks(added, self._max_per_frame):
            await ws.send(subscribe_frame(chunk, first=not self._opened))
            self._opened = True
            self._subscribed.update(chunk)

    async def _resync(self, ws: Any, now: float, reason: str) -> None:
        tokens = sorted(self._subscribed)
        if not tokens:
            return
        log.warning("marketdata.clob_resubscribe", reason=reason, tokens=len(tokens))
        for chunk in _chunks(tokens, self._max_per_frame):
            await ws.send(unsubscribe_frame(chunk))
        self._subscribed.clear()
        for chunk in _chunks(sorted(self._desired), self._max_per_frame):
            await ws.send(subscribe_frame(chunk, first=False))
            self._subscribed.update(chunk)
        self._resync_at = now
        self._resyncs += 1

    async def _housekeep(self, ws: Any) -> None:
        while True:
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._changed.wait(), timeout=self._tick_s)
            if self._changed.is_set():
                await self._sync(ws)
            now = self._time_fn()
            if now - self._last_ping_at >= self._ping_interval_s:
                self._last_ping_at = now
                await ws.send("PING")
            if self._resync_requested:
                self._resync_requested = False
                await self._resync(ws, now, "requested")
            await self._watchdog(ws, now)

    async def _watchdog(self, ws: Any, now: float) -> None:
        if not self._subscribed:
            self._resync_at = None
            return
        if self._resync_at is not None and self._quiet_since >= self._resync_at:
            self._resync_at = None  # data came back after the resubscribe
        silent_for = now - self._quiet_since
        if self._resync_at is None:
            if silent_for >= self._silence_s:
                await self._resync(ws, now, f"no data for {silent_for:.0f}s")
        elif now - self._resync_at >= self._silence_s:
            raise StreamSilent(f"no data for {silent_for:.0f}s, even after a resubscribe")
        if silent_for < LAG_FRESH_S and len(self._latency) >= LAG_WINDOW:
            recent = sorted(itertools.islice(reversed(self._latency), LAG_WINDOW))
            behind_ms = recent[LAG_WINDOW // 2] - (self._best_latency_ms or 0)
            if behind_ms > self._max_lag_ms:
                raise StreamLagging(f"{behind_ms / 1000:.0f}s behind this connection's best")

    async def _read(self, ws: Any) -> None:
        on_frame = self._on_frame
        while True:
            on_frame(await ws.recv())

    def _on_frame(self, frame: str | bytes) -> None:
        now = self._time_fn()
        received_ms = int(now * 1000)
        data = False
        sample: int | None = None
        for event in parse_frame(frame):
            if isinstance(event, TextFrame):
                self._on_text(event, now)
                continue
            data = True
            if isinstance(event, Unknown):
                self._unknown += 1
                continue
            if sample is None:
                sample = _live_ts(event)
            try:
                self._on_event(event, received_ms)
            except Exception as exc:  # noqa: BLE001 — a consumer bug must not drop the feed
                self._handler_errors += 1
                if self._handler_errors <= 5:
                    log.warning("marketdata.clob_handler_failed", error=describe_error(exc))
        if not data:
            return
        self._session_had_data = True
        self._frames_total += 1
        self._last_frame_at = now
        self._quiet_since = now
        second = int(now)
        buckets = self._buckets
        if buckets and buckets[-1][0] == second:
            buckets[-1][1] += 1
        else:
            buckets.append([second, 1])
        if sample is not None:
            latency_ms = received_ms - sample
            self._latency.append(latency_ms)
            if self._best_latency_ms is None or latency_ms < self._best_latency_ms:
                self._best_latency_ms = latency_ms

    def _on_text(self, event: TextFrame, now: float) -> None:
        if event.kind == PONG:
            self._last_pong_at = now
        elif event.kind in (NO_NEW_ASSETS, EMPTY_SNAPSHOT):
            return  # subscribed ids we already had / ids the server does not know
        elif event.text[:120] != self._last_notice:
            self._last_notice = event.text[:120]
            log.warning("marketdata.clob_notice", text=self._last_notice)
