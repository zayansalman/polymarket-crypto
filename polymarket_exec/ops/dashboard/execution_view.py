"""Execution view orchestrator (#37).

Loads journal data once and dispatches to the panel renderers in
``polymarket_exec/ops/dashboard/panels/``. Each panel is a pure (data, context) →
HTML function — see the panels package for the panel-specific logic.

This file deliberately stays small: it's the wiring layer between the
SQLite read layer (``panels/_data.py``) and the per-panel renderers.
"""
from __future__ import annotations

import config as _config
from db import get_config

from polymarket_exec.ops.dashboard.panels import _data as data
from polymarket_exec.ops.dashboard.panels import (
    blotter,
    controls,
    daily_altcoin,
    decision_engine,
    guardrails,
    market,
    market_selector,
    performance,
    ribbon,
    tca,
)


async def market_selector_html() -> str:
    """Render the topbar asset/timeframe selector with open-position glow."""
    from polymarket_bot import market_selection

    return market_selector.render(
        selection=await market_selection.get_selection(),
        open_pnl=market_selector.open_market_pnl(
            open_pos=await data.open_positions(_config.EXIT_STYLE),
            daily_open=await data.daily_positions(state="open"),
            tick=await data.latest_tick(),
        ),
    )


async def execution_view_html() -> str:
    """Render the full EMS view as one HTML string.

    Same public signature and output contract as before the panel split —
    ``app.py`` consumes this directly.
    """
    style = _config.EXIT_STYLE
    mode = await get_config("polymarket_bot.requested_mode", _config.BOT_MODE) or "paper"
    state = await get_config("polymarket_bot.state", "stopped") or "stopped"
    session_start = await get_config("polymarket_bot.session_start", None)

    # Split counters (issue #67): show LIVE vs PAPER P&L distinctly so the
    # ribbon and LOSS HALT panel never blend real-money and study results.
    # Falls back through the pre-split #64 key, then the legacy #20 keys.
    live_pnl = float(
        await get_config("risk.live_realized_pnl")
        or await get_config("risk.daily_realized_pnl")
        or await get_config("btc_live.daily_realized_pnl")
        or 0
    )
    paper_pnl = float(await get_config("risk.paper_realized_pnl") or 0)
    # Session high-water marks (#112): the loss halt trails these peaks. Absent
    # (pre-#112 state) → fall back to max(0, leg_pnl) so a never-profitable
    # session shows the old fixed -limit floor.
    live_peak = max(
        float(await get_config("risk.live_peak_pnl") or 0), live_pnl, 0.0
    )
    paper_peak = max(
        float(await get_config("risk.paper_peak_pnl") or 0), paper_pnl, 0.0
    )
    # Combined PnL for the ribbon's headline number. The loss-halt decision uses
    # the per-mode leg (live in live, paper in paper) — see RiskGate.halt_pnl (#76).
    day_pnl = live_pnl + paper_pnl
    day_notional = float(
        await get_config("risk.daily_buy_notional")
        or await get_config("btc_live.daily_buy_notional")
        or 0
    )
    bot_detail = await get_config("polymarket_bot.detail", "") or ""

    # ---- one-shot data load ----
    tick = await data.latest_tick()
    recent_ticks = await data.recent_decisions(limit=10)
    closed = await data.closed(style, None, limit=40)
    closed_session = await data.closed(style, session_start)  # this run, for the ribbon
    open_pos = await data.open_positions(style)
    # Per-mode mini-cards query each mode's own recent-40 window so a low-volume
    # mode (live) is never crowded out by a high-volume mode (paper).
    closed_live = await data.closed(style, None, limit=40, mode="live")
    closed_paper = await data.closed(style, None, limit=40, mode="paper")
    last_live_at = await data.last_live_order_at()
    spread = await data.avg_spread()
    blocked_today = await data.recent_blocked(limit=5)
    submitted_count, submitted_notional = await data.today_submitted_summary()

    perf = data.performance(closed)
    perf_live = data.performance(closed_live)
    perf_paper = data.performance(closed_paper)
    recon = await data.reconciliation()

    daily_open = await data.daily_positions(state="open")
    daily_closed = await data.daily_positions(state="settled")
    daily_perf = data.performance(daily_closed)

    # ---- panels ----
    from polymarket_exec.execution.gate import (
        get_loss_halt_bypass,
        get_runtime_trade_shares,
    )
    bypass_loss_halt = await get_loss_halt_bypass()
    # Share-denominated trade size (#89) — the operator-facing knob. None → the
    # CONTROLS input defaults to the venue minimum. ``current_price`` is the
    # favoured side's live ask (the side ≥ 0.50) for the $-value estimate.
    trade_shares_current = await get_runtime_trade_shares()
    _px = [
        p
        for p in ((tick or {}).get("market_up_price"), (tick or {}).get("market_down_price"))
        if isinstance(p, (int, float)) and p > 0
    ]
    current_price = max(_px) if _px else None

    ribbon_html = ribbon.render(
        mode=mode,
        state=state,
        session_start=session_start,
        live_pnl=live_pnl,
        paper_pnl=paper_pnl,
        day_pnl=day_pnl,
        open_pos=open_pos,
        closed_session=closed_session,
        tick=tick,
        last_live_at=last_live_at,
    )
    guardrails_html = guardrails.render(
        day_spend=day_notional,
        bankroll_cap=_config.TRADE_BANKROLL_CAP_USD,
        submitted_count=submitted_count,
        submitted_notional=submitted_notional,
        day_pnl=day_pnl,
        live_pnl=live_pnl,
        paper_pnl=paper_pnl,
        loss_halt_usd=_config.TRADE_DAILY_LOSS_HALT_USD,
        live_peak=live_peak,
        paper_peak=paper_peak,
        state=state,
        bot_detail=bot_detail,
        session_start=session_start,
        blocked=blocked_today,
        mode=mode,
        bypass_loss_halt=bypass_loss_halt,
    )
    controls_html = controls.render(
        trade_shares_current=trade_shares_current,
        current_price=current_price,
    )
    market_html = market.render(tick, open_pos)
    decision_html = decision_engine.render(tick, recent_ticks)
    performance_html = performance.render(
        style=style, perf=perf, perf_live=perf_live, perf_paper=perf_paper, recon=recon
    )
    tca_html = tca.render(perf=perf, spread=spread)
    blotter_html = blotter.render(closed=closed, open_pos=open_pos, tick=tick)
    daily_altcoin_html = daily_altcoin.render(open_positions=daily_open, perf=daily_perf)

    return (
        "<div class='execution-view'>"
        + ribbon_html
        + "<div class='execution-grid'>"
        + guardrails_html
        + controls_html
        + market_html
        + decision_html
        + performance_html
        + tca_html
        + blotter_html
        + daily_altcoin_html
        + "</div></div>"
    )
