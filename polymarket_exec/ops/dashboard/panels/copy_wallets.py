"""COPY TRADE WALLETS card: who we follow, what they trade, what they just did.

One row per followed wallet — the measurement that earned it the place, how
much it has traded in the last hour, and its most recent fills with a Copy
button on each. The two copy switches (watch, autocopy) live here rather than
in the strategies list, because a switch over somebody else's wallet says
nothing useful sitting next to strategies this repo decides for itself.

"Still live" is read off our own ledger, never guessed: a fill we hold an open
copy of is live, a fill whose copy settled is resolved, and a fill we never
copied is reported as exactly that. ``copy_trades.resolves_at`` is NULL for
every row ever written (the watcher never stamps it), so there is no window
clock to show and this card does not invent one.

Pure ``render(...) -> str``, no DB access — data is loaded in
``execution_view.py``, per this repo's dashboard convention.
"""

from __future__ import annotations

import time
from html import escape
from typing import Any

from polymarket_bot import strategies as _strategies
from polymarket_bot.copytrade.targets import Target

PROFILE_URL = "https://polymarket.com/profile/"

HOUR = 3600.0

MAX_FILLS_PER_WALLET = 5
"""The operator asked for five. More than that and the card stops being a
glance at what the wallet is doing right now."""


def _ago(ts: float, now: float) -> str:
    if not ts:
        return "never"
    d = max(0.0, now - ts)
    if d < 60:
        return f"{d:.0f}s"
    if d < 3600:
        return f"{d / 60:.0f}m"
    if d < 86400:
        return f"{d / 3600:.1f}h"
    return f"{d / 86400:.1f}d"


def _switch(name: str, is_on: bool) -> str:
    """One copy switch, wired to the same endpoint every other control uses."""
    strategy = _strategies.STRATEGIES[name]
    checked = " checked" if is_on else ""
    return (
        "<div class='ctl-row strategy-row'>"
        "<span class='strategy-name'>"
        f"<span class='settings-label' title='{escape(strategy.key)}'>"
        f"{escape(strategy.label)}</span>"
        f"<span class='strategy-desc'>{escape(strategy.description)}</span>"
        "</span>"
        f"<input id='strategy-{escape(name)}' type='checkbox'{checked} "
        f"onchange=\"setStrategy('{escape(name)}')\" "
        f"aria-label='{escape(strategy.label)}' />"
        "</div>"
    )


def _copy_state(row: dict[str, Any] | None) -> tuple[str, str]:
    """(css class, text) for what our ledger says about one of their fills."""
    if row is None:
        return "cw-unknown", "not copied"
    if row.get("state") == "open":
        return "cw-live", "LIVE &middot; we hold a copy"
    pnl = row.get("pnl")
    if pnl is None:
        return "cw-unknown", "copy settled, no P&amp;L recorded"
    cls = "cw-good" if pnl > 0 else ("cw-bad" if pnl < 0 else "cw-unknown")
    return cls, f"resolved &middot; copy {'won' if row.get('won') else 'lost'} ${pnl:+.2f}"


def _fill_row(fill: Any, row: dict[str, Any] | None, now: float) -> str:
    """One of their fills: what they did, what it did for us, and a Copy button."""
    cls, text = _copy_state(row)
    side = str(getattr(fill, "side", ""))
    side_cls = "copy-buy" if side == "BUY" else "copy-sell"
    notional = getattr(fill, "size", 0.0) * getattr(fill, "price", 0.0)
    # Copying only ever makes sense on an entry we do not already hold. A sell
    # is them leaving, and a market we hold cannot be entered twice.
    can_copy = (
        side == "BUY"
        and bool(getattr(fill, "followed", False))
        and row is None
    )
    button = (
        f"<button class='cw-copy-btn' onclick=\"copyFill('{escape(str(fill.tx))}')\" "
        f"id='cw-copy-{escape(str(fill.tx))}'>copy</button>"
        if can_copy else "<span class='cw-copy-none'>&mdash;</span>"
    )
    tag = (
        "" if getattr(fill, "followed", False)
        else "<span class='copy-other-tag'>off-family</span>"
    )
    return (
        "<div class='cw-fill'>"
        f"<span class='cw-when'>{_ago(getattr(fill, 'ts', 0), now)} ago</span>"
        f"<span class='{side_cls}'>{escape(side)} {escape(str(getattr(fill, 'outcome', '')))}"
        f"</span>"
        f"<span class='cw-sz'>{getattr(fill, 'size', 0.0):.0f}sh @ "
        f"{getattr(fill, 'price', 0.0):.3f}</span>"
        f"<span class='cw-notional'>${notional:,.0f}</span>"
        f"{button}"
        f"<span class='cw-title'>{escape(str(getattr(fill, 'title', '')))}</span>"
        f"<span class='cw-state {cls}'>{text}</span>{tag}"
        "</div>"
    )


def _wallet_html(
    *,
    target: Target,
    fills: list[Any],
    copies_by_tx: dict[str, dict[str, Any]],
    open_copies: int,
    now: float,
) -> str:
    last_hour = sum(1 for f in fills if getattr(f, "ts", 0) >= now - HOUR)
    followed = [f for f in fills if getattr(f, "followed", False)]
    recent = fills[:MAX_FILLS_PER_WALLET]

    link = (
        f"<a class='copy-link' href='{PROFILE_URL}{escape(target.address)}' "
        f"target='_blank' rel='noopener'>{escape(target.label)}</a>"
    )
    # An hour with no fills is the thing worth seeing at a glance, so it gets
    # the loud class rather than being a zero in a row of numbers.
    hour_cls = "cw-live" if last_hour else "cw-idle"
    rows = "".join(
        _fill_row(f, copies_by_tx.get(str(getattr(f, "tx", ""))), now)
        for f in recent
    ) or "<div class='gr-toggle-hint'>no fills seen yet.</div>"

    return (
        "<div class='cw-wallet'>"
        "<div class='cw-head'>"
        f"<span class='cw-name'>{link}</span>"
        f"<span class='cw-addr'>{escape(target.address[:10])}&hellip;"
        f"{escape(target.address[-4:])}</span>"
        f"<span class='cw-hour {hour_cls}'>{last_hour} fill"
        f"{'' if last_hour == 1 else 's'} in the last hour</span>"
        "</div>"
        "<div class='cw-what'>"
        f"trades <b>{escape(target.assets)}</b> &middot; {escape(target.cadence)} "
        f"&middot; {target.fills_per_day:.0f} fills/day"
        "</div>"
        "<div class='cw-evidence'>"
        f"measured <b>{target.edge_cents:+.2f}c/share</b> over "
        f"{target.markets} markets &middot; {100 * target.taker_share:.0f}% taker "
        f"&middot; avg stake ${target.avg_stake_usd:,.0f}"
        "</div>"
        "<div class='cw-counts'>"
        f"<span>fills seen <b>{len(fills)}</b></span>"
        f"<span>on followed markets <b>{len(followed)}</b></span>"
        f"<span>our open copies <b>{open_copies}</b></span>"
        "</div>"
        f"<div class='cw-fills'>{rows}</div>"
        "</div>"
    )


def render(
    *,
    targets: dict[str, Target],
    state: Any | None,
    enabled: dict[str, bool],
    copies_by_tx: dict[str, dict[str, Any]] | None = None,
    open_by_target: dict[str, int] | None = None,
    now: float | None = None,
) -> str:
    now = time.time() if now is None else now
    copies_by_tx = copies_by_tx or {}
    open_by_target = open_by_target or {}

    watching = bool(enabled.get("copy_macro_daily", False))
    autocopy = bool(enabled.get("copy_autocopy", False))

    if not watching:
        status = "<span class='win'>not watching</span>"
    elif state is None:
        status = "<span class='win'>watcher has not run yet</span>"
    elif not getattr(state, "connected", False):
        status = "<span class='win copy-bad'>feed down</span>"
    else:
        status = (
            f"<span class='win'>{len(targets)} wallet"
            f"{'' if len(targets) == 1 else 's'} &middot; polled "
            f"{_ago(getattr(state, 'last_poll', 0.0), now)} ago</span>"
        )

    switches = (
        _switch("copy_macro_daily", watching)
        + _switch("copy_autocopy", autocopy)
    )

    err = ""
    last_error = getattr(state, "last_error", "") if state is not None else ""
    if last_error:
        err = f"<div class='copy-error'>{escape(str(last_error))}</div>"

    if not targets:
        body = "<div class='gr-toggle-hint'>no wallets registered.</div>"
    else:
        body = "".join(
            _wallet_html(
                target=target,
                fills=(state.fills_for(address) if state is not None else []),
                copies_by_tx=copies_by_tx,
                open_copies=open_by_target.get(address, 0),
                now=now,
            )
            for address, target in targets.items()
        )

    return (
        "<section class='card copy-wallets-card'>"
        f"<div class='card-h'>COPY TRADE WALLETS{status}</div>"
        f"{switches}"
        f"{err}"
        f"{body}"
        "<div class='gr-toggle-hint' style='margin-top:8px'>"
        "PAPER — a copy is priced against the live ask ladder and charged the "
        "taker fee, but no real order is ever sent."
        "</div>"
        "</section>"
    )
