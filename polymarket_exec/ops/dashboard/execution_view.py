"""Execution view orchestrator (#37).

Loads journal data once and dispatches to the panel renderers in
``polymarket_exec/ops/dashboard/panels/``. Each panel is a pure (data, context) →
HTML function — see the panels package for the panel-specific logic.

This file deliberately stays small: it's the wiring layer between the
SQLite read layer (``panels/_data.py``) and the per-panel renderers.
"""
from __future__ import annotations

import time

import config as _config
from db import get_config
from polymarket_bot import runtime_knobs as _knobs

from polymarket_exec.marketdata import hub as marketdata_hub
from polymarket_exec.ops import feed_monitor, flow_recorder, macro_recorder
from polymarket_exec.ops.dashboard import quote_feed
from polymarket_exec.ops.dashboard.panels import _data as data
from polymarket_exec.ops.dashboard.panels import _wallet
from polymarket_exec.ops.dashboard.panels import (
    blotter,
    controls,
    copytrade as copytrade_panel,
    daily_altcoin,
    decision_engine,
    feeds,
    market,
    market_selector,
    performance,
    ribbon,
    settings as settings_panel,
    strategies as strategies_panel,
    tca,
)


async def market_selector_html() -> str:
    """Render the topbar asset/timeframe selector with open-position glow."""
    from polymarket_bot import market_selection

    style = await _knobs.get("exit_style")
    return market_selector.render(
        selection=await market_selection.get_selection(),
        open_pnl=market_selector.open_market_pnl(
            open_pos=await data.open_positions(style),
            daily_open=await data.daily_positions(state="open"),
            tick=await data.latest_tick(),
        ),
    )


async def execution_view_html() -> str:
    """Render the full EMS view as one HTML string.

    Same public signature and output contract as before the panel split —
    ``app.py`` consumes this directly.
    """
    style = await _knobs.get("exit_style")
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
    spread = await data.avg_spread()

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
    # ticket defaults to the venue minimum.
    trade_shares_current = await get_runtime_trade_shares()

    loss_halt_current = await _knobs.get("live_daily_loss_halt_usd")
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
        wallet=await _wallet.wallet_snapshot(),
        loss_halt_usd=loss_halt_current,
        live_peak=live_peak,
        paper_peak=paper_peak,
        bypass_loss_halt=bypass_loss_halt,
    )
    # The ticket prices the SELECTED market's live book (not the loop's last
    # tick), so it tracks the selector and stays live while the loop is stopped.
    from polymarket_bot import market_selection

    selection = await market_selection.get_selection()
    controls_html = controls.render(
        trade_shares_current=trade_shares_current,
        asset=selection.asset,
        timeframe=selection.timeframe,
        quote=quote_feed.snapshot(selection.asset, selection.timeframe),
        now=time.time(),
    )
    monitor = feed_monitor.current()
    recorder = flow_recorder.current()
    macro = macro_recorder.current()
    market_data = marketdata_hub.current()
    feeds_html = feeds.render(
        monitor.snapshot() if monitor is not None else None,
        recorder.snapshot() if recorder is not None else None,
        macro.snapshot() if macro is not None else None,
        market_data.snapshot() if market_data is not None else None,
    )
    # Sits directly under FEEDS: what data arrives, then what is done with it.
    from polymarket_bot import strategies as _strategies

    strategies_html = strategies_panel.render(enabled=await _strategies.enabled_map())
    market_html = market.render(tick, open_pos)
    decision_html = decision_engine.render(tick, recent_ticks)
    performance_html = performance.render(
        style=style, perf=perf, perf_live=perf_live, perf_paper=perf_paper, recon=recon
    )
    tca_html = tca.render(perf=perf, spread=spread)
    blotter_html = blotter.render(closed=closed, open_pos=open_pos, tick=tick)
    daily_altcoin_html = daily_altcoin.render(
        open_positions=daily_open,
        perf=daily_perf,
        scan_interval_seconds=await _knobs.get("daily_scan_interval_seconds"),
        trade_usd=await _knobs.get("daily_trade_usd"),
    )
    # Copy trade: the watcher keeps its snapshot in memory, so this is a read
    # of live state rather than a DB load.
    from polymarket_bot.copytrade import targets as _copy_targets
    from polymarket_bot.copytrade import watcher as _copy_watcher

    _cw = _copy_watcher.current()
    _cstate = _cw.state if _cw is not None else None
    from polymarket_bot.copytrade import ledger as _copy_ledger

    import time as _time

    try:
        _csummary = await _copy_ledger.summary()
        _cdecisions = await _copy_ledger.decision_counts(int(_time.time()) - 86400)
        _creach = await _copy_ledger.reachability(int(_time.time()) - 86400)
    except Exception:  # noqa: BLE001 — a missing table must not blank the page
        _csummary, _cdecisions, _creach = {}, [], []
    copytrade_html = copytrade_panel.render(
        state=_cstate,
        target=_copy_targets.get(_cstate.target) if _cstate else None,
        summary=_csummary,
        decisions=_cdecisions,
        reach=_creach,
    )
    settings_values = {name: await _knobs.get(name) for name in _knobs.KNOBS}
    settings_html = settings_panel.render(values=settings_values, knobs=_knobs.KNOBS)

    return (
        "<div class='execution-view'>"
        + ribbon_html
        + "<div class='execution-grid'>"
        + feeds_html
        + controls_html
        + strategies_html
        + market_html
        + decision_html
        + performance_html
        + tca_html
        + blotter_html
        + daily_altcoin_html
        + copytrade_html
        + settings_html
        + "</div></div>"
    )
