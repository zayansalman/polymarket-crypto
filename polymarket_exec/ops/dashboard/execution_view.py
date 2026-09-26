"""Execution view orchestrator (#37).

Loads the data once and dispatches to the panel renderers in
``polymarket_exec/ops/dashboard/panels/``. Each panel is a pure (data, context) →
HTML function — see the panels package for the panel-specific logic.
"""
from __future__ import annotations

import json
from typing import Any

from db import get_config
from polymarket_bot import inventory as _inv
from polymarket_bot import runtime_knobs as _knobs
from polymarket_bot import strategies as _strategies
from polymarket_bot import strategy_docs as _sd
from polymarket_exec.marketdata import hub as marketdata_hub
from polymarket_exec.ops.dashboard.panels import (
    fade_1h as fade_1h_panel,
    feeds,
    settings as settings_panel,
    strategies as strategies_panel,
    strategy_card as strategy_card_panel,
)


def fade_1h_status(memory: dict[str, Any], saved: Any) -> dict[str, Any]:
    """The runner's status for the card: this process's own, with the copy saved by an
    earlier run lending only its last pass for context.

    Before this process has run a pass, the earlier run's last pass (its time, its coins and
    their orders, its bankroll) is shown, marked as from an earlier run. The loop's state and
    any error always come from this process: a loop that died before its first pass must not
    be shown as the earlier run's clean "running".
    """
    if memory.get("last_pass_ts") or not isinstance(saved, dict) or not saved.get(
            "last_pass_ts"):
        return memory
    status = {**saved, "from_earlier_run": True,
              "state": memory.get("state") or "not_started"}
    if memory.get("last_error"):
        status.update(last_error=memory["last_error"],
                      last_error_ts=memory.get("last_error_ts"),
                      errors=list(memory.get("errors") or [memory["last_error"]]))
    return status


async def fade_1h_data() -> dict[str, Any]:
    """Everything the FADE 1H MOMENTUM ON 15M card shows, as ``fade_1h_panel.render`` kwargs.

    The runner's status comes from memory (``fade_1h_status``: before this process's first
    pass, the copy an earlier run saved lends its last pass). A failed read is returned as
    ``load_error`` so the card shows it: it never blanks the page, and it is never silent.
    """
    from polymarket_bot.fade_1h_momentum_15m import ledger as _fade_ledger
    from polymarket_bot.fade_1h_momentum_15m import runner as _fade_runner

    errors: list[str] = []
    status: dict[str, Any] = _fade_runner.status()
    if not status.get("last_pass_ts"):
        try:
            saved = json.loads(await get_config(_fade_runner.STATUS_KEY) or "null")
        except Exception as exc:  # noqa: BLE001 — shown on the card
            errors.append(f"the saved status ({type(exc).__name__}: {exc})")
        else:
            status = fade_1h_status(status, saved)
    out: dict[str, Any] = {"status": status, "summary": {}, "decisions": [], "orders": [],
                           "positions": [], "dials": None, "poll_s": None}
    try:
        out["poll_s"] = float(await _knobs.get("fade1h_poll_interval_seconds"))
    except Exception:  # noqa: BLE001 — the card falls back to the default interval
        out["poll_s"] = None
    try:
        out["summary"] = await _fade_ledger.summary()
        # A few recent rows as well as the newest per coin: a refusal row only names the
        # decision it refused, and the card shows that decision.
        out["decisions"] = (await _fade_ledger.latest_decisions()
                            + await _fade_ledger.recent_decisions(12))
        out["orders"] = await _fade_ledger.open_orders()
        out["positions"] = await _fade_ledger.open_positions()
        out["dials"] = await _fade_ledger.dials()
    except Exception as exc:  # noqa: BLE001 — a missing table must not blank the page
        errors.append(f"the ledger ({type(exc).__name__}: {exc})")
    out["load_error"] = "; ".join(errors) or None
    return out


async def execution_view_html() -> str:
    """Render the full page body as one HTML string; ``app.py`` consumes this directly."""
    from polymarket_bot.fade_1h_momentum_15m import executor as _fade_executor

    market_data = marketdata_hub.current()
    feeds_html = feeds.render(market_data.snapshot() if market_data is not None else None)

    _enabled = await _strategies.enabled_map()
    try:
        _fade = await fade_1h_data()
    except Exception as exc:  # noqa: BLE001 — the card says what failed
        _fade = {"load_error": f"{type(exc).__name__}: {exc}"}
    _fsummary = _fade.get("summary") or {}
    strategies_html = strategies_panel.render(
        enabled=_enabled,
        records={
            # Profit first: no win rate for a strategy that buys cheap.
            "fade_1h_momentum_15m": {
                "n": _fsummary.get("settled_windows"),
                "pnl": _fsummary.get("net_pnl_usd"),
                "win_rate": None,
            },
        },
    )
    # The executor the runner picks from the requested mode (paper; LIVE is not built).
    mode = await _fade_executor.requested_mode()
    _fade_on = _enabled.get("fade_1h_momentum_15m")
    try:
        fade_1h_html = fade_1h_panel.render(**_fade, enabled=_fade_on, mode=mode)
    except Exception as exc:  # noqa: BLE001 — odd rows must not blank the page either
        fade_1h_html = fade_1h_panel.render(
            load_error=f"the card could not be drawn ({type(exc).__name__}: {exc})",
            enabled=_fade_on, mode=mode,
        )
    strategy_card_html = strategy_card_panel.render(entries=[
        (f, _sd.glance(f.key))
        for _, fams in _inv.by_status()
        for f in fams
    ])

    settings_values = {name: await _knobs.get(name) for name in _knobs.KNOBS}
    settings_html = settings_panel.render(values=settings_values, knobs=_knobs.KNOBS)

    return (
        "<div class='execution-view'>"
        "<div class='execution-grid'>"
        + feeds_html
        + "<div class='grid-stack'>" + strategy_card_html + "</div>"
        + strategies_html
        + fade_1h_html
        + settings_html
        + "</div></div>"
    )
