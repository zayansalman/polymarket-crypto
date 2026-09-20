"""STRATEGIES card: one row per strategy with an on/off switch.

Pure ``render(...)`` transform like every other panel — the switch states are
loaded by ``execution_view.py`` and passed in.
"""

from __future__ import annotations

from polymarket_bot import strategies as _strategies
from polymarket_exec.ops.dashboard.panels import strategies as panel

ALL_ON = {name: True for name in _strategies.STRATEGIES}


def test_card_is_headed_strategies() -> None:
    assert "STRATEGIES" in panel.render(enabled=ALL_ON)


def test_every_strategy_gets_a_row() -> None:
    html = panel.render(enabled=ALL_ON)
    for strategy in _strategies.STRATEGIES.values():
        assert strategy.label in html


def test_each_row_explains_what_the_strategy_does() -> None:
    html = panel.render(enabled=ALL_ON)
    for strategy in _strategies.STRATEGIES.values():
        assert strategy.description in html


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


def test_the_header_counts_how_many_are_running() -> None:
    html = panel.render(enabled={**ALL_ON, "daily_altcoin": False})
    total = len(_strategies.STRATEGIES)
    assert f"{total - 1} of {total} on" in html
