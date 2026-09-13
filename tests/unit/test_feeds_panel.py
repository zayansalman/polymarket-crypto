"""FEEDS card: live rows built from the feed monitor's snapshot, not the bot's journal."""
from __future__ import annotations

import config as _config
from polymarket_exec.ops import feed_monitor as fm
from polymarket_exec.ops import flow_recorder as fr
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
