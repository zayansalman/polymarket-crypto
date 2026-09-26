"""FEEDS card: live rows built from the market-data hub's snapshot."""
from __future__ import annotations

import dataclasses

from polymarket_exec.marketdata import clob_shard as sh
from polymarket_exec.marketdata import clob_stream as cs
from polymarket_exec.marketdata import hub as md_hub
from polymarket_exec.marketdata import rest_poll as rp
from polymarket_exec.marketdata import rtds_stream as rs
from polymarket_exec.ops.dashboard.panels import feeds



HT = 5_000_000.0
MD_NAMES = ["Polymarket books", "Chainlink prices", "Chainlink 60s TWAP", "Binance prices"]
ASSETS = ("btc", "eth", "sol", "xrp", "doge", "bnb")
TIMEFRAMES = ("5m", "15m", "1h", "1d")
STREAMING, LINGERING, AVAILABLE = md_hub.STREAMING, md_hub.LINGERING, md_hub.AVAILABLE
DOWN_CONN = dict(connected=False, connected_since=None, subscribed=0)


def _clob(**kw) -> cs.StreamStatus:
    base = dict(connected=True, connected_since=HT - 300, last_frame_at=HT - 1,
                last_pong_at=HT - 3, frames_total=1000, frames_per_s=400.0,
                latency_ms_p50=48.0, latency_ms_p90=120.0, reconnects=0, resyncs=0,
                subscribed=4, desired=4, unknown=0, handler_errors=0, last_error=None,
                last_notice=None)
    base.update(kw)
    return cs.StreamStatus(**base)


def _src(**kw) -> rs.SourceStatus:
    base = dict(connected=True, last_update_age_s=0.4, newest_obs_ms=int((HT - 1.5) * 1000),
                points=900, updates=5000, snapshots=6, latency_ms_p50=1400.0, gaps=0,
                assets=6, reconnects=0, handler_errors=0, last_error=None)
    base.update(kw)
    return rs.SourceStatus(**base)


def _market(name: str, state: str = STREAMING, *, conns: tuple | None = None,
            owners: tuple[str, ...] = ("bot loop",), since: float = HT - 300,
            p50: float | None = 48.0, p90: float | None = 90.0, kib_s: float = 12.0,
            tokens: int = 4, left: float | None = None, hot: bool = False
            ) -> tuple[md_hub.GridMarket, sh.ShardStatus]:
    """One grid market and its socket group's status (the defaults: in use and healthy)."""
    asset, timeframe = name.split("-")
    if state == AVAILABLE:
        conns = conns or (_clob(**DOWN_CONN, last_frame_at=None, latency_ms_p50=None),)
        since, p50, p90, kib_s, tokens = None, None, None, 0.0, 0
    conns = conns or (_clob(),)
    up = sum(1 for c in conns if c.connected)
    market = md_hub.GridMarket(
        asset, timeframe, state, owners if state == STREAMING else (), since, left, up,
        len(conns), p50, p90, kib_s * 1024, tokens, hot)
    shard = sh.ShardStatus(
        name=name, connections=tuple(conns), connected=up, desired=tokens,
        served_latency_ms_p50=p50, served_latency_ms_p90=p90, served_latency_ms_max=p90,
        served_staleness_s=None, leader_switches=0, stalls_avoided=0, recycles=0,
        stall_episodes=tuple(0 for _ in conns), bytes_per_s=kib_s * 1024)
    return market, shard


def _md(*entries: tuple[md_hub.GridMarket, sh.ShardStatus], prices: dict | None = None,
        ages: dict | None = None, started_at: float = HT - 600, gamma_errors: int = 0,
        gamma_last_error: str | None = None,
        rest_poll: rp.PollStatus | None = None,
        gamma_last_error_at: float | None = None) -> md_hub.MarketDataSnapshot:
    """A hub snapshot of the default grid: the markets given, every other one AVAILABLE."""
    given = {f"{m.asset}-{m.timeframe}": (m, s) for m, s in entries}
    grid: dict[str, md_hub.GridMarket] = {}
    shards: dict[str, sh.ShardStatus] = {}
    for asset in ASSETS:
        for timeframe in TIMEFRAMES:
            name = f"{asset}-{timeframe}"
            grid[name], shards[name] = given.get(name) or _market(name, AVAILABLE)
    clob = md_hub.merge_shard_status(shards)
    return md_hub.MarketDataSnapshot(
        taken_at=HT, started_at=started_at, clob=clob, clob_shards=shards,
        slowest_shard=md_hub.slowest_shard(shards),
        prices=prices or {s: _src() for s in rs.SOURCES},
        price_ages=ages if ages is not None else {s: 1.5 for s in rs.SOURCES},
        markets=sum(2 for m in grid.values() if m.state != AVAILABLE),
        tokens=sum(m.tokens for m in grid.values()), subscribed=clob.subscribed,
        gamma_lookups=60, gamma_errors=gamma_errors, gamma_last_error=gamma_last_error,
        gamma_last_error_at=gamma_last_error_at,
        listeners=0, listener_drops=0, grid=grid, assets=ASSETS, timeframes=TIMEFRAMES,
        clob_kib_s=clob.bytes_per_s / 1024, rtds_kib_s=3.0, rest_poll=rest_poll,
    )


def _md_rows(md: md_hub.MarketDataSnapshot) -> dict[str, feeds.FeedRow]:
    return {r.name: r for r in feeds.build_rows(md) if r.name in MD_NAMES}


def _books(*entries, **kw) -> feeds.FeedRow:
    return _md_rows(_md(*entries, **kw))["Polymarket books"]



def test_no_hub_shows_one_off_row() -> None:
    rows = feeds.build_rows(None)
    assert [(r.name, r.status, r.level, r.used_by) for r in rows] == [
        ("Polymarket books", "OFF", "idle", "none")]
    html = feeds.render(None)
    assert ">OFF</span>" in html and "all OK" in html and "feeds-grid" not in html


def test_rows_are_the_books_then_the_prices() -> None:
    rows = feeds.build_rows(_md(_market("btc-5m")))
    assert [r.name for r in rows] == MD_NAMES
    assert [r.name for r in rows if r.grid is not None] == ["Polymarket books"]


CARD_COLUMNS = [
    # (feed, connection, used for, used by), in card order
    ("Polymarket books", "WebSocket · CLOB · on demand",
     "Up/Down books · trades (2 of 24 in use)", "bot loop · order ticket"),
    ("Chainlink prices", "WebSocket · RTDS", "spot · vol", "Fade 1h Momentum on 15m"),
    ("Chainlink 60s TWAP", "WebSocket · RTDS", "5m·15m settle ref", "Fade 1h Momentum on 15m"),
    ("Binance prices", "WebSocket · RTDS", "1h·1d settle ref", "Fade 1h Momentum on 15m"),
]


def test_the_fresh_rest_poll_shows_on_the_books_hover_and_on_its_markets() -> None:
    poll = rp.PollStatus(tokens=4, polls=600, errors=2, ahead=150, behind=440, same=10,
                         ahead_ms_p50=120.0, rtt_ms_p50=201.0, rate_hz=2.0,
                         last_error="HTTPStatusError: 429")
    md = _md(_market("btc-5m", hot=True), _market("eth-1h"), rest_poll=poll)
    row = _md_rows(md)["Polymarket books"]
    assert "fresh REST poll on 4 tokens, 2/s" in row.source
    assert "ahead of the sockets 25% of reads (by 120ms)" in row.source
    assert "round trip 201ms" in row.source and "2 failed" in row.source
    cells = {c.market: c for _asset, row_cells in row.grid.rows
             for c in row_cells if c}
    assert "fresh REST poll" in cells["btc-5m"].title
    assert "fresh REST poll" not in cells["eth-1h"].title
    # No poll running (nothing hot): nothing about it on the hover.
    quiet = _md(_market("btc-5m"))
    assert _md_rows(quiet)["Polymarket books"].source == feeds.BOOKS_SOURCE



def test_every_row_says_how_it_connects_what_it_is_for_and_who_uses_it() -> None:
    md = _md(_market("btc-5m", owners=("order ticket", "bot loop")),
             _market("eth-1h", owners=("bot loop",)))
    rows = feeds.build_rows(md)
    assert [(r.name, r.connection, r.role, r.used_by) for r in rows] == CARD_COLUMNS
    assert all(r.source for r in rows)  # the endpoint behind each connection, on hover


def test_render_shows_the_six_columns_and_the_endpoint_on_hover() -> None:
    html = feeds.render(_md(_market("btc-5m")))
    assert ("<thead><tr><th>Feed</th><th>Connection</th><th>Used for</th><th>Used by</th>"
            "<th class='feeds-delay'>Delay</th><th>Status</th></tr></thead>") in html
    assert ("<tr><td>Chainlink prices</td>"
            f"<td class='feeds-conn' title='RTDS · {rs.TOPICS[rs.CHAINLINK]}'>WebSocket · RTDS</td>"
            "<td class='feeds-role'>spot · vol</td>"
            "<td class='feeds-role'>Fade 1h Momentum on 15m</td>"
            "<td class='feeds-delay'>1.5s</td><td><span class='feed on'>OK</span></td>"
            "</tr>") in html
    assert ("<td class='feeds-conn' title='CLOB market channel'>"
            "WebSocket · CLOB · on demand</td>") in html


def test_healthy_marketdata_rows() -> None:
    rows = _md_rows(_md(_market("btc-5m")))
    books = rows["Polymarket books"]
    assert (books.connection, books.delay, books.status, books.level, books.used_by) == (
        "WebSocket · CLOB · on demand", "48ms", "OK", "on", "bot loop")
    assert [(rows[n].connection, rows[n].role, rows[n].delay, rows[n].status)
            for n in MD_NAMES[1:]] == [
        ("WebSocket · RTDS", "spot · vol", "1.5s", "OK"),
        ("WebSocket · RTDS", "5m·15m settle ref", "1.5s", "OK"),
        ("WebSocket · RTDS", "1h·1d settle ref", "1.5s", "OK"),
    ]
    html = feeds.render(_md(_market("btc-5m")))
    assert "Polymarket books" in html and "Chainlink 60s TWAP" in html


def test_the_market_grid_shows_every_market_by_state() -> None:
    md = _md(
        _market("btc-5m", owners=("bot loop", "smoke"), p50=30.0, p90=85.0, kib_s=210.4),
        _market("btc-15m", conns=(_clob(**DOWN_CONN),), since=HT - 5, p50=None, p90=None,
                kib_s=0.0),
        _market("btc-1h", conns=(_clob(**DOWN_CONN, last_error="OSError: reset"),
                                 _clob(**DOWN_CONN)), p50=110.0),
        _market("btc-1d", LINGERING, left=42.4, p50=95.0, p90=180.0, kib_s=1.5),
        _market("eth-5m", p50=6_200.0, p90=9_000.0),
        _market("eth-1d", p50=None, p90=None),  # connected, no live event yet
    )
    grid = _md_rows(md)["Polymarket books"].grid
    assert grid is not None and grid.timeframes == TIMEFRAMES
    assert [asset for asset, _ in grid.rows] == list(ASSETS)
    cells = {c.market: c for _, row in grid.rows for c in row}
    assert len(cells) == 24

    def show(name: str) -> tuple[str, str, str]:
        return cells[name].state, cells[name].text, cells[name].level

    assert show("btc-5m") == (STREAMING, "30ms", "on")
    assert show("btc-15m") == (STREAMING, "connecting", "idle")
    assert show("btc-1h") == (STREAMING, "down", "down")
    assert show("btc-1d") == (LINGERING, "lingering", "linger")
    assert show("eth-5m") == (STREAMING, "6.2s", "warn")
    assert show("eth-1d") == (STREAMING, "ok", "on")
    assert show("sol-1h") == (AVAILABLE, "available", "idle")
    assert cells["btc-5m"].title == (
        "btc 5m · used by bot loop, smoke · connections 1/1 · p50 30ms · p90 85ms · "
        "210.4 KiB/s")
    assert cells["btc-1h"].title == (
        "btc 1h · used by bot loop · connections 0/2 · p50 110ms · p90 90ms · 12.0 KiB/s · "
        "OSError: reset")
    assert cells["btc-1d"].title == (
        "btc 1d · lingering: nobody uses it; its sockets stop in 42s · connections 1/1 · "
        "p50 95ms · p90 180ms · 1.5 KiB/s")
    assert cells["eth-1d"].title.endswith("p50 — · p90 — · 12.0 KiB/s")
    assert cells["sol-1h"].title == "sol 1h · available: not streaming (nobody uses it)"
    html = feeds.render(md)
    assert ("<tr class='feeds-grid-row'><td colspan='6'><table class='feeds-grid'>"
            "<thead><tr><th></th><th>5m</th><th>15m</th><th>1h</th><th>1d</th></tr></thead>"
            ) in html
    assert ("<tr><th>BTC</th><td><span class='feed on' title='btc 5m · used by bot loop, "
            "smoke · connections 1/1 · p50 30ms · p90 85ms · 210.4 KiB/s'>30ms</span></td>"
            ) in html
    assert html.count(">available</span>") == 18
    assert html.count("<span class='feed linger' title='btc 1d · lingering") == 1


def test_a_grid_market_outside_the_grid_is_a_blank_cell() -> None:
    md = _md(_market("btc-5m"))
    grid = dict(md.grid)
    del grid["bnb-1h"]  # e.g. an asset without hourly markets
    cells = dict(_md_rows(dataclasses.replace(md, grid=grid))["Polymarket books"].grid.rows)
    assert cells["bnb"][2] is None
    html = feeds.render(dataclasses.replace(md, grid=grid))
    assert "<td class='feeds-grid-none'>—</td>" in html


def test_books_summary_counts_only_the_markets_in_use() -> None:
    idle = _books()
    assert (idle.status, idle.level, idle.delay, idle.used_by) == ("IDLE", "idle", "—", "none")
    assert idle.role == "Up/Down books · trades (0 of 24 in use)"
    assert idle.detail == "no market in use; 24 available"
    # A lingering market does not count, even with its sockets down.
    dead = _clob(**DOWN_CONN, last_error="OSError: reset")
    lingering = _books(_market("btc-5m", LINGERING, conns=(dead,), left=30.0, p50=9_000.0))
    assert (lingering.status, lingering.level, lingering.delay, lingering.detail) == (
        "IDLE", "idle", "—", "no market in use; 23 available, 1 lingering")
    ok = _books(_market("btc-5m", owners=("order ticket", "bot loop")),
                _market("eth-1h", owners=("bot loop",), p50=130.0),
                _market("sol-1d", LINGERING, p50=4_000.0))
    assert (ok.status, ok.level, ok.delay, ok.delay_warn, ok.detail) == (
        "OK", "on", "130ms", False, None)
    assert (ok.used_by, ok.role) == (
        "bot loop · order ticket", "Up/Down books · trades (2 of 24 in use)")


def test_books_row_states() -> None:
    down_conn = _clob(**DOWN_CONN, last_error="ConnectionClosedError: no close frame received")
    booting = _books(_market("btc-5m", conns=(_clob(**DOWN_CONN),), since=HT - 10, p50=None))
    assert (booting.status, booting.level, booting.delay) == ("CONNECTING", "idle", "—")
    down = _books(_market("btc-5m", conns=(down_conn,)))
    assert (down.status, down.level, down.detail) == (
        "DOWN", "down", "btc-5m: ConnectionClosedError: no close frame received")
    stale = _books(_market("btc-5m", conns=(_clob(last_frame_at=HT - 46),)))
    assert (stale.status, stale.level, stale.delay_warn) == ("STALE", "warn", True)
    assert stale.detail == "btc-5m: connected, but no data for 46s"
    assert _books(_market("btc-5m", conns=(_clob(last_frame_at=HT - 45),))).status == "OK"
    # Connected a moment ago with no data yet: timed from the connect, not an old frame.
    fresh = _books(_market("btc-5m", conns=(_clob(connected_since=HT - 5,
                                                  last_frame_at=HT - 400),)))
    assert fresh.status == "OK"
    slow = _books(_market("btc-5m", p50=2500.0))
    assert (slow.status, slow.delay, slow.delay_warn, slow.detail) == (
        "OK", "2.5s", True, "slowest: btc-5m")
    lagging = _books(_market("btc-5m"), _market("eth-5m", p50=12_300.0))
    assert (lagging.status, lagging.level, lagging.delay, lagging.delay_warn) == (
        "STALE", "warn", "12s", True)
    assert lagging.detail == "eth-5m: 12s behind (served latency)"
    assert _books(_market("btc-5m", p50=5_000.0)).status == "OK"
    hedged = _books(_market("btc-5m", conns=(_clob(), _clob(**DOWN_CONN))), _market("eth-5m"))
    assert (hedged.status, hedged.level, hedged.detail) == (
        "OK", "on", "btc-5m: 1 of 2 connections reconnecting; still served")
    joining = _books(_market("btc-5m"),
                     _market("eth-5m", conns=(_clob(**DOWN_CONN),), since=HT - 3, p50=None))
    assert (joining.status, joining.level, joining.delay, joining.detail) == (
        "OK", "on", "48ms", "eth-5m: connecting")
    no_tokens = _books(_market("btc-5m", conns=(_clob(**DOWN_CONN),), tokens=0, p50=None),
                       gamma_errors=12, gamma_last_error_at=HT - 5,
                       gamma_last_error="ConnectError: [Errno 8] nodename nor servname")
    assert (no_tokens.status, no_tokens.detail) == (
        "DOWN", "btc-5m: no market tokens yet (Gamma lookups failing: "
        "ConnectError: [Errno 8] nodename nor servname)")
    # An old Gamma error is history: it no longer explains why a market has no tokens.
    old_error = _books(_market("btc-5m", conns=(_clob(**DOWN_CONN),), tokens=0, p50=None),
                       gamma_errors=12, gamma_last_error_at=HT - 600,
                       gamma_last_error="ConnectError: an old one")
    assert (old_error.status, old_error.detail) == ("DOWN", "btc-5m: no market tokens yet")
    looking_up = _books(_market("btc-5m", conns=(_clob(**DOWN_CONN),), tokens=0,
                                since=HT - 2, p50=None))
    assert (looking_up.status, looking_up.level, looking_up.detail) == (
        "CONNECTING", "idle", "btc-5m: looking up its windows")
    reset = _clob(**DOWN_CONN, last_error="OSError: reset")
    partial = _books(_market("btc-5m", conns=(_clob(), _clob(**DOWN_CONN))),
                     _market("eth-5m"), _market("sol-1d", conns=(reset,)))
    assert (partial.status, partial.level, partial.detail) == (
        "DOWN", "down", "1 of 3 markets in use down; sol-1d: OSError: reset")
    all_down = _books(_market("sol-1d", conns=(reset,)),
                      _market("bnb-1d", conns=(_clob(**DOWN_CONN),)))
    assert all_down.detail == "sol-1d: OSError: reset (+1 more)"


def test_the_issue_count_ignores_lingering_and_available_markets() -> None:
    dead = _clob(**DOWN_CONN, last_error="OSError: reset")
    md = _md(_market("btc-5m"),
             _market("btc-1h", LINGERING, conns=(dead,), left=20.0, p50=9_000.0))
    html = feeds.render(md)
    assert "all OK" in html and "class='feed linger'" in html
    down = _md(_market("btc-5m", conns=(dead,)), _market("eth-5m", conns=(dead,)))
    assert ">1 issue<" in feeds.render(down)  # one row, two markets


def test_price_row_states() -> None:
    def twap(**kw) -> feeds.FeedRow:
        return _md_rows(_md(**kw))["Chainlink 60s TWAP"]

    ages = {s: 1.5 for s in rs.SOURCES}
    stale = twap(ages={**ages, rs.CHAINLINK_TWAP60: 11.0})
    assert (stale.status, stale.level, stale.delay, stale.delay_warn) == (
        "STALE", "warn", "11s", True)
    assert twap(ages={**ages, rs.CHAINLINK_TWAP60: 10.0}).status == "OK"
    offline = {s: _src(connected=False, last_error="OSError: network is down")
               for s in rs.SOURCES}
    booting = twap(prices=offline, ages={s: None for s in rs.SOURCES}, started_at=HT - 5)
    assert (booting.status, booting.level, booting.delay) == ("CONNECTING", "idle", "—")
    down = twap(prices=offline, ages={**ages, rs.CHAINLINK_TWAP60: 300.0})
    assert (down.status, down.level, down.detail, down.delay_warn) == (
        "DOWN", "down", "OSError: network is down", True)
    waiting = twap(ages={s: None for s in rs.SOURCES}, started_at=HT - 5)
    assert waiting.status == "CONNECTING"
    silent = twap(ages={s: None for s in rs.SOURCES})
    assert (silent.status, silent.level, silent.detail) == (
        "STALE", "warn", "connected, but no prints yet")
    html = feeds.render(_md(prices=offline, ages={s: None for s in rs.SOURCES}))
    assert "3 issues" in html  # the three price rows are DOWN; no market is in use


def test_card_folds_and_keeps_the_issue_count_in_its_header() -> None:
    html = feeds.render(None)
    assert html.startswith("<details class='card feeds-card fold' data-fold='feeds' open>")
    assert "ontoggle" not in html  # dashboard.js listens on the document
    summary = html[html.index("<summary"):html.index("</summary>")]
    assert "FEEDS" in summary and "all OK" in summary
