"""COPY TRADE card: the target wallet's fills, exactly as we saw them.

Shows what the watcher observed and how stale it was when we saw it — the whole
point of this card is that the copier has nowhere to hide. Observation lag is
displayed on every fill because it is the number that decides whether copying
is viable at all: at a 20s median against a 3-hour runway it is noise, and the
same number against a 2-minute runway would be the reason not to trade.

Fills outside the daily Up-or-Down families we mirror are still listed, dimmed
and marked. A target quietly drifting into markets it was never measured on is
the failure mode that disqualified two earlier candidates, so it must be seen
rather than filtered away.

Pure ``render(...) -> str``, no DB and no awaits, matching the panel convention.
"""

from __future__ import annotations

import time
from html import escape

from polymarket_bot.copytrade.targets import Target

PROFILE_URL = "https://polymarket.com/profile/"


def _ago(ts: float, now: float) -> str:
    if not ts:
        return "—"
    d = max(0.0, now - ts)
    if d < 90:
        return f"{d:.0f}s"
    if d < 5400:
        return f"{d / 60:.0f}m"
    return f"{d / 3600:.1f}h"


def _fill_row(f, now: float) -> str:
    cls = "copy-fill" if f.followed else "copy-fill copy-fill-other"
    mark = "" if f.followed else "<span class='copy-other-tag'>not followed</span>"
    # A backfilled fill's age is the backlog's, not the transport's — labelling
    # it as lag would misreport the number this card exists to show.
    if f.backfill:
        lag = "<span class='copy-lag copy-backfill'>backfill</span>"
    else:
        lag = f"<span class='copy-lag'>seen +{f.lag_seconds:.0f}s</span>"
    return (
        f"<div class='{cls}'>"
        f"<span class='copy-when'>{_ago(f.ts, now)} ago</span>"
        f"<span class='copy-side copy-{escape(f.side.lower())}'>"
        f"{escape(f.side)} {escape(f.outcome)}</span>"
        f"<span class='copy-size'>{f.size:,.0f} @ {f.price:.3f}</span>"
        f"<span class='copy-notional'>${f.notional:,.2f}</span>"
        f"<span class='copy-title'>{escape(f.title[:54])}</span>"
        f"{lag}"
        f"{mark}</div>"
    )


def _decisions_html(counts: list) -> str:
    """Every fill examined in the last day, and what happened to it.

    Skips are shown with their reason because that is where the cost hides: a
    copier that declines most of what it sees is not the strategy that was
    screened, and silence would not say so.
    """
    if not counts:
        return ""
    total = sum(c.get("n") or 0 for c in counts)
    copied = sum((c.get("n") or 0) for c in counts if c.get("decision") == "copied")
    rows = ""
    for c in counts[:8]:
        n = c.get("n") or 0
        d = str(c.get("decision") or "")
        cls = "copy-good" if d == "copied" else "copy-muted"
        rows += (
            "<div class='copy-result'>"
            f"<span class='{cls}'>{escape(d)}</span>"
            f"<span>{escape(str(c.get('reason') or '')[:74])}</span>"
            f"<span>{n}</span></div>"
        )
    return (
        "<div class='copy-results'>"
        f"<div class='copy-results-h'>DECISIONS (24h) — {copied} copied of "
        f"{total} fills examined</div>{rows}</div>"
    )


def _execution_html(summary: dict) -> str:
    """Optimal paper fill vs the same order re-priced when it would land."""
    t = (summary or {}).get("execution") or (summary or {}).get("total") or {}
    staked = t.get("staked") or 0.0
    real = t.get("real_staked") or 0.0
    slip = t.get("real_slip")
    unfilled = t.get("unfilled") or 0
    if not staked or not real:
        return ""
    diff = real - staked
    # A lower realistic cost is NOT good news when it is lower because orders
    # failed to fill. Only call it an improvement when everything filled.
    cls = "copy-good" if (diff < 0 and not unfilled) else "copy-bad"
    money = f"{'+' if diff >= 0 else '-'}${abs(diff):,.2f}"
    caveat = (
        " — lower only because some orders did not fill" if diff < 0 and unfilled
        else ""
    )
    return (
        "<div class='copy-results'>"
        "<div class='copy-results-h'>EXECUTION REALISM</div>"
        "<div class='copy-result'><span>priced at decision (optimistic)</span>"
        f"<span>${staked:,.2f}</span></div>"
        "<div class='copy-result'><span>re-priced ~25s later (realistic)</span>"
        f"<span>${real:,.2f}</span></div>"
        "<div class='copy-result'><span>cost of being late</span>"
        f"<span class='{cls}'>{money}"
        + (f" &middot; {100*slip:+.2f}c/share" if slip is not None else "")
        + escape(caveat)
        + "</span></div>"
        + (
            "<div class='copy-result'><span>orders that would NOT have filled"
            "</span>"
            f"<span class='copy-bad'>{unfilled} of {t.get('n') or 0}</span></div>"
            if unfilled else ""
        )
        + "</div>"
    )


def _drift_html(drift: dict) -> str:
    """How much of each target's recent flow is still on measured markets.

    A wallet that has moved to markets it was never screened on cannot be
    copied on that screening. This is the first thing to read, ahead of P&L.
    """
    if not drift:
        return ""
    rows = ""
    off = 0
    for addr, (label, followed, total) in sorted(
        drift.items(), key=lambda kv: -(kv[1][1] / kv[1][2] if kv[1][2] else 0)
    ):
        pct = (100 * followed / total) if total else 0
        if pct < 25:
            off += 1
        cls = "copy-good" if pct >= 50 else ("copy-warn" if pct >= 25 else "copy-bad")
        rows += (
            "<div class='copy-result'>"
            f"<a class='copy-link' href='{PROFILE_URL}{escape(addr)}' target='_blank' "
            f"rel='noopener'>{escape(label)}</a>"
            f"<span>{followed}/{total} fills on measured markets</span>"
            f"<span class='{cls}'>{pct:.0f}%</span>"
            "</div>"
        )
    warn = (
        f"<div class='copy-error'>{off} of {len(drift)} targets have left the "
        "markets they were measured on — their screened edge does not apply to "
        "what they are trading now.</div>"
        if off else ""
    )
    return (
        "<div class='copy-results'>"
        "<div class='copy-results-h'>TARGET DRIFT</div>"
        f"{warn}{rows}</div>"
    )


def _results_html(summary: dict) -> str:
    """Settled paper P&L per target — the number the morning read-out is for."""
    if not summary:
        return ""
    total = summary.get("total") or {}
    n = total.get("n") or 0
    rows = ""
    for r in sorted(summary.get("per_target") or [], key=lambda r: -(r.get("pnl") or 0)):
        rn = r.get("n") or 0
        wins = r.get("wins") or 0
        pnl = r.get("pnl") or 0.0
        staked = r.get("staked") or 0.0
        slip = r.get("slip") or 0.0
        cls = "copy-good" if pnl > 0 else "copy-bad"
        rows += (
            "<div class='copy-result'>"
            f"<span>{escape(str(r.get('target_label') or '-'))}</span>"
            f"<span>{rn} settled</span>"
            f"<span>{(100 * wins / rn) if rn else 0:.0f}% won</span>"
            f"<span>${staked:,.2f} staked</span>"
            f"<span>slip {100 * slip:+.2f}c</span>"
            f"<span class='{cls}'>${pnl:+,.2f}</span>"
            "</div>"
        )
    if not rows:
        openp = summary.get("open") or {}
        return (
            "<div class='copy-results'><div class='gr-toggle-hint'>"
            f"{openp.get('n') or 0} copies open, "
            f"${openp.get('staked') or 0:,.2f} staked — nothing settled yet."
            "</div></div>"
        )
    tp = total.get("pnl") or 0.0
    tw = total.get("wins") or 0
    openp = summary.get("open") or {}
    cls = "copy-good" if tp > 0 else "copy-bad"
    return (
        "<div class='copy-results'>"
        "<div class='copy-results-h'>PAPER RESULTS</div>"
        f"{rows}"
        "<div class='copy-result copy-result-total'>"
        f"<span><b>total</b></span><span>{n} settled</span>"
        f"<span>{(100 * tw / n) if n else 0:.0f}% won</span>"
        f"<span>${total.get('staked') or 0:,.2f} staked</span>"
        f"<span>{openp.get('n') or 0} open</span>"
        f"<span class='{cls}'><b>${tp:+,.2f}</b></span>"
        "</div></div>"
    )


def render(
    *, state, target: Target | None, summary: dict | None = None,
    decisions: list | None = None, now: float | None = None,
) -> str:
    now = time.time() if now is None else now
    if state is None:
        return (
            "<section class='card copytrade-card'>"
            "<div class='card-h'>COPY TRADE<span class='win'>not started</span></div>"
            "<div class='gr-toggle-hint'>watcher has not run yet.</div>"
            "</section>"
        )

    # The wallet name links to its Polymarket profile: the measured stats are
    # historical, and whether a target is still running the strategy we measured
    # is only answerable by looking at what it is doing right now.
    link = (
        f"<a class='copy-link' href='{PROFILE_URL}{escape(state.target)}' "
        f"target='_blank' rel='noopener'>{escape(state.label)}</a>"
    )
    if not state.enabled:
        status = f"<span class='win'>switched off &middot; {link}</span>"
    elif state.connected:
        extra = (
            f" +{len(state.watching) - 1} more"
            if len(state.watching) > 1 else ""
        )
        status = f"<span class='win'>watching {link}{extra}</span>"
    else:
        status = f"<span class='win copy-bad'>feed down &middot; {link}</span>"

    followed = state.followed_fills
    ev = ""
    if target is not None:
        ev = (
            "<div class='copy-evidence'>"
            f"measured edge <b>{target.edge_cents:+.2f}c/share</b> after fees "
            f"(t={target.t_stat:.2f}, {target.markets} markets) &middot; "
            f"{target.edge_left_30min:+.2f}c still left for a copier 30 min late "
            f"&middot; {escape(target.assets)}"
            "</div>"
        )

    err = (
        f"<div class='copy-error'>{escape(state.last_error)}</div>"
        if state.last_error else ""
    )
    rows = "".join(_fill_row(f, now) for f in state.fills[:25]) or (
        "<div class='gr-toggle-hint'>no fills seen yet.</div>"
    )
    return (
        "<section class='card copytrade-card'>"
        f"<div class='card-h'>COPY TRADE{status}</div>"
        f"{ev}"
        "<div class='copy-stats'>"
        f"<span>polls <b>{state.polls}</b></span>"
        f"<span>last <b>{_ago(state.last_poll, now)} ago</b></span>"
        f"<span>fills seen <b>{len(state.fills)}</b></span>"
        f"<span>since start <b>{state.live_fills}</b></span>"
        f"<span>on followed markets <b>{len(followed)}</b></span>"
        f"<span>median lag <b>{state.median_lag:.0f}s</b></span>"
        "</div>"
        f"{err}"
        f"{_drift_html(getattr(state, 'drift', {}) or {})}"
        f"{_results_html(summary or {})}"
        f"{_execution_html(summary or {})}"
        f"{_decisions_html(decisions or [])}"
        "<div class='gr-toggle-hint' style='margin:8px 0'>"
        "PAPER — copies are priced against the live ask ladder and charged the taker fee, but no real order is ever sent."
        "</div>"
        f"<div class='copy-fills' data-keep-scroll='copy-fills'>{rows}</div>"
        "</section>"
    )
