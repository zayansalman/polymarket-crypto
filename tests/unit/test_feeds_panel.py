"""FEEDS card: live rows built from the feed monitor's snapshot, not the bot's journal."""
from __future__ import annotations

import pytest

import config as _config
from polymarket_exec.marketdata import clob_stream as cs
from polymarket_exec.marketdata import hub as md_hub
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
    assert (spot.source, spot.delay, spot.status) == ("Polymarket WS", "1.2s", "OK")
    book = rows[("Polymarket book", "UP/DOWN quotes")]
    assert (book.delay, book.status) == ("120ms", "OK")
    assert all(r.status == "OK" for r in rows.values())
    html = feeds.render(_snap())
    assert "FEEDS" in html and "all OK" in html


def test_card_lists_only_live_feeds() -> None:
    names = {r.name for r in feeds.build_rows(_snap())}
    assert names == {
        "Chainlink BTC/USD", "Polymarket Gamma", "Polymarket book", "Binance BTCUSDT"
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
    row = _rows(snap)[("Binance BTCUSDT", "vol backup")]
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
    assert rows[("Binance BTCUSDT", "hourly flow")].status == "OK"
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


# --- Market-data hub rows (CLOB books + RTDS prices) ------------------------------

HT = 5_000_000.0
MD_NAMES = ["Polymarket books", "Chainlink prices", "Chainlink 60s TWAP", "Binance prices"]


def _clob(**kw) -> cs.StreamStatus:
    base = dict(connected=True, connected_since=HT - 300, last_frame_at=HT - 1,
                last_pong_at=HT - 3, frames_total=1000, frames_per_s=400.0,
                latency_ms_p50=48.0, latency_ms_p90=120.0, reconnects=0, resyncs=0,
                subscribed=96, desired=96, unknown=0, handler_errors=0, last_error=None,
                last_notice=None)
    base.update(kw)
    return cs.StreamStatus(**base)


def _src(**kw) -> rs.SourceStatus:
    base = dict(connected=True, last_update_age_s=0.4, newest_obs_ms=int((HT - 1.5) * 1000),
                points=900, updates=5000, snapshots=6, latency_ms_p50=1400.0, gaps=0,
                assets=6, reconnects=0, handler_errors=0, last_error=None)
    base.update(kw)
    return rs.SourceStatus(**base)


def _md(clob: cs.StreamStatus | None = None, prices: dict | None = None,
        ages: dict | None = None, started_at: float = HT - 600, markets: int = 48,
        tokens: int = 96, gamma_errors: int = 0,
        gamma_last_error: str | None = None) -> md_hub.MarketDataSnapshot:
    clob = clob or _clob()
    return md_hub.MarketDataSnapshot(
        taken_at=HT, started_at=started_at, clob=clob,
        prices=prices or {s: _src() for s in rs.SOURCES},
        price_ages=ages if ages is not None else {s: 1.5 for s in rs.SOURCES},
        markets=markets, tokens=tokens, subscribed=clob.subscribed, gamma_lookups=60,
        gamma_errors=gamma_errors, gamma_last_error=gamma_last_error, listeners=0,
        listener_drops=0,
    )


def _md_rows(md: md_hub.MarketDataSnapshot) -> dict[str, feeds.FeedRow]:
    return {r.name: r for r in feeds.build_rows(None, None, None, md) if r.name in MD_NAMES}


def test_no_marketdata_rows_without_a_hub() -> None:
    assert feeds.build_rows(_snap()) == feeds.build_rows(_snap(), None, None, None)
    assert feeds.render(_snap(), _flow({})) == feeds.render(_snap(), _flow({}), None, None)
    names = {r.name for r in feeds.build_rows(_snap(), _flow({}), _macro({}))}
    assert names.isdisjoint(MD_NAMES)


def test_marketdata_rows_sit_right_under_the_monitor_rows() -> None:
    rows = feeds.build_rows(_snap(), _flow({}), _macro({}), _md())
    assert [r.name for r in rows[:5]] == [
        "Chainlink BTC/USD", "Chainlink BTC/USD", "Polymarket Gamma", "Polymarket book",
        "Binance BTCUSDT"]
    assert [r.name for r in rows[5:9]] == MD_NAMES
    assert (rows[9].name, rows[9].role) == ("Binance BTCUSDT", "hourly flow")
    assert rows[-1].name == "ForexFactory week"


def test_healthy_marketdata_rows() -> None:
    rows = _md_rows(_md())
    books = rows["Polymarket books"]
    assert (books.role, books.source, books.delay, books.status, books.level) == (
        "Up/Down books · trades (48 markets)", "CLOB market WS", "48ms", "OK", "on")
    assert [(rows[n].role, rows[n].source, rows[n].delay, rows[n].status)
            for n in MD_NAMES[1:]] == [
        ("spot · vol", "RTDS WS", "1.5s", "OK"),
        ("5m·15m settle ref", "RTDS WS", "1.5s", "OK"),
        ("1h·1d settle ref", "RTDS WS", "1.5s", "OK"),
    ]
    html = feeds.render(None, None, None, _md())
    assert "Polymarket books" in html and "Chainlink 60s TWAP" in html


def test_books_row_states() -> None:
    def books(**kw) -> feeds.FeedRow:
        return _md_rows(_md(**kw))["Polymarket books"]

    booting = books(clob=_clob(connected=False, connected_since=None, last_frame_at=None,
                               latency_ms_p50=None), started_at=HT - 10)
    assert (booting.status, booting.level, booting.delay) == ("CONNECTING", "idle", "—")
    down = books(clob=_clob(connected=False, connected_since=None,
                            last_error="ConnectionClosedError: no close frame received"))
    assert (down.status, down.level, down.detail) == (
        "DOWN", "down", "ConnectionClosedError: no close frame received")
    stale = books(clob=_clob(last_frame_at=HT - 46))
    assert (stale.status, stale.level, stale.delay_warn) == ("STALE", "warn", True)
    assert "46s" in (stale.detail or "")
    assert books(clob=_clob(last_frame_at=HT - 45)).status == "OK"
    # Connected a moment ago with no data yet: timed from the connect, not an old frame.
    fresh = books(clob=_clob(connected_since=HT - 5, last_frame_at=HT - 400))
    assert fresh.status == "OK"
    slow = books(clob=_clob(latency_ms_p50=2500.0))
    assert (slow.status, slow.delay, slow.delay_warn) == ("OK", "2.5s", True)
    idle = books(clob=_clob(subscribed=0, last_frame_at=HT - 400))
    assert (idle.status, idle.level) == ("IDLE", "idle")
    no_markets = books(clob=_clob(connected=False, connected_since=None, subscribed=0),
                       markets=0, tokens=0, gamma_errors=12,
                       gamma_last_error="ConnectError: [Errno 8] nodename nor servname")
    assert (no_markets.status, no_markets.role) == (
        "DOWN", "Up/Down books · trades (0 markets)")
    assert "Gamma" in (no_markets.detail or "") and "ConnectError" in (no_markets.detail or "")


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
    assert "3 issues" in html  # the three price rows are DOWN; books are OK
