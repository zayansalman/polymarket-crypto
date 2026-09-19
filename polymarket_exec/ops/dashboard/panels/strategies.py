"""STRATEGIES card: one row per strategy, each with its own on/off switch.

Sits directly under FEEDS — the feeds card says what data is arriving, this
one says what is being done with it. Several strategies run side by side, so
each gets a switch rather than the card being a pick-one selector.

The switch applies on click (no Apply button, no confirm dialog — the click
is the intent) by posting to the same ``/api/runtime-config`` endpoint every
other runtime control uses. Rows are generated from
``polymarket_bot.strategies.STRATEGIES``, so registering a strategy there is
the only step needed to surface it here.

Pure ``render(...) -> str`` transform, no DB access — switch states are loaded
in ``execution_view.py`` and passed in, matching this dashboard's convention.
"""

from __future__ import annotations

from html import escape

from polymarket_bot.strategies import STRATEGIES, Strategy


def _row_html(strategy: Strategy, is_on: bool) -> str:
    checked = " checked" if is_on else ""
    return (
        "<div class='ctl-row strategy-row'>"
        "<span class='strategy-name'>"
        f"<span class='settings-label' title='{escape(strategy.key)}'>"
        f"{escape(strategy.label)}</span>"
        f"<span class='strategy-desc'>{escape(strategy.description)}</span>"
        "</span>"
        f"<input id='strategy-{escape(strategy.name)}' type='checkbox'{checked} "
        f"onchange=\"setStrategy('{escape(strategy.name)}')\" "
        f"aria-label='{escape(strategy.label)}' />"
        "</div>"
    )


def render(*, enabled: dict[str, bool]) -> str:
    on = sum(1 for name in STRATEGIES if enabled.get(name, False))
    rows = "".join(
        _row_html(strategy, enabled.get(name, False))
        for name, strategy in STRATEGIES.items()
    )
    return (
        "<section class='card strategies-card'>"
        f"<div class='card-h'>STRATEGIES<span class='win'>{on} of {len(STRATEGIES)} on"
        "</span></div>"
        "<div class='gr-toggle-hint' style='margin-bottom:10px'>"
        "off stops NEW entries only — open positions still settle."
        "</div>"
        f"{rows}"
        "</section>"
    )
