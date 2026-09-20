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
    summary = {"execution": {"n": 3, "filled_decision": 100.0,
                             "filled_real": 103.5, "real_slip": 0.012,
                             "unfilled": 0, "lost_to_unfilled": 0.0},
               "total": {}, "per_target": [], "open": {"n": 0, "staked": 0}}
    html = panel.render(state=state, target=None, summary=summary)
    assert "EXECUTION REALISM" in html
    assert "cost of arriving late" in html
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


def test_dead_orders_are_reported_separately_from_price_movement() -> None:
    """A dead order costs $0 and would otherwise flatter the total.

    Price drift and failure-to-fill are different problems with different
    fixes, so averaging them into one number hides both.
    """
    state = _watcher.WatcherState(target=_targets.DEFAULT_TARGET, label="t")
    summary = {"execution": {"n": 8, "filled_decision": 60.0, "filled_real": 61.2,
                             "real_slip": 0.004, "unfilled": 2,
                             "lost_to_unfilled": 24.57},
               "total": {}, "per_target": [], "open": {"n": 0, "staked": 0}}
    html = panel.render(state=state, target=None, summary=summary)
    assert "would NOT have filled" in html
    assert "2 of 8" in html and "$24.57" in html
    assert "cost of arriving late" in html and "+$1.20" in html


@pytest.mark.asyncio
async def test_old_fills_are_not_re_examined_after_many_targets_are_polled() -> None:
    """Regression: a global, trimmed key-set made history look new every poll.

    With 40 targets x 100 fills the old dedupe set blew its cap on the first
    pass, got trimmed to a couple of hundred keys, and then re-fed thousands of
    long-settled fills into the copier — which logged them as 'market already
    closed' and buried the real signal.
    """
    import polymarket_bot.strategies as _strategies

    original = _strategies.enabled
    considered: list[str] = []

    async def _on(_name: str) -> bool:
        return True

    async def _spy(_client, fill, _addr):
        considered.append(fill.tx)
        return False

    class _Resp:
        status_code = 200

        def __init__(self, rows):
            self._rows = rows

        def raise_for_status(self):
            return None

        def json(self):
            return self._rows

    row = {
        "type": "TRADE", "transactionHash": "0xold", "timestamp": 1000,
        "side": "BUY", "outcome": "Up", "size": 10, "price": 0.5,
        "title": "t", "slug": "bitcoin-up-or-down-september-1-2026-1am-et",
        "conditionId": "0xc", "asset": "1",
    }

    class _Client:
        async def get(self, *a, **k):
            return _Resp([row])

    _strategies.enabled = _on
    _watcher._strategies.enabled = _on
    spy_orig = _watcher._trader.consider
    _watcher._trader.consider = _spy
    try:
        w = _watcher.CopyWatcher()
        await w.poll_once(_Client())          # backfill: never copied
        for _ in range(30):                   # many polls, same single fill
            await w.poll_once(_Client())
    finally:
        _strategies.enabled = original
        _watcher._trader.consider = spy_orig
    assert considered == [], "an already-seen fill was re-examined"


@pytest.mark.asyncio
async def test_a_stale_fill_is_never_considered() -> None:
    """A fill from days ago is unfollowable at any latency.

    Guards against bookkeeping slips re-surfacing history: a target whose first
    poll returns nothing keeps a watermark of 0, and the next poll would
    otherwise treat its whole history as new.
    """
    import polymarket_bot.strategies as _strategies

    original = _strategies.enabled
    seen: list[str] = []

    async def _on(_name: str) -> bool:
        return True

    async def _spy(_c, fill, _a):
        seen.append(fill.tx)
        return False

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return [{
                "type": "TRADE", "transactionHash": "0xancient",
                "timestamp": 1, "side": "BUY", "outcome": "Up", "size": 10,
                "price": 0.5, "title": "t",
                "slug": "bitcoin-up-or-down-september-1-2026-1am-et",
                "conditionId": "0xc", "asset": "1",
            }]

    class _Client:
        async def get(self, *a, **k):
            return _Resp()

    _strategies.enabled = _on
    _watcher._strategies.enabled = _on
    spy_orig = _watcher._trader.consider
    _watcher._trader.consider = _spy
    try:
        w = _watcher.CopyWatcher()
        w._polled.update(_targets.TARGETS)   # pretend backfill already happened
        await w.poll_once(_Client())
    finally:
        _strategies.enabled = original
        _watcher._trader.consider = spy_orig
    assert seen == [], "a fill from 1970 reached the copier"


def test_copies_larger_than_the_target_bet_are_flagged() -> None:
    """Rounding a sub-floor clip up to 5 shares is not a 1:1 mirror.

    Per-share edge is unchanged, which is what the lab measures, but the
    exposure is larger than the bet the wallet actually made — so the card has
    to say so rather than let it read as a faithful copy.
    """
    state = _watcher.WatcherState(target=_targets.DEFAULT_TARGET, label="t")
    summary = {"execution": {"n": 10, "filled_decision": 50.0, "filled_real": 50.0,
                             "unfilled": 0, "lost_to_unfilled": 0.0, "upsized": 4},
               "total": {}, "per_target": [], "open": {"n": 0, "staked": 0}}
    html = panel.render(state=state, target=None, summary=summary)
    assert "larger than the target" in html and "4 of 10" in html


def test_results_show_the_realistic_pnl_beside_the_paper_one() -> None:
    """A paper ledger's biggest lie is counting a never-filled order as a win."""
    state = _watcher.WatcherState(target=_targets.DEFAULT_TARGET, label="t")
    # 10 settled but only 6 re-priced: the realistic figure must be shown
    # against the paper figure for those same 6, never against all 10.
    summary = {"total": {"n": 10, "wins": 4, "pnl": -2.82, "real_pnl": -5.10,
                         "staked": 45.57, "never_filled": 3,
                         "real_n": 6, "paper_on_real": -4.90},
               "per_target": [], "open": {"n": 4, "staked": 65.94},
               "execution": {}}
    html = panel.render(state=state, target=None, summary=summary)
    assert "6 re-priced at landing" in html
    assert "paper $-4.90" in html   # matched subset, not the -2.82 total
    assert "3 never filled" in html
    assert "real $-5.10" in html


def test_each_target_is_judged_on_its_realistic_pnl() -> None:
    """Which of the forty to keep is a question the paper number cannot answer."""
    state = _watcher.WatcherState(target=_targets.DEFAULT_TARGET, label="t")
    summary = {
        "total": {"n": 4, "wins": 2, "pnl": 5.0, "real_pnl": 1.0,
                  "staked": 40.0, "never_filled": 1},
        "per_target": [
            {"target_label": "t-aaaa", "n": 3, "wins": 2, "pnl": 6.0,
             "real_pnl": -1.5, "staked": 30.0, "never_filled": 1, "slip": 0.01},
        ],
        "open": {"n": 0, "staked": 0}, "execution": {},
    }
    html = panel.render(state=state, target=None, summary=summary)
    # paper says +6.00, reality says -1.50 — both must be visible
    assert "paper $+6.00" in html
    assert "$-1.50" in html
    assert "1 unfilled" in html


def test_reachability_is_reported_per_target() -> None:
    """A wallet can have a real edge and still be impossible to follow."""
    state = _watcher.WatcherState(target=_targets.DEFAULT_TARGET, label="t")
    reach = [{"target_label": "t-aaaa", "seen": 20, "copied": 2,
              "too_late": 15, "lag": 14.0}]
    html = panel.render(state=state, target=None, reach=reach)
    assert "REACHABILITY" in html
    assert "2 of 20 fills followed" in html


def test_a_target_exiting_while_we_hold_is_reported() -> None:
    """After they sell and we do not, the result is ours, not theirs.

    Silently holding on would let the row keep reading as a faithful copy of a
    wallet that is no longer in the trade.
    """
    state = _watcher.WatcherState(target=_targets.DEFAULT_TARGET, label="t")
    summary = {"execution": {"n": 6, "filled_decision": 30.0, "filled_real": 30.0,
                             "unfilled": 0, "lost_to_unfilled": 0.0,
                             "upsized": 0, "diverged": 2},
               "total": {}, "per_target": [], "open": {"n": 0, "staked": 0}}
    html = panel.render(state=state, target=None, summary=summary)
    assert "target exited while we still held" in html


def test_skips_are_scored_on_what_they_refused() -> None:
    """A slippage cap is guesswork until the trades it refuses are priced."""
    state = _watcher.WatcherState(target=_targets.DEFAULT_TARGET, label="t")
    board = [
        {"reason": "book moved +10.0c against us, over the 3c cap",
         "n": 6, "pnl": -4.20, "wins": 1},
        {"reason": "their clip 1.4sh is under the 5sh venue floor",
         "n": 3, "pnl": 2.50, "wins": 3},
    ]
    html = panel.render(state=state, target=None, skips=board)
    assert "SKIPS, SCORED" in html
    assert "saved us $4.20" in html   # refusing losers: the cap earning its keep
    assert "cost us $2.50" in html    # refusing winners: the rule costing us


def test_partial_fills_are_reported() -> None:
    """A thin book fills part of the order; paper pretends it filled whole."""
    state = _watcher.WatcherState(target=_targets.DEFAULT_TARGET, label="t")
    summary = {"execution": {"n": 9, "filled_decision": 40.0, "filled_real": 41.0,
                             "unfilled": 1, "lost_to_unfilled": 3.0,
                             "upsized": 0, "diverged": 0, "partial": 4},
               "total": {}, "per_target": [], "open": {"n": 0, "staked": 0}}
    html = panel.render(state=state, target=None, summary=summary)
    assert "partial fills" in html and "4 of 9" in html


def test_our_return_is_shown_against_the_targets_own() -> None:
    """The copy P&L alone cannot separate a bad fill from a bad call."""
    state = _watcher.WatcherState(target=_targets.DEFAULT_TARGET, label="t")
    # Only 2 of the 6 settled rows have the target's result, so the comparison
    # must use those 2 on BOTH sides — not their 2 against our 6.
    summary = {"total": {"n": 6, "wins": 3, "pnl": 5.0, "real_pnl": 2.0,
                         "staked": 100.0, "never_filled": 0,
                         "their_pnl": 8.0, "their_staked": 100.0,
                         "matched_n": 2, "matched_staked": 100.0,
                         "matched_pnl": 2.0},
               "per_target": [], "open": {"n": 0, "staked": 0}, "execution": {}}
    html = panel.render(state=state, target=None, summary=summary)
    assert "same 2 fills" in html
    assert "+8.0%" in html      # theirs
    assert "ours +2.0%" in html # ours, on realistic fills
    assert "gap -6.0pp" in html
