"""MAKER card: every quote we rested, including the ones that never filled.

A card that showed only fills would be a highlight reel. For a passive strategy
the fill rate IS the result — an edge you cannot get filled on is not an edge —
so unfilled and expired quotes are listed beside the filled ones, each carrying
the queue it joined and the volume it watched trade past.

The band table repeats the research cut (profitable between 0.55 and 0.92,
inverted below the midpoint) against our OWN fills, so the live record can
contradict the study instead of inheriting its answer.

Pure ``render(...) -> str``, no DB and no awaits, matching the panel convention.
"""

from __future__ import annotations

import time
from html import escape

MARKET_URL = "https://polymarket.com/event/"


def _ago(ts: float, now: float) -> str:
    if not ts:
        return "—"
    d = max(0.0, now - ts)
    if d < 90:
        return f"{d:.0f}s"
    if d < 5400:
        return f"{d / 60:.0f}m"
    return f"{d / 3600:.1f}h"


_STATE_CLASS = {
    "resting": "mk-resting", "filled": "mk-filled",
    "settled": "mk-settled", "expired": "mk-expired",
}


def _quote_row(q: dict, now: float) -> str:
    state = str(q.get("state") or "resting")
    slug = str(q.get("window_slug") or "")
    label = escape(slug[:44] or "—")
    link = (f"<a class='mk-link' href='{MARKET_URL}{escape(slug)}' target='_blank' "
            f"rel='noopener'>{label}</a>") if slug else label

    price = float(q.get("quote_price") or 0.0)
    queue = float(q.get("depth_ahead") or 0.0)
    crossed = float(q.get("crossed") or 0.0)

    if state == "settled":
        pnl = float(q.get("pnl") or 0.0)
        size = float(q.get("filled_size") or 0.0)
        cps = (100.0 * pnl / size) if size else 0.0
        right = (f"<span class='{'win' if pnl >= 0 else 'loss'}'>"
                 f"{pnl:+.2f} ({cps:+.1f}c/sh)</span>")
    elif state == "filled":
        right = (f"<span class='mk-pending'>filled {float(q.get('filled_size') or 0):.0f}sh"
                 f" &middot; awaiting resolution</span>")
    elif state == "expired":
        # The distinction that matters: nothing came, versus it came and we were
        # behind it. Collapsing them would hide which one to fix.
        right = ("<span class='mk-expired-tag'>expired &middot; "
                 + (f"{crossed:.0f}sh traded past us" if crossed > 0
                    else "no flow reached our price") + "</span>")
    else:
        right = (f"<span class='mk-resting-tag'>resting &middot; "
                 f"{crossed:.0f}sh seen</span>")

    return (
        f"<div class='mk-row {_STATE_CLASS.get(state, '')}'>"
        f"<span class='mk-mkt'>{link}</span>"
        f"<span class='mk-side'>{escape(str(q.get('outcome') or ''))}</span>"
        f"<span class='mk-px'>{price:.2f}</span>"
        f"<span class='mk-q'>q{queue:.0f}</span>"
        f"<span class='mk-age'>{_ago(float(q.get('quoted_ts') or 0), now)}</span>"
        f"<span class='mk-res'>{right}</span>"
        "</div>"
    )


def _summary_block(s: dict) -> str:
    placed = int(s.get("placed") or 0)
    if not placed:
        return "<div class='gr-toggle-hint'>no quotes rested yet.</div>"
    shares = float(s.get("settled_shares") or 0.0)
    n = int(s.get("settled_n") or 0)
    pnl = float(s.get("pnl") or 0.0)
    wins = int(s.get("wins") or 0)
    cps = float(s.get("cents_per_share") or 0.0)
    # Each figure is labelled with the rows it covers. Two numbers on one line
    # over different populations is the mistake this project keeps making.
    return (
        "<div class='mk-summary'>"
        f"<span><b>{placed}</b> quoted</span>"
        f"<span><b>{int(s.get('filled') or 0)}</b> filled "
        f"({100 * float(s.get('fill_rate') or 0):.0f}%)</span>"
        f"<span><b>{int(s.get('resting') or 0)}</b> resting</span>"
        f"<span><b>{int(s.get('expired') or 0)}</b> expired</span>"
        f"<span class='{'win' if pnl >= 0 else 'loss'}'><b>{pnl:+.2f}</b> "
        f"over {n} settled</span>"
        f"<span class='{'win' if cps >= 0 else 'loss'}'><b>{cps:+.2f}c</b>/share "
        f"on {shares:,.0f}sh</span>"
        f"<span>{wins}/{n} won</span>"
        "</div>"
    )


def _band_block(bands: list[dict]) -> str:
    if not bands:
        return ""
    rows = []
    for b in bands:
        shares = float(b.get("shares") or 0.0)
        pnl = float(b.get("pnl") or 0.0)
        cps = (100.0 * pnl / shares) if shares else 0.0
        rows.append(
            f"<div class='mk-band-row'><span>{escape(str(b.get('band')))}</span>"
            f"<span>{int(b.get('n') or 0)} fills</span>"
            f"<span>{shares:,.0f}sh</span>"
            f"<span>{int(b.get('wins') or 0)} won</span>"
            f"<span class='{'win' if cps >= 0 else 'loss'}'>{cps:+.2f}c/sh</span>"
            "</div>")
    return ("<div class='mk-bands'><div class='mk-band-h'>by fill price "
            "&mdash; the study said this inverts under 0.55</div>"
            + "".join(rows) + "</div>")


def _queue_block(q: dict) -> str:
    un = int(q.get("unfilled_n") or 0)
    if not un and not int(q.get("filled_n") or 0):
        return ""
    touched = int(q.get("unfilled_touched") or 0)
    return (
        "<div class='mk-queue'>"
        f"<span>{touched}/{un} unfilled quotes had flow trade past them</span>"
        f"<span>fills waited through {float(q.get('filled_avg_queue') or 0):.0f}sh "
        f"of queue on average</span>"
        "</div>")


def _skips_block(decisions: list[dict]) -> str:
    if not decisions:
        return ""
    rows = [f"<div class='mk-skip'><span>{int(d.get('n') or 0)}&times;</span>"
            f"<span>{escape(str(d.get('decision')))}</span>"
            f"<span>{escape(str(d.get('reason') or ''))}</span></div>"
            for d in decisions[:8]]
    return ("<div class='mk-skips'><div class='mk-band-h'>decisions, last 24h"
            "</div>" + "".join(rows) + "</div>")


def render(
    *, summary: dict | None = None, quotes: list[dict] | None = None,
    bands: list[dict] | None = None, queue: dict | None = None,
    decisions: list[dict] | None = None, now: float | None = None,
) -> str:
    now = time.time() if now is None else now
    summary = summary or {}
    quotes = quotes or []

    placed = int(summary.get("placed") or 0)
    head = (f"<span class='win'>{placed} quoted &middot; maker only, no fee</span>"
            if placed else "<span class='win'>idle</span>")

    body = (
        _summary_block(summary)
        + _queue_block(queue or {})
        + _band_block(bands or [])
        + ("<div class='mk-rows'>"
           + "".join(_quote_row(q, now) for q in quotes[:25])
           + "</div>" if quotes else "")
        + _skips_block(decisions or [])
    )
    return (
        "<section class='card maker-card'>"
        f"<div class='card-h'>MAKER{head}</div>"
        + body
        + "</section>"
    )
