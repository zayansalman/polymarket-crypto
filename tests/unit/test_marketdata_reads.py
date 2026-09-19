"""What a strategy calls: ``book_top`` (freshest book, never a stale one) and ``wait_ready``."""
from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import httpx
import pytest
from websockets.exceptions import ConnectionClosedError

from polymarket_exec.marketdata import hub as hub_mod
from polymarket_exec.marketdata import rest_poll
from polymarket_exec.marketdata.order_book import REST

# Captured before the autouse conftest fixture swaps ``run`` for an offline stub.
_REAL_RUN = hub_mod.MarketDataHub.run

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "marketdata"
UP = "48347891018346333616777312811599713638650075692149435329595052184345334860240"
DOWN = "43094655420909802468126769661333866266512911139677430764258029764666702009377"
MARKET = "0x607e1f5bb3d34a6588dae7410afd16ab9e25e84cbe22bf1747f5bbb01570d026"
T0 = 1_789_554_461.0  # inside btc-updown-5m-1789554300, when the fixtures were captured
CURRENT = "btc-updown-5m-1789554300"
SNAPSHOT = (FIXTURES / "clob_book_snapshot_array.json").read_text()


def _gamma(request: httpx.Request) -> httpx.Response:
    slug = request.url.params["slug"]
    tokens = [UP, DOWN] if slug == CURRENT else [f"{slug}:up", f"{slug}:down"]
    return httpx.Response(200, json=[{"slug": slug, "conditionId": MARKET if slug == CURRENT
                                      else f"cond:{slug}", "outcomes": '["Up", "Down"]',
                                      "clobTokenIds": json.dumps(tokens)}])


class FakeWs:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.incoming: asyncio.Queue = asyncio.Queue()

    async def __aenter__(self) -> FakeWs:
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def send(self, text: str) -> None:
        self.sent.append(text)

    async def recv(self) -> str:
        item = await self.incoming.get()
        if isinstance(item, BaseException):
            raise item
        return item


class Connector:
    def __init__(self, sockets: list[FakeWs] | None = None) -> None:
        self.made: list[FakeWs] = []
        self.sockets = list(sockets or [])

    def __call__(self, url: str) -> FakeWs:
        self.made.append(self.sockets.pop(0) if self.sockets else FakeWs())
        return self.made[-1]


def _hub(clock: dict, **kw) -> hub_mod.MarketDataHub:
    kw.setdefault("clob_connect", Connector())
    kw.setdefault("rtds_connect", Connector())
    kw.setdefault("hedge", {})
    kw.setdefault("client_factory",
                  lambda: httpx.AsyncClient(transport=httpx.MockTransport(_gamma)))
    return hub_mod.MarketDataHub(
        assets=("btc",), timeframes=("5m",), pinned=[("btc", "5m", "test")],
        time_fn=lambda: clock["t"], **kw,
    )


async def until(pred, timeout: float = 3.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not pred():
        if loop.time() > end:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_book_top_serves_the_live_stream_and_counts_the_read() -> None:
    clock = {"t": T0}
    socket = FakeWs()
    hub = _hub(clock, clob_connect=Connector([socket]))
    stop = asyncio.Event()
    task = asyncio.create_task(_REAL_RUN(hub, stop))
    try:
        await until(lambda: socket.sent)
        socket.incoming.put_nowait(SNAPSHOT)
        await until(lambda: hub.book_top(UP) is not None)
        top = hub.book_top(UP, max_age_s=2.0)
        assert (top.best_bid, top.best_ask) == (0.8, 0.82)
        assert top.source == hub_mod.STREAM and top.live is True
        assert hub.book_top(DOWN).best_ask == 0.19
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=2)
    reads = hub.snapshot().reads
    assert reads.from_stream >= 3 and reads.from_rest == 0
    assert reads.reads == reads.from_stream + reads.from_rest + reads.missing + reads.stale


@pytest.mark.asyncio
async def test_book_top_never_serves_a_book_that_is_not_live_or_is_too_old() -> None:
    clock = {"t": T0}
    first, second = FakeWs(), FakeWs()
    hub = _hub(clock, clob_connect=Connector([first, second]))
    stream = hub._shards["btc-5m"]._conns[0].stream
    stop = asyncio.Event()
    task = asyncio.create_task(_REAL_RUN(hub, stop))
    try:
        await until(lambda: first.sent)
        first.incoming.put_nowait(SNAPSHOT)
        await until(lambda: hub.book_top(UP) is not None)
        clock["t"] = T0 + 5  # the book is now 5 s old on our own clock
        assert hub.book_top(UP, max_age_s=2.0) is None
        assert hub.book_top(UP) is not None  # no limit asked for: still served
        clock["t"] = T0
        first.incoming.put_nowait(ConnectionClosedError(None, None))
        await until(lambda: not stream.connected)
        assert hub.top(UP) is not None and hub.top(UP).live is False  # last values kept
        assert hub.book_top(UP) is None  # ... but never served as a fresh read
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=2)
    reads = hub.snapshot().reads
    assert reads.stale == 1 and reads.missing >= 1  # the poll loop also missed


def test_book_top_of_a_market_nobody_streams_is_none() -> None:
    hub = _hub({"t": T0})
    assert hub.book_top(UP) is None
    assert hub.snapshot().reads.missing == 1


@pytest.mark.asyncio
async def test_wait_ready_returns_true_once_the_books_are_live_and_false_on_timeout(
) -> None:
    clock = {"t": T0}
    socket = FakeWs()
    hub = _hub(clock, clob_connect=Connector([socket]))
    stop = asyncio.Event()
    task = asyncio.create_task(_REAL_RUN(hub, stop))
    try:
        assert await hub.wait_ready("btc", "5m", 0.05) is False  # no books yet
        await until(lambda: socket.sent)
        socket.incoming.put_nowait(SNAPSHOT)
        assert await hub.wait_ready("btc", "5m", 2.0) is True
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=2)


@pytest.mark.asyncio
async def test_the_module_level_helpers_do_nothing_without_a_hub() -> None:
    assert hub_mod.current() is None
    assert hub_mod.book_top(UP) is None
    assert hub_mod.want("btc", "5m", "bot loop") is None
    assert hub_mod.release("bot loop") == 0
    assert await hub_mod.wait_ready("btc", "5m", 0.01) is False


# --- the fresh REST poll on the markets in use ----------------------------------------

@pytest.fixture
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lift the poll-rate cap so these tests finish in milliseconds."""
    monkeypatch.setattr(rest_poll, "MAX_POLL_HZ", 500.0)


class Venue:
    """Gamma windows and CLOB books from one transport, with the /book reads counted."""

    def __init__(self, ts_ms: int = 1_789_554_461_000, book_hash: str = "rest") -> None:
        self.books: list[str] = []
        self.ts_ms = ts_ms
        self.hash = book_hash

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self._handle))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/book"):
            token = request.url.params["token_id"]
            self.books.append(token)
            return httpx.Response(200, json={
                "asset_id": token, "timestamp": str(self.ts_ms), "hash": self.hash,
                "bids": [{"price": "0.55", "size": "9"}],
                "asks": [{"price": "0.56", "size": "8"}],
            })
        return _gamma(request)


@pytest.mark.asyncio
async def test_only_hot_wanted_markets_are_polled_over_rest(fast: None) -> None:
    venue = Venue()
    hub = _hub({"t": T0}, client_factory=venue.client, poll_hz=200.0)
    stop = asyncio.Event()
    task = asyncio.create_task(_REAL_RUN(hub, stop))
    try:
        await until(lambda: hub.market("btc", "5m") is not None)
        await asyncio.sleep(0.05)
        assert hub.snapshot().rest_poll.tokens == 0  # wanted, but nobody trades it
        assert venue.books == []
        hub.want("btc", "5m", "bot loop", hot=True)
        await until(lambda: set(venue.books) == {UP, DOWN})
        assert hub.hot() == frozenset({("btc", "5m")})
        assert hub.snapshot().grid["btc-5m"].hot is True
        hub.release("bot loop")
        await until(lambda: hub.snapshot().rest_poll.tokens == 0)
        seen = len(venue.books)
        await asyncio.sleep(0.05)
        assert len(venue.books) == seen  # the poll stopped with the demand
        assert hub.book_top(UP) is None  # and its read went with it
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=2)


@pytest.mark.asyncio
async def test_book_top_falls_back_to_the_rest_read_when_the_sockets_drop(
    fast: None,
) -> None:
    clock = {"t": T0}
    first, second = FakeWs(), FakeWs()
    venue = Venue(ts_ms=1_789_554_470_000)  # later than the snapshot's stamp
    hub = _hub(clock, clob_connect=Connector([first, second]), client_factory=venue.client,
               poll_hz=200.0)
    stream = hub._shards["btc-5m"]._conns[0].stream
    stop = asyncio.Event()
    task = asyncio.create_task(_REAL_RUN(hub, stop))
    try:
        hub.want("btc", "5m", "bot loop", hot=True)
        await until(lambda: first.sent)
        first.incoming.put_nowait(SNAPSHOT)
        await until(lambda: venue.books and hub.top(UP) is not None)
        first.incoming.put_nowait(ConnectionClosedError(None, None))
        await until(lambda: not stream.connected)
        top = hub.book_top(UP)
        assert top is not None and top.source == REST
        assert (top.best_bid, top.best_ask) == (0.55, 0.56)
        assert hub.snapshot().reads.from_rest >= 1
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=2)
    assert hub.snapshot().rest_poll.ahead >= 1


# --- the BTC loop's book reads (polymarket_bot/paper.py) ------------------------------

def _clob_rest(calls: list[str], bids: str = "0.70", asks: str = "0.75") -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.params["token_id"])
        return httpx.Response(200, json={  # CLOB books list levels worst-to-best
            "bids": [{"price": "0.10", "size": "1"}, {"price": bids, "size": "11"}],
            "asks": [{"price": "0.90", "size": "2"}, {"price": asks, "size": "22"}],
        })

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_the_loop_reads_the_hub_and_skips_rest_when_it_serves_the_token() -> None:
    from polymarket_bot import paper

    clock = {"t": T0}
    socket = FakeWs()
    hub = _hub(clock, clob_connect=Connector([socket]))
    hub_mod.set_current(hub)
    stop = asyncio.Event()
    task = asyncio.create_task(_REAL_RUN(hub, stop))
    calls: list[str] = []
    try:
        await until(lambda: socket.sent)
        socket.incoming.put_nowait(SNAPSHOT)
        await until(lambda: hub.book_top(UP) is not None)
        async with _clob_rest(calls) as client:
            book = await paper._fetch_clob_book(client, UP)
            clock["t"] = T0 + 5  # the hub's book is now older than the loop allows
            fallback = await paper._fetch_clob_book(client, UP)
    finally:
        hub_mod.set_current(None)
        stop.set()
        await asyncio.wait_for(task, timeout=2)
    assert isinstance(book, paper.BookTop)
    assert (book.best_bid, book.best_ask) == (0.8, 0.82)  # the streamed book
    assert (book.bid_size, book.ask_size) == (282.0, 20.0)
    assert (fallback.best_bid, fallback.best_ask) == (0.70, 0.75)  # the REST read
    assert calls == [UP]  # only the stale read went to the venue
    reads = hub.snapshot().reads
    assert reads.from_stream >= 1 and reads.stale == 1


@pytest.mark.asyncio
async def test_the_loop_reads_rest_without_a_hub_and_for_an_empty_token() -> None:
    from polymarket_bot import paper

    calls: list[str] = []
    async with _clob_rest(calls) as client:
        book = await paper._fetch_clob_book(client, UP)
        empty = await paper._fetch_clob_book(client, "")
    assert (book.best_bid, book.best_ask, book.bid_size) == (0.70, 0.75, 11.0)
    assert empty is paper.EMPTY_BOOK
    assert calls == [UP]


@pytest.mark.asyncio
async def test_the_loop_wants_its_market_while_it_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import db as _db
    from polymarket_bot import paper

    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "loop.db")
    await _db.init_db()
    paper.set_shared_chainlink_feed(object())  # no WS feed of its own for this run
    demand: list[tuple] = []
    monkeypatch.setattr(hub_mod, "want",
                        lambda *a, **k: demand.append(("want", *a, k.get("hot"))))
    monkeypatch.setattr(hub_mod, "release", lambda *a: demand.append(("release", *a)))
    stop = threading.Event()
    stop.set()  # one pass through the loop's setup and teardown
    try:
        await paper.run_paper_loop(stop, mode="paper")
    finally:
        paper.set_shared_chainlink_feed(None)
    assert demand == [("want", "btc", "5m", "bot loop", True),
                      ("release", "bot loop")]


def test_the_module_level_helpers_use_the_registered_hub() -> None:
    hub = _hub({"t": T0})
    hub_mod.set_current(hub)
    try:
        demand = hub_mod.want("btc", "5m", "bot loop")
        assert demand is not None and ("btc", "5m") in hub.wanted()
        assert "bot loop" in hub.wanted()[("btc", "5m")]
        assert hub_mod.book_top(UP) is None  # no book yet, but no crash either
        assert hub_mod.release("bot loop") == 1
        assert "bot loop" not in hub.wanted().get(("btc", "5m"), frozenset())
    finally:
        hub_mod.set_current(None)
