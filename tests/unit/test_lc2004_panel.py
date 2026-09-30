"""The lc2004-Kronos BTC 24h forecast card (``ems/dashboard/panels/lc2004_kronos_btc_24h.py``):
pure render, the page wiring, and the data it reads.

Pins: an empty card; a full forecast with the market, the window, the spread of the paths and
the hour-by-hour history; missing weights with the one-line fix; a failed run (the missing
einops fix); switched off; every value escaped. The card is display-only (Zayan (operator),
2026-09-29). Claude, 2026-09-29.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ems import db as _db
from ems.dashboard import execution_view
from ems.dashboard.panels import lc2004_kronos_btc_24h as panel
from ems.kronos_forecast import client as kronos_client
from ems.lc2004_kronos_btc_24h import ledger
from ems.lc2004_kronos_btc_24h.market import window_at
from ems.lc2004_kronos_btc_24h.maths import path_spread
from tests.unit.test_standard_terms import COINED


def _ts(iso: str) -> int:
    return int(datetime.fromisoformat(iso).replace(tzinfo=UTC).timestamp())


NOW = _ts("2026-09-22T17:05:00")  # 13:05 EDT
W = window_at(NOW)  # 12:00 EDT Sep 22 -> 12:00 EDT Sep 23
WINDOW = {"slug": W.slug, "start_ts": W.start_ts, "end_ts": W.end_ts}
STRIKE = 100_050.0
CLOSES = ([STRIKE + 100 + 10 * i for i in range(24)]
          + [STRIKE - 200 - 10 * i for i in range(6)])


def _row(candle_iso: str, *, k: int, up_ask: float, down_ask: float, created_iso: str,
         horizon: int, error: str | None = None) -> dict:
    row = {
        "id": 1, "window_slug": W.slug, "window_start_ts": W.start_ts,
        "window_end_ts": W.end_ts, "last_candle_open_ms": _ts(candle_iso) * 1000,
        "horizon_hours": horizon, "strike": STRIKE, "last_close": 100_200.0, "paths": 30,
        "paths_above": k, "p_raw": k / 30, "q_up": (k + 1) / 32,
        "sampling_se": ((k / 30) * (1 - k / 30) / 30) ** 0.5, "final_closes": list(CLOSES),
        "seconds": 312.4, "error": None, "torch_version": "2.4.1", "up_bid": up_ask - 0.02,
        "up_ask": up_ask, "down_bid": down_ask - 0.02, "down_ask": down_ask,
        "created_ts": _ts(created_iso),
    }
    if error is not None:
        row.update(paths_above=None, p_raw=None, q_up=None, sampling_se=None,
                   final_closes=None, seconds=None, error=error)
    return row


NOON = _row("2026-09-22T15:00:00", k=20, up_ask=0.60, down_ask=0.42,
            created_iso="2026-09-22T16:06:10", horizon=24)
ONE_PM = _row("2026-09-22T16:00:00", k=24, up_ask=0.55, down_ask=0.47,
              created_iso="2026-09-22T17:04:10", horizon=23)
STATUS = {
    "state": "running", "last_pass_ts": NOW - 4, "errors": [], "last_error": None,
    "window": dict(WINDOW),
    "market": {"slug": W.slug, "question": "Bitcoin Up or Down on September 23?"},
    "strike": STRIKE,
    "books": {"ts": NOW - 20, "up": {"bid": 0.53, "ask": 0.55},
              "down": {"bid": 0.45, "ask": 0.47}},
    "forecast_running": None, "waiting": [], "forecast_note": None,
    "next_forecast_after": _ts("2026-09-22T18:00:05"), "missing_weights": None,
}
EINOPS = ("Kronos worker is missing einops in /usr/bin/python3; install the optional "
          "'kronos' extra into that interpreter")


def test_an_empty_card_says_there_is_no_forecast_yet() -> None:
    html = panel.render(now=NOW)
    assert f"data-fold='{panel.FOLD}'" in html and panel.TITLE in html
    assert "Display only — places no orders." in html and "DISPLAY ONLY" in html
    assert "no forecast for this window yet" in html and "last pass never" in html
    assert "The forecast loop has not run yet" in html
    assert not COINED.search(html)


def test_a_full_forecast_with_the_market_the_window_the_spread_and_the_history() -> None:
    html = panel.render(window=WINDOW, status=STATUS, forecast=ONE_PM, history=[NOON, ONE_PM],
                        enabled=True, missing_weights=None, now=NOW)
    # The headline: q_up = 25/32, with the raw share and its sampling error.
    for text in ("Model: Up 78% <span>(24 of 30 paths end above the strike)</span>",
                 "Down 22%", "raw share 24/30 = 80.0%, sampling error ±7.3 pts",
                 "P(Up) = (paths above + 1) / 32"):
        assert text in html, text
    # The market now: each side's fair value minus its ask, in cents.
    assert "bid 0.53 / ask 0.55" in html and "bid 0.45 / ask 0.47" in html
    assert "model 0.78 vs ask 0.55, <span class='pos'>+23c</span>" in html
    assert "model 0.22 vs ask 0.47, <span class='neg'>-25c</span>" in html
    assert "20s ago" in html
    # The window.
    for text in (f"href='https://polymarket.com/event/{W.slug}'",
                 "Bitcoin Up or Down on September 23?", "$100,050.00",
                 "Binance BTCUSDT 1m close at 12:00 ET, Tue Sep 22", "$100,200.00",
                 "the 1h candle closing 13:00 ET, +$150.00 vs the strike",
                 "Wed Sep 23 12:00 ET", "22h 55m left", "<b>23<span"):
        assert text in html, text
    # The spread of the 30 paths against the strike.
    spread = path_spread(CLOSES, STRIKE)
    for key in ("p10", "p50", "p90"):
        assert f"${spread[key]:,.2f}" in html, key
    assert f"-${abs(spread['p10_minus_strike']):,.2f}" in html
    assert f"+${spread['p90_minus_strike']:,.2f}" in html
    # When it was made and when the next one runs.
    assert "13:04 ET" in html and "worker took 312 s" in html
    assert "after 14:00 ET" in html
    # This window's history, newest first.
    table = html[html.index("lk-table"):]
    assert table.index("13:00 ET") < table.index("12:00 ET")
    for text in ("24 of 30", "20 of 30", "<td>78%</td>", "<td>66%</td>", "<td>0.60</td>",
                 "<td>0.42</td>"):
        assert text in table, text
    assert "failed" not in html and "SWITCHED OFF" not in html
    assert not COINED.search(html)


def test_missing_weights_are_shown_with_the_fix() -> None:
    missing = kronos_client.missing_weights(kronos_client.ModelSpec("a/b", "0" * 40, "c/d",
                                                                    "1" * 40))
    assert missing is not None  # nothing is cached under those names
    html = panel.render(window=WINDOW, status=STATUS, enabled=True, missing_weights=missing,
                        now=NOW)
    assert "No model weights, so no forecast runs:" in html
    assert "run python3 tools/fetch_lc2004_kronos_weights.py" in html
    assert "after 14:00 ET" not in html  # no next forecast without weights
    # The headline gives the real reason, not "waiting for the noon candle" (the strike is known).
    assert "within a minute of the model weights being installed" in html
    assert "noon candle" not in html


def test_a_failed_run_is_shown_with_the_kronos_python_fix() -> None:
    failed = _row("2026-09-22T17:00:00", k=0, up_ask=0.56, down_ask=0.46,
                  created_iso="2026-09-22T18:01:00", horizon=22, error=EINOPS)
    html = panel.render(window=WINDOW, status=STATUS, forecast=failed,
                        history=[NOON, ONE_PM, failed], enabled=True,
                        now=_ts("2026-09-22T18:02:00"))
    assert "The 14:00 ET forecast failed: Kronos worker is missing einops" in html
    assert "KRONOS_PYTHON in .env to the absolute path of a Python" in html
    # The headline stays on the newest forecast that worked, and says so.
    assert "Model: Up 78%" in html and "from the 13:00 ET run; the newest run failed" in html
    assert "<td>failed</td>" in html


def test_errors_and_a_failed_read_are_shown() -> None:
    status = {**STATUS, "state": "pass_failed",
              "errors": ["Looking up the market failed: RuntimeError: gamma down"],
              "last_error": "Looking up the market failed: RuntimeError: gamma down"}
    html = panel.render(window=WINDOW, status=status, enabled=True,
                        load_error="the forecast table (OperationalError: no such table)",
                        now=NOW)
    assert "PASS FAILED" in html and "gamma down" in html
    assert "Could not read the forecasts: the forecast table" in html


def test_waiting_for_the_noon_candle_is_shown() -> None:
    status = {**STATUS, "strike": None,
              "waiting": ["Waiting for the noon candle: the strike is the Binance BTCUSDT 1m "
                          "close at 12:00 ET, known once that minute has closed."]}
    html = panel.render(window=WINDOW, status=status, enabled=True, now=W.start_ts + 30)
    assert "Waiting for the noon candle" in html and "for the 12:00 ET 1m candle to close" in html


def test_a_failed_strike_read_is_shown_as_a_failure_not_a_wait() -> None:
    """A failed read is not "waiting for the noon candle" (review finding, Claude, 2026-09-29)."""
    error = ("Reading the strike failed: MarketDataError: binance returned the 1m candle "
             "opening at 1790006460000, not 1790006400000")
    status = {**STATUS, "state": "pass_failed", "strike": None, "strike_failed": True,
              "errors": [error], "last_error": error, "waiting": [],
              "next_forecast_after": None}
    html = panel.render(window=WINDOW, status=status, enabled=True, now=NOW)
    assert error in html and "could not be read" in html
    assert "Waiting for the noon candle" not in html and "for the 12:00 ET 1m candle" not in html
    assert "<span>Next</span>" not in html


def test_sub_cent_book_prices_are_shown_to_the_tick() -> None:
    """Near the extremes the tick is 0.001: 0.004 is never shown as 0.00 (review finding)."""
    decided = _row("2026-09-23T14:00:00", k=30, up_ask=0.999, down_ask=0.004,
                   created_iso="2026-09-23T15:04:10", horizon=1)
    status = {**STATUS, "books": {"slug": W.slug, "ts": NOW - 20,
                                  "up": {"bid": 0.998, "ask": 0.999},
                                  "down": {"bid": None, "ask": 0.004}}}
    html = panel.render(window=WINDOW, status=status, forecast=decided, history=[decided],
                        enabled=True, now=NOW)
    assert "bid 0.998 / ask 0.999" in html and "bid — / ask 0.004" in html
    assert "model 0.03 vs ask 0.004, <span class='pos'>+3c</span>" in html
    assert "model 0.97 vs ask 0.999, <span class='neg'>-3c</span>" in html
    assert "<td>0.999</td><td>0.004</td>" in html  # the history's asks
    assert "ask 0.00," not in html and "ask 1.00," not in html
    # Whole cents keep two places.
    assert [panel._price(v) for v in (0.53, 0.5, 1.0, 0.001, None)] == [
        "0.53", "0.50", "1.00", "0.001", "—"]


def test_switched_off_is_shown() -> None:
    status = {**STATUS, "state": "switched_off"}
    html = panel.render(window=WINDOW, status=status, forecast=ONE_PM, history=[ONE_PM],
                        enabled=False, now=NOW)
    assert "SWITCHED OFF" in html
    assert "Switched off in Settings: no forecast runs until it is switched back on." in html
    assert "after 14:00 ET" not in html  # no next forecast while off
    assert "Model: Up 78%" in html  # the last forecast stays readable


def test_a_running_forecast_is_shown() -> None:
    status = {**STATUS, "forecast_running": {"window": W.slug,
                                             "candle_open_ms": _ts("2026-09-22T16:00:00") * 1000,
                                             "started_ts": NOW - 30}}
    html = panel.render(window=WINDOW, status=status, enabled=True, now=NOW)
    assert "MODEL RUNNING" in html and "since 13:04 ET" in html
    assert "for the 1h candle closing 13:00 ET" in html
    assert "The first one is running now" in html


def test_every_value_is_escaped() -> None:
    evil = "<script>alert(1)</script>"
    status = {**STATUS, "market": {**STATUS["market"], "question": evil},
              "errors": [evil], "waiting": [evil], "forecast_note": evil}
    failed = {**ONE_PM, "error": evil, "q_up": None}
    html = panel.render(window=WINDOW, status=status, forecast=failed, history=[failed],
                        enabled=True, missing_weights=evil, load_error=evil, now=NOW)
    assert "<script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    quoted = panel.render(window={**WINDOW, "slug": "x' onmouseover='y"}, now=NOW)
    assert "x' onmouseover" not in quoted and "x&#x27; onmouseover" in quoted


def test_odd_rows_do_not_break_the_card() -> None:
    odd = {**ONE_PM, "final_closes": ["nan?"], "q_up": "x", "paths": None}
    html = panel.render(window=WINDOW, status={"state": "running"}, forecast=odd,
                        history=[odd, "not a row"], now=NOW)  # type: ignore[list-item]
    assert panel.TITLE in html


# --- the page ----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_card_data_reads_the_current_window(tmp_path: Path,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "lc2004_panel.db")
    monkeypatch.setattr(kronos_client, "missing_weights", lambda: "no weights; run the tool")
    await _db.init_db()
    for row in (NOON, ONE_PM):
        await ledger.insert_forecast(**{k: v for k, v in row.items() if k != "id"})
    data = await execution_view.lc2004_forecast_data(now=NOW)
    assert data["window"] == WINDOW and data["enabled"] is True
    assert data["missing_weights"] == "no weights; run the tool"
    assert data["forecast"]["q_up"] == pytest.approx(25 / 32)
    assert [r["paths_above"] for r in data["history"]] == [20, 24]
    assert data["history"][1]["final_closes"] == pytest.approx(CLOSES)
    assert data["load_error"] is None
    html = panel.render(**data)
    assert "Model: Up 78%" in html


def test_the_page_shows_the_card_after_kelly_and_shuts_down_fast(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ems.dashboard.app import app

    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "lc2004_page.db")
    started = time.monotonic()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        text = client.get("/").text
        exiting = time.monotonic()
    assert time.monotonic() - exiting < 3.0
    assert time.monotonic() - started < 10.0
    assert f"data-fold='{panel.FOLD}'" in text and "Display only — places no orders." in text
    assert (text.index("data-fold='kelly-horse-race'") < text.index(f"data-fold='{panel.FOLD}'")
            < text.index("data-fold='settings'"))
    assert "Run the lc2004-Kronos BTC 24h forecast" in text  # the Settings switch
