"""Market-data hub: books and tops from CLOB events, pushed events, cross-thread reads."""
from __future__ import annotations

import asyncio
import dataclasses
import json
import threading
from pathlib import Path

import httpx
import pytest

from polymarket_exec.marketdata import clob_messages as cm
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

    async def __aenter__(self) -> FakeWs:
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def send(self, text: str) -> None:
        self.sent.append(text)

    async def recv(self) -> str:
        return await self.incoming.get()


class Connector:
    def __init__(self) -> None:
        self.made: list[FakeWs] = []

    def __call__(self, url: str) -> FakeWs:
        self.made.append(FakeWs())
        return self.made[-1]


def _hub(clock: dict | None = None, **kw) -> hub_mod.MarketDataHub:
    clock = clock if clock is not None else {"t": T0}
    kw.setdefault("clob_connect", Connector())
    kw.setdefault("rtds_connect", Connector())
    return hub_mod.MarketDataHub(
        assets=("btc",), timeframes=("5m",),
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(_gamma)),
        time_fn=lambda: clock["t"], **kw,
    )


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
    hub._apply_tokens({UP, DOWN})
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
    hub._apply_tokens({UP, DOWN})
    for event in _events("clob_book_snapshot_array.json"):
        hub.handle_clob_event(event, 1_000)
    listener = hub.listen()
    trade = _events("clob_last_trade_price.json")[0]  # a DOWN buy at 0.19
    hub.handle_clob_event(trade, 2_000)
    events = _drain(listener)
    assert events == [hub_mod.Trade(DOWN, 0.19, 10.526316, "BUY", 1_789_554_460_868)]
    assert hub.top(DOWN).last_trade_price == 0.19
    tick = cm.TickSizeEvent(MARKET, UP, 0.01, 0.001, 5)
    hub.handle_clob_event(tick, 3_000)
    hub.handle_clob_event(tick, 3_001)  # the channel repeats it
    assert hub.top(UP).tick_size == 0.001 and _drain(listener) == []
    hub.handle_clob_event(cm.BookEvent("m", "stranger", ((0.1, 1.0),), (), 1, "h"), 4_000)
    assert hub.top("stranger") is None and hub.levels("stranger", "bid", 5) == ()
    # Unfollowing a token drops its book.
    hub._apply_tokens({DOWN})
    assert hub.top(UP) is None and hub.top(DOWN) is not None


@pytest.mark.asyncio
async def test_reads_return_immutable_objects() -> None:
    clock = {"t": T0}
    hub = _hub(clock)
    try:
        update = await hub._universe.refresh()
    finally:
        await hub._universe.aclose()
    hub._apply_tokens(update.tokens)
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
        await until(lambda: clob.made and clob.made[0].sent and rtds.made and rtds.made[0].sent)
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
        await until(lambda: hub.price(rs.BINANCE, "btc") is not None
                    and hub.snapshot().clob.frames_total == 2)
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
async def test_market_resolved_names_the_window() -> None:
    clock = {"t": T0}
    hub = _hub(clock)
    try:
        update = await hub._universe.refresh()
    finally:
        await hub._universe.aclose()
    hub._apply_tokens(update.tokens)
    listener = hub.listen()
    resolved = cm.MarketResolvedEvent("1", MARKET, (UP, DOWN), DOWN, "Down", 5, ("5M",))
    hub.handle_clob_event(resolved, 6)
    events = _drain(listener)
    assert len(events) == 1 and isinstance(events[0], hub_mod.MarketResolved)
    assert events[0].market.slug == CURRENT
    assert (events[0].winning_token, events[0].winning_outcome) == (DOWN, "Down")
    # Still inside its window, so it stays followed until 30 s after the end.
    assert UP in hub._clob.desired
    clock["t"] = 1_789_554_600 + 31
    hub._apply_tokens(hub._universe.tokens())
    assert UP not in hub._clob.desired


def test_snapshot_before_running() -> None:
    hub = _hub()
    snap = hub.snapshot()
    assert snap.taken_at == T0 and snap.started_at == T0
    assert (snap.markets, snap.tokens, snap.subscribed, snap.listeners) == (0, 0, 0, 0)
    assert snap.clob.connected is False
    assert set(snap.prices) == set(rs.SOURCES)
    assert all(age is None for age in snap.price_ages.values())


def test_default_grid_and_registry() -> None:
    hub = hub_mod.MarketDataHub()
    assert hub.assets == ("btc", "eth", "sol", "xrp", "doge", "bnb")
    assert hub.timeframes == ("5m", "15m", "1h", "1d")
    hub_mod.set_current(hub)
    try:
        assert hub_mod.current() is hub
    finally:
        hub_mod.set_current(None)
    assert hub_mod.current() is None
