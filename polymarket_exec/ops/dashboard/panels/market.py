"""Live market panel: probability gauge, UP/DOWN book, basis.

Renders as a full-width (``card wide``) card on purpose. The execution grid has an
odd number of single-width cards; left half-width, this card orphaned the
neighbouring grid cell (an empty band under STRATEGY, right of LIVE MARKET).
Spanning both columns fixes the parity — do not revert to a bare ``card``.
"""
from __future__ import annotations

from html import escape
from typing import Any

from . import _shared as s


def _open_position_block(
    tick: dict[str, Any], open_pos: list[dict[str, Any]] | None
) -> str:
    """Live unrealized P&L for the open position(s), marked at the current side
    mid (#113). A position in a window other than the live tick's is shown
    without a fabricated mark — there's no live book for a past window."""
    if not open_pos:
        return ""
    cur = tick.get("window_slug")
    rows = ""
    agg = 0.0
    have_agg = False
    for p in open_pos:
        side = (p.get("side") or "").upper()
        entry = p.get("entry_price") or 0.0
        shares = p.get("shares") or 0.0
        mark = s.side_mid(tick, side) if p.get("window_slug") == cur else None
        if mark is not None:
            unreal = (mark - entry) * shares
            agg += unreal
            have_agg = True
            mark_html = f"{mark:.3f}"
            unreal_html = f"<b class='mono {s.cls(unreal)}'>{s.money(unreal, True)}</b>"
        else:
            mark_html = "—"
            unreal_html = (
                "<b class='mono dim' title='position is in a window other than the "
                "live one — no live mark'>—</b>"
            )
        rows += (
            "<div class='op-row'>"
            f"<span class='tag {side.lower()}'>{escape(side)}</span>"
            f"<span class='mono dim'>{shares:g}sh @ {entry:.3f}</span>"
            f"<span class='mono'>mark {mark_html}</span>"
            f"{unreal_html}"
            "</div>"
        )
    agg_html = (
        f"<div class='op-agg'><span>Unrealized</span>"
        f"<b class='mono {s.cls(agg)}'>{s.money(agg, True)}</b></div>"
        if have_agg
        else ""
    )
    return (
        "<div class='open-pos'><div class='op-h'>OPEN POSITION "
        "<span class='win'>live mark · mid</span></div>" + rows + agg_html + "</div>"
    )


def render(
    tick: dict[str, Any] | None, open_pos: list[dict[str, Any]] | None = None
) -> str:
    if not tick:
        return (
            "<section class='card wide'><div class='card-h'>LIVE MARKET</div>"
            "<div class='chart-empty'>no ticks yet</div></section>"
        )
    spot = tick.get("spot_price") or 0
    ref = tick.get("reference_price") or 0
    basis = spot - ref
    rem = tick.get("remaining_seconds") or 0
    edge = tick.get("edge")
    return (
        "<section class='card wide'><div class='card-h'>LIVE MARKET"
        f"<span class='win'>{escape((tick.get('window_slug') or '').replace('btc-updown-5m-', '#'))} · {rem}s</span></div>"
        f"{s.gauge(tick.get('fair_up_prob'))}"
        "<div class='book'>"
        "<div class='book-side up'><div class='bk-l'>UP</div>"
        f"<div class='bk-px'>{s.bk(tick.get('up_best_bid'))} / {s.bk(tick.get('up_best_ask'))}</div></div>"
        "<div class='book-side down'><div class='bk-l'>DOWN</div>"
        f"<div class='bk-px'>{s.bk(tick.get('down_best_bid'))} / {s.bk(tick.get('down_best_ask'))}</div></div>"
        "</div>"
        "<div class='kv tight'>"
        f"<div><span>BTC spot</span><b>${spot:,.2f}</b></div>"
        f"<div><span>Window ref</span><b>${ref:,.2f}</b></div>"
        f"<div><span>Basis</span><b class='{s.cls(basis)}'>{basis:+.2f}</b></div>"
        f"<div><span>Edge</span><b class='{s.cls(edge)}'>{('%+.3f' % edge) if edge is not None else '—'}</b></div>"
        "</div>"
        f"<div class='decision'>{escape((tick.get('reason') or 'idle'))}</div>"
        f"{_open_position_block(tick, open_pos)}"
        "</section>"
    )
