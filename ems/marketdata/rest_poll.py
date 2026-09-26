"""A light REST /book poll running alongside the sockets on the markets in use.

The market channel is normally first: racing a WebSocket against polling on one
clock, the stream showed each book state first in 437 of 440 samples at 5 polls/s
and 1094 of 1157 at ~20 polls/s (median 8-22 ms earlier). But "normally" is not
"always" — a single connection can stall for seconds — so a market that something
is actually trading also gets a REST read a couple of times a second, and whichever
source holds the newer book wins (:meth:`MarketDataHub.book_top`).

A reply is only newer when its CONTENT is: the book ``hash`` says the sockets have
already applied exactly this book, and only then does the snapshot timestamp
decide. Server stamps are not a clock we own — this machine runs ~66 ms behind
them — so they order books against each other and nothing else.

Guard rails: one request in flight per token (a token's poll is one task), a
doubling backoff on any bad reply, and nothing at all for a token no longer
wanted. At 2 reads/s over the four tokens of two markets this is 8 requests/s,
far under Cloudflare's 15,000 per 10 s.

Observation only: nothing here decides or gates trades.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

import httpx

from ems.config import POLYMARKET_CLOB_API
from ems.logging_setup import get_logger
from ems.marketdata.clob_stream import (
    copy_deque,
    describe_error,
    nap,
    pause,
    percentile,
)
from ems.marketdata.order_book import REST, TopOfBook

log = get_logger("marketdata.rest_poll")

POLL_HZ = 2.0  # reads per second per token
MAX_POLL_HZ = 5.0
MIN_POLL_HZ = 0.1
BACKOFF_S = 1.0  # after a bad reply; doubles up to MAX_BACKOFF_S
MAX_BACKOFF_S = 30.0
RECONCILE_S = 1.0  # how often the poll re-checks which tokens it should follow
REQUEST_TIMEOUT_S = 5.0
SAMPLES = 256  # round trips and "ahead by" measurements kept


@dataclass(frozen=True)
class PollStatus:
    tokens: int  # tokens being polled
    polls: int  # replies read
    errors: int
    ahead: int  # replies holding a book the sockets had not applied
    behind: int  # the sockets were already past it
    same: int  # the very same book (same hash)
    ahead_ms_p50: float | None  # by how much, when it was ahead
    rtt_ms_p50: float | None
    rate_hz: float
    last_error: str | None


def _default_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S)


class BookPoller:
    """Polls ``/book`` for the hot tokens and keeps each token's last read.

    ``served`` answers with the top the sockets serve for a token (None when none
    does), which is what a reply is judged against. ``set_tokens``, ``run`` and the
    polls belong to one event loop; ``top`` and ``status`` are safe from any thread.
    """

    def __init__(
        self,
        *,
        served: Callable[[str], TopOfBook | None],
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
        time_fn: Callable[[], float] = time.time,
        rate_hz: float = POLL_HZ,
        backoff_s: float = BACKOFF_S,
    ) -> None:
        self._served = served
        self._client_factory = client_factory or _default_client
        self._time_fn = time_fn
        self.rate_hz = min(MAX_POLL_HZ, max(MIN_POLL_HZ, float(rate_hz)))
        self._backoff_s = backoff_s
        self._client: httpx.AsyncClient | None = None
        self._wanted: frozenset[str] = frozenset()
        self._tops: dict[str, TopOfBook] = {}  # swapped entries: safe to read anywhere
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._wake = asyncio.Event()
        self._rtt_ms: deque[float] = deque(maxlen=SAMPLES)
        self._ahead_ms: deque[float] = deque(maxlen=SAMPLES)
        self._polls = 0
        self._errors = 0
        self._ahead = 0
        self._behind = 0
        self._same = 0
        self._last_error: str | None = None

    @property
    def interval_s(self) -> float:
        return 1.0 / self.rate_hz

    # --- reads (any thread) ------------------------------------------------------------

    def top(self, token_id: str) -> TopOfBook | None:
        """This token's last REST read, or None when it is not polled (or not read yet)."""
        return self._tops.get(token_id)

    def status(self) -> PollStatus:
        return PollStatus(
            tokens=len(self._tasks),
            polls=self._polls,
            errors=self._errors,
            ahead=self._ahead,
            behind=self._behind,
            same=self._same,
            ahead_ms_p50=percentile(sorted(copy_deque(self._ahead_ms)), 0.5),
            rtt_ms_p50=percentile(sorted(copy_deque(self._rtt_ms)), 0.5),
            rate_hz=self.rate_hz,
            last_error=self._last_error,
        )

    # --- the hub's loop ------------------------------------------------------------------

    def set_tokens(self, tokens: Iterable[str]) -> None:
        """Poll exactly these tokens; a token that goes is forgotten at once."""
        wanted = frozenset(tokens)
        if wanted == self._wanted:
            return
        self._wanted = wanted
        for token in [t for t in self._tops if t not in wanted]:
            del self._tops[token]
        self._wake.set()

    async def run(self, stop_event: asyncio.Event) -> None:
        self._client = self._client_factory()
        try:
            while not stop_event.is_set():
                self._wake.clear()
                self._reconcile(stop_event)
                await nap(stop_event, self._wake, RECONCILE_S)
        finally:
            tasks = list(self._tasks.values())
            self._tasks.clear()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            client, self._client = self._client, None
            if client is not None:
                await client.aclose()

    def _reconcile(self, stop_event: asyncio.Event) -> None:
        """One polling task per wanted token — no more, no fewer."""
        for token in [t for t, task in self._tasks.items() if task.done()]:
            del self._tasks[token]
        for token in [t for t in self._tasks if t not in self._wanted]:
            self._tasks.pop(token).cancel()
        for token in self._wanted - set(self._tasks):
            self._tasks[token] = asyncio.ensure_future(self._poll_forever(token, stop_event))

    async def _poll_forever(self, token: str, stop_event: asyncio.Event) -> None:
        """One token's poll: one request at a time, backing off while replies are bad."""
        fails = 0
        while not stop_event.is_set():
            if await self._poll_once(token):
                fails = 0
                await pause(stop_event, self.interval_s)
            else:
                fails += 1
                await pause(stop_event, min(MAX_BACKOFF_S, self._backoff_s * 2 ** (fails - 1)))

    async def _poll_once(self, token: str) -> bool:
        client = self._client
        if client is None:
            return False
        started = self._time_fn()
        try:
            reply = await client.get(f"{POLYMARKET_CLOB_API}/book",
                                     params={"token_id": token})
            reply.raise_for_status()
            data = reply.json()
        except Exception as exc:  # noqa: BLE001 — the poll must outlive any bad read
            self._errors += 1
            self._last_error = describe_error(exc)
            log.warning("marketdata.rest_poll_failed", token=token[:16],
                        error=self._last_error)
            return False
        now = self._time_fn()
        self._polls += 1
        self._rtt_ms.append((now - started) * 1000)
        if isinstance(data, dict):
            self._observe(token, data, int(now * 1000))
        return True

    def _observe(self, token: str, data: dict[str, Any], received_ms: int) -> None:
        """Judge one reply against what the sockets serve, and keep it."""
        top = _to_top(token, data, received_ms)
        streamed = self._served(token)
        streamed_ts = None if streamed is None else streamed.server_ts_ms
        if streamed is None or not streamed.live:
            self._ahead += 1  # nothing live to be behind
        elif top.book_hash is not None and top.book_hash == streamed.book_hash:
            self._same += 1  # the very same book, whatever the stamps say
        elif top.server_ts_ms is not None and top.server_ts_ms > (streamed_ts or -1):
            self._ahead += 1
            self._ahead_ms.append(float(top.server_ts_ms - (streamed_ts or 0)))
        else:
            self._behind += 1
        if token in self._wanted:  # it may have been dropped while this was in flight
            self._tops[token] = top


def _to_top(token_id: str, data: dict[str, Any], received_ms: int) -> TopOfBook:
    best_bid, bid_size = _best(data.get("bids"))
    best_ask, ask_size = _best(data.get("asks"))
    book_hash = str(data.get("hash") or "")
    return TopOfBook(
        token_id=token_id,
        best_bid=best_bid,
        best_ask=best_ask,
        bid_size=bid_size,
        ask_size=ask_size,
        tick_size=_number(data.get("tick_size")),
        last_trade_price=_number(data.get("last_trade_price")),
        server_ts_ms=_stamp(data.get("timestamp")),
        received_ms=received_ms,
        live=True,
        source=REST,
        book_hash=book_hash or None,
    )


def _best(levels: Any) -> tuple[float | None, float | None]:
    """(price, size) of the best level — the LAST element, books list worst-to-best."""
    if not isinstance(levels, list) or not levels:
        return None, None
    best = levels[-1]
    if not isinstance(best, dict):
        return None, None
    return _number(best.get("price")), _number(best.get("size"))


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _stamp(value: Any) -> int | None:
    number = _number(value)
    return None if number is None else int(number)
