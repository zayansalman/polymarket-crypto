"""CLOB market-channel stream: subscribe frames, diffs, heartbeat, watchdog, reconnects, stop."""
from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import ssl
from pathlib import Path
from types import SimpleNamespace

import pytest
import websockets
from websockets.exceptions import ConnectionClosedOK
from websockets.frames import Close

from polymarket_exec.marketdata import clob_messages as cm
from polymarket_exec.marketdata import clob_shard as sh
from polymarket_exec.marketdata import clob_stream as cs
from polymarket_exec.marketdata import rtds_stream as rs

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "marketdata"


class FakeWs:
    def __init__(self, frames: tuple = (), *, hang_on_enter: bool = False) -> None:
        self.sent: list[str] = []
        self.incoming: asyncio.Queue = asyncio.Queue()
        for frame in frames:
            self.incoming.put_nowait(frame)
        self.hang_on_enter = hang_on_enter
        self.exited = False

    async def __aenter__(self) -> FakeWs:
        if self.hang_on_enter:
            await asyncio.sleep(3600)
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

    def push(self, frame) -> None:
        self.incoming.put_nowait(frame)

    def frames(self) -> list[dict]:
        return [json.loads(s) for s in self.sent if s != "PING"]


class Refused:
    """A handshake that fails, like websockets.connect() raising on enter."""

    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    async def __aenter__(self):
        raise self.exc

    async def __aexit__(self, *exc) -> bool:
        return False


class Connector:
    def __init__(self, sockets: list | None = None) -> None:
        self.urls: list[str] = []
        self.sockets = list(sockets or [])
        self.made: list = []

    def __call__(self, url: str):
        self.urls.append(url)
        ws = self.sockets.pop(0) if self.sockets else FakeWs()
        self.made.append(ws)
        return ws


class Clock:
    def __init__(self, t: float = 1_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


async def until(pred, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not pred():
        if loop.time() > end:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.001)


def _stream(connect, clock=None, events: list | None = None, handler=None, **kw):
    events = events if events is not None else []
    kw.setdefault("tick_s", 0.001)
    kw.setdefault("initial_backoff_s", 0.001)
    return cs.ClobMarketStream(
        handler or (lambda ev, ms: events.append((ev, ms))),
        connect=connect, time_fn=clock or Clock(), rng=lambda: 0.5, **kw,
    )


@contextlib.asynccontextmanager
async def running(stream):
    stop = asyncio.Event()
    task = asyncio.create_task(stream.run(stop))
    try:
        yield stop
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=2)


def _trade(ts_ms: int) -> str:
    return json.dumps({"event_type": "last_trade_price", "market": "m", "asset_id": "a",
                       "price": "0.5", "size": "1", "side": "BUY", "timestamp": str(ts_ms)})


@pytest.mark.asyncio
async def test_first_frame_is_type_market_with_the_flag() -> None:
    conn = Connector()
    stream = _stream(conn)
    stream.set_tokens({"b", "a"})
    async with running(stream):
        await until(lambda: conn.made and conn.made[0].sent)
        assert stream.status().connected is True
    assert conn.urls == [cs.CLOB_MARKET_WS]
    assert conn.made[0].sent[0] == (
        '{"assets_ids":["a","b"],"type":"market","custom_feature_enabled":true}')


@pytest.mark.asyncio
async def test_no_connection_until_tokens_are_wanted() -> None:
    conn = Connector()
    stream = _stream(conn)
    async with running(stream):
        await asyncio.sleep(0.05)
        assert conn.urls == [] and stream.status().connected is False
        stream.set_tokens({"a"})
        await until(lambda: conn.made and conn.made[0].sent)


@pytest.mark.asyncio
async def test_large_sets_are_chunked_and_every_subscribe_frame_carries_the_flag() -> None:
    conn = Connector()
    stream = _stream(conn)
    tokens = {f"t{i:04d}" for i in range(1200)}
    stream.set_tokens(tokens)
    async with running(stream):
        await until(lambda: conn.made and len(conn.made[0].sent) >= 3)
        assert stream.status().subscribed == 1200
    frames = conn.made[0].frames()
    assert [len(f["assets_ids"]) for f in frames] == [500, 500, 200]
    assert frames[0]["type"] == "market" and "operation" not in frames[0]
    assert all(f.get("operation") == "subscribe" and "type" not in f for f in frames[1:])
    assert all(f["custom_feature_enabled"] is True for f in frames)
    assert sorted(t for f in frames for t in f["assets_ids"]) == sorted(tokens)


@pytest.mark.asyncio
async def test_token_changes_go_out_as_operation_diffs() -> None:
    conn = Connector()
    stream = _stream(conn)
    stream.set_tokens({"a", "b"})
    async with running(stream):
        await until(lambda: conn.made and conn.made[0].sent)
        ws = conn.made[0]
        stream.set_tokens({"b", "c"})
        await until(lambda: len(ws.sent) == 3)
        stream.set_tokens({"c", "b"})  # same set: nothing to send
        await asyncio.sleep(0.02)
        assert stream.status().subscribed == 2 and stream.status().desired == 2
    assert ws.frames()[1:] == [
        {"assets_ids": ["a"], "operation": "unsubscribe"},
        {"assets_ids": ["c"], "operation": "subscribe", "custom_feature_enabled": True},
    ]
    assert len(conn.made) == 1


@pytest.mark.asyncio
async def test_emptying_the_set_unsubscribes_and_keeps_the_socket() -> None:
    clock = Clock()
    conn = Connector()
    stream = _stream(conn, clock)
    stream.set_tokens({"a"})
    async with running(stream):
        await until(lambda: conn.made and conn.made[0].sent)
        ws = conn.made[0]
        stream.set_tokens(set())
        await until(lambda: len(ws.sent) == 2)
        clock.t += 500  # nothing subscribed: the watchdog stays quiet
        await asyncio.sleep(0.02)
        assert stream.status().resyncs == 0 and stream.status().subscribed == 0
        stream.set_tokens({"c"})
        await until(lambda: len(ws.frames()) == 3)
    assert ws.frames()[1:] == [
        {"assets_ids": ["a"], "operation": "unsubscribe"},
        {"assets_ids": ["c"], "operation": "subscribe", "custom_feature_enabled": True},
    ]
    assert len(conn.made) == 1


@pytest.mark.asyncio
async def test_ping_every_ten_seconds_on_the_injected_clock() -> None:
    clock = Clock()
    conn = Connector()
    stream = _stream(conn, clock)
    stream.set_tokens({"a"})
    async with running(stream):
        await until(lambda: conn.made and conn.made[0].sent)
        ws = conn.made[0]
        clock.t += 9.5
        await asyncio.sleep(0.02)
        assert "PING" not in ws.sent
        clock.t += 0.5
        await until(lambda: ws.sent.count("PING") == 1)
        clock.t += 5
        await asyncio.sleep(0.02)
        assert ws.sent.count("PING") == 1
        clock.t += 5
        await until(lambda: ws.sent.count("PING") == 2)
        ws.push("PONG")
        await until(lambda: stream.status().last_pong_at == clock.t)
        assert stream.status().frames_total == 0  # a PONG is not market data


@pytest.mark.asyncio
async def test_events_are_dispatched_with_the_receive_time_and_counted() -> None:
    clock = Clock(1_789_554_461.0)
    conn = Connector()
    events: list = []
    stream = _stream(conn, clock, events)
    stream.set_tokens({"a"})
    async with running(stream):
        await until(lambda: conn.made and conn.made[0].sent)
        ws = conn.made[0]
        ws.push((FIXTURES / "clob_price_change.json").read_text())
        ws.push('{"event_type": "brand_new_thing"}')
        ws.push("NO NEW ASSETS")
        ws.push("[]")
        ws.push("INVALID OPERATION")
        await until(lambda: stream.status().last_notice == "INVALID OPERATION")
        st = stream.status()
    assert len(events) == 1
    event, received_ms = events[0]
    assert isinstance(event, cm.PriceChangeEvent) and received_ms == 1_789_554_461_000
    assert (st.frames_total, st.unknown, st.last_frame_at) == (2, 1, clock.t)
    assert st.latency_ms_p50 == st.latency_ms_p90 == 146.0  # 461_000 - 460_854


@pytest.mark.asyncio
async def test_rate_and_latency_percentiles() -> None:
    clock = Clock(1_000.0)
    conn = Connector()
    stream = _stream(conn, clock)
    stream.set_tokens({"a"})
    async with running(stream):
        await until(lambda: conn.made and conn.made[0].sent)
        ws = conn.made[0]
        for delay in range(1, 21):
            ws.push(_trade(1_000_000 - delay))
        await until(lambda: stream.status().frames_total == 20)
        clock.t = 1_001.0
        snapshot = json.dumps([{"event_type": "book", "market": "m", "asset_id": "a",
                                "bids": [], "asks": [], "timestamp": "1", "hash": ""}])
        for _ in range(10):  # data frames, but snapshots carry no latency sample
            ws.push(snapshot)
        await until(lambda: stream.status().frames_total == 30)
        clock.t = 1_002.5
        st = stream.status()
        assert st.frames_per_s == 3.0  # 30 frames over the last 10 whole seconds
        assert (st.latency_ms_p50, st.latency_ms_p90, st.latency_ms_max) == (11.0, 19.0, 20.0)
        assert sorted(stream.latency_samples()) == list(range(1, 21))
        sent_bytes = sum(len(_trade(1_000_000 - d)) for d in range(1, 21)) + 10 * len(snapshot)
        assert st.bytes_total == sent_bytes
        clock.t = 1_020.0
        assert stream.status().frames_per_s == 0.0


@pytest.mark.asyncio
async def test_reconnects_after_close_1000_and_resubscribes_everything() -> None:
    first, second = FakeWs(), FakeWs()
    conn = Connector([first, second])
    stream = _stream(conn)
    stream.set_tokens({"a", "b"})
    async with running(stream):
        await until(lambda: first.sent)
        first.push(ConnectionClosedOK(Close(1000, "all subscribed assets resolved"), None))
        await until(lambda: second.sent)
        st = stream.status()
        assert st.connected is True and st.reconnects == 1
        assert "all subscribed assets resolved" in (st.last_error or "")
    assert first.exited is True
    assert json.loads(second.sent[0]) == {
        "assets_ids": ["a", "b"], "type": "market", "custom_feature_enabled": True}


@pytest.mark.asyncio
async def test_watchdog_resubscribes_then_reconnects() -> None:
    clock = Clock()
    first, second = FakeWs(), FakeWs()
    conn = Connector([first, second])
    stream = _stream(conn, clock)
    stream.set_tokens({"b", "a"})
    async with running(stream):
        await until(lambda: first.sent)
        first.push(_trade(1_000_000))
        await until(lambda: stream.status().frames_total == 1)
        clock.t += 44.5
        await asyncio.sleep(0.02)
        assert stream.status().resyncs == 0 and len(first.frames()) == 1
        clock.t += 0.5
        await until(lambda: stream.status().resyncs == 1)
        assert first.frames()[1:] == [
            {"assets_ids": ["a", "b"], "operation": "unsubscribe"},
            {"assets_ids": ["a", "b"], "operation": "subscribe", "custom_feature_enabled": True},
        ]
        clock.t += 44.5
        await asyncio.sleep(0.02)
        assert len(conn.made) == 1
        clock.t += 0.5
        await until(lambda: second.sent)
        assert "no data" in (stream.status().last_error or "")
    assert first.exited is True
    assert json.loads(second.sent[0])["type"] == "market"


NEW_MARKET = (FIXTURES / "clob_new_market.json").read_text()


@pytest.mark.asyncio
async def test_announcements_are_not_book_data_for_the_watchdog() -> None:
    # Every socket gets the platform-wide new_market broadcast (0.6-1.4/s live), so it
    # must not hide a subscription that stopped sending data for its own tokens.
    clock = Clock()
    first, second = FakeWs(), FakeWs()
    conn = Connector([first, second])
    events: list = []
    stream = _stream(conn, clock, events)
    stream.set_tokens({"a"})
    async with running(stream):
        await until(lambda: first.sent)
        first.push(_trade(1_000_000))
        await until(lambda: stream.status().frames_total == 1)
        for step in range(1, 4):  # every 15 s an announcement and an unknown event, only
            clock.t += 15
            first.push(NEW_MARKET)
            first.push('{"event_type": "brand_new_thing"}')
            await until(lambda: stream.status().frames_total == 1 + 2 * step)
        await until(lambda: stream.status().resyncs == 1)
        st = stream.status()
        assert st.last_frame_at == 1_000.0  # the time of the last book data
        assert st.frames_total == 7  # announcements are still counted as traffic
        for _ in range(3):
            clock.t += 15
            first.push(NEW_MARKET)
        await until(lambda: second.sent)
        assert "even after a resubscribe" in (stream.status().last_error or "")
    assert any(isinstance(ev, cm.NewMarketEvent) for ev, _ in events)


@pytest.mark.asyncio
async def test_a_connection_with_only_announcements_does_not_reset_the_backoff() -> None:
    quiet = FakeWs([NEW_MARKET, ConnectionClosedOK(Close(1000, "bye"), None)])
    good = FakeWs([_trade(1), ConnectionClosedOK(Close(1000, "bye"), None)])
    conn = Connector([Refused(OSError("a")), quiet, good, Refused(OSError("b"))])
    stream = _stream(conn, initial_backoff_s=1.0, max_backoff_s=30.0)
    delays: list[float] = []

    async def pause(stop_event: asyncio.Event, delay: float) -> None:
        delays.append(delay)
        if len(delays) == 4:
            stop_event.set()

    stream._pause = pause
    stream.set_tokens({"a"})
    await asyncio.wait_for(stream.run(asyncio.Event()), timeout=2)
    assert delays == [1.0, 2.0, 1.0, 2.0]  # only the connection with a trade resets it


@pytest.mark.asyncio
async def test_data_after_a_resubscribe_keeps_the_connection() -> None:
    clock = Clock()
    conn = Connector()
    stream = _stream(conn, clock)
    stream.set_tokens({"a"})
    async with running(stream):
        await until(lambda: conn.made and conn.made[0].sent)
        ws = conn.made[0]
        clock.t += 45  # silent since the subscribe
        await until(lambda: stream.status().resyncs == 1)
        clock.t += 1
        ws.push(_trade(1_046_000))
        await until(lambda: stream.status().frames_total == 1)
        clock.t += 44.5  # 89.5 s after the resubscribe, 44.5 s after data: no action
        await asyncio.sleep(0.02)
        assert stream.status().resyncs == 1 and len(conn.made) == 1
        clock.t += 0.5  # 45 s without data again: another resubscribe, not a reconnect
        await until(lambda: stream.status().resyncs == 2)
        assert len(conn.made) == 1


@pytest.mark.asyncio
async def test_a_socket_far_behind_its_best_is_replaced() -> None:
    clock = Clock(1_000.0)
    first, second = FakeWs(), FakeWs()
    conn = Connector([first, second])
    stream = _stream(conn, clock, max_lag_s=10.0)
    stream.set_tokens({"a"})
    async with running(stream):
        await until(lambda: first.sent)
        for _ in range(64):  # 100 ms behind: this connection's best
            first.push(_trade(1_000_000 - 100))
        await until(lambda: stream.status().frames_total == 64)
        for _ in range(64):  # 9.9 s more than the best: still tolerated
            first.push(_trade(1_000_000 - 10_000))
        await until(lambda: stream.status().frames_total == 128)
        await asyncio.sleep(0.02)
        assert len(conn.made) == 1
        for _ in range(64):  # 11.9 s more than the best: drop the backlog, reconnect
            first.push(_trade(1_000_000 - 12_000))
        await until(lambda: second.sent)
        st = stream.status()
        assert st.reconnects == 1 and "behind" in (st.last_error or "")
        assert st.latency_ms_p50 is None  # samples restart with the new connection
    assert first.exited is True


@pytest.mark.asyncio
async def test_a_steady_clock_offset_is_not_lag() -> None:
    clock = Clock(1_000.0)
    conn = Connector()
    stream = _stream(conn, clock, max_lag_s=10.0)
    stream.set_tokens({"a"})
    async with running(stream):
        await until(lambda: conn.made and conn.made[0].sent)
        for _ in range(200):  # our clock runs 30 s ahead of the server's
            conn.made[0].push(_trade(1_000_000 - 30_000))
        await until(lambda: stream.status().frames_total == 200)
        await asyncio.sleep(0.05)
        st = stream.status()
    assert len(conn.made) == 1 and st.reconnects == 0
    assert st.latency_ms_p50 == 30_000.0


@pytest.mark.asyncio
async def test_a_requested_reconnect_replaces_the_connection() -> None:
    first, second = FakeWs(), FakeWs()
    conn = Connector([first, second])
    stream = _stream(conn)
    stream.set_tokens({"a"})
    async with running(stream):
        await until(lambda: first.sent)
        session = stream.session
        stream.request_reconnect("recycled: 3.5s behind the freshest connection")
        await until(lambda: second.sent)
        st = stream.status()
        assert stream.session == session + 1 and st.reconnects == 1
        assert st.last_error == "StreamRecycled: recycled: 3.5s behind the freshest connection"
        await asyncio.sleep(0.02)
        assert len(conn.made) == 2  # the request is used up by one reconnect
    assert first.exited is True


@pytest.mark.asyncio
async def test_the_own_lag_rule_can_be_turned_off() -> None:
    clock = Clock(1_000.0)
    conn = Connector()
    stream = _stream(conn, clock, max_lag_s=0)
    stream.set_tokens({"a"})
    async with running(stream):
        await until(lambda: conn.made and conn.made[0].sent)
        assert stream.behind_best_ms() is None  # not enough events yet
        for latency in [100] * 64 + [12_000] * 64:
            conn.made[0].push(_trade(1_000_000 - latency))
        await until(lambda: stream.status().frames_total == 128)
        await asyncio.sleep(0.05)
        assert len(conn.made) == 1 and stream.status().reconnects == 0
        assert stream.behind_best_ms() == 11_900


@pytest.mark.asyncio
async def test_requested_resync_sends_fresh_subscriptions() -> None:
    conn = Connector()
    stream = _stream(conn)
    stream.set_tokens({"a"})
    async with running(stream):
        await until(lambda: conn.made and conn.made[0].sent)
        stream.request_resync()
        await until(lambda: stream.status().resyncs == 1)
    assert conn.made[0].frames()[1:] == [
        {"assets_ids": ["a"], "operation": "unsubscribe"},
        {"assets_ids": ["a"], "operation": "subscribe", "custom_feature_enabled": True},
    ]


async def _stops_within_a_second(stream, ready) -> None:
    stop = asyncio.Event()
    task = asyncio.create_task(stream.run(stop))
    await until(ready)
    loop = asyncio.get_running_loop()
    started = loop.time()
    stop.set()
    await asyncio.wait_for(task, timeout=1.0)
    assert loop.time() - started < 1.0


@pytest.mark.asyncio
async def test_stop_while_connected() -> None:
    conn = Connector()
    stream = _stream(conn)
    stream.set_tokens({"a"})
    await _stops_within_a_second(stream, lambda: conn.made and conn.made[0].sent)
    assert conn.made[0].exited is True and stream.status().connected is False


@pytest.mark.asyncio
async def test_stop_during_a_hung_handshake() -> None:
    conn = Connector([FakeWs(hang_on_enter=True)])
    stream = _stream(conn)
    stream.set_tokens({"a"})
    await _stops_within_a_second(stream, lambda: conn.made)


@pytest.mark.asyncio
async def test_stop_during_the_backoff_wait() -> None:
    conn = Connector([Refused(OSError("refused"))])
    stream = _stream(conn, initial_backoff_s=30.0)
    stream.set_tokens({"a"})
    await _stops_within_a_second(stream, lambda: stream.status().last_error is not None)
    assert stream.status().last_error == "OSError: refused"


def _release(waiter: asyncio.Future, *args) -> None:
    if not waiter.done():
        waiter.set_result(None)


async def _cancel_and_wait(fut: asyncio.Future) -> None:
    waiter = asyncio.get_running_loop().create_future()
    callback = functools.partial(_release, waiter)
    fut.add_done_callback(callback)
    try:
        fut.cancel()
        await waiter
    finally:
        fut.remove_done_callback(callback)


async def wait_for_311(awaitable, timeout):
    """``asyncio.wait_for`` as Python 3.11 ships it (the positive-timeout path). A cancel
    that lands just as the awaited thing completes is lost: the result comes back."""
    loop = asyncio.get_running_loop()
    waiter = loop.create_future()
    timeout_handle = loop.call_later(timeout, _release, waiter)
    callback = functools.partial(_release, waiter)
    fut = asyncio.ensure_future(awaitable)
    fut.add_done_callback(callback)
    try:
        try:
            await waiter
        except asyncio.CancelledError:
            if fut.done():
                return fut.result()
            fut.remove_done_callback(callback)
            await _cancel_and_wait(fut)
            raise
        if fut.done():
            return fut.result()
        fut.remove_done_callback(callback)
        await _cancel_and_wait(fut)
        try:
            return fut.result()
        except asyncio.CancelledError as exc:
            raise TimeoutError() from exc
    finally:
        timeout_handle.cancel()


async def _cancelled_beside_a_token_change(run, set_tokens, ready) -> bool:
    """Start ``run``; change the tokens, stop and cancel in one step. True if it ended."""
    stop = asyncio.Event()
    task = asyncio.create_task(run(stop))
    try:
        await until(ready)
        await asyncio.sleep(0.05)  # housekeeping is waiting for a change
        set_tokens({"a", "b"})  # e.g. the hub applying a refresh as the app shuts down
        stop.set()
        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=1.0)
        return bool(done)
    finally:
        task.cancel()
        await asyncio.wait({task}, timeout=1.0)


@pytest.mark.asyncio
async def test_a_cancel_beside_a_token_change_stops_the_stream(monkeypatch) -> None:
    # On Python 3.11 the lost cancel kept run() going for up to two watchdog periods.
    monkeypatch.setattr(asyncio, "wait_for", wait_for_311)
    conn = Connector()
    stream = _stream(conn, tick_s=0.5)
    stream.set_tokens({"a"})
    assert await _cancelled_beside_a_token_change(
        stream.run, stream.set_tokens, lambda: conn.made and conn.made[0].sent)


@pytest.mark.asyncio
async def test_a_cancel_beside_a_token_change_stops_a_hedged_group(monkeypatch) -> None:
    monkeypatch.setattr(asyncio, "wait_for", wait_for_311)
    conn = Connector()
    shard = sh.ClobShard("btc-5m", 2, on_top=lambda *a: None, on_trade=lambda e: None,
                         on_other=lambda e: None, connect=conn)
    shard.set_tokens({"a"})
    assert await _cancelled_beside_a_token_change(
        shard.run, shard.set_tokens, lambda: len(conn.made) == 2
        and all(ws.sent for ws in conn.made))


@pytest.mark.asyncio
async def test_pause_passes_on_a_cancel_that_meets_the_stop(monkeypatch) -> None:
    monkeypatch.setattr(asyncio, "wait_for", wait_for_311)
    stop = asyncio.Event()
    task = asyncio.create_task(cs.pause(stop, 30.0))
    await asyncio.sleep(0.01)
    stop.set()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.wait_for(cs.pause(asyncio.Event(), 0.01), timeout=1) is None


@pytest.mark.asyncio
async def test_backoff_doubles_to_the_cap_and_resets_after_data() -> None:
    good = FakeWs([_trade(1), ConnectionClosedOK(Close(1000, "bye"), None)])
    conn = Connector([Refused(OSError("a")), Refused(OSError("b")), good,
                      *[Refused(OSError("c")) for _ in range(7)]])
    stream = _stream(conn, initial_backoff_s=1.0, max_backoff_s=30.0)
    delays: list[float] = []

    async def pause(stop_event: asyncio.Event, delay: float) -> None:
        delays.append(delay)
        if len(delays) == 10:
            stop_event.set()

    stream._pause = pause
    stream.set_tokens({"a"})
    await asyncio.wait_for(stream.run(asyncio.Event()), timeout=2)
    assert delays == [1.0, 2.0, 1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0]
    assert stream.status().reconnects == 10


@pytest.mark.asyncio
async def test_a_rate_limited_handshake_waits_at_least_a_minute() -> None:
    class Limited(Exception):
        response = SimpleNamespace(status_code=429)

    conn = Connector([Refused(Limited("server rejected WebSocket connection: HTTP 429"))])
    stream = _stream(conn, initial_backoff_s=1.0)
    delays: list[float] = []

    async def pause(stop_event: asyncio.Event, delay: float) -> None:
        delays.append(delay)
        stop_event.set()

    stream._pause = pause
    stream.set_tokens({"a"})
    await asyncio.wait_for(stream.run(asyncio.Event()), timeout=2)
    assert delays == [60.0]


@pytest.mark.asyncio
async def test_a_failing_handler_never_stops_the_stream() -> None:
    conn = Connector()

    def boom(event, received_ms) -> None:
        raise RuntimeError("consumer bug")

    stream = _stream(conn, handler=boom)
    stream.set_tokens({"a"})
    async with running(stream):
        await until(lambda: conn.made and conn.made[0].sent)
        conn.made[0].push(_trade(1))
        conn.made[0].push(_trade(2))
        await until(lambda: stream.status().handler_errors == 2)
        st = stream.status()
        assert st.connected is True and st.frames_total == 2 and st.reconnects == 0


def test_default_connection_options(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def fake_connect(url: str, **kwargs):
        captured.update(kwargs, url=url)
        return object()

    monkeypatch.setattr(websockets, "connect", fake_connect)
    cs._default_connect(cs.CLOB_MARKET_WS)
    assert captured == {
        "url": "wss://ws-subscriptions-clob.polymarket.com/ws/market",
        "open_timeout": 15, "ping_interval": None, "max_size": 16 * 2**20,
        "max_queue": 8192, "compression": None, "close_timeout": 1,
        "ssl": cs.tls_context(),
    }


def test_every_socket_shares_one_tls_context(monkeypatch: pytest.MonkeyPatch) -> None:
    # websockets makes a TLS context per wss:// socket unless given one; each costs
    # ~13 ms of blocking CPU, so ~28 sockets froze the loop for 215-340 ms at start.
    made: list[ssl.SSLContext] = []
    real = ssl.create_default_context

    def counting(*args, **kwargs) -> ssl.SSLContext:
        made.append(real(*args, **kwargs))
        return made[-1]

    captured: list[dict] = []
    monkeypatch.setattr(cs, "_tls", None)
    monkeypatch.setattr(ssl, "create_default_context", counting)
    monkeypatch.setattr(websockets, "connect",
                        lambda url, **kwargs: captured.append(kwargs) or object())
    for _ in range(3):
        cs._default_connect(cs.CLOB_MARKET_WS)
        rs._default_connect(rs.RTDS_WS)
    assert len(made) == 1 and all(kw["ssl"] is made[0] for kw in captured)
    assert made[0].verify_mode == ssl.CERT_REQUIRED and made[0].check_hostname
    cs._default_connect("ws://127.0.0.1:9/ws")  # plain sockets take no TLS argument
    rs._default_connect("ws://127.0.0.1:9/")
    assert "ssl" not in captured[-1] and "ssl" not in captured[-2]


def test_status_before_running() -> None:
    stream = cs.ClobMarketStream(lambda ev, ms: None)
    st = stream.status()
    assert (st.connected, st.frames_total, st.subscribed, st.reconnects) == (False, 0, 0, 0)
    assert st.latency_ms_p50 is None and st.frames_per_s == 0.0 and st.last_error is None
    assert (st.bytes_total, st.latency_ms_max, stream.session) == (0, None, 0)
