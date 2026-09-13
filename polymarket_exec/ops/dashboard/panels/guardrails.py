"""Risk guardrails: daily spend, loss-halt, bot state, recent BLOCKED entries.

Loss-halt controls (#76): the STATUS pill is a button that toggles the bypass
in BOTH paper and live (one click, journaled server-side), and a Reset button
zeroes today's realized-loss tally. Reset is disabled while the bot is running
(the running loop owns the in-memory counters) — but the halt auto-stops the
bot, so after a halt the operator is already stopped and Reset is live. The
halt is decided on the running mode's own leg, so the Headroom shown is the
live leg in live mode and the paper leg in paper mode.
"""
from __future__ import annotations

from datetime import UTC, datetime
from html import escape
from typing import Any

from . import _shared as s


def render(
    *,
    day_spend: float,
    bankroll_cap: float | None,
    submitted_count: int,
    submitted_notional: float,
    day_pnl: float,
    live_pnl: float,
    paper_pnl: float,
    loss_halt_usd: float,
    live_peak: float = 0.0,
    paper_peak: float = 0.0,
    state: str,
    bot_detail: str,
    session_start: str | None,
    blocked: list[dict[str, Any]],
    mode: str = "paper",
    bypass_loss_halt: bool = False,
) -> str:
    # ── DAILY SPEND ─────────────────────────────────────────────────────
    if bankroll_cap is not None and bankroll_cap > 0:
        pct = day_spend / bankroll_cap
        cap_str = f"${bankroll_cap:,.2f}"
        cap_cls = "down" if pct >= 0.95 else ("up" if pct < 0.6 else "")
        headroom_line = (
            f"<div><span>Cap headroom</span>"
            f"<b class='mono {cap_cls}'>${max(0.0, bankroll_cap - day_spend):,.2f}</b></div>"
        )
    else:
        cap_str = "disabled"
        cap_cls = "dim"
        headroom_line = ""
    spend_col = (
        "<div class='de-col'>"
        "<div class='de-h'>DAILY SPEND (today, UTC)</div>"
        "<div class='de-kv'>"
        f"<div><span>Filled notional</span><b class='mono'>${day_spend:,.2f}</b></div>"
        f"<div><span>Cap</span><b class='mono {cap_cls}'>{escape(cap_str)}</b></div>"
        + headroom_line
        + f"<div><span>Submitted today</span><b class='mono'>{submitted_count} entries · ${submitted_notional:,.2f}</b></div>"
        "</div></div>"
    )

    # ── LOSS HALT ───────────────────────────────────────────────────────
    # Halt is decided on the running mode's OWN leg (#76): real money in live,
    # study PnL in paper. Paper losses no longer halt live.
    # Trailing high-water-mark halt (#112): the floor trails the session PEAK
    # realized PnL, so banked profit can't be bled back beyond the limit. With
    # peak 0 (never profitable) this collapses to the old fixed -limit floor.
    # MUST mirror RiskGate.loss_halt_breached — that is the enforcement truth.
    is_live_mode = mode == "live"
    leg_label = "live" if is_live_mode else "paper"
    halt_pnl = live_pnl if is_live_mode else paper_pnl
    peak = live_peak if is_live_mode else paper_peak
    floor = peak - loss_halt_usd
    halted = halt_pnl <= floor and not bypass_loss_halt
    headroom = halt_pnl - floor
    headroom_cls = "down" if headroom < loss_halt_usd * 0.4 else ""

    # STATUS is a BUTTON (#76): one click toggles the loss-halt bypass in BOTH
    # paper and live (no confirm; the POST is journaled server-side). Clicking
    # OK/HALTED disables the halt; clicking BYPASS re-enables it.
    if bypass_loss_halt:
        status_cls, status_label = "warn", "BYPASS"
        status_title = "Halt disabled — click to re-enable"
    elif halted:
        status_cls, status_label = "down", "HALTED"
        status_title = "Loss halt hit — click to bypass and keep trading"
    else:
        status_cls, status_label = "on", "OK"
        status_title = "Halt active — click to bypass"
    next_bypass = "false" if bypass_loss_halt else "true"
    status_btn = (
        f"<button class='pill {status_cls} gr-pill-btn' "
        f"title='{escape(status_title)}' "
        f"onclick=\"fetch('/api/loss_halt/bypass',"
        f"{{method:'POST',headers:{{'Content-Type':'application/json'}},"
        f"body:JSON.stringify({{enabled:{next_bypass}}})}})"
        f".then(()=>setTimeout(refreshAll,300))\">{status_label}</button>"
    )

    # RESET is the operator's "let me trade again": when stopped it zeroes the
    # loss-halt tally + peaks. Disabled while running (the loss-halt tally is
    # owned by the loop then, and a real halt would have auto-stopped the bot).
    if state == "running":
        reset_btn = (
            "<button class='gr-btn' disabled "
            "title='Stop the bot to reset the loss-halt tally "
            "(nothing to clear while running)'>Reset halt</button>"
        )
    else:
        reset_title = "Zero today&#39;s loss-halt tally + peaks so entries resume"
        reset_btn = (
            "<button class='gr-btn btn-ok' "
            f"title='{reset_title}' "
            "onclick=\"fetch('/api/loss_halt/reset',{method:'POST'})"
            ".then(()=>setTimeout(refreshAll,300))\">Reset halt</button>"
        )

    halt_hint = (
        "bypass + reset affect REAL MONEY in live"
        if is_live_mode
        else "paper study — affects this study only"
    )
    toggle_html = (
        "<div class='gr-toggle'>"
        + reset_btn
        + f"<span class='gr-toggle-hint'>{escape(halt_hint)}</span>"
        "</div>"
    )
    live_title = (
        "Real money — drives the halt"
        if is_live_mode
        else "Real money — does not affect the paper halt"
    )
    paper_title = (
        "Study — does not affect the live halt"
        if is_live_mode
        else "Study — drives the halt"
    )
    halt_col = (
        "<div class='de-col'>"
        "<div class='de-h'>LOSS HALT</div>"
        "<div class='de-kv'>"
        f"<div><span>Live P&amp;L</span><b class='mono {s.cls(live_pnl)}' "
        f"title='{escape(live_title)}'>{s.money(live_pnl, True)}</b></div>"
        f"<div><span>Paper P&amp;L</span><b class='mono {s.cls(paper_pnl)}' "
        f"title='{escape(paper_title)}'>{s.money(paper_pnl, True)}</b></div>"
        f"<div><span>Peak ({leg_label})</span>"
        f"<b class='mono {s.cls(peak)}' title='Session high-water mark — the halt "
        f"floor trails ${loss_halt_usd:,.2f} below this'>{s.money(peak, True)}</b></div>"
        f"<div><span>Halt floor</span><b class='mono'>{s.money(floor, True)}</b></div>"
        f"<div><span>Headroom ({leg_label})</span>"
        f"<b class='mono {headroom_cls}'>${headroom:,.2f}</b></div>"
        f"<div><span>Status</span>{status_btn}</div>"
        "</div>"
        + toggle_html
        + "</div>"
    )

    # ── BOT STATE + last error ──────────────────────────────────────────
    state_pill_cls = "on" if state == "running" else "off"
    detail_first = bot_detail.split("\n", 1)[0].strip()
    detail_lower = detail_first.lower()
    if any(k in detail_lower for k in ("error", "fail", "refused", "typeerror", "attributeerror", "exception", "crash")):
        detail_cls = "down"
    elif any(k in detail_lower for k in ("running", "starting", "started")):
        detail_cls = "up"
    else:
        detail_cls = "dim"
    detail_display = (
        (detail_first[:90] + "…") if len(detail_first) > 90 else (detail_first or "—")
    )
    state_col = (
        "<div class='de-col'>"
        "<div class='de-h'>BOT STATE</div>"
        "<div class='de-kv'>"
        f"<div><span>State</span><span class='pill {state_pill_cls}'>{escape(state.upper())}</span></div>"
        f"<div><span>Uptime</span><b class='mono'>{escape(s.ago(session_start))}</b></div>"
        f"<div><span>Last detail</span><b class='mono {detail_cls}' title='{escape(bot_detail)}'>{escape(detail_display)}</b></div>"
        "</div></div>"
    )

    # ── LAST 5 BLOCKED ──────────────────────────────────────────────────
    if blocked:
        rows = ""
        for r in blocked:
            ts = r.get("created_at") or ""
            try:
                t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if t.tzinfo is None:
                    t = t.replace(tzinfo=UTC)
                hhmm = t.strftime("%H:%M")
            except ValueError:
                hhmm = ts[-8:-3] if len(ts) >= 8 else ts
            reason = (r.get("error") or "").strip() or "—"
            short = reason[:64] + "…" if len(reason) > 64 else reason
            row_mode = (r.get("mode") or "live").lower()
            mode_tag = (
                "<span class='pill dim' style='margin-right:6px'>paper</span>"
                if row_mode == "paper" else ""
            )
            rows += (
                "<tr>"
                f"<td class='mono dim'>{escape(hhmm)}</td>"
                f"<td class='mono down' title='{escape(reason)}' style='white-space:normal'>"
                f"{mode_tag}{escape(short)}</td>"
                "</tr>"
            )
        blocked_html = f"<table class='gr-tail'><tbody>{rows}</tbody></table>"
    else:
        blocked_html = (
            "<div class='gr-tail-empty'>no blocked entries today</div>"
        )
    blocked_col = (
        "<div class='de-col'>"
        "<div class='de-h'>BLOCKED (LAST 5 TODAY)</div>"
        f"{blocked_html}"
        "</div>"
    )

    return (
        "<section class='card wide'>"
        "<div class='card-h'>RISK GUARDRAILS"
        "<span class='win'>silent-stop surface</span></div>"
        "<div class='gr-grid'>"
        + spend_col
        + halt_col
        + state_col
        + blocked_col
        + "</div></section>"
    )
