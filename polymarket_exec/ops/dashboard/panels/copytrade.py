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


def render(*, state, target: Target | None, now: float | None = None) -> str:
    now = time.time() if now is None else now
    if state is None:
        return (
            "<section class='card copytrade-card'>"
            "<div class='card-h'>COPY TRADE<span class='win'>not started</span></div>"
            "<div class='gr-toggle-hint'>watcher has not run yet.</div>"
            "</section>"
        )

    if not state.enabled:
        status = "<span class='win'>switched off</span>"
    elif state.connected:
        status = f"<span class='win'>watching {escape(state.label)}</span>"
    else:
        status = "<span class='win copy-bad'>feed down</span>"

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
        "<div class='gr-toggle-hint' style='margin:8px 0'>"
        "observation only — this places no orders and writes no positions."
        "</div>"
        f"<div class='copy-fills'>{rows}</div>"
        "</section>"
    )
