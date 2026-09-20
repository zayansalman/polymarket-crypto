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


def _audit_html(a: dict) -> str:
    """Says whether the record is complete, instead of implying it.

    A log can be wrong in ways that look right: a decision with no reason, a
    copy logged but never booked, a position tracing back to nothing.
    """
    if not a or not a.get("n"):
        return ""
    if a.get("clean"):
        return (
            "<div class='gr-toggle-hint' style='margin-top:8px'>"
            f"record complete &middot; {a['n']} decisions in 24h, all with a "
            f"reason &middot; {a.get('skips_scored') or 0} refusals scored "
            "&middot; every copy booked and traceable</div>"
        )
    bad = []
    if a.get("no_reason"):
        bad.append(f"{a['no_reason']} decisions with no reason")
    if a.get("copied_unbooked"):
        bad.append(f"{a['copied_unbooked']} copies logged but never booked")
    if a.get("untraced_positions"):
        bad.append(f"{a['untraced_positions']} positions tracing to nothing")
    return f"<div class='copy-error'>RECORD INCOMPLETE — {'; '.join(bad)}</div>"


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
    """Optimal paper fill vs the same order re-priced when it would land.

    Two different things go wrong and they must not be averaged: the price can
    move against us, and the order can fail to fill at all. A dead order costs
    $0, which flatters the total unless it is broken out.
    """
    t = (summary or {}).get("execution") or {}
    n = t.get("n") or 0
    if not n:
        return ""
    fd = t.get("filled_decision") or 0.0
    fr = t.get("filled_real") or 0.0
    unfilled = t.get("unfilled") or 0
    lost = t.get("lost_to_unfilled") or 0.0
    slip = t.get("real_slip")

    rows = ""
    if fd:
        move = fr - fd
        cls = "copy-bad" if move > 0 else "copy-good"
        sign = "+" if move >= 0 else "-"
        rows += (
            "<div class='copy-result'><span>filled: price at decision</span>"
            f"<span>${fd:,.2f}</span></div>"
            "<div class='copy-result'><span>filled: re-priced ~25s later</span>"
            f"<span>${fr:,.2f}</span></div>"
            "<div class='copy-result'><span>cost of arriving late</span>"
            f"<span class='{cls}'>{sign}${abs(move):,.2f}"
            + (f" &middot; {100 * slip:+.2f}c/share" if slip is not None else "")
            + "</span></div>"
        )
    partial = t.get("partial") or 0
    if partial:
        rows += (
            "<div class='copy-result'>"
            "<span>partial fills — book too thin for the whole order</span>"
            f"<span class='copy-warn'>{partial} of {n}</span></div>"
        )
    diverged = t.get("diverged") or 0
    if diverged:
        rows += (
            "<div class='copy-result'>"
            "<span>target exited while we still held — copy no longer "
            "tracking them</span>"
            f"<span class='copy-warn'>{diverged}</span></div>"
        )
    upsized = t.get("upsized") or 0
    if upsized:
        rows += (
            "<div class='copy-result'>"
            "<span>copies larger than the target's own clip "
            "(their bet was under the 5-share venue floor)</span>"
            f"<span class='copy-muted'>{upsized} of {n}</span></div>"
        )
    if unfilled:
        rows += (
            "<div class='copy-result'>"
            "<span>orders whose book was gone — would NOT have filled</span>"
            f"<span class='copy-bad'>{unfilled} of {n}"
            + (f" &middot; ${lost:,.2f} of paper stake" if lost else "")
            + "</span></div>"
        )
    return (
        "<div class='copy-results'>"
        "<div class='copy-results-h'>EXECUTION REALISM — paper assumes the "
        "displayed ladder survives; this re-walks the same order when it would "
        "actually land</div>"
        f"{rows}</div>"
    )


def _drift_html(drift: dict) -> str:
    """How much of each target's recent flow is still on measured markets.

    A wallet that has moved to markets it was never screened on cannot be
    copied on that screening. This is the first thing to read, ahead of P&L.
    """
    if not drift:
        return ""
    # Only the drifted ones are worth listing. Rendering all forty made the card
    # tall enough that its height changed on every refresh, which moved the rest
    # of the page under the reader.
    rows = ""
    off = 0
    shown = 0
    for addr, (label, followed, total) in sorted(
        drift.items(), key=lambda kv: (kv[1][1] / kv[1][2] if kv[1][2] else 0)
    ):
        pct = (100 * followed / total) if total else 0
        if pct < 25:
            off += 1
        elif shown >= 4:
            continue
        shown += 1
        cls = "copy-good" if pct >= 50 else ("copy-warn" if pct >= 25 else "copy-bad")
        rows += (
            "<div class='copy-result'>"
            f"<a class='copy-link' href='{PROFILE_URL}{escape(addr)}' target='_blank' "
            f"rel='noopener'>{escape(label)}</a>"
            f"<span>{followed}/{total} fills on measured markets</span>"
            f"<span class='{cls}'>{pct:.0f}%</span>"
            "</div>"
        )
    ok_n = len(drift) - off
    warn = (
        f"<div class='copy-error'>{off} of {len(drift)} targets have left the "
        "markets they were measured on — their screened edge does not apply to "
        "what they are trading now.</div>"
        if off else ""
    )
    summary = (
        "<div class='gr-toggle-hint'>"
        f"{ok_n} of {len(drift)} targets still on their measured markets"
        "</div>"
    )
    return (
        "<div class='copy-results'>"
        "<div class='copy-results-h'>TARGET DRIFT</div>"
        f"{warn}{summary}{rows}</div>"
    )


def _verdict_html(rows: list) -> str:
    """Per target: what we got, against what taking every fill would have got.

    Settled copies accumulate slowly because most fills are declined, but every
    declined fill is scored, so this answers the faster question — is the WALLET
    worth following, separately from whether our rules let us take the trade.

    When "all fills" is worse than "taken", the filters are carrying the result
    and the edge is ours, not the wallet's.
    """
    if not rows:
        return ""
    out = ""
    for r in rows[:12]:
        scored = r.get("scored_n") or 0
        if scored < 2:
            continue
        allp = r.get("all_pnl") or 0.0
        taken = r.get("taken_pnl") or 0.0
        taken_n = r.get("taken_n") or 0
        acls = "copy-good" if allp > 0 else "copy-bad"
        out += (
            "<div class='copy-result'>"
            f"<span>{escape(str(r.get('target_label') or '-'))}</span>"
            f"<span>{taken_n} taken ${taken:+,.2f}</span>"
            f"<span>{scored} fills seen</span>"
            f"<span class='{acls}'>all fills ${allp:+,.2f}</span></div>"
        )
    if not out:
        return ""
    return (
        "<div class='copy-results'>"
        "<div class='copy-results-h'>TARGET VERDICT — what we got vs taking "
        "every fill they made</div>" + out + "</div>"
    )


def _skips_html(board: list) -> str:
    """Was declining right? Each skip reason, scored on what it refused.

    The slippage cap is guesswork until the trades it refuses are priced. A
    reason whose refused trades would have LOST money is earning its keep; one
    whose refused trades would have won is costing us.
    """
    scored = [r for r in (board or []) if (r.get("n") or 0) > 0]
    if not scored:
        return ""
    rows = ""
    for r in scored[:8]:
        n = r.get("n") or 0
        pnl = r.get("pnl") or 0.0
        wins = r.get("wins") or 0
        # Refusing losers is the cap working; refusing winners is it costing us.
        good = pnl <= 0
        cls = "copy-good" if good else "copy-bad"
        verdict = "saved us" if good else "cost us"
        rows += (
            "<div class='copy-result'>"
            f"<span>{escape(str(r.get('reason') or '')[:58])}</span>"
            f"<span>{n} refused, {wins} would have won</span>"
            f"<span class='{cls}'>{verdict} ${abs(pnl):,.2f}</span></div>"
        )
    return (
        "<div class='copy-results'>"
        "<div class='copy-results-h'>SKIPS, SCORED — what declining actually "
        "did</div>" + rows + "</div>"
    )


def _reach_html(reach: list) -> str:
    """Per target: how often we could actually get in.

    A wallet can have a real edge and still be unfollowable, because it enters
    where a follower arriving seconds later finds no book. Offline research
    cannot see this; only live polling can.
    """
    if not reach:
        return ""
    rows = ""
    for r in reach[:12]:
        seen = r.get("seen") or 0
        copied = r.get("copied") or 0
        if not seen:
            continue
        pct = 100 * copied / seen
        cls = "copy-good" if pct >= 50 else ("copy-warn" if pct >= 20 else "copy-bad")
        rows += (
            "<div class='copy-result'>"
            f"<span>{escape(str(r.get('target_label') or '-'))}</span>"
            f"<span>{copied} of {seen} fills followed</span>"
            f"<span class='{cls}'>{pct:.0f}%</span></div>"
        )
    if not rows:
        return ""
    return (
        "<div class='copy-results'>"
        "<div class='copy-results-h'>REACHABILITY — fills we could actually "
        "act on</div>" + rows + "</div>"
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
        cls = "copy-good" if pnl > 0 else "copy-bad"
        # The realistic figure decides whether a target is worth keeping; the
        # paper one is only there to show how far off it is.
        rpnl = r.get("real_pnl")
        nf = r.get("never_filled") or 0
        if rpnl is None:
            real = "<span class='copy-muted'>real n/a</span>"
        else:
            rcls = "copy-good" if rpnl > 0 else "copy-bad"
            real = f"<span class='{rcls}'><b>${rpnl:+,.2f}</b></span>"
        rows += (
            "<div class='copy-result'>"
            f"<span>{escape(str(r.get('target_label') or '-'))}</span>"
            f"<span>{rn} settled</span>"
            f"<span>{(100 * wins / rn) if rn else 0:.0f}% won</span>"
            f"<span>${staked:,.2f} staked</span>"
            + (f"<span class='copy-muted'>{nf} unfilled</span>" if nf else "")
            + f"<span class='copy-muted'>paper ${pnl:+,.2f}</span>"
            f"{real}"
            "</div>"
        )
    # Key the empty state off the settled COUNT, not off whether the per-target
    # breakdown happens to be populated — otherwise a real total (and the
    # realistic P&L beside it) disappears behind "nothing settled yet".
    if not n:
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
    # The same positions at the price a real order would have got. A copy whose
    # book was gone never filled, so it is flat — counting it as a win is the
    # single biggest way a paper ledger flatters itself.
    rp = total.get("real_pnl")
    nf = total.get("never_filled") or 0
    # Their own return per dollar vs ours, over exactly the same rows. The copy
    # P&L alone cannot tell a bad fill from a bad call; this can — but only if
    # both sides cover the same trades.
    their = total.get("their_pnl")
    their_staked = total.get("their_staked") or 0.0
    matched_n = total.get("matched_n") or 0
    matched_staked = total.get("matched_staked") or 0.0
    matched_pnl = total.get("matched_pnl") or 0.0
    fidelity = ""
    if their is not None and their_staked > 0 and matched_staked > 0:
        theirs_pct = 100 * their / their_staked
        ours_pct = 100 * matched_pnl / matched_staked
        gap = ours_pct - theirs_pct
        gcls = "copy-good" if gap >= 0 else "copy-bad"
        fidelity = (
            "<div class='copy-result'>"
            f"<span>same {matched_n} fills — their return (gross of their fee)"
            "</span>"
            f"<span>{theirs_pct:+.1f}%</span>"
            f"<span>ours {ours_pct:+.1f}%</span>"
            f"<span class='{gcls}'>gap {gap:+.1f}pp</span></div>"
        )
    real_row = ""
    real_n = total.get("real_n") or 0
    if rp is not None and real_n:
        rcls = "copy-good" if rp > 0 else "copy-bad"
        # Compare against the paper figure for the SAME rows, not the paper
        # total over every settled trade — most of which have no realistic
        # price yet, so the totals cover different sets.
        paper_same = total.get("paper_on_real") or 0.0
        real_row = (
            "<div class='copy-result copy-result-total'>"
            f"<span><b>of those, {real_n} re-priced at landing</b></span>"
            f"<span>paper ${paper_same:+,.2f}</span>"
            f"<span>{nf} never filled</span>"
            f"<span class='{rcls}'><b>real ${rp:+,.2f}</b></span>"
            "</div>"
        )
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
        "</div>"
        f"{real_row}{fidelity}</div>"
    )


MARKET_URL = "https://polymarket.com/event/"


def _trade_row(r: dict, now: float) -> str:
    """One copy, with both prices and what it actually paid.

    Their price and ours sit side by side because a losing copy has two
    different causes — the target was wrong, or following them cost more than
    their edge — and only showing both tells those apart.
    """
    slug = str(r.get("window_slug") or "")
    label = escape(slug[:38] or "—")
    mkt = (f"<a class='copy-link' href='{MARKET_URL}{escape(slug)}' "
           f"target='_blank' rel='noopener'>{label}</a>") if slug else label

    ours = float(r.get("our_price") or 0.0)
    theirs = float(r.get("their_price") or 0.0)
    size = float(r.get("our_size") or r.get("size") or 0.0)
    slip = 100 * (ours - theirs)

    state = str(r.get("state") or "open")
    if state == "settled":
        pnl = float(r.get("pnl") or 0.0)
        # The realistic price is the one that decides anything; the paper price
        # is only here to show how far off it was.
        rpnl = r.get("real_pnl")
        won = "W" if r.get("won") else "L"
        cls = "copy-good" if pnl > 0 else "copy-bad"
        real = ""
        if rpnl is not None and abs(float(rpnl) - pnl) > 0.005:
            rcls = "copy-good" if float(rpnl) > 0 else "copy-bad"
            real = f" <span class='{rcls} copy-muted'>real {float(rpnl):+.2f}</span>"
        res = (f"<span class='{cls}'><b>{won} {pnl:+.2f}</b></span>{real}")
    else:
        res = "<span class='copy-muted'>open</span>"

    return (
        "<div class='copy-trade'>"
        f"<span class='copy-t-mkt'>{mkt}</span>"
        f"<span class='copy-t-side'>{escape(str(r.get('outcome') or ''))}</span>"
        f"<span class='copy-t-px'>{ours:.2f}</span>"
        f"<span class='copy-t-sz'>{size:.0f}sh</span>"
        f"<span class='copy-t-slip' title='their price {theirs:.2f}'>"
        f"{slip:+.1f}c</span>"
        f"<span class='copy-t-age'>{_ago(float(r.get('our_ts') or 0), now)}</span>"
        f"<span class='copy-t-res'>{res}</span>"
        "</div>"
    )


def _trades_html(trades: list, now: float) -> str:
    """Every copy, one row each — open ones first, then settled, newest first.

    An aggregate can hide a run of identical losers behind a flat total, which
    is exactly what a three-day-screened target needs watching for.
    """
    if not trades:
        return ""
    head = ("<div class='copy-t-h'>every copy &mdash; price shown is OURS, "
            "the cents column is what arriving late cost</div>")
    return ("<div class='copy-trades'>" + head
            + "".join(_trade_row(r, now) for r in trades[:40]) + "</div>")


def render(
    *, state, target: Target | None, summary: dict | None = None,
    decisions: list | None = None, reach: list | None = None,
    skips: list | None = None, verdict: list | None = None,
    audit: dict | None = None, trades: list | None = None,
    now: float | None = None,
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
        f"{_trades_html(trades or [], now)}"
        f"{_execution_html(summary or {})}"
        f"{_verdict_html(verdict or [])}"
        f"{_skips_html(skips or [])}"
        f"{_reach_html(reach or [])}"
        f"{_decisions_html(decisions or [])}"
        f"{_audit_html(audit or {})}"
        "<div class='gr-toggle-hint' style='margin:8px 0'>"
        "PAPER — copies are priced against the live ask ladder and charged the taker fee, but no real order is ever sent."
        "</div>"
        f"<div class='copy-fills' data-keep-scroll='copy-fills'>{rows}</div>"
        "</section>"
    )
