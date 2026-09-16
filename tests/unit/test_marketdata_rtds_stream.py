"""RTDS price stream: subscribe payload, snapshot classification, dedupe, history, reconnects."""
from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path

import pytest
import websockets

from polymarket_exec.marketdata import rtds_stream as rs

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "marketdata"
RECEIVED = 1_789_555_690_000
ASSETS = frozenset(rs.DEFAULT_ASSETS)


def _frame(name: str) -> str:
    return (FIXTURES / name).read_text()


def _update(source: str, asset: str, obs_ms: int, value: float = 100.0) -> str:
    topic = rs.TOPICS[source]
    payload = {"full_accuracy_value": str(value), "symbol": rs.symbol(source, asset),
               "timestamp": obs_ms, "value": value}
    if source == rs.CHAINLINK_TWAP60:
        payload["window_s"] = 60
    return json.dumps({"connection_id": "c", "payload": payload, "timestamp": obs_ms + 400,
                       "topic": topic, "type": "update"})


def test_subscribe_message_is_unfiltered_first_then_one_compact_filter_per_asset() -> None:
    message = rs.subscribe_message(("btc", "eth"))
    body = json.loads(message)
    assert body["action"] == "subscribe"
    assert body["subscriptions"] == [
        {"topic": "crypto_prices", "type": "update"},
        {"topic": "crypto_prices", "type": "update", "filters": '{"symbol":"btcusdt"}'},
        {"topic": "crypto_prices", "type": "update", "filters": '{"symbol":"ethusdt"}'},
        {"topic": "crypto_prices_chainlink", "type": "update"},
        {"topic": "crypto_prices_chainlink", "type": "update",
         "filters": '{"symbol":"btc/usd"}'},
        {"topic": "crypto_prices_chainlink", "type": "update",
         "filters": '{"symbol":"eth/usd"}'},
        {"topic": "crypto_prices_twap_sixty", "type": "update"},
        {"topic": "crypto_prices_twap_sixty", "type": "update",
         "filters": '{"symbol":"btc/usd"}'},
        {"topic": "crypto_prices_twap_sixty", "type": "update",
         "filters": '{"symbol":"eth/usd"}'},
    ]
    assert '": ' not in message and '", ' not in message  # compact all the way through


def test_update_frames_normalise_to_price_points() -> None:
    binance = rs.parse_rtds_frame(_frame("rtds_binance_update.json"), RECEIVED, ASSETS)
    assert binance.kind == rs.UPDATE
    assert binance.points == (rs.PricePoint(
        "binance", "btc", 75964.0, 1_789_555_684_000, 1_789_555_684_182, RECEIVED),)
    chainlink = rs.parse_rtds_frame(_frame("rtds_chainlink_update.json"), RECEIVED, ASSETS)
    assert chainlink.points == (rs.PricePoint(
        "chainlink", "btc", 75913.47245426329, 1_789_555_589_000, 1_789_555_590_420, RECEIVED),)
    twap = rs.parse_rtds_frame(_frame("rtds_twap60_update.json"), RECEIVED, ASSETS)
    assert [(p.source, p.asset, p.value) for p in twap.points] == [
        ("chainlink_twap60", "sol", 97.26542568815323)]


def test_the_thirty_second_twap_is_not_used() -> None:
    frame = rs.parse_rtds_frame(_frame("rtds_twap30_update.json"), RECEIVED, ASSETS)
    assert frame.kind == rs.OTHER and frame.points == ()


def test_snapshots_are_classified_by_symbol_form_and_topic() -> None:
    binance = rs.parse_rtds_frame(_frame("rtds_binance_snapshot.json"), RECEIVED, ASSETS)
    assert binance.kind == rs.SNAPSHOT
    assert {(p.source, p.asset) for p in binance.points} == {("binance", "btc")}
    assert [p.obs_ms for p in binance.points][:2] == [1_789_555_564_000, 1_789_555_565_000]
    assert binance.points[0].value == 75986.0 and binance.points[0].publish_ms is None
    # The Chainlink history arrives under topic crypto_prices, not crypto_prices_chainlink.
    raw = json.loads(_frame("rtds_chainlink_snapshot.json"))
    assert raw["topic"] == "crypto_prices"
    chainlink = rs.parse_rtds_frame(json.dumps(raw), RECEIVED, ASSETS)
    assert {(p.source, p.asset) for p in chainlink.points} == {("chainlink", "btc")}
    assert len(chainlink.points) == 6
    twap = rs.parse_rtds_frame(_frame("rtds_twap60_snapshot.json"), RECEIVED, ASSETS)
    assert {(p.source, p.asset) for p in twap.points} == {("chainlink_twap60", "btc")}
    assert twap.points[-1].value == 75882.54725851085


def test_untracked_assets_are_dropped_unless_tracked() -> None:
    hype = _update(rs.CHAINLINK, "hype", 1_000)
    frame = rs.parse_rtds_frame(hype, RECEIVED, ASSETS)
    assert frame.kind == rs.UPDATE and frame.points == ()  # still proves the stream flows
    kept = rs.parse_rtds_frame(hype, RECEIVED, frozenset({"hype"}))
    assert [(p.source, p.asset) for p in kept.points] == [("chainlink", "hype")]


def test_acks_errors_and_junk() -> None:
    assert rs.parse_rtds_frame("", RECEIVED, ASSETS).kind == rs.ACK
    errors = json.loads(_frame("rtds_errors.json"))
    first = rs.parse_rtds_frame(json.dumps(errors[0]), RECEIVED, ASSETS)
    assert first.kind == rs.ERROR and first.error.startswith("401: leger GetTopics error")
    second = rs.parse_rtds_frame(json.dumps(errors[1]), RECEIVED, ASSETS)
    assert (second.kind, second.error) == (rs.ERROR, "Invalid request body")
    for junk in ("not json", "[]", '{"topic": "crypto_prices", "type": "update"}',
                 '{"topic": "crypto_prices", "type": "update", "payload": {"symbol": "btcusdt"}}'):
        assert rs.parse_rtds_frame(junk, RECEIVED, ASSETS).kind == rs.OTHER


def _stream(**kw) -> rs.RtdsPriceStream:
    kw.setdefault("time_fn", lambda: RECEIVED / 1000)
    return rs.RtdsPriceStream(**kw)


def test_updates_are_deduped_and_pushed_once() -> None:
    pushed: list = []
    stream = _stream(on_point=pushed.append)
    frame = _update(rs.BINANCE, "btc", 1_000_000)
    stream.handle_frame(frame)
    stream.handle_frame(frame)
    assert [p.obs_ms for p in stream.history(rs.BINANCE, "btc")] == [1_000_000]
    assert len(pushed) == 1 and pushed[0] == stream.latest(rs.BINANCE, "btc")
    assert stream.status()[rs.BINANCE].updates == 1


def test_snapshots_merge_into_history_without_duplicates() -> None:
    pushed: list = []
    stream = _stream(on_point=pushed.append)
    stream.handle_frame(_update(rs.BINANCE, "btc", 1_789_555_682_000, 75964.0))
    stream.handle_frame(_update(rs.BINANCE, "btc", 1_789_555_683_000, 75965.0))
    stream.handle_frame(_frame("rtds_binance_snapshot.json"))  # ends at ..682_000
    history = stream.history(rs.BINANCE, "btc")
    stamps = [p.obs_ms for p in history]
    assert stamps == sorted(set(stamps)) and len(stamps) == 7
    assert history[-1].value == 75965.0 and history[-1].publish_ms is not None
    assert stream.latest(rs.BINANCE, "btc") == history[-1]
    assert len(pushed) == 2  # history is not pushed as live ticks
    st = stream.status()[rs.BINANCE]
    assert (st.snapshots, st.points, st.updates) == (1, 7, 2)
    # An older live print still lands in order.
    stream.handle_frame(_update(rs.BINANCE, "btc", 1_789_555_600_000, 1.0))
    stamps = [p.obs_ms for p in stream.history(rs.BINANCE, "btc")]
    assert stamps == sorted(stamps) and 1_789_555_600_000 in stamps


def test_ring_buffer_keeps_the_newest_points_and_counts_gaps_inside_it() -> None:
    stream = _stream(history_points=4)
    for second in (1, 2, 4, 7, 8):  # missing: 3, 5, 6
        stream.handle_frame(_update(rs.CHAINLINK, "eth", second * 1000))
    held = [p.obs_ms for p in stream.history(rs.CHAINLINK, "eth")]
    assert held == [2000, 4000, 7000, 8000]
    assert stream.status()[rs.CHAINLINK].gaps == 3  # 1 -> 2 fell out of the window
    stream.handle_frame(_update(rs.CHAINLINK, "eth", 9000))
    assert stream.status()[rs.CHAINLINK].gaps == 2  # 3 fell out too
    pushed_before = stream.status()[rs.CHAINLINK].updates
    stream.handle_frame(_update(rs.CHAINLINK, "eth", 3000))  # older than the whole window
    stream.handle_frame(_update(rs.CHAINLINK, "eth", 8000))  # duplicate
    assert [p.obs_ms for p in stream.history(rs.CHAINLINK, "eth")] == [4000, 7000, 8000, 9000]
    assert stream.status()[rs.CHAINLINK].updates == pushed_before
    stream.handle_frame(_update(rs.CHAINLINK, "eth", 6000))  # late, but inside the window
    assert [p.obs_ms for p in stream.history(rs.CHAINLINK, "eth")] == [6000, 7000, 8000, 9000]
    assert stream.status()[rs.CHAINLINK].gaps == 0
    assert stream.status()[rs.CHAINLINK].updates == pushed_before + 1


def test_history_window_latest_and_status() -> None:
    now = {"t": 1_000.0}
    stream = _stream(time_fn=lambda: now["t"])
    for second in range(930, 1000):
        stream.handle_frame(_update(rs.CHAINLINK_TWAP60, "btc", second * 1000, float(second)))
    now["t"] = 1_000.5
    last_minute = stream.history(rs.CHAINLINK_TWAP60, "btc", seconds=60)
    assert [p.obs_ms for p in last_minute] == [s * 1000 for s in range(941, 1000)]
    assert stream.latest(rs.CHAINLINK_TWAP60, "btc").value == 999.0
    assert stream.latest(rs.CHAINLINK_TWAP60, "doge") is None
    assert stream.history("nope", "btc") == ()
    st = stream.status()
    twap = st[rs.CHAINLINK_TWAP60]
    assert (twap.points, twap.assets, twap.newest_obs_ms, twap.gaps) == (70, 1, 999_000, 0)
    assert twap.last_update_age_s == 0.5
    assert twap.latency_ms_p50 is not None
    assert set(st) == {rs.CHAINLINK, rs.CHAINLINK_TWAP60, rs.BINANCE}
    assert st[rs.BINANCE].points == 0 and st[rs.BINANCE].last_update_age_s is None


def test_a_failing_consumer_does_not_lose_the_point() -> None:
    def boom(point) -> None:
        raise RuntimeError("consumer bug")

    stream = _stream(on_point=boom)
    stream.handle_frame(_update(rs.BINANCE, "sol", 5_000))
    assert stream.latest(rs.BINANCE, "sol") is not None
    assert stream.status()[rs.BINANCE].handler_errors == 1


# --- connection loop ------------------------------------------------------------


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
    def __init__(self) -> None:
        self.made: list[FakeWs] = []

    def __call__(self, url: str) -> FakeWs:
        assert url == rs.RTDS_WS
        self.made.append(FakeWs())
        return self.made[-1]


async def until(pred, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not pred():
        if loop.time() > end:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.001)


@contextlib.asynccontextmanager
async def running(stream):
    stop = asyncio.Event()
    task = asyncio.create_task(stream.run(stop))
    try:
        yield stop
    finally:
        stop.set()
        loop = asyncio.get_running_loop()
        started = loop.time()
        await asyncio.wait_for(task, timeout=1)
        assert loop.time() - started < 1.0


@pytest.mark.asyncio
async def test_run_subscribes_pings_and_reconnects_when_prices_stop() -> None:
    clock = {"t": 2_000.0}
    conn = Connector()
    stream = rs.RtdsPriceStream(("btc",), connect=conn, time_fn=lambda: clock["t"],
                                tick_s=0.001, initial_backoff_s=0.001, rng=lambda: 0.5)
    async with running(stream):
        await until(lambda: conn.made and conn.made[0].sent)
        first = conn.made[0]
        assert first.sent == [rs.subscribe_message(("btc",))]
        first.incoming.put_nowait("")
        first.incoming.put_nowait(_update(rs.BINANCE, "btc", 1_999_000))
        await until(lambda: stream.status()[rs.BINANCE].updates == 1)
        assert stream.status()[rs.BINANCE].connected is True
        clock["t"] += 5
        await until(lambda: first.sent.count("PING") == 1)
        clock["t"] += 24.5  # 29.5 s without a price update
        await asyncio.sleep(0.02)
        assert len(conn.made) == 1
        clock["t"] += 0.5
        await until(lambda: len(conn.made) == 2 and conn.made[1].sent)
        assert conn.made[1].sent == [rs.subscribe_message(("btc",))]
        st = stream.status()[rs.BINANCE]
        assert first.exited and st.reconnects == 1
        assert "no price update" in (st.last_error or "")
        assert len(stream.history(rs.BINANCE, "btc")) == 1  # history survives reconnects
        assert stream.acks == 1


@pytest.mark.asyncio
async def test_run_records_server_errors_and_socket_failures() -> None:
    conn = Connector()
    stream = rs.RtdsPriceStream(("btc",), connect=conn, tick_s=0.001,
                                initial_backoff_s=0.001, rng=lambda: 0.5)
    async with running(stream):
        await until(lambda: conn.made)
        conn.made[0].incoming.put_nowait(json.dumps(json.loads(_frame("rtds_errors.json"))[1]))
        await until(lambda: stream.status()[rs.CHAINLINK].last_error == "Invalid request body")
        conn.made[0].incoming.put_nowait(ConnectionResetError("reset by peer"))
        await until(lambda: len(conn.made) == 2)
        assert stream.status()[rs.CHAINLINK].last_error == "ConnectionResetError: reset by peer"


def test_default_connection_options(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def fake_connect(url: str, **kwargs):
        captured.update(kwargs, url=url)
        return object()

    monkeypatch.setattr(websockets, "connect", fake_connect)
    rs._default_connect(rs.RTDS_WS)
    assert captured == {"url": "wss://ws-live-data.polymarket.com", "open_timeout": 15,
                        "ping_interval": None, "max_queue": 1024, "close_timeout": 1}
