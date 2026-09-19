"""Hedged market sockets: per-connection books, the freshest served, events pushed once."""
from __future__ import annotations

import asyncio

import pytest

from polymarket_exec.marketdata import clob_messages as cm
from polymarket_exec.marketdata import clob_shard as sh
from polymarket_exec.marketdata.clob_stream import MAX_LAG_S

UP, DOWN = "up-token", "down-token"


def _book(token: str, bid: float, ask: float, ts: int, bid_size: float = 10.0,
          ask_size: float = 10.0, snapshot: bool = True, bids=None, asks=None) -> cm.BookEvent:
    return cm.BookEvent("m", token, tuple(bids or ((bid, bid_size),)),
                        tuple(asks or ((ask, ask_size),)), ts, "h", 0.01, None, snapshot)


def _change(token: str, price: float, size: float, side: str, ts: int) -> cm.PriceChangeEvent:
    mirror = DOWN if token == UP else UP
    other = "SELL" if side == "BUY" else "BUY"
    return cm.PriceChangeEvent("m", (
        cm.LevelChange(token, price, size, side, None, None, "h"),
        cm.LevelChange(mirror, round(1 - price, 2), size, other, None, None, "h"),
    ), ts)


def _trade(token: str, price: float, size: float, side: str, ts: int) -> cm.LastTradeEvent:
    return cm.LastTradeEvent("m", token, price, size, side, ts, "0x")


class Recorder:
    def __init__(self) -> None:
        self.pushed: list[tuple] = []
        self.trades: list[cm.LastTradeEvent] = []
        self.other: list = []

    def on_top(self, token: str, top) -> None:
        self.pushed.append(("top", token, top.best_bid, top.best_ask, top.bid_size, top.ask_size))

    def on_trade(self, trade: cm.LastTradeEvent) -> None:
        self.trades.append(trade)
        self.pushed.append(("trade", trade.asset_id, trade.price, trade.size, trade.side,
                            trade.ts_ms))

    def on_other(self, event) -> None:
        self.other.append(event)


def _shard(n: int = 2, **kw) -> tuple[sh.ClobShard, Recorder]:
    rec = Recorder()
    shard = sh.ClobShard("btc-5m", n, on_top=rec.on_top, on_trade=rec.on_trade,
                         on_other=rec.on_other, **kw)
    shard.set_tokens({UP, DOWN})
    return shard, rec


# A sweep: several top changes and two trades inside one millisecond (1002).
FRAMES = [
    _book(UP, 0, 0, 1_000, bids=((0.50, 10.0),), asks=((0.53, 20.0), (0.52, 10.0))),
    _book(DOWN, 0, 0, 1_000, bids=((0.47, 20.0), (0.48, 10.0)), asks=((0.50, 10.0),)),
    _change(UP, 0.51, 3.0, "BUY", 1_001),
    _trade(UP, 0.52, 10.0, "BUY", 1_002),
    _change(UP, 0.52, 0.0, "SELL", 1_002),
    _change(UP, 0.53, 15.0, "SELL", 1_002),
    _trade(DOWN, 0.47, 5.0, "SELL", 1_002),
    _change(UP, 0.51, 0.0, "BUY", 1_003),
]


EXPECTED = [
        ("top", UP, 0.50, 0.52, 10.0, 10.0),
        ("top", DOWN, 0.48, 0.50, 10.0, 10.0),
        ("top", UP, 0.51, 0.52, 3.0, 10.0),
        ("top", DOWN, 0.48, 0.49, 10.0, 3.0),
        ("trade", UP, 0.52, 10.0, "BUY", 1_002),
        ("top", UP, 0.51, 0.53, 3.0, 20.0),
        ("top", DOWN, 0.47, 0.49, 20.0, 3.0),
        ("top", UP, 0.51, 0.53, 3.0, 15.0),
        ("top", DOWN, 0.47, 0.49, 15.0, 3.0),
        ("trade", DOWN, 0.47, 5.0, "SELL", 1_002),
        ("top", UP, 0.50, 0.53, 10.0, 15.0),
        ("top", DOWN, 0.47, 0.50, 15.0, 10.0),
]


def test_one_connection_pushes_every_top_change_and_trade() -> None:
    shard, rec = _shard(n=1)
    for i, frame in enumerate(FRAMES):
        shard.handle_event(0, frame, 10_000 + i)
    assert rec.pushed == EXPECTED


def test_a_trailing_connection_never_repeats_or_rewinds_events() -> None:
    shard, rec = _shard()
    lag = 2  # connection 1 delivers each frame two frames later, and slower
    order = []
    for i in range(len(FRAMES) + lag):
        if i < len(FRAMES):
            order.append((0, i))
        if i >= lag:
            order.append((1, i - lag))
    for conn, i in order:
        received = 10_000 + i * 10 + (0 if conn == 0 else 400)
        shard.handle_event(conn, FRAMES[i], received)
    assert rec.pushed == EXPECTED


def test_a_stalled_connection_is_covered_and_its_late_frames_are_dropped() -> None:
    shard, rec = _shard()
    for i in range(3):  # connection 0 delivers three frames, then stalls
        shard.handle_event(0, FRAMES[i], 10_000 + i)
    for i, frame in enumerate(FRAMES):  # connection 1 delivers everything
        shard.handle_event(1, frame, 10_050 + i)
    for i in range(3, len(FRAMES)):  # connection 0 comes back with its backlog
        shard.handle_event(0, FRAMES[i], 15_000 + i)
    assert rec.pushed == EXPECTED
    assert shard.leader(UP) == 1 and shard.leader(DOWN) == 1


def test_leadership_flipping_on_every_frame_never_repeats_a_push() -> None:
    shard, rec = _shard()
    warm = cm.PriceChangeEvent("m", (cm.LevelChange("warm", 0.5, 1.0, "BUY", None, None, "h"),), 0)
    shard.handle_event(0, warm, 2_000)  # connection 0 has been slow, connection 1 fast
    shard.handle_event(1, warm, 100)
    for frame in FRAMES:
        shard.handle_event(0, frame, frame.ts_ms + 150)  # 0 delivers first ...
        shard.handle_event(1, frame, frame.ts_ms + 160)  # ... 1 ties and, being faster, leads
        assert shard.leader(UP) in (0, 1)
    assert rec.pushed == EXPECTED
    assert shard.leader(UP) == 1 and shard.status().leader_switches >= 8


def test_the_freshest_connection_serves_the_book() -> None:
    shard, _ = _shard()
    shard.handle_event(0, _book(UP, 0.50, 0.52, 100), 1_100)
    shard.handle_event(1, _book(UP, 0.50, 0.52, 100), 1_150)
    assert shard.leader(UP) == 0 and shard.top(UP).best_bid == 0.50
    shard.handle_event(1, _change(UP, 0.51, 5.0, "BUY", 200), 1_250)  # connection 1 is ahead
    assert shard.leader(UP) == 1
    assert (shard.top(UP).best_bid, shard.top(UP).bid_size) == (0.51, 5.0)
    assert shard.levels(UP, "bid", 2) == ((0.51, 5.0), (0.50, 10.0))
    shard.handle_event(0, _book(UP, 0.40, 0.60, 150, snapshot=False), 1_260)  # behind
    assert shard.leader(UP) == 1 and shard.top(UP).best_bid == 0.51
    assert shard.top("unknown") is None and shard.levels("unknown", "bid", 3) == ()
    assert shard.leader("unknown") is None
    assert shard.status().leader_switches == 1


def test_a_tie_goes_to_the_clearly_faster_connection() -> None:
    shard, _ = _shard()
    shard.handle_event(0, _book(UP, 0.50, 0.52, 100, snapshot=False), 100 + 900)
    shard.handle_event(1, _book(UP, 0.50, 0.52, 100, snapshot=False), 100 + 100)
    assert shard.leader(UP) == 1  # same place in the stream, 800 ms faster
    shard.handle_event(0, _change(UP, 0.50, 11.0, "BUY", 300), 300 + 900)
    assert shard.leader(UP) == 0  # ahead wins, however slow
    shard.handle_event(1, _change(UP, 0.50, 11.0, "BUY", 300), 300 + 110)
    assert shard.leader(UP) == 1
    close, _ = _shard()
    close.handle_event(0, _book(UP, 0.50, 0.52, 100, snapshot=False), 100 + 110)
    close.handle_event(1, _book(UP, 0.50, 0.52, 100, snapshot=False), 100 + 100)
    assert close.leader(UP) == 0  # 10 ms faster is within the margin: keep the leader


def test_each_connection_keeps_its_own_books() -> None:
    shard, _ = _shard()
    shard.handle_event(0, _book(UP, 0.50, 0.52, 100), 1)
    shard.handle_event(1, _book(UP, 0.10, 0.90, 90), 1)
    book0, book1 = shard.book(0, UP), shard.book(1, UP)
    assert book0 is not None and book1 is not None and book0 is not book1
    shard.handle_event(1, _change(UP, 0.20, 5.0, "BUY", 95), 2)
    assert book0.levels("bid", 5) == ((0.50, 10.0),)
    assert book1.levels("bid", 5) == ((0.20, 5.0), (0.10, 10.0))
    shard.handle_event(0, _change(UP, 0.51, 1.0, "BUY", 110), 3)
    assert book1.levels("bid", 5) == ((0.20, 5.0), (0.10, 10.0))
    assert shard.book(1, DOWN).levels("ask", 5) == ((0.80, 5.0),)  # its own mirror only
    assert shard.book(0, DOWN).levels("ask", 5) == ((0.49, 1.0),)
    assert shard.top(UP).best_bid == 0.51  # connection 0 is the freshest


def test_removed_tokens_leave_every_connection_and_the_served_state() -> None:
    shard, _ = _shard()
    for conn in (0, 1):
        shard.handle_event(conn, _book(UP, 0.5, 0.6, 100), 1)
        shard.handle_event(conn, _book(DOWN, 0.4, 0.5, 100), 1)
    shard.set_tokens({DOWN})
    assert shard.top(UP) is None and shard.book(0, UP) is None and shard.book(1, UP) is None
    assert shard.top(DOWN) is not None
    assert all(c.stream.desired == frozenset({DOWN}) for c in shard._conns)
    shard.handle_event(0, _book(UP, 0.5, 0.6, 200), 2)  # no longer followed
    assert shard.top(UP) is None and shard.book(0, UP) is None


def test_other_events_are_forwarded() -> None:
    shard, rec = _shard()
    resolved = cm.MarketResolvedEvent("1", "m", (UP, DOWN), UP, "Up", 5, ())
    shard.handle_event(0, resolved, 1)
    shard.handle_event(1, resolved, 2)
    assert rec.other == [resolved, resolved]  # the hub pushes it once per market


def test_trade_memory_is_bounded() -> None:
    shard, rec = _shard(trade_memory=4)
    for ts in range(10):
        shard.handle_event(0, _trade(UP, 0.5, 1.0, "BUY", ts), ts)
    shard.handle_event(1, _trade(UP, 0.5, 1.0, "BUY", 9), 20)  # remembered: dropped
    assert len([p for p in rec.pushed if p[0] == "trade"]) == 10
    assert len(shard._trades_pushed[UP]) == 4
    assert len(shard._conns[0].trades_seen[UP]) == 4


def test_distinct_trades_that_look_alike_are_all_pushed() -> None:
    # Live on 2026-09-16, one socket: two trades on one token in the same millisecond,
    # both 5 shares at 0.22 bought, with different transaction hashes.
    first = cm.LastTradeEvent("m", UP, 0.22, 5.0, "BUY", 1_000, "0xc08d")
    second = cm.LastTradeEvent("m", UP, 0.22, 5.0, "BUY", 1_000, "0x48b6")
    single, alone = _shard(n=1)
    hedged, pair = _shard(n=2)
    for event in (first, second):
        single.handle_event(0, event, 1_050)
        hedged.handle_event(0, event, 1_050)
    for event in (first, second):
        hedged.handle_event(1, event, 1_090)  # the hedge delivers both, later
    assert [t.transaction_hash for t in alone.trades] == ["0xc08d", "0x48b6"]
    assert [t.transaction_hash for t in pair.trades] == ["0xc08d", "0x48b6"]


def test_identical_fills_count_per_connection() -> None:
    # One transaction can fill several makers at the same price and size.
    fill = cm.LastTradeEvent("m", UP, 0.5, 5.0, "BUY", 2_000, "0xaa")
    shard, rec = _shard()
    shard.handle_event(1, fill, 2_040)  # connection 1 is ahead: two fills
    shard.handle_event(1, fill, 2_041)
    shard.handle_event(0, fill, 2_090)  # connection 0 delivers the same two
    shard.handle_event(0, fill, 2_091)
    assert len(rec.trades) == 2
    shard.handle_event(0, fill, 2_092)  # a third, seen first on connection 0
    shard.handle_event(1, fill, 2_093)
    assert len(rec.trades) == 3
    shard._conns[1].stream.session += 1  # replaced: no replay, so its counts start over
    shard.handle_event(1, _book(UP, 0.5, 0.6, 2_100), 2_140)
    assert shard._conns[1].trades_seen == {}


def test_status_reports_served_latency_staleness_and_connections() -> None:
    clock = {"t": 50.0}
    shard, _ = _shard(time_fn=lambda: clock["t"])
    shard.handle_event(0, _book(UP, 0.5, 0.6, 40_000), 40_010)  # snapshot: no sample
    shard.handle_event(0, _change(UP, 0.5, 2.0, "BUY", 41_000), 41_100)  # first: 100 ms
    shard.handle_event(1, _change(UP, 0.5, 2.0, "BUY", 41_000), 41_900)  # late copy
    shard.handle_event(1, _change(UP, 0.5, 3.0, "BUY", 42_000), 42_300)  # first: 300 ms
    st = shard.status()
    # Each served event counts once, for both of its tokens.
    assert (st.served_latency_ms_p50, st.served_latency_ms_max) == (300.0, 300.0)
    assert st.served_latency_ms_p90 == 300.0
    assert st.served_staleness_s == pytest.approx(50.0 - 42.0)
    assert (st.name, len(st.connections), st.connected, st.desired) == ("btc-5m", 2, 0, 2)
    assert (st.leader_switches, st.stalls_avoided, st.recycles) == (2, 0, 0)  # UP and DOWN
    assert st.stall_episodes == (0, 0)
    empty, _ = _shard()
    assert empty.status().served_latency_ms_p50 is None
    assert empty.status().served_staleness_s is None


def _up(shard: sh.ClobShard, *connected: int) -> None:
    for c in shard._conns:
        c.stream._connected = c.index in connected


def test_a_connection_three_seconds_behind_is_recycled_while_the_other_serves(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list = []

    class Log:
        def warning(self, event: str, **kw) -> None:
            events.append((event, kw))

        info = warning

    monkeypatch.setattr(sh, "log", Log())
    shard, _ = _shard()
    _up(shard, 0, 1)
    shard.handle_event(0, _book(UP, 0.5, 0.6, 20_000), 1)
    shard.handle_event(1, _book(UP, 0.5, 0.6, 17_500), 1)
    shard._check()
    assert shard._conns[1].stream._reconnect_reason is None  # 2.5 s: stalled, not recycled
    assert shard.status().stall_episodes == (0, 1) and shard.status().stalls_avoided == 1
    shard.handle_event(0, _change(UP, 0.5, 2.0, "BUY", 20_600), 2)
    shard._check()
    assert shard._conns[1].stream._reconnect_reason == (
        "recycled: 3.1s behind the freshest connection")
    assert shard._conns[0].stream._reconnect_reason is None
    assert events == [("marketdata.clob_recycle",
                       {"shard": "btc-5m", "connection": 1, "lag_ms": 3100,
                        "reason": "behind the freshest connection"})]
    shard._check()
    assert shard.status().recycles == 1  # one request until that connection is replaced
    shard._conns[1].stream.session += 1  # the replacement connection opened
    shard._check()
    assert shard.status().recycles == 1  # not judged again until it delivers data
    assert shard._conns[1].pending_session is None
    shard.handle_event(1, _book(UP, 0.5, 0.6, 20_600), 3)
    assert shard._conns[1].newest_ms == 20_600
    shard._check()
    assert shard.top(UP).bid_size == 2.0  # connection 0 kept serving
    assert shard.status().stall_episodes == (0, 1) and shard.status().recycles == 1


def test_reads_say_whether_a_connection_that_is_up_serves_them() -> None:
    shard, rec = _shard(n=1)
    shard.handle_event(0, _book(UP, 0.5, 0.6, 1_000), 1_050)
    shard.handle_event(0, _book(DOWN, 0.4, 0.5, 1_000), 1_050)
    assert shard.top(UP).live is False  # the connection is not up
    _up(shard, 0)
    assert shard.top(UP).live is True
    # A sweep in one millisecond: two fills, then the book change the socket never got.
    shard.handle_event(0, _trade(UP, 0.6, 1.0, "BUY", 1_500), 1_550)
    shard.handle_event(0, _trade(UP, 0.6, 2.0, "BUY", 1_500), 1_550)
    _up(shard)  # the socket drops: the last values stay, marked as not live
    top = shard.top(UP)
    assert (top.live, top.best_bid, top.best_ask) == (False, 0.5, 0.6)
    _up(shard, 0)
    shard._conns[0].stream.session += 1  # a new connection, nothing received yet
    assert shard.top(UP).live is False
    shard.handle_event(0, _book(UP, 0.5, 0.7, 1_500), 1_600)  # its snapshot, for UP only
    assert (shard.top(UP).live, shard.top(UP).best_ask) == (True, 0.7)
    assert rec.pushed[-1] == ("top", UP, 0.5, 0.7, 10.0, 10.0)  # the new top is news
    assert shard.book(0, DOWN) is None  # the old connection's books are gone
    assert (shard.top(DOWN).live, shard.top(DOWN).best_bid) == (False, 0.4)
    assert shard.levels(DOWN, "bid", 3) == ((0.4, 10.0),)  # the last levels seen
    shard.handle_event(0, _book(DOWN, 0.3, 0.5, 1_500), 1_600)
    assert (shard.top(DOWN).live, shard.top(DOWN).best_bid) == (True, 0.3)
    assert shard.leader(UP) == shard.leader(DOWN) == 0


def test_a_connection_that_drops_hands_its_tokens_to_one_that_is_up() -> None:
    shard, rec = _shard()
    _up(shard, 0, 1)
    shard.handle_event(0, _book(UP, 0.5, 0.6, 1_000), 1_050)
    shard.handle_event(1, _book(UP, 0.5, 0.6, 1_000), 1_080)
    shard.handle_event(0, _change(UP, 0.5, 7.0, "BUY", 1_100), 1_150)
    assert shard.leader(UP) == 0 and shard.top(UP).live
    _up(shard, 1)  # connection 0 drops before connection 1 got that change
    assert shard.top(UP).live is False and shard.top(DOWN).live is False
    shard._check()
    assert shard.leader(UP) == 1 and shard.leader(DOWN) is None  # 1 has no DOWN book yet
    assert (shard.top(UP).live, shard.top(UP).bid_size) == (True, 10.0)  # live, behind
    shard.handle_event(1, _change(UP, 0.5, 7.0, "BUY", 1_100), 1_160)  # it catches up
    assert shard.top(UP).bid_size == 7.0 and shard.top(DOWN).live is True
    assert rec.pushed.count(("top", UP, 0.5, 0.6, 7.0, 10.0)) == 1  # pushed once only
    shard._conns[0].stream.session += 1  # connection 0 comes back
    _up(shard, 0, 1)
    shard._check()
    assert shard.book(0, UP) is None and shard.leader(UP) == 1
    shard.handle_event(0, _book(UP, 0.5, 0.6, 1_100, bid_size=7.0), 1_190)  # its snapshot
    assert shard.leader(UP) == 1 and shard.top(UP).live  # a tie: 1 keeps serving
    _up(shard)
    shard._check()  # nothing is up: no connection serves, the values stay
    assert shard.leader(UP) is None
    assert (shard.top(UP).live, shard.top(UP).bid_size) == (False, 7.0)


ANNOUNCEMENT = cm.NewMarketEvent("0xm", "some-new-market", "q", ("Yes", "No"), ("y", "n"),
                                 "0xm", True, 0.01, None, 1)


@pytest.mark.parametrize("replacement_gets", ["nothing", "only announcements"])
def test_a_replacement_without_book_data_is_recycled(replacement_gets: str) -> None:
    clock = {"t": 100.0}
    shard, rec = _shard(time_fn=lambda: clock["t"])
    _up(shard, 0, 1)
    for conn in (0, 1):
        shard.handle_event(conn, _book(UP, 0.5, 0.6, 20_000), 20_050)
    shard._conns[1].stream.session += 1  # connection 1 was replaced
    shard._check()
    reasons = []
    for k in range(1, 7):  # connection 0 keeps serving, one event a second
        clock["t"] += 1
        ts = 20_000 + k * 1_000
        shard.handle_event(0, _change(UP, 0.5, float(k), "BUY", ts), ts + 50)
        if replacement_gets == "only announcements":
            shard.handle_event(1, ANNOUNCEMENT, ts + 60)
        shard._check()
        reasons.append(shard._conns[1].stream._reconnect_reason)
    assert reasons[:3] == [None, None, None]  # 3 s is allowed for the snapshot
    assert reasons[3] == "recycled: 4.0s without book data since it connected"
    assert shard.status().recycles == 1 and shard._conns[0].stream._reconnect_reason is None
    assert rec.other.count(ANNOUNCEMENT) == (6 if replacement_gets != "nothing" else 0)


def test_a_replacement_is_given_time_even_when_the_front_jumps() -> None:
    clock = {"t": 100.0}
    shard, _ = _shard(time_fn=lambda: clock["t"])
    _up(shard, 0, 1)
    shard.handle_event(0, _book(UP, 0.5, 0.6, 10_000), 20_050)  # a snapshot: an old stamp
    shard.handle_event(1, _book(UP, 0.5, 0.6, 10_000), 20_050)
    shard._conns[1].stream.session += 1
    shard._check()
    clock["t"] += 0.5
    shard.handle_event(0, _change(UP, 0.5, 2.0, "BUY", 20_000), 20_050)  # 10 s ahead at once
    shard._check()
    assert shard._conns[1].stream._reconnect_reason is None
    clock["t"] += 0.2
    shard.handle_event(1, _book(UP, 0.5, 0.6, 20_000, bid_size=2.0), 20_060)  # its snapshot
    for _ in range(10):
        clock["t"] += 1
        shard._check()
    assert shard._conns[1].stream._reconnect_reason is None and shard.status().recycles == 0


def test_a_replacement_without_data_stays_while_no_other_connection_serves() -> None:
    clock = {"t": 100.0}
    shard, _ = _shard(time_fn=lambda: clock["t"])
    _up(shard, 1)  # connection 0, the one with data, is down
    shard.handle_event(0, _book(UP, 0.5, 0.6, 20_000), 20_050)
    shard._conns[1].stream.session += 1
    for k in range(1, 7):
        clock["t"] += 1
        shard.handle_event(0, _change(UP, 0.5, float(k), "BUY", 20_000 + k * 1_000), 1)
        shard._check()
    assert shard._conns[1].stream._reconnect_reason is None and shard.status().recycles == 0


def test_nothing_is_recycled_without_another_fresh_connection() -> None:
    shard, _ = _shard()
    shard.handle_event(0, _book(UP, 0.5, 0.6, 20_000), 1)
    shard.handle_event(1, _book(UP, 0.5, 0.6, 10_000), 1)
    _up(shard, 1)  # the fresh connection is down
    shard._check()
    assert shard._conns[1].stream._reconnect_reason is None
    assert shard.status().stalls_avoided == 0 and shard.status().recycles == 0


def test_both_behind_their_best_recycles_one_at_a_time(monkeypatch) -> None:
    shard, _ = _shard()
    _up(shard, 0, 1)
    for c in shard._conns:
        monkeypatch.setattr(c.stream, "behind_best_ms", lambda: 12_000.0)
    shard._check()
    reasons = [c.stream._reconnect_reason for c in shard._conns]
    assert reasons == ["recycled: 12.0s behind its own best", None]
    shard._check()
    assert [c.stream._reconnect_reason for c in shard._conns][1] is None


def test_a_single_connection_keeps_the_stream_rules() -> None:
    single, rec = _shard(n=1)
    double, _ = _shard(n=2)
    assert single._conns[0].stream._max_lag_ms == MAX_LAG_S * 1000
    assert all(c.stream._max_lag_ms == 0 for c in double._conns)
    single.handle_event(0, _book(UP, 0.5, 0.6, 20_000), 1)
    _up(single, 0)
    single._check()
    assert single._conns[0].stream._reconnect_reason is None
    assert len(rec.pushed) == 1


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
        return await self.incoming.get()


class Connector:
    def __init__(self) -> None:
        self.made: list[FakeWs] = []

    def __call__(self, url: str) -> FakeWs:
        self.made.append(FakeWs())
        return self.made[-1]


async def until(pred, timeout: float = 3.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not pred():
        if loop.time() > end:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.005)


def _book_json(token: str, bid: str, ts: int) -> str:
    return ('[{"market":"m","asset_id":"%s","bids":[{"price":"%s","size":"10"}],'
            '"asks":[{"price":"0.9","size":"10"}],"timestamp":"%d","hash":"h",'
            '"event_type":"book","tick_size":"0.01"}]' % (token, bid, ts))


def _change_json(token: str, price: str, size: str, ts: int) -> str:
    return ('{"market":"m","timestamp":"%d","event_type":"price_change","price_changes":'
            '[{"asset_id":"%s","price":"%s","size":"%s","side":"BUY","hash":"h"}]}'
            % (ts, token, price, size))


@pytest.mark.asyncio
async def test_run_recycles_a_lagging_connection_and_keeps_serving() -> None:
    shard, _ = _shard(supervise_s=0.01)
    connectors = [Connector(), Connector()]
    for c, connector in zip(shard._conns, connectors):
        c.stream._connect = connector
        c.stream._backoff._initial_s = c.stream._backoff._next_s = 0.001
    stop = asyncio.Event()
    task = asyncio.create_task(shard.run(stop))
    try:
        await until(lambda: all(c.made and c.made[0].sent for c in connectors))
        fast, slow = connectors[0].made[0], connectors[1].made[0]
        fast.incoming.put_nowait(_book_json(UP, "0.5", 20_000))
        slow.incoming.put_nowait(_book_json(UP, "0.5", 16_000))
        fast.incoming.put_nowait(_change_json(UP, "0.5", "7", 20_500))
        await until(lambda: len(connectors[1].made) == 2 and connectors[1].made[1].sent)
        assert slow.exited is True and len(connectors[0].made) == 1
        assert shard._conns[0].stream.connected and shard.top(UP).bid_size == 7.0
        assert shard.status().recycles == 1
        replacement = connectors[1].made[1]
        replacement.incoming.put_nowait(_book_json(UP, "0.5", 20_500))
        await until(lambda: shard._conns[1].newest_ms == 20_500)
        await asyncio.sleep(0.05)
        assert shard.status().recycles == 1 and shard.status().connected == 2
    finally:
        stop.set()
        loop = asyncio.get_running_loop()
        started = loop.time()
        await asyncio.wait_for(task, timeout=3)
        assert loop.time() - started < 1.5
