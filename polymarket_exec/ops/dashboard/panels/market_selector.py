"""Topbar market selector: asset buttons over timeframe buttons.

The selection is global (paper AND live share it — see
``polymarket_bot/market_selection.py``). Markets holding an open position glow
green/red by net unrealized P&L (flat glow when no live mark exists). Pure
``render(...)`` transform — data is loaded by the caller.
"""
from __future__ import annotations

import re
from html import escape
from typing import Any

from polymarket_bot import market_selection as ms

from . import _shared as s

_UPDOWN_SLUG = re.compile(r"^([a-z]+)-updown-(5m|15m|1h)-\d+$")

# Simplified inline coin marks (no network fetch; 16px, brand colours).
_LOGOS: dict[str, str] = {
    "btc": (
        "<circle cx='12' cy='12' r='12' fill='#F7931A'/>"
        "<text x='12' y='17' text-anchor='middle' font-size='15' font-weight='700' "
        "font-family='Arial,sans-serif' fill='#fff' transform='rotate(14 12 12)'>₿</text>"
    ),
    "eth": (
        "<circle cx='12' cy='12' r='12' fill='#627EEA'/>"
        "<path d='M12 3.5v6.3l5.3 2.4z' fill='#fff' fill-opacity='.6'/>"
        "<path d='M12 3.5 6.7 12.2 12 9.8z' fill='#fff'/>"
        "<path d='M12 16.3v4.2l5.3-7.4z' fill='#fff' fill-opacity='.6'/>"
        "<path d='M12 20.5v-4.2l-5.3-3.2z' fill='#fff'/>"
        "<path d='m12 15.3 5.3-3.1L12 9.8z' fill='#fff' fill-opacity='.2'/>"
        "<path d='m6.7 12.2 5.3 3.1V9.8z' fill='#fff' fill-opacity='.6'/>"
    ),
    "sol": (
        "<circle cx='12' cy='12' r='12' fill='#000'/>"
        "<defs><linearGradient id='solg' x1='0' y1='1' x2='1' y2='0'>"
        "<stop offset='0' stop-color='#9945FF'/><stop offset='1' stop-color='#14F195'/>"
        "</linearGradient></defs>"
        "<path d='M8 7.2h9.6l-1.6 1.7H6.4zM6.4 11.2H16l1.6 1.7H8zM8 15.2h9.6l-1.6 1.7H6.4z' "
        "fill='url(#solg)'/>"
    ),
    "xrp": (
        "<circle cx='12' cy='12' r='12' fill='#23292F'/>"
        "<path d='M6.5 6.5h2l2.4 2.3a1.6 1.6 0 0 0 2.2 0l2.4-2.3h2l-3.4 3.3a3 3 0 0 1-4.2 0z"
        "M6.5 17.5h2l2.4-2.3a1.6 1.6 0 0 1 2.2 0l2.4 2.3h2l-3.4-3.3a3 3 0 0 0-4.2 0z' fill='#fff'/>"
    ),
    "doge": (
        "<circle cx='12' cy='12' r='12' fill='#C2A633'/>"
        "<text x='12' y='17' text-anchor='middle' font-size='14' font-weight='700' "
        "font-family='Arial,sans-serif' fill='#fff'>Ð</text>"
    ),
    "bnb": (
        "<circle cx='12' cy='12' r='12' fill='#F3BA2F'/>"
        "<path d='m12 5.2 2 2-4 4-2-2zM16.8 10l2 2-2 2-2-2zM7.2 10l2 2-2 2-2-2zM14 12.8l2 2L12 18.8l-4-4 2-2 2 2z"
        "M12 10l2 2-2 2-2-2z' fill='#fff'/>"
    ),
}


def _logo(asset: str) -> str:
    body = _LOGOS.get(asset)
    if not body:
        return ""
    return (
        "<svg class='mkt-logo' viewBox='0 0 24 24' width='14' height='14' aria-hidden='true'>"
        f"{body}</svg>"
    )


def open_market_pnl(
    *,
    open_pos: list[dict[str, Any]],
    daily_open: list[dict[str, Any]],
    tick: dict[str, Any] | None,
) -> dict[tuple[str, str], float | None]:
    """(asset, timeframe) → net unrealized P&L of its open positions.

    ``None`` means positions are open but none has a live mark.
    """
    out: dict[tuple[str, str], float | None] = {}

    def _add(key: tuple[str, str], pnl: float | None) -> None:
        prev = out.get(key)
        if pnl is None:
            out.setdefault(key, None)
        else:
            out[key] = (prev or 0.0) + pnl

    cur_window = (tick or {}).get("window_slug")
    for p in open_pos:
        m = _UPDOWN_SLUG.match(str(p.get("window_slug") or ""))
        if not m:
            continue
        mark = (
            s.side_mid(tick, p["side"])
            if tick and p.get("window_slug") == cur_window
            else None
        )
        pnl = (
            (mark - (p["entry_price"] or 0.0)) * (p["shares"] or 0.0)
            if mark is not None
            else None
        )
        _add((m.group(1), m.group(2)), pnl)
    for p in daily_open:
        asset = str(p.get("asset") or "").lower()
        if asset:
            _add((asset, "1d"), None)
    return out


def _glow(pnl: float | None) -> str:
    if pnl is None:
        return " glow-flat"
    if pnl > 0:
        return " glow-pos"
    if pnl < 0:
        return " glow-neg"
    return " glow-flat"


def render(
    *,
    selection: ms.MarketSelection,
    open_pnl: dict[tuple[str, str], float | None],
) -> str:
    def _net(keys: list[tuple[str, str]]) -> str:
        held = [k for k in keys if k in open_pnl]
        if not held:
            return ""
        vals = [open_pnl[k] for k in held if open_pnl[k] is not None]
        return _glow(sum(vals) if vals else None)

    def _btn(kind: str, value: str, label: str, active: bool, glow: str, tip: str) -> str:
        logo = _logo(value) if kind == "asset" else ""
        return (
            f"<button class='mkt-btn{' active' if active else ''}{glow}' "
            f"data-{kind}='{escape(value)}' title='{escape(tip)}' "
            f"onclick=\"setMarket('{kind}','{escape(value)}')\">{logo}{escape(label)}</button>"
        )

    assets = "".join(
        _btn(
            "asset", a, label, a == selection.asset,
            _net([(a, tf) for tf in ms.TIMEFRAMES]),
            label,
        )
        for a, label in ms.ASSETS.items()
    )
    timeframes = "".join(
        _btn(
            "timeframe", tf, label, tf == selection.timeframe,
            _net([(selection.asset, tf)]),
            f"{ms.ASSETS[selection.asset]} {label}",
        )
        for tf, label in ms.TIMEFRAMES.items()
    )
    unwired = "" if selection.loop_supported else " unwired"
    tip = "" if selection.loop_supported else " title='Loop not wired for this market yet'"
    return (
        f"<div class='mkt-sel{unwired}'{tip}>"
        f"<div class='mkt-row'>{assets}</div>"
        f"<div class='mkt-row'>{timeframes}</div>"
        "</div>"
    )
