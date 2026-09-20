"""Top status ribbon: wallet, P&L, open-position (live) P&L, loss-halt control,
feed liveness chips.

The loss halt lives here (#76, #112): a typeable limit with Set (runtime knob,
next tick, no restart) and Reset (when stopped, zeroes today's tally + peaks).
Its verdict MUST mirror RiskGate.loss_halt_breached — trailing floor =
peak - limit, decided on the running mode's own leg.
"""
from __future__ import annotations

from html import escape
from pathlib import Path
from typing import Any

import config as _config

from . import _shared as s


def kill_switch_armed() -> bool:
    """True when the kill-switch file exists (every entry is refused while it does)."""
    return Path(str(_config.KILL_SWITCH_PATH)).exists()


def render(
    *,
    mode: str,
    state: str,
    session_start: str | None,
    live_pnl: float,
    paper_pnl: float,
    day_pnl: float,
    open_pos: list[dict[str, Any]],
    closed_session: list[dict[str, Any]],
    tick: dict[str, Any] | None,
    last_live_at: str | None,
    wallet: dict[str, Any] | None = None,
    loss_halt_usd: float | None = None,
    live_peak: float = 0.0,
    paper_peak: float = 0.0,
    bypass_loss_halt: bool = False,
) -> str:
    is_live = mode == "live"
    halt = loss_halt_usd if loss_halt_usd is not None else _config.TRADE_DAILY_LOSS_HALT_USD
    halt_pnl = live_pnl if is_live else paper_pnl
    peak = live_peak if is_live else paper_peak
    floor = peak - halt
    headroom = halt_pnl - floor  # remaining loss budget on the running leg
    halted = halt_pnl <= floor and not bypass_loss_halt
    kill_armed = kill_switch_armed()
    mode_pnl = live_pnl if is_live else paper_pnl
    session_pnl = sum(c["realized_pnl_usd"] or 0.0 for c in closed_session)


    # Real liveness comes from (a) when the loop last journaled a tick, and
    # (b) what feed_source that tick recorded for each upstream. Each chip
    # flips off when its source goes degraded; a TICK chip shows loop age.
    tick_age = s.tick_age_seconds(tick.get("created_at") if tick else None)
    stale_after = int(max(_config.PAPER_TICK_SECONDS * 3, 20))
    parts = s.parse_feed_source(tick.get("feed_source") if tick else None)
    book_ok = bool(tick) and (
        tick.get("up_best_ask") is not None
        or tick.get("down_best_ask") is not None
        or tick.get("up_best_bid") is not None
        or tick.get("down_best_bid") is not None
    )
    if tick_age is None:
        tick_chip = "<span class='feed warn'>TICK ∅</span>"
    elif tick_age <= stale_after:
        tick_chip = f"<span class='feed on'>TICK {tick_age}s</span>"
    else:
        tick_chip = f"<span class='feed warn'>TICK {tick_age}s STALE</span>"

    def _chip(label: str, ok: bool) -> str:
        return f"<span class='feed {'on' if ok else 'warn'}'>{label}</span>"

    chips = [
        _chip("SPOT", (parts.get("spot") or "").startswith("chainlink")),
        _chip("REF", (parts.get("ref") or "").startswith("chainlink")),
        _chip("VOL", parts.get("vol") == "chainlink_ws"),
        _chip("BOOK", book_ok),
    ]
    if is_live:
        live_age = s.tick_age_seconds(last_live_at)
        if live_age is None:
            chips.append("<span class='feed warn'>EXEC ∅</span>")
        else:
            # "Real trade is X minutes ago" was the exact diagnostic the
            # operator needed when no entries are firing — surface it here.
            label = (
                f"EXEC {live_age}s"
                if live_age < 60
                else f"EXEC {live_age // 60}m{live_age % 60:02d}s"
            )
            # Treat >5min without ANY live-order action as warn-worthy when
            # the bot is supposed to be live. A bot that lost CLOB write
            # access often keeps reading and journaling skips.
            chips.append(_chip(label, live_age <= 300))
    feeds = "".join(chips)
    kill_chip = "<span class='pill live'>KILL ARMED</span>" if kill_armed else ""

    # Run state lives in the topbar Start/Stop buttons; the ribbon only
    # surfaces the exceptional kill condition.
    alert_chips = kill_chip
    # Live P&L = unrealized P&L of open positions, marked at the book mid for
    # positions in the current window (same mark as the blotter, #113).
    cur_window = (tick or {}).get("window_slug")
    unreal, marked = 0.0, 0
    for p in open_pos:
        mark = s.side_mid(tick, p["side"]) if tick and p.get("window_slug") == cur_window else None
        if mark is not None:
            unreal += (mark - (p["entry_price"] or 0.0)) * (p["shares"] or 0.0)
            marked += 1
    if not open_pos:
        live_pnl_stat = s.stat("Live P&L", "—", "", "no open positions")
    elif not marked:
        live_pnl_stat = s.stat("Live P&L", "—", "", f"{len(open_pos)} open · no mark")
    else:
        live_pnl_stat = s.stat(
            "Live P&L", s.money(unreal, True), s.cls(unreal), f"{len(open_pos)} open", flash="pnl"
        )

    # Loss halt: typeable limit + Set / Reset. No dialogs — the click applies.
    if bypass_loss_halt:
        status_cls, status_label = "warn", "BYPASS"
    elif halted:
        status_cls, status_label = "down", "HALTED"
    else:
        status_cls, status_label = "up", "OK"
    headroom_cls = "down" if headroom < halt * 0.4 else ""
    leg = "live" if is_live else "paper"
    halt_title = (
        f"{leg} leg · P&L {s.money(halt_pnl, True)} · peak {s.money(peak, True)} · "
        f"floor {s.money(floor, True)}"
    )
    halt_ctl = (
        f"<div class='stat halt-ctl' title='{escape(halt_title)}'>"
        "<div class='stat-l'>Loss Halt</div>"
        "<div class='halt-row'>"
        f"<input id='halt-usd' class='ctl-input halt-input' type='number' step='1' min='0' "
        f"value='{halt:g}' aria-label='Daily loss halt in USD' "
        "oninput=\"this.dataset.dirty='1'\" "
        "onkeydown=\"if(event.key==='Enter')setLossHalt()\" />"
        "<button class='gr-btn btn-ok' onclick='setLossHalt()'>Set</button>"
        "<button class='gr-btn' onclick='resetLossHalt()'>Reset</button>"
        "</div>"
        f"<div class='stat-s'><b class='halt-status {status_cls}'>{status_label}</b> · "
        f"headroom <span class='{headroom_cls}'>${headroom:,.2f}</span></div>"
        "</div>"
    )

    # Real Polymarket wallet (cash + open positions), same in paper and live.
    if wallet is None:
        wallet_stat = s.stat("Wallet", "—", "", "no wallet")
    else:
        sub = f"cash {s.money(wallet['cash'])} · pos {s.money(wallet['positions'])}"
        wallet_stat = s.stat(
            "Wallet",
            s.money(wallet["total"]),
            "",
            sub + (" · stale" if wallet.get("stale") else ""),
        )
    return (
        "<div class='ribbon'>"
        + (f"<div class='ribbon-id'>{alert_chips}</div>" if alert_chips else "")
        + "<div class='ribbon-stats'>"
        f"{wallet_stat}"
        f"{s.stat('Equity Δ (session)', s.money(session_pnl, True) if closed_session else '—', s.cls(session_pnl), flash='pnl')}"
        f"{s.stat('P&L (today)', s.money(mode_pnl, True), s.cls(mode_pnl), flash='pnl')}"
        f"{live_pnl_stat}"
        f"{s.stat('Open Risk', s.money(sum(p['notional_usd'] or 0 for p in open_pos)), '', f'{len(open_pos)} pos')}"
        f"{halt_ctl}"
        f"<div class='feeds tick-box'>{tick_chip}</div>"
        f"<div class='feeds'>{feeds}</div>"
        f"{s.stat('Uptime', s.ago(session_start))}"
        "</div></div>"
    )
