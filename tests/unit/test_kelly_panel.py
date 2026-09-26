"""KELLY HORSE-RACE card (``ems/dashboard/panels/kelly_horse_race.py``): pure render, and the
page wiring with a real pass's records."""

from __future__ import annotations

import pytest

from ems import runtime_knobs as _knobs
from ems.dashboard import execution_view
from ems.dashboard.panels import kelly_horse_race as panel
from ems.kelly_horse_race import runner as rn
from tests.unit.test_kelly_runner import (  # noqa: F401 - the fixtures
    NOW,
    SLUG,
    kelly_db,
    make_hub,
    make_runner,
    venue,
)
from tests.unit.test_standard_terms import COINED

DECISION = {
    "id": 1, "window_slug": SLUG, "window_start": NOW - 30, "window_end": NOW + 870,
    "k_price": 100_000.0, "k_source": "TWAP-60s print at the open", "x_price": 100_020.0,
    "r60": 0.0015, "sigma_h": 0.0137, "tau_h": 0.24, "z": 0.21, "p_up": 0.583, "u1": 0.1,
    "side": "Up", "u2": 0.5, "best_bid": 0.5, "best_ask": 0.52, "bid_size": 20.0,
    "tick_size": 0.01, "min_order_size": 5.0, "max_notional_usd": 5.0, "price": 0.5,
    "shares": 7.5, "notional_usd": 3.75, "reason": None, "outcome": None,
    "orders": {
        "paper": {"state": "resting", "size": 7.5, "filled_size": 2.0, "pnl_usd": None},
        "live": {"state": "blocked", "size": 7.5, "filled_size": 0.0,
                 "reason": "max_trade: Per-trade cap: $3.75 is above the live cap of $3.00."},
    },
}
STATUS = {
    "state": "running", "last_pass_ts": NOW - 4, "errors": [],
    "endpoints": {"paper": {"state": "on", "message": "Paper: on.", "active": True},
                  "live": {"state": "not_built", "message": "Live: none built.",
                           "active": False}},
    "window": {"slug": SLUG},
}


def test_an_empty_card_says_nothing_was_decided() -> None:
    html = panel.render(now=NOW)
    assert "data-fold='kelly-horse-race'" in html and panel.TITLE in html
    assert "No window decided yet." in html and "last pass never" in html


def test_the_latest_decision_factor_by_factor() -> None:
    html = panel.render(decisions=[DECISION], status=STATUS, enabled=True, now=NOW,
                        caps={"max_notional_usd": 5.0, "paper_max_trade_usd": 5.0,
                              "live_max_trade_usd": 3.0})
    for text in ("PAPER ON", "LIVE NOT BUILT", "SWITCH ON", "100,000.00", "58.3%",
                 "u1 0.1000 → UP", "bid 0.50 × 20", "7.50 shares at 0.50 = $3.75", "RESTING",
                 "2.00 of 7.50 filled", "BLOCKED", "max_trade"):
        assert text in html, text
    assert "live per-trade cap ($3.00) is below" in html
    assert "paper per-trade cap" not in html


def test_a_window_with_no_order_says_why() -> None:
    no_order = {**DECISION, "side": None, "reason": "k_missing: not held. Still missing.",
                "orders": {}}
    assert "No order this window: k_missing" in panel.render(decisions=[no_order], now=NOW)


def test_errors_and_a_failed_read_are_shown() -> None:
    html = panel.render(status={**STATUS, "state": "pass_failed",
                                "errors": ["Checking fills failed: boom"]},
                        load_error="the ledger (OperationalError: no such table)", now=NOW)
    assert "PASS FAILED" in html and "Checking fills failed: boom" in html
    assert "Could not read this strategy's records" in html


def test_recent_windows_list_each_mode() -> None:
    settled = {**DECISION, "id": 2, "outcome": "Up",
               "orders": {"paper": {"state": "filled", "filled_size": 7.5, "pnl_usd": 3.75}}}
    html = panel.render(decisions=[DECISION, settled], now=NOW)
    assert "kelly-table" in html and "$+3.75" in html


def test_no_coined_terms_on_the_card() -> None:
    html = panel.render(decisions=[DECISION], status=STATUS, now=NOW)
    assert not COINED.search(html)


@pytest.mark.asyncio
async def test_the_page_shows_a_real_pass(kelly_db, venue) -> None:  # noqa: F811
    await make_runner(venue, make_hub(), {"now": NOW}).pass_once()
    data = await execution_view.kelly_horse_race_data()
    assert data["load_error"] is None and data["decisions"][0]["window_slug"] == SLUG
    assert data["caps"]["max_notional_usd"] == await _knobs.get(
        "kelly_horse_race_max_notional_usd")
    html = await execution_view.execution_view_html()
    assert panel.TITLE in html and SLUG in html
    assert html.index("FADE 1H MOMENTUM ON 15M") < html.index(panel.TITLE) < html.index(
        "SETTINGS")
    assert rn.status()["window"]["slug"] == SLUG
