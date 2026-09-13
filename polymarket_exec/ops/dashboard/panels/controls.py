"""Operator runtime controls (#50, #89): live-editable risk knobs.

Exposes the trade size in SHARES (#89): the operator picks a share count and the
bot trades that many shares per clip. The persisted value is re-read every tick,
so changes apply without a restart, in paper AND live. Pure ``render(...)``
transform — no DB access here (values are loaded in ``ems.py`` and passed in).
"""
from __future__ import annotations

from polymarket_exec.execution.live import DEFAULT_MIN_ORDER_SIZE


def render(
    *,
    trade_shares_current: float | None,
    current_price: float | None,
) -> str:
    """Render the CONTROLS card.

    ``trade_shares_current`` is the operator-set share count (None when unset →
    the input defaults to the venue minimum). ``current_price`` is the favoured
    side's live ask, used for the $-value estimate (None when no live market).
    """
    minsh = float(DEFAULT_MIN_ORDER_SIZE)
    shares = trade_shares_current if trade_shares_current is not None else minsh
    source = "operator" if trade_shares_current is not None else "default"
    px = current_price if (current_price and current_price > 0) else None
    if px:
        value_str = f"≈ ${shares * px:,.2f} at {px:.2f}"
    else:
        value_str = f"≈ ${shares * 0.5:,.2f}–${shares * 1.0:,.2f}"
    lo, hi = shares * 0.50, shares * 1.00
    # Infographic: one pip per venue-minimum share, with a label.
    pips = "".join("<span class='shp'></span>" for _ in range(int(minsh)))
    return (
        "<section class='card'>"
        "<div class='card-h'>CONTROLS<span class='win'>runtime · no restart</span></div>"
        "<div class='de-kv'>"
        f"<div><span>Trade size</span>"
        f"<b class='mono'>{shares:g} shares <em class='up'>({source})</em></b></div>"
        f"<div><span>≈ value</span>"
        f"<b class='mono' id='ctl-shares-val' data-px='{(px or 0):.4f}'>{value_str}</b></div>"
        f"<div><span>$ range</span><b class='mono dim'>${lo:,.2f} – ${hi:,.2f}</b></div>"
        "</div>"
        "<div class='ctl-row'>"
        f"<input id='ctl-shares' class='ctl-input' type='number' step='1' "
        f"min='{minsh:.0f}' value='{shares:g}' oninput='updateShareValue()' "
        "aria-label='Trade size in shares' />"
        "<span class='ctl-unit'>shares</span>"
        "<button class='gr-btn btn-ok' onclick='setTradeShares()'>Apply</button>"
        "</div>"
        "<div class='share-min'>"
        f"<div class='share-pips' aria-hidden='true'>{pips}</div>"
        f"<span class='share-min-lbl'>Polymarket minimum order · {minsh:.0f} shares</span>"
        "</div>"
        "<div class='gr-toggle-hint'>"
        "you set the share count · every order ≥ 5 shares (venue minimum) · "
        "applies to paper + live"
        "</div>"
        "</section>"
    )
