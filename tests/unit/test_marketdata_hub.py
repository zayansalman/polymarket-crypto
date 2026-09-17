"""Market-data hub: books and tops from CLOB events, pushed events, cross-thread reads."""
from __future__ import annotations

import asyncio
import dataclasses
import json
import threading
from pathlib import Path

import httpx
import pytest
from websockets.exceptions import ConnectionClosedError

from polymarket_exec.marketdata import clob_messages as cm
from polymarket_exec.marketdata import clob_shard as sh
from polymarket_exec.marketdata import clob_stream as cs
from polymarket_exec.marketdata import hub as hub_mod
from polymarket_exec.marketdata import rtds_stream as rs

# Captured before the autouse conftest fixture swaps ``run`` for an offline stub.
_REAL_RUN = hub_mod.MarketDataHub.run

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "marketdata"
UP = "48347891018346333616777312811599713638650075692149435329595052184345334860240"
DOWN = "43094655420909802468126769661333866266512911139677430764258029764666702009377"
MARKET = "0x607e1f5bb3d34a6588dae7410afd16ab9e25e84cbe22bf1747f5bbb01570d026"
T0 = 1_789_554_461.0  # inside btc-updown-5m-1789554300, when the fixtures were captured
CURRENT = "btc-updown-5m-1789554300"
NEXT = "btc-updown-5m-1789554600"


def _events(name: str) -> list:
    return cm.parse_frame((FIXTURES / name).read_text())


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
        self.exited = False

    async def __aenter__(self) -> FakeWs:
        return self

    async def __aexit__(self, *exc) -> bool:
        self.exited = True
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


def _hub(clock: dict | None = None, **kw) -> hub_mod.MarketDataHub:
    """A hub on the fake venue; btc 5m is wanted (by "test") unless ``pinned`` says else."""
    clock = clock if clock is not None else {"t": T0}
    kw.setdefault("clob_connect", Connector())
    kw.setdefault("rtds_connect", Connector())
    kw.setdefault("assets", ("btc",))
    kw.setdefault("timeframes", ("5m",))
    kw.setdefault("pinned", [(asset, tf, "test") for asset in kw["assets"]
                             for tf in kw["timeframes"]])
    return hub_mod.MarketDataHub(
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(_gamma)),
        time_fn=lambda: clock["t"], **kw,
    )


def _follow(hub: hub_mod.MarketDataHub, *tokens: str) -> None:
    hub._apply_groups({("btc", "5m"): frozenset(tokens)})


def _drain(listener: hub_mod.Listener) -> list:
    out = []
    while (event := listener.get_nowait()) is not None:
        out.append(event)
    return out


def _price_change(changes: list[tuple[str, str, str, str]], ts: int = 1_789_554_462_000) -> str:
    return json.dumps({
        "market": MARKET, "timestamp": str(ts), "event_type": "price_change",
        "price_changes": [{"asset_id": a, "price": p, "size": s, "side": side, "hash": "h",
                           "best_bid": "0", "best_ask": "1"} for a, p, s, side in changes],
    })


@pytest.mark.asyncio
async def test_top_changed_is_pushed_only_when_the_top_moves() -> None:
    hub = _hub()
    _follow(hub, UP, DOWN)
    listener = hub.listen()
    for event in _events("clob_book_snapshot_array.json"):
        hub.handle_clob_event(event, 1_000)
    first = _drain(listener)
    assert [(type(e).__name__, e.token_id) for e in first] == [
        ("TopChanged", UP), ("TopChanged", DOWN)]
    assert first[0].top.best_bid == 0.8 and first[0].top.ask_size == 20.0
    # Both entries of the fixture change deep levels: tops refresh, nothing is pushed.
    hub.handle_clob_event(_events("clob_price_change.json")[0], 2_000)
    assert _drain(listener) == []
    assert hub.top(UP).received_ms == 2_000 and hub.top(UP).best_bid == 0.8
    # A new size at the best bid is a top change for that token only.
    change = cm.parse_frame(_price_change([(UP, "0.8", "300", "BUY"),
                                           (DOWN, "0.2", "300", "SELL")]))[0]
    hub.handle_clob_event(change, 3_000)
    moved = _drain(listener)
    assert [(e.token_id, e.top.bid_size) for e in moved] == [(UP, 300.0)]
    assert hub.levels(DOWN, "ask", 2) == ((0.19, 49.47), (0.2, 300.0))


@pytest.mark.asyncio
async def test_trades_tick_sizes_and_unfollowed_tokens() -> None:
    hub = _hub()
    _follow(hub, UP, DOWN)
    for event in _events("clob_book_snapshot_array.json"):
        hub.handle_clob_event(event, 1_000)
    listener = hub.listen()
    trade = _events("clob_last_trade_price.json")[0]  # a DOWN buy at 0.19
    hub.handle_clob_event(trade, 2_000)
    events = _drain(listener)
    assert events == [hub_mod.Trade(DOWN, 0.19, 10.526316, "BUY", 1_789_554_460_868,
                                    "0x" + "ab" * 32)]
    assert hub.top(DOWN).last_trade_price == 0.19
    tick = cm.TickSizeEvent(MARKET, UP, 0.01, 0.001, 5)
    hub.handle_clob_event(tick, 3_000)
    hub.handle_clob_event(tick, 3_001)  # the channel repeats it
    assert hub.top(UP).tick_size == 0.001 and _drain(listener) == []
    hub.handle_clob_event(cm.BookEvent("m", "stranger", ((0.1, 1.0),), (), 1, "h"), 4_000)
    assert hub.top("stranger") is None and hub.levels("stranger", "bid", 5) is None
    # Unfollowing a token drops its book.
    _follow(hub, DOWN)
    assert hub.top(UP) is None and hub.top(DOWN) is not None


@pytest.mark.asyncio
async def test_reads_return_immutable_objects() -> None:
    clock = {"t": T0}
    hub = _hub(clock)
    try:
        update = await hub._universe.refresh()
    finally:
        await hub._universe.aclose()
    hub._apply_groups(update.groups)
    for event in _events("clob_book_snapshot_array.json"):
        hub.handle_clob_event(event, 1_000)
    quote = hub.quote("btc", "5m")
    assert quote is not None and quote.market.slug == CURRENT
    assert (quote.up.best_ask, quote.down.best_ask) == (0.82, 0.19)
    assert hub.quote("btc", "5m", "next").up is None  # followed, no book yet
    assert hub.quote("eth", "5m") is None
    assert hub.market("btc", "5m", "next").slug == NEXT
    rtds_frame = (FIXTURES / "rtds_chainlink_update.json").read_text()
    hub._rtds.handle_frame(rtds_frame)
    point = hub.price(rs.CHAINLINK, "btc")
    assert point is not None and hub.prices(rs.CHAINLINK, "btc") == (point,)
    for value, field in ((quote, "up"), (quote.up, "best_bid"), (quote.market, "slug"),
                         (point, "value")):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(value, field, None)
    assert isinstance(hub.levels(UP, "bid", 3), tuple)


def test_a_listener_on_another_threads_loop_gets_every_event_in_order() -> None:
    hub = _hub()
    ready, done = threading.Event(), threading.Event()
    got: list = []

    def worker() -> None:
        async def consume() -> None:
            listener = hub.listen()
            ready.set()
            async for event in listener:
                got.append(event)
                if len(got) == 200:
                    break
            listener.close()

        asyncio.run(consume())
        done.set()

    thread = threading.Thread(target=worker)
    thread.start()
    assert ready.wait(2)
    for i in range(200):
        hub._emit(hub_mod.Trade("tok", 0.5, 1.0, "BUY", i))
    assert done.wait(5)
    thread.join(2)
    assert [e.ts_ms for e in got] == list(range(200))
    assert hub.snapshot().listeners == 0


@pytest.mark.asyncio
async def test_a_full_listener_drops_the_oldest_events_and_counts_them() -> None:
    hub = _hub()
    listener = hub.listen(maxsize=3)
    for i in range(5):
        hub._emit(hub_mod.Trade("tok", 0.5, 1.0, "BUY", i))
    assert listener.dropped == 2
    assert [e.ts_ms for e in _drain(listener)] == [2, 3, 4]
    assert hub.snapshot().listener_drops == 2
    listener.close()
    hub._emit(hub_mod.Trade("tok", 0.5, 1.0, "BUY", 9))
    assert listener.get_nowait() is None
    assert (hub.snapshot().listener_drops, hub.snapshot().listeners) == (2, 0)


@pytest.mark.asyncio
async def test_closing_a_listener_ends_its_iteration() -> None:
    hub = _hub()
    listener = hub.listen()
    received: list = []

    async def consume() -> None:
        async for event in listener:
            received.append(event)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.01)
    hub._emit(hub_mod.Trade("tok", 0.5, 1.0, "BUY", 1))
    await asyncio.sleep(0.01)
    listener.close()
    await asyncio.wait_for(task, timeout=1)
    assert [e.ts_ms for e in received] == [1]


async def until(pred, timeout: float = 3.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not pred():
        if loop.time() > end:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.005)


def _binance_print(obs_s: float) -> str:
    return json.dumps({"connection_id": "c", "topic": "crypto_prices", "type": "update",
                       "timestamp": int(obs_s * 1000) + 300,
                       "payload": {"symbol": "btcusdt", "timestamp": int(obs_s * 1000),
                                   "value": 75964.0, "full_accuracy_value": "75964"}})


@pytest.mark.asyncio
async def test_run_wires_the_universe_both_streams_and_the_listeners() -> None:
    clob, rtds = Connector(), Connector()
    hub = _hub(clob_connect=clob, rtds_connect=rtds)
    listener = hub.listen()
    stop = asyncio.Event()
    task = asyncio.create_task(_REAL_RUN(hub, stop))
    try:
        await until(lambda: len(clob.made) == 2 and all(ws.sent for ws in clob.made)
                    and rtds.made and rtds.made[0].sent)
        assert clob.made[0].sent == clob.made[1].sent  # two connections, same subscription
        first = json.loads(clob.made[0].sent[0])
        assert first["type"] == "market" and set(first["assets_ids"]) == {
            UP, DOWN, f"{NEXT}:up", f"{NEXT}:down"}
        assert rtds.made[0].sent == [rs.subscribe_message(("btc",))]
        clob.made[0].incoming.put_nowait((FIXTURES / "clob_book_snapshot_array.json").read_text())
        rtds.made[0].incoming.put_nowait(_binance_print(T0 - 2))
        resolved = json.loads((FIXTURES / "clob_market_resolved.json").read_text())
        resolved.update(market=MARKET, assets_ids=[UP, DOWN], winning_asset_id=DOWN,
                        winning_outcome="Down")
        clob.made[0].incoming.put_nowait(json.dumps(resolved))
        await until(lambda: hub.snapshot().clob.frames_total == 2)
        for frame in ((FIXTURES / "clob_book_snapshot_array.json").read_text(),
                      json.dumps(resolved)):
            clob.made[1].incoming.put_nowait(frame)  # the same frames, later
        await until(lambda: hub.price(rs.BINANCE, "btc") is not None
                    and hub.snapshot().clob.frames_total == 4)
        snap = hub.snapshot()
        assert (snap.markets, snap.tokens, snap.subscribed, snap.gamma_lookups) == (2, 4, 4, 2)
        assert snap.gamma_errors == 0 and snap.listeners == 1
        assert snap.clob.connected and snap.prices[rs.BINANCE].connected
        assert snap.price_ages[rs.BINANCE] == pytest.approx(2.0)
        assert snap.price_ages[rs.CHAINLINK] is None
        assert hub.top(UP) is not None and hub.top(UP).best_ask == 0.82
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=2)
    events = _drain(listener)
    kinds = [type(e).__name__ for e in events]
    assert kinds[0] == "WindowOpened" and events[0].market.slug == CURRENT
    assert kinds.count("TopChanged") == 2 and kinds.count("PriceTick") == 1
    resolved_events = [e for e in events if isinstance(e, hub_mod.MarketResolved)]
    assert len(resolved_events) == 1 and resolved_events[0].market.slug == CURRENT
    assert listener.closed is True  # the hub closes its listeners when it stops
    assert hub.snapshot().clob.connected is False


@pytest.mark.asyncio
async def test_reads_say_when_no_connection_serves_them() -> None:
    first, second = FakeWs(), FakeWs()
    clob = Connector([first, second])
    hub = _hub(clob_connect=clob, hedge={})
    stream = hub._shards["btc-5m"]._conns[0].stream
    stream._backoff = cs.Backoff(0.05, 0.05, lambda: 0.5)
    snapshot = (FIXTURES / "clob_book_snapshot_array.json").read_text()
    stop = asyncio.Event()
    task = asyncio.create_task(_REAL_RUN(hub, stop))
    try:
        await until(lambda: first.sent)
        first.incoming.put_nowait(snapshot)
        await until(lambda: hub.top(UP) is not None)
        quote = hub.quote("btc", "5m")
        assert quote.live and quote.up.live and quote.down.live
        assert hub.quote("btc", "5m", "next").live is False  # no books yet
        first.incoming.put_nowait(ConnectionClosedError(None, None))  # the socket drops
        await until(lambda: not stream.connected)
        stale = hub.quote("btc", "5m")
        assert stale.live is False and stale.up.live is False and stale.down.live is False
        assert (stale.up.best_bid, stale.up.best_ask) == (0.8, 0.82)  # the last values
        await until(lambda: second.sent)  # a new connection, before its snapshot
        assert hub.top(UP).live is False and hub.quote("btc", "5m").live is False
        books = json.loads(snapshot)
        for book in books:
            if book["asset_id"] == UP:
                book["bids"].append({"price": "0.81", "size": "5"})  # a better bid
        second.incoming.put_nowait(json.dumps(books))
        await until(lambda: hub.top(UP).live)
        fresh = hub.quote("btc", "5m")
        assert fresh.live and (fresh.up.best_bid, fresh.up.bid_size) == (0.81, 5.0)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=2)


@pytest.mark.asyncio
async def test_windows_roll_on_time_while_a_lookup_hangs() -> None:
    clock = {"t": T0}
    release = asyncio.Event()

    async def gamma(request: httpx.Request) -> httpx.Response:
        if "15m" in request.url.params["slug"]:
            await release.wait()  # Gamma hangs on the 15-minute windows
        return _gamma(request)

    hub = hub_mod.MarketDataHub(
        ("btc",), ("5m", "15m"), hedge={}, clob_connect=Connector(), rtds_connect=Connector(),
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(gamma)),
        time_fn=lambda: clock["t"], refresh_s=0.01,
        pinned=[("btc", "5m", "test"), ("btc", "15m", "test")])
    listener = hub.listen()
    stop = asyncio.Event()
    task = asyncio.create_task(_REAL_RUN(hub, stop))
    try:
        await until(lambda: hub.market("btc", "5m", "next") is not None)
        assert hub.market("btc", "15m") is None
        clock["t"] = 1_789_554_600 + 1  # the next 5m window starts
        await until(lambda: hub.market("btc", "5m").slug == NEXT)
        assert {UP, f"{NEXT}:up", f"{NEXT}:down"} <= hub._shards["btc-5m"].desired
        opened = [e.market.slug for e in _drain(listener)
                  if isinstance(e, hub_mod.WindowOpened)]
        assert opened == [CURRENT, NEXT]
    finally:
        release.set()
        stop.set()
        await asyncio.wait_for(task, timeout=2)


@pytest.mark.asyncio
async def test_stop_does_not_wait_for_gamma_lookups() -> None:
    asked = asyncio.Event()

    async def hanging_gamma(request: httpx.Request) -> httpx.Response:
        asked.set()
        await asyncio.sleep(3600)  # the real client waits up to 10 s per read
        raise AssertionError("unreachable")

    hub = hub_mod.MarketDataHub(
        ("btc", "eth"), ("5m", "15m", "1h", "1d"), hedge={}, clob_connect=Connector(),
        rtds_connect=Connector(), time_fn=lambda: T0,
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(hanging_gamma)),
        pinned=[(a, tf, "test") for a in ("btc", "eth") for tf in ("5m", "15m", "1h", "1d")])
    stop = asyncio.Event()
    task = asyncio.create_task(_REAL_RUN(hub, stop))
    await asyncio.wait_for(asked.wait(), timeout=2)
    loop = asyncio.get_running_loop()
    began = loop.time()
    stop.set()
    await asyncio.wait_for(task, timeout=3)
    assert loop.time() - began < 1.0
    assert hub.snapshot().gamma_lookups == 4  # four were in flight
    assert hub._universe._client is None  # and the HTTP client was closed


@pytest.mark.asyncio
async def test_market_resolved_names_the_window() -> None:
    clock = {"t": T0}
    hub = _hub(clock)
    try:
        update = await hub._universe.refresh()
    finally:
        await hub._universe.aclose()
    hub._apply_groups(update.groups)
    listener = hub.listen()
    resolved = cm.MarketResolvedEvent("1", MARKET, (UP, DOWN), DOWN, "Down", 5, ("5M",))
    hub.handle_clob_event(resolved, 6)
    events = _drain(listener)
    assert len(events) == 1 and isinstance(events[0], hub_mod.MarketResolved)
    assert events[0].market.slug == CURRENT
    assert (events[0].winning_token, events[0].winning_outcome) == (DOWN, "Down")
    # Still inside its window, so it stays followed until 30 s after the end.
    shard = hub._shards["btc-5m"]
    assert UP in shard.desired
    clock["t"] = 1_789_554_600 + 31
    hub._apply_groups(hub._universe.groups())
    assert UP not in shard.desired and f"{NEXT}:up" in shard.desired


@pytest.mark.asyncio
async def test_each_asset_timeframe_gets_its_own_socket() -> None:
    clob = Connector()
    hub = _hub(clob_connect=clob, timeframes=("5m", "15m"), hedge={})
    stop = asyncio.Event()
    task = asyncio.create_task(_REAL_RUN(hub, stop))
    try:
        await until(lambda: len(clob.made) == 2 and all(ws.sent for ws in clob.made))
        sent = sorted(json.loads(ws.sent[0])["assets_ids"] for ws in clob.made)
        fifteen = ["btc-updown-15m-1789553700", "btc-updown-15m-1789554600"]
        assert sent == [
            sorted([UP, DOWN, f"{NEXT}:up", f"{NEXT}:down"]),
            sorted([f"{fifteen[0]}:down", f"{fifteen[0]}:up",
                    f"{fifteen[1]}:down", f"{fifteen[1]}:up"]),
        ]
        # Events reach the books whichever socket delivered them.
        clob.made[0].incoming.put_nowait(
            (FIXTURES / "clob_book_snapshot_array.json").read_text())
        clob.made[1].incoming.put_nowait(
            (FIXTURES / "clob_book_snapshot_array.json").read_text())
        await until(lambda: hub.top(UP) is not None)
        snap = hub.snapshot()
        assert set(snap.clob_shards) == {"btc-5m", "btc-15m"}
        assert all(len(s.connections) == 1 for s in snap.clob_shards.values())
        assert snap.clob.connected is True and snap.subscribed == 8
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=2)


def _conn(**kw) -> cs.StreamStatus:
    base = dict(connected=True, connected_since=100.0, last_frame_at=150.0,
                last_pong_at=None, frames_total=10, frames_per_s=2.0, latency_ms_p50=None,
                latency_ms_p90=None, reconnects=1, resyncs=0, subscribed=4, desired=4,
                unknown=0, handler_errors=0, last_error=None, last_notice=None,
                bytes_total=1000)
    base.update(kw)
    return cs.StreamStatus(**base)


def _group(name: str, *conns: cs.StreamStatus, p50=None, p90=None, top=None,
           desired: int = 4, **kw) -> sh.ShardStatus:
    base = dict(name=name, connections=conns,
                connected=sum(1 for c in conns if c.connected), desired=desired,
                served_latency_ms_p50=p50, served_latency_ms_p90=p90,
                served_latency_ms_max=top, served_staleness_s=None, leader_switches=0,
                stalls_avoided=0, recycles=0, stall_episodes=tuple(0 for _ in conns))
    base.update(kw)
    return sh.ShardStatus(**base)


def test_shard_statuses_merge_into_one_feed_status() -> None:
    shards = {
        "btc-5m": _group("btc-5m", _conn(last_frame_at=160.0, frames_total=30,
                                         frames_per_s=5.5),
                         _conn(connected=False, connected_since=None, subscribed=0,
                               last_error="recycled: 3.2s behind the freshest connection"),
                         p50=120.0, p90=300.0, top=900.0),
        "eth-5m": _group("eth-5m", _conn(connected_since=90.0, resyncs=2,
                                         last_notice="INVALID OPERATION"),
                         p50=4_000.0, p90=4_500.0, top=5_000.0),
        "sol-1d": _group("sol-1d", _conn(connected=False, connected_since=None,
                                         subscribed=0, last_error="OSError: reset"),
                         p50=90.0, p90=95.0, top=99.0),
        "bnb-1h": _group("bnb-1h", _conn(connected=False, connected_since=None,
                                         subscribed=0, desired=0, last_error="old"),
                         p50=9_999.0, desired=0),  # wants nothing: ignored
    }
    merged = hub_mod.merge_shard_status(shards)
    assert merged.connected is False  # sol-1d has no connection up
    assert merged.last_error == "sol-1d: OSError: reset"
    assert (merged.frames_total, merged.frames_per_s, merged.reconnects, merged.resyncs) == (
        70, 13.5, 5, 2)
    assert (merged.subscribed, merged.desired, merged.bytes_total) == (8, 12, 5000)
    assert (merged.last_frame_at, merged.connected_since) == (160.0, 90.0)
    # The worst market's served latency, not the worst single connection's.
    assert (merged.latency_ms_p50, merged.latency_ms_p90, merged.latency_ms_max) == (
        4_000.0, 4_500.0, 5_000.0)
    assert merged.last_notice == "INVALID OPERATION"
    assert hub_mod.slowest_shard(shards) == "eth-5m"
    healthy = hub_mod.merge_shard_status({k: v for k, v in shards.items() if k != "sol-1d"})
    assert healthy.connected is True and healthy.last_error is None  # btc-5m still serves
    assert hub_mod.merge_shard_status({}).connected is False
    assert hub_mod.slowest_shard({}) is None


def test_hedge_counts() -> None:
    def counts(hub: hub_mod.MarketDataHub) -> dict[str, int]:
        return {name: shard.connections for name, shard in hub._shards.items()
                if shard.connections > 1}

    assert counts(hub_mod.MarketDataHub()) == {"btc-5m": 2, "btc-15m": 2, "btc-1h": 2}
    assert counts(hub_mod.MarketDataHub(hedge={"btc": 2})) == {
        "btc-5m": 2, "btc-15m": 2, "btc-1h": 2, "btc-1d": 2}
    assert counts(hub_mod.MarketDataHub(hedge={("eth", "5m"): 3, "sol-1h": 2})) == {
        "eth-5m": 3, "sol-1h": 2}
    assert counts(hub_mod.MarketDataHub(hedge={})) == {}
    assert hub_mod.hedge_count({"btc": 0}, "btc", "5m") == 1


@pytest.mark.asyncio
async def test_a_resolution_from_two_connections_is_pushed_once() -> None:
    hub = _hub()
    try:
        update = await hub._universe.refresh()
    finally:
        await hub._universe.aclose()
    hub._apply_groups(update.groups)
    listener = hub.listen()
    resolved = cm.MarketResolvedEvent("1", MARKET, (UP, DOWN), DOWN, "Down", 5, ("5M",))
    shard = hub._shards["btc-5m"]
    shard.handle_event(0, resolved, 6)
    shard.handle_event(1, resolved, 7)
    assert [type(e).__name__ for e in _drain(listener)] == ["MarketResolved"]


def test_snapshot_before_running() -> None:
    hub = _hub()
    snap = hub.snapshot()
    assert snap.taken_at == T0 and snap.started_at == T0
    assert (snap.markets, snap.tokens, snap.subscribed, snap.listeners) == (0, 0, 0, 0)
    assert snap.clob.connected is False and set(snap.clob_shards) == {"btc-5m"}
    assert len(snap.clob_shards["btc-5m"].connections) == 2 and snap.slowest_shard is None
    assert set(snap.prices) == set(rs.SOURCES)
    assert all(age is None for age in snap.price_ages.values())


def test_default_grid_and_registry() -> None:
    hub = hub_mod.MarketDataHub()
    assert hub.assets == ("btc", "eth", "sol", "xrp", "doge", "bnb")
    assert hub.timeframes == ("5m", "15m", "1h", "1d")
    assert len(hub._shards) == 24  # one market-channel socket group per asset x timeframe
    assert sum(s.connections for s in hub._shards.values()) == 27  # busy BTC ones doubled
    hub_mod.set_current(hub)
    try:
        assert hub_mod.current() is hub
    finally:
        hub_mod.set_current(None)
    assert hub_mod.current() is None


# --- demand: only the markets someone uses are streamed ------------------------------

SNAPSHOT = (FIXTURES / "clob_book_snapshot_array.json").read_text()


def test_want_is_idempotent_and_release_drops_demand() -> None:
    hub = _hub(pinned=(), timeframes=("5m", "1h"))
    assert hub.wanted() == {}
    assert hub.grid == (("btc", "5m"), ("btc", "1h"))
    demand = hub.want("btc", "5m", "bot loop")
    assert demand == hub.want("btc", "5m", "bot loop")  # the same claim
    assert (demand.asset, demand.timeframe, demand.owner) == ("btc", "5m", "bot loop")
    hub.want("btc", "5m", "order ticket")
    hub.want("btc", "1h", "bot loop")
    assert hub.wanted() == {("btc", "5m"): {"bot loop", "order ticket"},
                            ("btc", "1h"): {"bot loop"}}
    demand.release()
    demand.release()  # twice is harmless
    assert hub.wanted()[("btc", "5m")] == {"order ticket"}
    assert hub.release("bot loop") == 1  # the rest of its demand: btc 1h
    assert hub.release("bot loop") == 0
    assert hub.wanted() == {("btc", "5m"): {"order ticket"}}
    with hub.want("btc", "1h", "panel") as held:
        assert held.owner == "panel" and hub.wanted()[("btc", "1h")] == {"panel"}
    assert ("btc", "1h") not in hub.wanted()
    hub.want("btc", "1h", "order ticket")
    assert hub.release("order ticket", timeframe="1h") == 1
    assert hub.release("order ticket", asset="eth") == 0
    assert hub.release("order ticket", asset="btc") == 1
    assert hub.wanted() == {}
    view = hub.wanted()
    with pytest.raises(TypeError):
        view[("btc", "5m")] = frozenset({"x"})  # read-only
    for bad in (("hype", "5m", "x"), ("btc", "4h", "x"), ("btc", "5m", ""),
                ("btc", "5m", "  ")):
        with pytest.raises(ValueError):
            hub.want(*bad)


def test_pinned_demand_stays() -> None:
    hub = _hub(pinned=[("btc", "5m", "dashboard")])
    assert hub.wanted() == {("btc", "5m"): {"dashboard"}}
    assert hub.release("dashboard") == 0
    hub.want("btc", "5m", "dashboard").release()  # a no-op for pinned demand
    hub.want("btc", "5m", "strategy")
    assert hub.release("strategy") == 1
    assert hub.wanted() == {("btc", "5m"): {"dashboard"}}
    with pytest.raises(ValueError):
        _hub(pinned=[("btc", "4h", "dashboard")])


@pytest.mark.asyncio
async def test_nothing_is_streamed_or_looked_up_until_a_market_is_wanted() -> None:
    clob, rtds = Connector(), Connector()
    hub = _hub(clob_connect=clob, rtds_connect=rtds, pinned=())
    stop = asyncio.Event()
    task = asyncio.create_task(_REAL_RUN(hub, stop))
    try:
        await until(lambda: rtds.made and rtds.made[0].sent)  # prices are always on
        await asyncio.sleep(0.05)
        assert clob.made == [] and hub.snapshot().gamma_lookups == 0
        assert hub.quote("btc", "5m") is None and hub.market("btc", "5m") is None
        assert hub.top(UP) is None and hub.levels(UP, "bid", 5) is None
        hub.want("btc", "5m", "strategy")  # applied at once, not at the next 2 s tick
        await until(lambda: len(clob.made) == 2 and all(ws.sent for ws in clob.made))
        assert hub.snapshot().gamma_lookups == 2  # btc 5m current and next only
        clob.made[0].incoming.put_nowait(SNAPSHOT)
        await until(lambda: hub.top(UP) is not None)
        assert hub.quote("btc", "5m").live and hub.levels(UP, "bid", 1) == ((0.8, 282.0),)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=2)


@pytest.mark.asyncio
async def test_a_market_can_be_wanted_from_another_threads_event_loop() -> None:
    clob = Connector()
    hub = _hub(clob_connect=clob, pinned=(), hedge={})
    stop = asyncio.Event()
    task = asyncio.create_task(_REAL_RUN(hub, stop))
    demands: list = []

    def strategy_thread() -> None:
        async def main() -> None:
            demands.append(hub.want("btc", "5m", "bot loop"))

        asyncio.run(main())

    try:
        await asyncio.sleep(0.02)
        thread = threading.Thread(target=strategy_thread)
        thread.start()
        await asyncio.to_thread(thread.join, 2)
        await until(lambda: clob.made and clob.made[0].sent)
        assert hub.wanted() == {("btc", "5m"): {"bot loop"}}
        released = threading.Thread(target=demands[0].release)
        released.start()
        await asyncio.to_thread(released.join, 2)
        assert hub.wanted() == {}
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=2)


@pytest.mark.asyncio
async def test_a_released_market_lingers_then_stops_and_drops_its_books() -> None:
    clock = {"t": T0}
    clob = Connector()
    hub = _hub(clock, clob_connect=clob, pinned=(), hedge={}, refresh_s=0.01,
               demand_linger_s=60.0)
    stop = asyncio.Event()
    task = asyncio.create_task(_REAL_RUN(hub, stop))
    try:
        demand = hub.want("btc", "5m", "strategy")
        await until(lambda: clob.made and clob.made[0].sent)
        socket = clob.made[0]
        socket.incoming.put_nowait(SNAPSHOT)
        await until(lambda: hub.top(UP) is not None)
        demand.release()
        clock["t"] = T0 + 30
        hub.want("btc", "5m", "strategy")  # back within the linger: no churn
        demand.release()
        clock["t"] = T0 + 89  # 59 s after the last release: still streaming
        await asyncio.sleep(0.05)
        assert not socket.exited and len(clob.made) == 1
        assert hub.quote("btc", "5m").live and hub.levels(UP, "bid", 1) == ((0.8, 282.0),)
        clock["t"] = T0 + 90
        await until(lambda: socket.exited)
        assert hub.top(UP) is None and hub.top(DOWN) is None
        assert hub.levels(UP, "bid", 5) is None and hub.quote("btc", "5m") is None
        assert hub.market("btc", "5m") is None
        assert hub._shards["btc-5m"].desired == frozenset()
        await asyncio.sleep(0.05)
        assert len(clob.made) == 1  # stopped, not reconnecting
        snap = hub.snapshot()
        assert (snap.markets, snap.tokens, snap.subscribed, snap.clob.reconnects) == (0, 0, 0, 0)
        hub.want("btc", "5m", "strategy")  # wanted again: tokens known, a new socket
        await until(lambda: len(clob.made) == 2 and clob.made[1].sent)
        assert hub.snapshot().gamma_lookups == 2
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=2)


def test_dashboard_lifespan_registers_and_clears_the_hub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import db as _db

    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "t.db")
    from fastapi.testclient import TestClient

    from polymarket_exec.ops.dashboard.app import app

    with TestClient(app) as client:
        assert isinstance(hub_mod.current(), hub_mod.MarketDataHub)
        page = client.get("/").text
        assert "Polymarket books" in page and "Chainlink 60s TWAP" in page  # FEEDS card
    assert hub_mod.current() is None
