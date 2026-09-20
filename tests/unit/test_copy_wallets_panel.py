"""COPY TRADE WALLETS card: who we follow, what they trade, what they just did.

Pure ``render(...)`` transform — the watcher state and ledger rows are loaded
by ``execution_view.py`` and passed in.
"""

from __future__ import annotations

import time

from polymarket_bot import strategies as _strategies
from polymarket_bot.copytrade import targets as _targets
from polymarket_bot.copytrade import watcher as _watcher
from polymarket_exec.ops.dashboard.panels import copy_wallets as panel

ADDR = _targets.DEFAULT_TARGET
BOTH_ON = {"copy_macro_daily": True, "copy_autocopy": True}


def _fill(**kw):
    base = dict(
        tx="0xabc", ts=int(time.time()) - 120, wallet=ADDR, side="BUY",
        outcome="Up", size=40.0, price=0.54,
        title="Bitcoin Up or Down - September 21, 3PM ET",
        slug="bitcoin-up-or-down-september-21-3pm-et",
        condition_id="0xdead", token_id="1234", followed=True,
    )
    base.update(kw)
    return _watcher.ObservedFill(**base)


def _state(fills):
    st = _watcher.WatcherState()
    st.fills = list(fills)
    st.watching = [ADDR]
    st.connected = True
    st.last_poll = time.time()
    st.enabled = True
    return st


def _render(**kw):
    kw.setdefault("targets", _targets.TARGETS)
    kw.setdefault("state", _state([_fill()]))
    kw.setdefault("enabled", BOTH_ON)
    return panel.render(**kw)


def test_card_is_headed_copy_trade_wallets() -> None:
    assert "COPY TRADE WALLETS" in _render()


def test_every_registered_wallet_gets_a_block() -> None:
    html = _render()
    for target in _targets.TARGETS.values():
        assert target.label in html


def test_a_wallet_always_links_to_its_polymarket_profile() -> None:
    # Measured stats are historical; whether a target is still running the
    # strategy we measured is only answerable by looking at it right now.
    html = _render()
    for address in _targets.TARGETS:
        assert f"https://polymarket.com/profile/{address}" in html


def test_the_card_says_what_each_wallet_trades() -> None:
    html = _render()
    for target in _targets.TARGETS.values():
        assert target.assets in html
        assert target.cadence in html


def test_activity_in_the_last_hour_is_counted() -> None:
    now = time.time()
    html = _render(
        state=_state([
            _fill(tx="0x1", ts=int(now) - 60),
            _fill(tx="0x2", ts=int(now) - 600),
            _fill(tx="0x3", ts=int(now) - 7200),  # older than an hour
        ]),
        now=now,
    )
    assert "2 fills in the last hour" in html


def test_a_quiet_hour_reads_as_quiet_rather_than_as_a_bare_zero() -> None:
    now = time.time()
    html = _render(state=_state([_fill(ts=int(now) - 7200)]), now=now)
    assert "0 fills in the last hour" in html
    assert "cw-idle" in html


def test_only_five_of_their_fills_are_shown() -> None:
    fills = [_fill(tx=f"0x{i}") for i in range(12)]
    html = _render(state=_state(fills))
    shown = [f"0x{i}" for i in range(12) if f"cw-copy-0x{i}" in html]
    assert len(shown) == panel.MAX_FILLS_PER_WALLET


def test_an_uncopied_entry_offers_a_copy_button() -> None:
    html = _render(state=_state([_fill(tx="0xfeed")]))
    assert "copyFill('0xfeed')" in html


def test_a_fill_we_already_hold_is_marked_live_and_offers_no_button() -> None:
    html = _render(
        state=_state([_fill(tx="0xheld")]),
        copies_by_tx={"0xheld": {"tx": "0xheld", "state": "open"}},
    )
    assert "we hold a copy" in html
    assert "copyFill('0xheld')" not in html


def test_a_settled_copy_reports_the_result_rather_than_a_guess_at_the_market() -> None:
    html = _render(
        state=_state([_fill(tx="0xdone")]),
        copies_by_tx={
            "0xdone": {"tx": "0xdone", "state": "settled", "won": 1, "pnl": 3.21}
        },
    )
    assert "resolved" in html
    assert "$+3.21" in html


def test_their_exit_is_never_offered_as_something_to_copy() -> None:
    html = _render(state=_state([_fill(tx="0xsell", side="SELL")]))
    assert "copyFill('0xsell')" not in html


def test_an_off_family_fill_is_shown_but_not_copyable() -> None:
    # A target quietly changing what it trades is the failure mode that killed
    # two earlier candidates, so it has to be visible — just not followable.
    html = _render(state=_state([_fill(tx="0xdrift", followed=False)]))
    assert "off-family" in html
    assert "copyFill('0xdrift')" not in html


def test_both_copy_switches_live_on_this_card() -> None:
    html = _render()
    for name, strategy in _strategies.in_group(_strategies.COPY).items():
        assert f"id='strategy-{name}'" in html
        assert f"setStrategy('{name}')" in html
        assert strategy.label in html


def test_autocopy_off_still_shows_the_fills_and_their_copy_buttons() -> None:
    html = _render(enabled={"copy_macro_daily": True, "copy_autocopy": False})
    assert "copyFill('0xabc')" in html
    row = html.split("id='strategy-copy_autocopy'")[1].split(">")[0]
    assert "checked" not in row


def test_not_watching_is_said_plainly() -> None:
    html = _render(enabled={"copy_macro_daily": False, "copy_autocopy": False})
    assert "not watching" in html


def test_a_card_rendered_before_the_watcher_starts_does_not_blow_up() -> None:
    html = _render(state=None)
    assert "COPY TRADE WALLETS" in html
    assert "no fills seen yet" in html


def test_the_card_says_it_is_paper() -> None:
    assert "PAPER" in _render()


def test_no_browser_dialogs() -> None:
    # The click is the intent: never a confirm/alert/prompt pop-up.
    html = _render()
    for banned in ("confirm(", "alert(", "prompt("):
        assert banned not in html
