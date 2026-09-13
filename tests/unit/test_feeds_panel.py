"""FEEDS card: per-feed source, delay, and status rows (moved out of the ribbon)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

import config as _config
from polymarket_bot import paper
from polymarket_exec.ops.dashboard.panels import feeds, ribbon

_HEALTHY = "spot=chainlink_ws;ref=chainlink_rest;vol=chainlink_ws;quotes=clob"


def _ts(age_s: float) -> str:
    return (datetime.now(UTC) - timedelta(seconds=age_s)).isoformat()


def _tick(age_s: float = 2, feed_source: str = _HEALTHY, book: bool = True) -> dict:
    return {
        "created_at": _ts(age_s),
        "feed_source": feed_source,
        "up_best_bid": 0.48 if book else None,
        "up_best_ask": 0.52 if book else None,
        "down_best_bid": None,
        "down_best_ask": None,
    }


def _rows(**kw) -> dict[tuple[str, str], feeds.FeedRow]:
    args = dict(
        tick=_tick(), is_live=False, last_live_at=None, chainlink_age_s=None, tick_seconds=5.0
    )
    args.update(kw)
    return {(r.name, r.role): r for r in feeds.build_rows(**args)}


def test_healthy_tick_all_ok_and_binance_on_standby() -> None:
    rows = _rows(chainlink_age_s=1.24)
    assert rows[("Bot loop", "decision tick")].status == "OK"
    spot = rows[("Chainlink BTC/USD", "spot · vol")]
    assert (spot.source, spot.delay, spot.status) == ("Polymarket WS", "1.2s", "OK")
    assert rows[("Chainlink BTC/USD", "window open")].status == "OK"
    assert rows[("Polymarket book", "UP/DOWN quotes")].status == "OK"
    assert rows[("Polymarket Gamma", "market lookup")].status == "OK"
    assert rows[("Binance BTCUSDT", "vol backup")].level == "idle"
    html = feeds.render(
        tick=_tick(), is_live=False, last_live_at=None, chainlink_age_s=1.24, tick_seconds=5.0
    )
    assert "FEEDS" in html and "all OK" in html
    assert "Polymarket orders" not in html  # live-only row


def test_fallbacks_and_failures_are_flagged() -> None:
    src = "spot=chainlink_rest_poll;ref=unavailable;vol=binance_shape_fallback;quotes=clob"
    rows = _rows(tick=_tick(feed_source=src, book=False))
    assert rows[("Chainlink BTC/USD", "spot · vol")].status == "FALLBACK"
    assert rows[("Chainlink BTC/USD", "window open")].level == "down"
    assert rows[("Polymarket book", "UP/DOWN quotes")].status == "EMPTY"
    assert rows[("Binance BTCUSDT", "vol backup")].status == "IN USE"

    rows = _rows(tick=_tick(feed_source="spot=unavailable;ref=chainlink_rest;vol=floor"))
    assert rows[("Chainlink BTC/USD", "spot · vol")].level == "down"
    assert rows[("Binance BTCUSDT", "vol backup")].status == "DOWN"


def test_stale_tick_greys_out_feed_rows_instead_of_showing_old_ok() -> None:
    rows = _rows(tick=_tick(age_s=600))
    loop = rows[("Bot loop", "decision tick")]
    assert (loop.status, loop.delay) == ("STALE", "10m00s")
    others = [r for k, r in rows.items() if k[0] != "Bot loop"]
    assert others and all(r.level == "idle" for r in others)


def test_stale_cutoff_follows_runtime_tick_interval() -> None:
    # 25s-old tick: stale at a 5s interval (cutoff 20s), fresh at 30s (cutoff 90s).
    assert _rows(tick=_tick(age_s=25))[("Bot loop", "decision tick")].status == "STALE"
    rows = _rows(tick=_tick(age_s=25), tick_seconds=30.0)
    assert rows[("Bot loop", "decision tick")].status == "OK"
    assert rows[("Chainlink BTC/USD", "spot · vol")].level == "on"


def test_no_tick_reports_no_data() -> None:
    rows = _rows(tick=None)
    assert rows[("Bot loop", "decision tick")].status == "NO DATA"
    html = feeds.render(
        tick=None, is_live=False, last_live_at=None, chainlink_age_s=None, tick_seconds=5.0
    )
    assert "1 issue" in html


def test_chainlink_delay_warns_past_stale_threshold() -> None:
    age = _config.CHAINLINK_STALE_SECONDS + 5
    assert _rows(chainlink_age_s=age)[("Chainlink BTC/USD", "spot · vol")].delay_warn
    assert not _rows(chainlink_age_s=0.5)[("Chainlink BTC/USD", "spot · vol")].delay_warn


@pytest.mark.parametrize(
    ("last_age", "status", "delay"),
    [(None, "NONE", "—"), (42, "OK", "42s ago"), (feeds.ORDER_QUIET_AFTER_S + 60, "QUIET", "6m00s ago")],
)
def test_live_mode_adds_order_row(last_age, status, delay) -> None:
    last = None if last_age is None else _ts(last_age)
    row = _rows(is_live=True, last_live_at=last)[("Polymarket orders", "order entry")]
    assert (row.status, row.delay) == (status, delay)


def test_ribbon_no_longer_carries_feed_chips() -> None:
    html = ribbon.render(
        mode="paper", state="running", session_start=None,
        live_pnl=0.0, paper_pnl=0.0, day_pnl=0.0,
        open_pos=[], closed_session=[], tick=_tick(),
    )
    assert "TICK" not in html and "class='feed " not in html


def test_chainlink_print_age_from_live_feed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(paper, "_chainlink_feed", None)
    assert paper.chainlink_print_age_seconds() is None

    class _Feed:
        def latest(self):
            return (datetime.now(UTC).timestamp() - 3.0, 100_000.0)

    monkeypatch.setattr(paper, "_chainlink_feed", _Feed())
    age = paper.chainlink_print_age_seconds()
    assert age is not None and 2.5 < age < 4.0
