"""The fresh REST /book poll that races the sockets on the markets in use."""
from __future__ import annotations

import asyncio

import httpx
import pytest

from polymarket_exec.marketdata import rest_poll as rp
from polymarket_exec.marketdata.order_book import REST, STREAM, TopOfBook

TOKEN = "tok-up"
OTHER = "tok-down"
T0 = 1_789_554_461.0


def _reply(ts_ms: int, *, book_hash: str = "h1", bid: str = "0.80", ask: str = "0.82") -> dict:
    return {  # levels come worst-to-best, so the best one is last
        "market": "0xabc", "asset_id": TOKEN, "timestamp": str(ts_ms), "hash": book_hash,
        "min_order_size": "5", "tick_size": "0.01",
        "bids": [{"price": "0.10", "size": "1"}, {"price": bid, "size": "11"}],
        "asks": [{"price": "0.99", "size": "2"}, {"price": ask, "size": "22"}],
    }


def _streamed(ts_ms: int | None, *, book_hash: str | None = "h0", live: bool = True) -> TopOfBook:
    return TopOfBook(TOKEN, 0.79, 0.83, 5.0, 6.0, 0.01, None, ts_ms, 1_000, live=live,
                     source=STREAM, book_hash=book_hash)


class Venue:
    """A CLOB /book endpoint under our control, counting requests in flight."""

    def __init__(self) -> None:
        self.replies: dict[str, object] = {}
        self.calls: list[str] = []
        self.in_flight = 0
        self.max_in_flight = 0
        self.gate: asyncio.Event | None = None

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self._handle))

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        token = request.url.params["token_id"]
        self.calls.append(token)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.gate is not None:
                await self.gate.wait()
            reply = self.replies.get(token, _reply(1_789_554_462_000))
            if isinstance(reply, int):
                return httpx.Response(reply, json={})
            return httpx.Response(200, json=reply)
        finally:
            self.in_flight -= 1


@pytest.fixture
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lift the poll-rate cap so these tests finish in milliseconds."""
    monkeypatch.setattr(rp, "MAX_POLL_HZ", 500.0)


def _poller(venue: Venue, served, clock: dict, **kw) -> rp.BookPoller:
    kw.setdefault("rate_hz", 500.0)
    return rp.BookPoller(served=served, client_factory=venue.client,
                         time_fn=lambda: clock["t"], **kw)


async def _run(poller: rp.BookPoller, steps) -> None:
    stop = asyncio.Event()
    task = asyncio.create_task(poller.run(stop))
    try:
        await steps()
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=2)


async def until(pred, timeout: float = 3.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not pred():
        if loop.time() > end:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_a_reply_newer_than_the_sockets_wins_and_carries_the_book(fast: None) -> None:
    venue, clock = Venue(), {"t": T0}
    venue.replies[TOKEN] = _reply(1_789_554_462_000)
    poller = _poller(venue, lambda token: _streamed(1_789_554_461_000), clock)

    async def steps() -> None:
        poller.set_tokens({TOKEN})
        await until(lambda: poller.top(TOKEN) is not None)

    await _run(poller, steps)
    top = poller.top(TOKEN)
    assert (top.best_bid, top.best_ask, top.bid_size, top.ask_size) == (0.80, 0.82, 11.0, 22.0)
    assert (top.source, top.live, top.book_hash) == (REST, True, "h1")
    assert (top.server_ts_ms, top.tick_size) == (1_789_554_462_000, 0.01)
    status = poller.status()
    assert status.ahead >= 1 and status.behind == 0 and status.same == 0
    assert status.ahead_ms_p50 == 1000.0 and status.rtt_ms_p50 is not None


@pytest.mark.asyncio
async def test_a_reply_the_sockets_are_already_past_does_not_win(fast: None) -> None:
    venue, clock = Venue(), {"t": T0}
    venue.replies[TOKEN] = _reply(1_789_554_461_000)
    poller = _poller(venue, lambda token: _streamed(1_789_554_462_000), clock)

    async def steps() -> None:
        poller.set_tokens({TOKEN})
        await until(lambda: poller.status().behind >= 2)

    await _run(poller, steps)
    assert poller.status().ahead == 0


@pytest.mark.asyncio
async def test_the_same_book_is_not_newer_however_it_is_stamped(fast: None) -> None:
    venue, clock = Venue(), {"t": T0}
    venue.replies[TOKEN] = _reply(1_789_554_465_000, book_hash="same")
    poller = _poller(venue, lambda token: _streamed(1_789_554_461_000, book_hash="same"), clock)

    async def steps() -> None:
        poller.set_tokens({TOKEN})
        await until(lambda: poller.status().same >= 2)

    await _run(poller, steps)
    assert poller.status().ahead == 0


@pytest.mark.asyncio
async def test_a_token_with_no_live_socket_book_is_always_a_win(fast: None) -> None:
    venue, clock = Venue(), {"t": T0}
    poller = _poller(venue, lambda token: None, clock)

    async def steps() -> None:
        poller.set_tokens({TOKEN})
        await until(lambda: poller.top(TOKEN) is not None)

    await _run(poller, steps)
    assert poller.status().ahead >= 1


@pytest.mark.asyncio
async def test_one_request_in_flight_per_token(fast: None) -> None:
    venue, clock = Venue(), {"t": T0}
    poller = _poller(venue, lambda token: None, clock)

    async def steps() -> None:
        venue.gate = asyncio.Event()
        poller.set_tokens({TOKEN, OTHER})
        await until(lambda: venue.in_flight == 2)
        await asyncio.sleep(0.05)  # many poll intervals with both requests held open
        assert venue.max_in_flight == 2  # one each, never a second for the same token
        venue.gate.set()
        await until(lambda: len(venue.calls) > 4)

    await _run(poller, steps)
    assert set(venue.calls) == {TOKEN, OTHER}


@pytest.mark.asyncio
async def test_a_bad_reply_backs_off_and_the_poll_keeps_running(fast: None) -> None:
    venue, clock = Venue(), {"t": T0}
    venue.replies[TOKEN] = 429
    poller = _poller(venue, lambda token: None, clock, backoff_s=0.01)

    async def steps() -> None:
        poller.set_tokens({TOKEN})
        await until(lambda: poller.status().errors >= 2)
        venue.replies[TOKEN] = _reply(1_789_554_462_000)
        await until(lambda: poller.top(TOKEN) is not None)

    await _run(poller, steps)
    status = poller.status()
    assert status.errors >= 2 and status.last_error and "429" in status.last_error


@pytest.mark.asyncio
async def test_dropping_a_token_stops_its_poll_and_forgets_its_book(fast: None) -> None:
    venue, clock = Venue(), {"t": T0}
    poller = _poller(venue, lambda token: None, clock)

    async def steps() -> None:
        poller.set_tokens({TOKEN})
        await until(lambda: poller.top(TOKEN) is not None)
        poller.set_tokens(set())
        await until(lambda: poller.status().tokens == 0)
        assert poller.top(TOKEN) is None
        seen = len(venue.calls)
        await asyncio.sleep(0.05)  # many poll intervals
        assert len(venue.calls) == seen

    await _run(poller, steps)


def test_the_poll_rate_is_capped() -> None:
    venue, clock = Venue(), {"t": T0}
    assert _poller(venue, lambda token: None, clock, rate_hz=100.0).rate_hz == rp.MAX_POLL_HZ
    assert _poller(venue, lambda token: None, clock, rate_hz=0.0).rate_hz > 0
