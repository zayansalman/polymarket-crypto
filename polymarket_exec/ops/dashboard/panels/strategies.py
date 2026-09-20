"""MY STRATEGIES card: every strategy family in the repo, hiding none of them.

Sits directly under FEEDS — the feeds card says what data is arriving, this
one says what is being done with it.

The card shows the whole ``polymarket_bot.inventory`` list, grouped by status,
not just the switchable ones. A card listing three working strategies next to
a tree holding eleven more in various states of abandonment reads as a tidy
repo, and this one is not tidy. The dead entries are here so they can be
deleted for being visible, and each carries the one-line case for deleting it.

A family with a switch gets a checkbox; one that cannot run gets its status
and its verdict instead, because a checkbox in front of code nothing can reach
is a lie about what clicking it would do.

Copy-trade switches are NOT here — they act on somebody else's wallet and
render on COPY TRADE WALLETS, beside the wallet they control. The copy family
still appears in the list, with a pointer to where its switches live.

The switch applies on click (no Apply button, no confirm dialog — the click
is the intent) by posting to the same ``/api/runtime-config`` endpoint every
other runtime control uses.

Pure ``render(...) -> str`` transform, no DB access — switch states and live
records are loaded in ``execution_view.py`` and passed in, matching this
dashboard's convention.
"""

from __future__ import annotations

from html import escape
from typing import Any

from polymarket_bot import inventory as _inv
from polymarket_bot.strategies import MINE, STRATEGIES


def _record_html(family: _inv.Family, live: dict[str, Any] | None) -> str:
    """What it has actually done — the live number when we have one."""
    if live and live.get("n") is not None:
        n = int(live.get("n") or 0)
        if not n:
            return "<span class='strategy-record'>no settled positions yet</span>"
        pnl = float(live.get("pnl") or 0.0)
        cls = "pos" if pnl > 0 else ("neg" if pnl < 0 else "")
        win = live.get("win_rate")
        win_txt = f" &middot; {100 * float(win):.0f}% won" if win is not None else ""
        return (
            "<span class='strategy-record'>"
            f"{n} settled &middot; <b class='{cls}'>${pnl:+,.2f}</b>{win_txt}</span>"
        )
    return f"<span class='strategy-record'>{escape(family.record)}</span>"


def _switch_html(family: _inv.Family, is_on: bool) -> str:
    if family.switch is None or family.switch not in STRATEGIES:
        # No switch: say what state it is in instead of offering a control
        # that would do nothing.
        return (
            f"<span class='strategy-status st-{escape(family.status)}'>"
            f"{escape(_inv.STATUS_LABEL[family.status])}</span>"
        )
    name = family.switch
    checked = " checked" if is_on else ""
    return (
        f"<input id='strategy-{escape(name)}' type='checkbox'{checked} "
        f"onchange=\"setStrategy('{escape(name)}')\" "
        f"aria-label='{escape(STRATEGIES[name].label)}' />"
    )


def _row_html(family: _inv.Family, is_on: bool, live: dict[str, Any] | None) -> str:
    verdict = (
        f"<span class='strategy-verdict'>{escape(family.verdict)}</span>"
        if family.verdict else ""
    )
    return (
        "<div class='ctl-row strategy-row'>"
        "<span class='strategy-name'>"
        f"<span class='settings-label' title='{escape(family.path)}'>"
        f"{escape(family.label)}</span>"
        f"<span class='strategy-path'>"
        f"{escape(family.path) if family.path else 'source deleted — only its rows remain'}"
        "</span>"
        f"{_record_html(family, live)}"
        f"<span class='strategy-desc'>{escape(family.what)}</span>"
        f"{verdict}"
        "</span>"
        f"{_switch_html(family, is_on)}"
        "</div>"
    )


def render(
    *,
    enabled: dict[str, bool],
    records: dict[str, dict[str, Any]] | None = None,
) -> str:
    records = records or {}
    switchable = [
        f for f in _inv.FAMILIES
        if f.group == MINE and f.switch is not None and f.switch in STRATEGIES
    ]
    on = sum(1 for f in switchable if enabled.get(f.switch or "", False))

    sections = ""
    for status, families in _inv.by_status(MINE):
        rows = "".join(
            _row_html(f, enabled.get(f.switch or "", False), records.get(f.key))
            for f in families
        )
        sections += (
            f"<div class='strategy-group st-{escape(status)}'>"
            f"{escape(_inv.STATUS_LABEL[status])} &middot; {len(families)}</div>"
            f"{rows}"
        )

    return (
        "<section class='card strategies-card'>"
        f"<div class='card-h'>MY STRATEGIES<span class='win'>{on} of "
        f"{len(switchable)} switchable on &middot; "
        f"{len([f for f in _inv.FAMILIES if f.group == MINE])} in the tree"
        "</span></div>"
        "<div class='gr-toggle-hint' style='margin-bottom:10px'>"
        "off stops NEW entries only — open positions still settle. Everything "
        "in the tree is listed, working or not, so nothing rots unseen."
        "</div>"
        f"{sections}"
        "</section>"
    )
