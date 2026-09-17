"""FEEDS card: live rows built from the feed monitor's snapshot, not the bot's journal."""
from __future__ import annotations

import config as _config
from polymarket_exec.ops import feed_monitor as fm
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
