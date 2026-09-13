"""Top status ribbon: pause/kill chips, P&L stats, feed liveness chips."""
from __future__ import annotations

from html import escape
from pathlib import Path
from typing import Any

import config as _config

from . import _shared as s


def render(
    *,
    mode: str,
    state: str,
    session_start: str | None,
    paused: bool,
    pause_reason: str,
    live_pnl: float,
    paper_pnl: float,
    day_pnl: float,
    open_pos: list[dict[str, Any]],
    closed_session: list[dict[str, Any]],
    tick: dict[str, Any] | None,
    last_live_at: str | None,
) -> str:
    is_live = mode == "live"
    halt = _config.TRADE_DAILY_LOSS_HALT_USD
    headroom = halt + min(0.0, day_pnl)  # remaining loss budget
    kill_armed = Path(str(_config.KILL_SWITCH_PATH)).exists()
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
        tick_chip,
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
    pause_chip = (
        f"<span class='pill warn' title='{escape(pause_reason)}'>⏸ AUTO-PAUSED</span>"
        if paused
        else ""
    )
    kill_chip = "<span class='pill live'>KILL ARMED</span>" if kill_armed else ""

    return (
        "<div class='ribbon'>"
        + (f"<div class='ribbon-id'>{pause_chip}{kill_chip}</div>" if pause_chip or kill_chip else "")
        + "<div class='ribbon-stats'>"
        f"{s.stat('Equity Δ (session)', s.money(session_pnl, True) if closed_session else '—', s.cls(session_pnl), flash='pnl')}"
        f"{s.stat('P&L (today)', s.money(mode_pnl, True), s.cls(mode_pnl), flash='pnl')}"
        f"{s.stat('Open Risk', s.money(sum(p['notional_usd'] or 0 for p in open_pos)), '', f'{len(open_pos)} pos')}"
        f"{s.stat('Halt Headroom', s.money(headroom), 'down' if headroom < halt * 0.4 else '')}"
        f"<div class='feeds'>{feeds}</div>"
        f"{s.stat('Uptime', s.ago(session_start))}"
        "</div></div>"
    )
