"""FEEDS card: live rows built from the feed monitor's snapshot, not the bot's journal."""
from __future__ import annotations

import dataclasses

import pytest

import config as _config
from polymarket_exec.marketdata import clob_shard as sh
from polymarket_exec.marketdata import clob_stream as cs
from polymarket_exec.marketdata import hub as md_hub
from polymarket_exec.marketdata import rest_poll as rp
from polymarket_exec.marketdata import rtds_stream as rs
from polymarket_exec.ops import feed_monitor as fm
from polymarket_exec.ops import flow_recorder as fr
from polymarket_exec.ops import macro_recorder as mr
from polymarket_exec.ops.dashboard.panels import feeds, ribbon

NOW = 1_800_000_000.0


def _probe(ok: bool = True, ms: float = 120.0, age: float = 1.0, detail: str | None = None):
    return fm.ProbeResult(ok=ok, latency_ms=ms, checked_at=NOW - age, detail=detail)


def _snap(**kw) -> fm.FeedsSnapshot:
    args = dict(
        taken_at=NOW,
        started_at=NOW - 600,
        interval_s=10.0,
        ws_connected=True,
        ws_fresh=True,
        ws_print_age_s=1.24,
        probes={
            fm.GAMMA: _probe(),
            fm.CLOB_BOOK: _probe(),
            fm.CHAINLINK_REST: _probe(),
            fm.BINANCE: _probe(),
        },
    )
    args.update(kw)
    return fm.FeedsSnapshot(**args)


def _rows(snap) -> dict[tuple[str, str], feeds.FeedRow]:
    return {(r.name, r.role): r for r in feeds.build_rows(snap)}


def test_healthy_feeds_all_ok() -> None:
    rows = _rows(_snap())
    spot = rows[("Chainlink BTC/USD", "spot · vol")]
    assert (spot.connection, spot.delay, spot.status) == ("WebSocket · RTDS", "1.2s", "OK")
    book = rows[("Polymarket book", "UP/DOWN quotes")]
    assert (book.delay, book.status) == ("120ms", "OK")
    assert all(r.status == "OK" for r in rows.values())
    html = feeds.render(_snap())
    assert "FEEDS" in html and "all OK" in html


def test_card_lists_only_live_feeds() -> None:
    names = {r.name for r in feeds.build_rows(_snap())}
    assert names == {
        "Chainlink BTC/USD", "Polymarket Gamma", "Polymarket book", "Binance BTCUSDT 1s"
    }
    assert "Bot loop" not in feeds.render(_snap())


def test_failed_check_is_down_with_error_on_hover() -> None:
    snap = _snap(probes={**_snap().probes, fm.GAMMA: _probe(ok=False, detail="HTTPStatusError: 503")})
    row = _rows(snap)[("Polymarket Gamma", "market lookup")]
    assert (row.status, row.level, row.detail) == ("DOWN", "down", "HTTPStatusError: 503")
    html = feeds.render(snap)
    assert "1 issue" in html and "title='HTTPStatusError: 503'" in html


def test_empty_book_is_a_warning_not_down() -> None:
    snap = _snap(probes={**_snap().probes, fm.CLOB_BOOK: _probe(ok=False, detail="empty book")})
    row = _rows(snap)[("Polymarket book", "UP/DOWN quotes")]
    assert (row.status, row.level) == ("EMPTY", "warn")


def test_old_check_goes_stale() -> None:
    snap = _snap(probes={**_snap().probes, fm.BINANCE: _probe(age=45)})
    row = _rows(snap)[("Binance BTCUSDT 1s", "vol backup")]
    assert (row.status, row.level) == ("STALE", "warn")


def test_slow_round_trip_flags_delay_only() -> None:
    snap = _snap(probes={**_snap().probes, fm.CHAINLINK_REST: _probe(ms=2600)})
    row = _rows(snap)[("Chainlink BTC/USD", "window open")]
    assert (row.status, row.delay, row.delay_warn) == ("OK", "2.6s", True)


def test_not_checked_yet_shows_checking() -> None:
    row = _rows(_snap(probes={}))[("Polymarket Gamma", "market lookup")]
    assert (row.status, row.level, row.delay) == ("CHECKING", "idle", "—")


def test_ws_states() -> None:
    ws = ("Chainlink BTC/USD", "spot · vol")
    stale_age = _config.CHAINLINK_STALE_SECONDS + 5
    row = _rows(_snap(ws_fresh=False, ws_print_age_s=stale_age))[ws]
    assert (row.status, row.level, row.delay_warn) == ("STALE", "warn", True)

    row = _rows(_snap(ws_fresh=False, ws_connected=False))[ws]
    assert (row.status, row.level) == ("DOWN", "down")

    booting = _snap(ws_fresh=False, ws_connected=False, ws_print_age_s=None, started_at=NOW - 2)
    row = _rows(booting)[ws]
    assert (row.status, row.level, row.delay) == ("CONNECTING", "idle", "—")


def test_no_monitor_shows_off_rows() -> None:
    rows = feeds.build_rows(None)
    assert rows and all((r.status, r.level) == ("OFF", "idle") for r in rows)


def test_ribbon_no_longer_carries_feed_chips() -> None:
    html = ribbon.render(
        mode="paper", state="running", session_start=None,
        live_pnl=0.0, paper_pnl=0.0, day_pnl=0.0,
        open_pos=[], closed_session=[], tick=None,
    )
    assert "TICK" not in html and "class='feed " not in html



def _flow(feeds: dict, taken_at: float = 10_000.0, started_at: float = 9_000.0) -> fr.FlowSnapshot:
    base = {k: fr.FeedStatus("rest", None, False, None, None, None, None) for k in fr.REST_KEYS}
    base.update({k: fr.FeedStatus("ws", None, False, None, None, None, None) for k in fr.WS_KEYS})
    base.update(feeds)
    return fr.FlowSnapshot(taken_at, started_at, 60.0, base)


def _flow_rows(flow: fr.FlowSnapshot | None) -> dict:
    return {(r.name, r.role): r for r in feeds.build_rows(None, flow)}


def test_no_flow_rows_without_a_recorder() -> None:
    # Keeps the monitor-only card (and its existing tests) unchanged outside the app.
    names = {r.name for r in feeds.build_rows(None)}
    assert "Kraken BTC/USD" not in names and "Binance liquidations" not in names


def test_flow_rest_rows_ok_down_stale_and_checking() -> None:
    flow = _flow({
        fr.BINANCE_SPOT_BTC: fr.FeedStatus("rest", True, False, None, 9_990.0, 1, None),
        fr.BINANCE_PERP_BTC: fr.FeedStatus("rest", False, False, None, None, None,
                                           "HTTPStatusError: 503"),
        fr.BINANCE_SPOT_ETH: fr.FeedStatus("rest", True, False, None, 9_000.0, 1, None),
    })
    rows = _flow_rows(flow)
    assert rows[("Binance BTCUSDT 1h", "hourly flow")].status == "OK"
    perp = rows[("Binance perp BTCUSDT", "hourly flow")]
    assert (perp.status, perp.level, perp.detail) == ("DOWN", "down", "HTTPStatusError: 503")
    assert rows[("Binance ETHUSDT", "hourly flow")].status == "STALE"  # 1000 s > 3 × 60 s
    assert rows[("Kraken PF_XBTUSD", "funding · OI")].status == "CHECKING"


def test_flow_ws_rows_connecting_ok_stale_quiet_down() -> None:
    flow = _flow(
        {
            fr.KRAKEN_SPOT: fr.FeedStatus("ws", None, True, 9_500.0, 9_995.0, None, None),
            fr.KRAKEN_FUTURES: fr.FeedStatus("ws", None, True, 9_500.0, 9_600.0, None, None),
            fr.BINANCE_LIQ: fr.FeedStatus("ws", None, False, None, None, None,
                                          "ConnectionError: closed"),
        },
    )
    rows = _flow_rows(flow)
    assert rows[("Kraken BTC/USD", "hourly flow")].status == "OK"
    assert rows[("Kraken PF_XBTUSD", "hourly flow")].status == "QUIET"  # sparse feed, 400 s
    liq = rows[("Binance liquidations", "liquidation flow")]
    assert (liq.status, liq.detail) == ("DOWN", "ConnectionError: closed")
    stale = _flow_rows(_flow({
        fr.KRAKEN_SPOT: fr.FeedStatus("ws", None, True, 9_000.0, 9_500.0, None, None)}))
    assert stale[("Kraken BTC/USD", "hourly flow")].status == "STALE"
    early = _flow_rows(_flow({}, taken_at=9_010.0, started_at=9_000.0))
    assert early[("Kraken BTC/USD", "hourly flow")].status == "CONNECTING"


def test_render_includes_flow_rows() -> None:
    html = feeds.render(None, _flow({}))
    assert "Kraken BTC/USD" in html and "Binance liquidations" in html


# --- Macro calendar rows -------------------------------------------------------

MT = 1_000_000.0
H6 = 6 * 3600.0


def _macro(statuses: dict, tick_s: float = 60.0,
           started_at: float = MT - 50_000.0) -> mr.MacroSnapshot:
    base = {s.key: mr.MacroFeedStatus(s.cadence_s, None, None, None, None, None, None)
            for s in mr.default_sources()}
    base.update(statuses)
    return mr.MacroSnapshot(MT, started_at, tick_s, base)


def _ok(age: float, cadence: float = H6) -> mr.MacroFeedStatus:
    return mr.MacroFeedStatus(cadence, True, MT - age, MT - age, MT - age + cadence, 10, None)


def test_no_macro_rows_without_a_macro_recorder() -> None:
    assert feeds.build_rows(_snap()) == feeds.build_rows(_snap(), None, None)
    assert feeds.render(_snap(), _flow({})) == feeds.render(_snap(), _flow({}), None)
    names = {r.name for r in feeds.build_rows(None, _flow({}))}
    assert "BLS schedule" not in names and "ForexFactory week" not in names


def test_macro_rows_follow_flow_rows_in_card_order() -> None:
    rows = feeds.build_rows(_snap(), _flow({}), _macro({}))
    assert [(r.name, r.role) for r in rows[-5:]] == [
        ("BLS schedule", "CPI · jobs · PPI times"),
        ("BEA schedule", "GDP · PCE times"),
        ("Census schedule", "retail sales times"),
        ("Fed calendar", "FOMC · speeches"),
        ("ForexFactory week", "forecasts · claims"),
    ]
    assert all((r.status, r.level, r.delay) == ("CHECKING", "idle", "—") for r in rows[-5:])


def test_macro_row_states() -> None:
    down = mr.MacroFeedStatus(H6, False, MT - (5 * 3600 + 180), MT - 60, MT + 840, 119,
                              "HTTP 403 from www.bls.gov/schedule/news_release/bls.ics")
    stale = _ok(2 * H6 + 60 + 1)
    # Rate limited again and again for two days: still waiting, but that is an outage.
    limited_for_days = mr.MacroFeedStatus(
        3600.0, False, MT - (2 * 86_400 + 4 * 3600 + 5), MT - 10, MT + 290, 8,
        "rate limited (HTTP 429); next try in 300s", True)
    rows = {r.name: r for r in feeds.build_rows(None, None, _macro({
        mr.BLS_SCHEDULE: down,
        mr.BEA_SCHEDULE: _ok(42),
        mr.CENSUS_SCHEDULE: stale,
        mr.FF_WEEK: limited_for_days,
    }))}
    bls = rows["BLS schedule"]
    assert (bls.status, bls.level, bls.delay, bls.detail) == (
        "DOWN", "down", "5h03m", "HTTP 403 from www.bls.gov/schedule/news_release/bls.ics")
    assert (rows["BEA schedule"].status, rows["BEA schedule"].level,
            rows["BEA schedule"].delay) == ("OK", "on", "42s")
    census = rows["Census schedule"]
    assert (census.status, census.level, census.delay_warn, census.delay) == (
        "STALE", "warn", True, "12h01m")
    assert rows["Fed calendar"].status == "CHECKING"
    ff = rows["ForexFactory week"]
    assert (ff.status, ff.level, ff.delay, ff.delay_warn, ff.detail) == (
        "STALE", "warn", "2d04h", True, "rate limited (HTTP 429); next try in 300s")
    # Exactly 2 × cadence + tick old is still OK; a RetryAfter whose time has passed is DOWN.
    edge = {r.name: r for r in feeds.build_rows(None, None, _macro({
        mr.CENSUS_SCHEDULE: _ok(2 * H6 + 60),
        mr.FF_WEEK: mr.MacroFeedStatus(3600.0, False, None, MT - 400, MT - 100, None,
                                       "rate limited", True),
    }))}
    assert edge["Census schedule"].status == "OK"
    assert (edge["ForexFactory week"].status, edge["ForexFactory week"].delay) == ("DOWN", "—")
    html = feeds.render(None, None, _macro({mr.BLS_SCHEDULE: down, mr.CENSUS_SCHEDULE: stale,
                                            mr.FF_WEEK: limited_for_days}))
    assert "BLS schedule" in html and "ForexFactory week" in html
    assert "HTTP 403 from www.bls.gov" in html
    # DOWN and STALE are issues; CHECKING is not. The monitor-off rows are OFF (idle).
    assert "3 issues" in html


def test_macro_wait_row_lasts_until_the_retry_tick_and_not_past_the_stale_limit() -> None:
    detail = "rate limited (HTTP 429); next try in 300s"
    limit = 2 * 3600 + 60  # 2 x cadence + tick for the hourly ForexFactory feed

    def limited(last_ok_age: float | None, wait_left: float) -> mr.MacroFeedStatus:
        last_ok = None if last_ok_age is None else MT - last_ok_age
        return mr.MacroFeedStatus(3600.0, False, last_ok, MT - 300, MT + wait_left, 8, detail,
                                  True)

    def ff_row(status: mr.MacroFeedStatus, up_for: float = 50_000.0) -> feeds.FeedRow:
        snap = _macro({mr.FF_WEEK: status}, started_at=MT - up_for)
        return {r.name: r for r in feeds.build_rows(None, None, snap)}["ForexFactory week"]

    row = ff_row(limited(1800, 120))
    assert (row.status, row.level, row.delay, row.delay_warn, row.detail) == (
        "WAIT", "idle", "30m", False, detail)
    # The wait just ended; the retry runs on the recorder's next tick (60s) plus a margin.
    assert ff_row(limited(1800, -5)).status == "WAIT"
    assert ff_row(limited(1800, -89)).status == "WAIT"
    assert (ff_row(limited(1800, -91)).status, ff_row(limited(1800, -91)).level) == (
        "DOWN", "down")
    # Still waiting, but no data for longer than 2 x cadence + tick: STALE, an issue.
    assert ff_row(limited(limit, 120)).status == "WAIT"
    old = ff_row(limited(limit + 1, 120))
    assert (old.status, old.level, old.delay_warn, old.detail) == ("STALE", "warn", True, detail)
    # Never succeeded: how long the recorder has been running decides.
    assert ff_row(limited(None, 120), up_for=limit).status == "WAIT"
    never = ff_row(limited(None, 120), up_for=limit + 1)
    assert (never.status, never.level, never.delay, never.delay_warn) == (
        "STALE", "warn", "—", True)
    assert "all OK" in feeds.render(None, None, _macro({mr.FF_WEEK: limited(1800, -5)}))
    assert "1 issue<" in feeds.render(None, None, _macro({mr.FF_WEEK: limited(limit + 1, 120)}))


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(0, "0s"), (42.9, "42s"), (59, "59s"), (60, "1m"), (17 * 60 + 30, "17m"),
     (3600, "1h00m"), (5 * 3600 + 3 * 60 + 59, "5h03m"), (86_400, "1d00h"),
     (2 * 86_400 + 4 * 3600 + 59 * 60, "2d04h"), (-3, "0s")],
)
def test_long_age_formatter(seconds: float, text: str) -> None:
    assert feeds._age(seconds) == text
    assert feeds._secs(90) == "1m30s"  # the short formatter is unchanged


# --- Market-data hub rows (CLOB books on demand + RTDS prices) -----------------------

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
    return {r.name: r for r in feeds.build_rows(None, None, None, md) if r.name in MD_NAMES}


def _books(*entries, **kw) -> feeds.FeedRow:
    return _md_rows(_md(*entries, **kw))["Polymarket books"]


def test_no_marketdata_rows_without_a_hub() -> None:
    assert feeds.build_rows(_snap()) == feeds.build_rows(_snap(), None, None, None)
    assert feeds.render(_snap(), _flow({})) == feeds.render(_snap(), _flow({}), None, None)
    names = {r.name for r in feeds.build_rows(_snap(), _flow({}), _macro({}))}
    assert names.isdisjoint(MD_NAMES)


def test_render_without_a_hub_still_works() -> None:
    html = feeds.render(_snap())
    assert "Polymarket books" not in html and "feeds-grid" not in html
    assert html.count("<tr><td>") == 5 and "<th>Used by</th>" in html
    off = feeds.render(None)
    assert off.count(">OFF</span>") == 5 and "all OK" in off


def test_marketdata_rows_sit_right_under_the_monitor_rows() -> None:
    rows = feeds.build_rows(_snap(), _flow({}), _macro({}), _md(_market("btc-5m")))
    assert [r.name for r in rows[:5]] == [
        "Chainlink BTC/USD", "Chainlink BTC/USD", "Polymarket Gamma", "Polymarket book",
        "Binance BTCUSDT 1s"]
    assert [r.name for r in rows[5:9]] == MD_NAMES
    assert (rows[9].name, rows[9].role) == ("Binance BTCUSDT 1h", "hourly flow")
    assert rows[-1].name == "ForexFactory week"
    assert [r.name for r in rows if r.grid is not None] == ["Polymarket books"]


MONITOR_BY = "FEEDS check"
CARD_COLUMNS = [
    # (feed, connection, used for, used by), in card order
    ("Chainlink BTC/USD", "WebSocket · RTDS", "spot · vol",
     f"bot loop · {MONITOR_BY}"),
    ("Chainlink BTC/USD", "REST · every 10 s", "window open",
     f"bot loop · {MONITOR_BY}"),
    ("Polymarket Gamma", "REST · every 10 s", "market lookup",
     f"bot loop · order ticket · daily scanner · market hub · {MONITOR_BY}"),
    ("Polymarket book", "REST · every 10 s", "UP/DOWN quotes",
     f"bot loop · order ticket · live executor · {MONITOR_BY}"),
    ("Binance BTCUSDT 1s", "REST · every 10 s", "vol backup", f"bot loop · {MONITOR_BY}"),
    ("Polymarket books", "WebSocket · CLOB · on demand",
     "Up/Down books · trades (2 of 24 in use)", "bot loop · order ticket"),
    ("Chainlink prices", "WebSocket · RTDS", "spot · vol", "Fade 1h Momentum on 15m"),
    ("Chainlink 60s TWAP", "WebSocket · RTDS", "5m·15m settle ref", "Fade 1h Momentum on 15m"),
    ("Binance prices", "WebSocket · RTDS", "1h·1d settle ref", "Fade 1h Momentum on 15m"),
    ("Binance BTCUSDT 1h", "REST · every 60 s", "hourly flow", "flow recorder"),
    ("Binance ETHUSDT", "REST · every 60 s", "hourly flow", "flow recorder"),
    ("Binance perp BTCUSDT", "REST · every 60 s", "hourly flow", "flow recorder"),
    ("Binance perp BTCUSDT", "REST · hourly", "funding · OI", "flow recorder"),
    ("Binance liquidations", "WebSocket · Binance futures", "liquidation flow",
     "flow recorder"),
    ("Kraken BTC/USD", "WebSocket · Kraken", "hourly flow", "flow recorder"),
    ("Kraken PF_XBTUSD", "WebSocket · Kraken Futures", "hourly flow", "flow recorder"),
    ("Kraken PF_XBTUSD", "REST · hourly", "funding · OI", "flow recorder"),
    ("BLS schedule", "REST · every 6 h", "CPI · jobs · PPI times", "macro recorder"),
    ("BEA schedule", "REST · every 6 h", "GDP · PCE times", "macro recorder"),
    ("Census schedule", "REST · every 6 h", "retail sales times", "macro recorder"),
    ("Fed calendar", "REST · hourly", "FOMC · speeches", "macro recorder"),
    ("ForexFactory week", "REST · hourly", "forecasts · claims", "macro recorder"),
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
    rows = feeds.build_rows(_snap(), _flow({}), _macro({}), md)
    assert [(r.name, r.connection, r.role, r.used_by) for r in rows] == CARD_COLUMNS
    assert all(r.source for r in rows)  # the endpoint behind each connection, on hover
    # The monitor's rows keep their facts while it is off.
    off = feeds.build_rows(None)
    assert [(r.name, r.connection, r.role, r.used_by, r.status) for r in off] == [
        (*facts, "OFF") for facts in CARD_COLUMNS[:5]]
    # Cadences come from the recorders themselves.
    slow_flow = fr.FlowSnapshot(10_000.0, 9_000.0, 120.0, _flow({}).feeds)
    flow_rows = feeds.build_rows(None, slow_flow)
    assert flow_rows[5].connection == "REST · every 2 min"
    assert flow_rows[8].connection == "REST · hourly"  # perp state: once an hour


@pytest.mark.parametrize(("seconds", "text"), [
    (10, "every 10 s"), (60, "every 60 s"), (90, "every 90 s"), (120, "every 2 min"),
    (900, "every 15 min"), (3600, "hourly"), (6 * 3600, "every 6 h"), (86_400, "every 24 h"),
    (45.5, "every 46 s")])
def test_cadence_wording(seconds: float, text: str) -> None:
    assert feeds._every(seconds) == text


def test_render_shows_the_six_columns_and_the_endpoint_on_hover() -> None:
    html = feeds.render(_snap(), None, None, _md(_market("btc-5m")))
    assert ("<thead><tr><th>Feed</th><th>Connection</th><th>Used for</th><th>Used by</th>"
            "<th class='feeds-delay'>Delay</th><th>Status</th></tr></thead>") in html
    assert ("<tr><td>Chainlink BTC/USD</td>"
            "<td class='feeds-conn' title='RTDS · crypto_prices_chainlink'>WebSocket · RTDS</td>"
            "<td class='feeds-role'>spot · vol</td>"
            f"<td class='feeds-role'>bot loop · {MONITOR_BY}</td>"
            "<td class='feeds-delay'>1.2s</td><td><span class='feed on'>OK</span></td>"
            "</tr>") in html
    assert "<td class='feeds-conn' title='CLOB /book'>REST · every 10 s</td>" in html
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
    html = feeds.render(None, None, None, _md(_market("btc-5m")))
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
    html = feeds.render(None, None, None, md)
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
    html = feeds.render(None, None, None, dataclasses.replace(md, grid=grid))
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
    html = feeds.render(_snap(), None, None, md)
    assert "all OK" in html and "class='feed linger'" in html
    down = _md(_market("btc-5m", conns=(dead,)), _market("eth-5m", conns=(dead,)))
    assert ">1 issue<" in feeds.render(_snap(), None, None, down)  # one row, two markets


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
    html = feeds.render(None, None, None, _md(prices=offline, ages={s: None for s in rs.SOURCES}))
    assert "3 issues" in html  # the three price rows are DOWN; no market is in use


def test_card_folds_and_keeps_the_issue_count_in_its_header() -> None:
    html = feeds.render(_snap())
    assert html.startswith("<details class='card feeds-card fold' data-fold='feeds' open>")
    assert "ontoggle" not in html  # dashboard.js listens on the document
    summary = html[html.index("<summary"):html.index("</summary>")]
    assert "FEEDS" in summary and "all OK" in summary
