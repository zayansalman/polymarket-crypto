"""MY STRATEGIES card: one row per strategy this repo decides for itself.

Pure ``render(...)`` transform like every other panel — the switch states and
records are loaded by ``execution_view.py`` and passed in.

The copy-trade switches are deliberately NOT here: they act on somebody else's
wallet and are rendered on COPY TRADE WALLETS, beside the wallet they control.
"""

from __future__ import annotations

from html import escape

from polymarket_bot import inventory as _inv
from polymarket_bot import strategies as _strategies
from polymarket_exec.ops.dashboard.panels import strategies as panel

ALL_ON = {name: True for name in _strategies.STRATEGIES}
MINE = _strategies.in_group(_strategies.MINE)


def test_card_is_headed_my_strategies() -> None:
    assert "MY STRATEGIES" in panel.render(enabled=ALL_ON)


def test_every_strategy_i_run_myself_gets_a_row() -> None:
    html = panel.render(enabled=ALL_ON)
    for strategy in MINE.values():
        assert strategy.label in html


def test_each_row_explains_what_the_family_does_and_where_it_lives() -> None:
    html = panel.render(enabled=ALL_ON)
    for family in _inv.in_group(_strategies.MINE):
        assert escape(family.what) in html
        assert family.path in html


def test_copy_switches_are_not_in_this_card() -> None:
    # They belong beside the wallet they act on. A switch listed away from the
    # thing it controls cannot be judged.
    html = panel.render(enabled=ALL_ON)
    for name, strategy in _strategies.in_group(_strategies.COPY).items():
        assert f"id='strategy-{name}'" not in html
        assert strategy.label not in html


def test_an_enabled_strategy_renders_a_checked_switch() -> None:
    html = panel.render(enabled={**ALL_ON, "daily_altcoin": True})
    row = html.split("id='strategy-daily_altcoin'")[1].split(">")[0]
    assert "checked" in row


def test_a_disabled_strategy_renders_an_unchecked_switch() -> None:
    html = panel.render(enabled={**ALL_ON, "daily_altcoin": False})
    row = html.split("id='strategy-daily_altcoin'")[1].split(">")[0]
    assert "checked" not in row


def test_the_switch_applies_on_click_with_no_apply_button() -> None:
    # The click IS the intent — no confirm dialog, no second press.
    html = panel.render(enabled=ALL_ON)
    assert "setStrategy('daily_altcoin')" in html
    assert "Apply" not in html


def test_the_header_counts_switchable_rows_and_the_whole_tree() -> None:
    html = panel.render(enabled={**ALL_ON, "daily_altcoin": False})
    mine = _inv.in_group(_strategies.MINE)
    switchable = [f for f in mine if f.switch]
    assert f"{len(switchable) - 1} of {len(switchable)} switchable on" in html
    assert f"{len(mine)} in the tree" in html


def test_a_strategy_shows_what_it_has_actually_settled() -> None:
    # A switch with no number beside it reads the same whether it is working
    # or wired to nothing — which is the question this card exists to answer.
    html = panel.render(
        enabled=ALL_ON,
        records={"daily_altcoin": {"n": 18, "pnl": -67.08, "win_rate": 0.44}},
    )
    assert "18 settled" in html
    assert "$-67.08" in html
    assert "44% won" in html


def test_a_family_that_cannot_trade_says_why_instead_of_showing_a_zero() -> None:
    html = panel.render(enabled=ALL_ON)
    btc = next(f for f in _inv.FAMILIES if f.key == "btc_updown")
    assert btc.verdict in html
    assert "0 positions, ever" in html


def test_nothing_in_the_tree_is_hidden_from_the_card() -> None:
    # The operator asked to see the bloat so they can say what to delete.
    html = panel.render(enabled=ALL_ON)
    for family in _inv.in_group(_strategies.MINE):
        assert family.label in html, family.key


def test_a_family_with_no_switch_shows_its_state_not_a_dead_checkbox() -> None:
    html = panel.render(enabled=ALL_ON)
    for family in _inv.in_group(_strategies.MINE):
        if family.switch is None:
            assert f"st-{family.status}" in html
            assert f"id='strategy-{family.key}'" not in html


def test_families_are_grouped_by_status() -> None:
    html = panel.render(enabled=ALL_ON)
    for status, _rows in _inv.by_status(_strategies.MINE):
        assert _inv.STATUS_LABEL[status] in html


def test_a_strategy_with_no_record_yet_says_that_too() -> None:
    html = panel.render(enabled=ALL_ON, records={"daily_altcoin": {"n": 0}})
    assert "no settled positions yet" in html
