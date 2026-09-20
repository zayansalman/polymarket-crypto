"""Decision engine panel: inputs → computation → final banner + tail.

A transparency view of what the bot digests on each tick and why it does
(or doesn't) trade. Two columns — DIGESTING (inputs) and COMPUTING (fair
value and executable edge, as market observations) — plus a decision banner
and the last N tick decisions so the operator can watch decisions evolve.
The v0 GATES column was archived with the v0 strategy (2026-09-13).
"""
from __future__ import annotations

from html import escape
from typing import Any

from . import _shared as s


def render(
    tick: dict[str, Any] | None,
    recent: list[dict[str, Any]],
    execution_strategy: str = "model",
) -> str:
    """``execution_strategy`` "market" means the model never auto-enters: the
    banner says so instead of ENTER UP/DOWN, and the model's signal is shown
    for reference only."""
    if not tick:
        return (
            "<section class='card wide'><div class='card-h'>DECISION ENGINE"
            "<span class='win'>thinking…</span></div>"
            "<div class='chart-empty'>no ticks yet — start the bot to see decisions stream in</div>"
            "</section>"
        )

    # ── inputs being digested ──────────────────────────────────────────
    spot = tick.get("spot_price") or 0.0
    ref = tick.get("reference_price") or 0.0
    sigma = tick.get("sigma_per_second") or 0.0
    rem = tick.get("remaining_seconds") or 0
    up_bid = tick.get("up_best_bid")
    up_ask = tick.get("up_best_ask")
    down_bid = tick.get("down_best_bid")
    down_ask = tick.get("down_best_ask")
    feed = s.parse_feed_source(tick.get("feed_source"))

    inputs_html = (
        "<div class='de-col'>"
        "<div class='de-h'>DIGESTING</div>"
        "<div class='de-kv'>"
        f"<div><span>BTC spot</span><b class='mono'>${spot:,.2f}</b></div>"
        f"<div><span>Window ref</span><b class='mono'>${ref:,.2f}</b></div>"
        f"<div><span>Basis</span><b class='mono {s.cls(spot - ref)}'>{(spot - ref):+.2f}</b></div>"
        f"<div><span>Remaining</span><b class='mono'>{rem}s</b></div>"
        f"<div><span>σ / sec</span><b class='mono'>{sigma:.6f}</b></div>"
        f"<div><span>UP book (bid/ask)</span><b class='mono'>{s.bk(up_bid)} / {s.bk(up_ask)}</b></div>"
        f"<div><span>DOWN book (bid/ask)</span><b class='mono'>{s.bk(down_bid)} / {s.bk(down_ask)}</b></div>"
        f"<div><span>Spot feed</span><b class='mono dim'>{escape(feed.get('spot', '—'))}</b></div>"
        f"<div><span>Vol feed</span><b class='mono dim'>{escape(feed.get('vol', '—'))}</b></div>"
        "</div></div>"
    )

    # ── computation step (model output) ────────────────────────────────
    fair_up = tick.get("fair_up_prob")
    edge_up = (fair_up - up_ask) if (fair_up is not None and up_ask is not None) else None
    edge_dn = ((1 - fair_up) - down_ask) if (fair_up is not None and down_ask is not None) else None
    def _edge_html(v: float | None) -> str:
        if v is None:
            return "<b class='mono dim'>—</b>"
        return f"<b class='mono {s.cls(v)}'>{v:+.4f}</b>"

    fair_up_s = f"{fair_up * 100:.1f}%" if fair_up is not None else "—"
    fair_dn_s = f"{(1 - fair_up) * 100:.1f}%" if fair_up is not None else "—"

    compute_html = (
        "<div class='de-col'>"
        "<div class='de-h'>COMPUTING</div>"
        "<div class='de-kv'>"
        f"<div><span>fair Up</span><b class='mono'>{fair_up_s}</b></div>"
        f"<div><span>fair Down</span><b class='mono'>{fair_dn_s}</b></div>"
        f"<div><span>edge Up = fair − ask</span>{_edge_html(edge_up)}</div>"
        f"<div><span>edge Down = (1−fair) − ask</span>{_edge_html(edge_dn)}</div>"
        "</div></div>"
    )

    # ── final decision banner ──────────────────────────────────────────
    reason = (tick.get("reason") or "idle").strip()
    side = tick.get("signal_side")
    notional = tick.get("notional_usd") or 0.0
    if execution_strategy == "market":
        # The operator buys from the EXECUTION card; a model signal here is
        # never traded, so it must not read as an entry.
        d_cls, d_lbl = "dim", "MARKET — auto entries off"
        signal = f"model: {side} · {reason}" if side in ("Up", "Down") else reason
        d_body = f"model signal shown for reference · {signal}"
    elif side in ("Up", "Down"):
        d_cls, d_lbl = "up", f"ENTER {side.upper()}"
        d_body = f"size ${notional:.0f} · {reason}"
    elif reason.startswith("enter"):
        d_cls, d_lbl, d_body = "up", "ENTER (queued)", reason
    else:
        d_cls, d_lbl, d_body = "dim", "SKIP", reason

    decision_banner = (
        "<div class='de-decision'>"
        f"<span class='de-arrow'>▸</span><b class='{d_cls}'>{escape(d_lbl)}</b>"
        f"<span class='de-reason'>{escape(d_body)}</span>"
        "</div>"
    )

    # ── recent decision tail ───────────────────────────────────────────
    tail_rows = ""
    for r in recent:
        ts_ago = s.ago(r.get("created_at"))
        sp = r.get("spot_price") or 0.0
        fu = r.get("fair_up_prob")
        rs = (r.get("reason") or "").strip()
        is_entry = rs.startswith("enter")
        rcls = "up" if is_entry else "dim"
        cand = r.get("signal_side") or "—"
        ask_html = (
            f"ask {(r.get('up_best_ask') if cand == 'Up' else r.get('down_best_ask') or 0):.3f}"
            if cand in ("Up", "Down")
            else "—"
        )
        fair_t = f"{fu * 100:.1f}%" if fu is not None else "—"
        tail_rows += (
            "<tr>"
            f"<td class='mono dim'>{escape(ts_ago)}</td>"
            f"<td class='mono'>${sp:,.0f}</td>"
            f"<td class='mono'>{fair_t}</td>"
            f"<td class='mono'>{escape(cand)} {ask_html}</td>"
            f"<td class='mono {rcls}' style='white-space:normal'>{escape(rs)}</td>"
            "</tr>"
        )
    tail_html = (
        "<div class='de-tail'>"
        "<div class='de-h'>RECENT TICKS — what the bot saw &amp; decided</div>"
        "<table class='de-tail-tbl'><thead><tr>"
        "<th>age</th><th>spot</th><th>fair Up</th><th>side @ ask</th><th>decision</th>"
        "</tr></thead><tbody>"
        + (
            tail_rows
            or "<tr><td colspan='5' class='dim' style='text-align:center;padding:10px'>no recent ticks</td></tr>"
        )
        + "</tbody></table></div>"
    )

    return (
        "<section class='card wide'><div class='card-h'>DECISION ENGINE"
        "<span class='win'>no strategy loaded</span>"
        "</div>"
        "<div class='de-grid'>"
        + inputs_html
        + compute_html
        + "</div>"
        + decision_banner
        + tail_html
        + "</section>"
    )
