"""The copy-trade watcher observes, and is honest about what it observed."""

from __future__ import annotations

import time

import pytest

from polymarket_bot.copytrade import targets as _targets
from polymarket_bot.copytrade import watcher as _watcher
from polymarket_exec.ops.dashboard.panels import copytrade as panel


def _fill(**kw):
    base = dict(
        tx="0xabc", ts=1000, side="BUY", outcome="Up", size=100.0, price=0.99,
        title="Silver (XAGUSD) Up or Down on September 18?",
        slug="xagusd-up-or-down-on-september-18-2026",
        condition_id="0xdeadbeef", token_id="1234", followed=True,
    )
    base.update(kw)
    return _watcher.ObservedFill(**base)


def test_every_target_carries_the_measurement_that_selected_it() -> None:
    assert _targets.TARGETS
    for target in _targets.TARGETS.values():
        assert target.address == target.address.lower()
        assert target.edge_cents > 0, target.label
        assert target.t_stat > 0, target.label
        assert target.markets > 0, target.label
        # The whole reason these were chosen over the hourly wallets: a copier
        # arriving late still keeps the edge.
        assert target.edge_left_30min > 0, target.label


def test_the_default_target_is_one_of_the_registered_targets() -> None:
    assert _targets.get(_targets.DEFAULT_TARGET) is not None


def test_backfilled_fills_are_left_out_of_the_lag_median() -> None:
    """A restart must not make the transport look broken.

    The first poll returns history, so those fills are hours old through no
    fault of the feed. Counting them would misreport the one number that says
    whether copying is viable.
    """
    now = time.time()
    state = _watcher.WatcherState()
    state.fills = [
        _fill(ts=int(now - 7200), observed_at=now, backfill=True),
        _fill(ts=int(now - 20), observed_at=now, backfill=False),
    ]
    assert state.median_lag == pytest.approx(20, abs=2)
    assert state.live_fills == 1


def test_lag_median_is_zero_before_any_live_fill_arrives() -> None:
    now = time.time()
    state = _watcher.WatcherState()
    state.fills = [_fill(ts=int(now - 9999), observed_at=now, backfill=True)]
    assert state.median_lag == 0.0


def test_fills_outside_the_followed_markets_are_kept_not_dropped() -> None:
    """A target drifting into unmeasured markets has to be visible."""
    state = _watcher.WatcherState()
    state.fills = [_fill(followed=True), _fill(followed=False, slug="btc-updown-5m-1")]
    assert len(state.fills) == 2
    assert len(state.followed_fills) == 1


@pytest.mark.asyncio
async def test_a_switched_off_watcher_does_not_call_the_api() -> None:
    called = False

    class _Client:
        async def get(self, *a, **k):  # pragma: no cover - must not run
            nonlocal called
            called = True
            raise AssertionError("polled while switched off")

    import polymarket_bot.strategies as _strategies

    original = _strategies.enabled

    async def _off(_name: str) -> bool:
        return False

    _strategies.enabled = _off
    _watcher._strategies.enabled = _off
    try:
        w = _watcher.CopyWatcher()
        await w.poll_once(_Client())
    finally:
        _strategies.enabled = original
        _watcher._strategies.enabled = original
    assert called is False
    assert w.state.polls == 0


def test_the_panel_says_plainly_that_it_is_paper_only() -> None:
    state = _watcher.WatcherState(
        target=_targets.DEFAULT_TARGET, label="kodeoed", enabled=True,
        connected=True, last_poll=time.time(), polls=3,
    )
    state.fills = [_fill(observed_at=time.time())]
    html = panel.render(state=state, target=_targets.get(_targets.DEFAULT_TARGET))
    assert "paper" in html.lower()


def test_the_panel_uses_no_browser_dialogs() -> None:
    state = _watcher.WatcherState(target=_targets.DEFAULT_TARGET, label="kodeoed")
    html = panel.render(state=state, target=_targets.get(_targets.DEFAULT_TARGET))
    for banned in ("confirm(", "alert(", "prompt("):
        assert banned not in html


def test_the_panel_renders_before_the_watcher_has_started() -> None:
    assert "not started" in panel.render(state=None, target=None)


def test_the_wallet_links_to_its_polymarket_profile() -> None:
    """Measured stats are historical; the profile says what it is doing now."""
    state = _watcher.WatcherState(
        target=_targets.DEFAULT_TARGET, label="kodeoed", enabled=True, connected=True
    )
    html = panel.render(state=state, target=_targets.get(_targets.DEFAULT_TARGET))
    assert f"https://polymarket.com/profile/{_targets.DEFAULT_TARGET}" in html
    assert "rel='noopener'" in html


def test_drift_is_reported_when_a_target_leaves_its_measured_markets() -> None:
    """The failure that disqualified an earlier candidate must be visible.

    A wallet screened on Up-or-Down markets that is now trading weather or
    sports cannot be copied on that screening, and the card has to say so
    before it shows any P&L.
    """
    state = _watcher.WatcherState(
        target=_targets.DEFAULT_TARGET, label="kodeoed", enabled=True, connected=True
    )
    state.drift = {
        _targets.DEFAULT_TARGET: ("kodeoed", 0, 40),      # fully drifted
        "0x83451c358d50b3f8982124fc741e8bda4b4edd93": ("Oldstreet", 83, 89),
    }
    html = panel.render(state=state, target=_targets.get(_targets.DEFAULT_TARGET))
    assert "TARGET DRIFT" in html
    assert "have left the markets they were measured on" in html
    assert "0/40" in html and "83/89" in html


def test_every_target_is_a_taker_not_a_quoter() -> None:
    """The whole point of the screen: a maker's profit is a copier's cost."""
    for t in _targets.TARGETS.values():
        assert t.taker_share >= 0.55, f"{t.label} rests too much of its notional"
        assert t.edge_cents > 0, t.label
        assert t.in_scope >= 0.6, f"{t.label} has drifted off measured markets"


def test_the_registry_covers_mid_price_entries() -> None:
    """A registry of only 99c scalpers is the failure mode this replaced."""
    mid = [t for t in _targets.TARGETS.values() if t.avg_entry < 0.6]
    assert len(mid) >= 5, "screen collapsed back onto near-certainty buyers"


def test_the_panel_reports_execution_realism_not_just_the_paper_price() -> None:
    """A paper fill assumes the ladder survives; the card must show both."""
    state = _watcher.WatcherState(
        target=_targets.DEFAULT_TARGET, label="t", enabled=True, connected=True)
    summary = {"total": {"n": 3, "wins": 2, "pnl": 1.0, "staked": 100.0,
                         "real_staked": 103.5, "real_slip": 0.012},
               "per_target": [], "open": {"n": 0, "staked": 0}}
    html = panel.render(state=state, target=None, summary=summary)
    assert "EXECUTION REALISM" in html
    assert "cost of being late" in html
    assert "$103.50" in html and "+$3.50" in html


def test_the_panel_shows_skips_with_their_reason() -> None:
    """Silently declining most fills must not look like having nothing to do."""
    state = _watcher.WatcherState(target=_targets.DEFAULT_TARGET, label="t")
    decisions = [
        {"decision": "skipped", "reason": "empty book — market already settled", "n": 12},
        {"decision": "copied", "reason": "mirrored at the live ask", "n": 3},
    ]
    html = panel.render(state=state, target=None, decisions=decisions)
    assert "DECISIONS (24h)" in html
    assert "3 copied of 15 fills examined" in html
    assert "already settled" in html


def test_a_cheaper_realistic_cost_is_not_called_an_improvement_when_orders_failed() -> None:
    """Unfilled orders cost $0, which makes the total look better than reality."""
    state = _watcher.WatcherState(target=_targets.DEFAULT_TARGET, label="t")
    summary = {"execution": {"n": 8, "staked": 84.57, "real_staked": 72.57,
                             "real_slip": -0.01, "unfilled": 2},
               "total": {}, "per_target": [], "open": {"n": 0, "staked": 0}}
    html = panel.render(state=state, target=None, summary=summary)
    assert "lower only because some orders did not fill" in html
    assert "copy-bad" in html
    assert "orders that would NOT have filled" in html
