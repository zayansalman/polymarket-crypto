"""Execution panel: Model | Market strategy toggle and the Market Buy Up / Buy Down buttons.

Model = the strategy enters on its own. Market = model auto-entries are off and
the operator clicks BUY UP / BUY DOWN; the click goes to ``POST
/api/market-order`` and the running loop executes it through the same entry
pipeline the model uses (paper fill or live order). Pure ``render(...)``
transform — data is loaded in ``execution_view.py``.

The disabled reasons here are a courtesy so the operator isn't clicking into a
certain refusal. The server route and the runner stay the authority: they
re-check everything (and the risk gate) when the click arrives.
"""
from __future__ import annotations

from html import escape
from typing import Any

from polymarket_bot import market_selection as ms
from polymarket_exec.execution.live import DEFAULT_MIN_ORDER_SIZE

_SIDES = (("Up", "up_best_ask", "market_up_price"), ("Down", "down_best_ask", "market_down_price"))


def selection_label(asset: str, timeframe: str) -> str:
    """'BTC 5m' — the operator-facing name of an asset/timeframe selection."""
    return f"{ms.ASSETS.get(asset, asset.upper())} {ms.TIMEFRAMES.get(timeframe, timeframe)}"


def side_ask(tick: dict[str, Any] | None, side: str) -> float | None:
    """The side's best ask from the latest tick (the price a Buy fills at), or None."""
    if not tick:
        return None
    for name, ask_key, fallback_key in _SIDES:
        if name == side:
            for key in (ask_key, fallback_key):
                v = tick.get(key)
                if isinstance(v, (int, float)) and v > 0:
                    return float(v)
    return None


def disabled_reason(
    *,
    running: bool,
    loop_supported: bool,
    selection: str,
    tick: dict[str, Any] | None,
    open_position_count: int,
    kill_armed: bool,
) -> str | None:
    """Why both Buy buttons are off right now, or None when a click can go through.

    Only the conditions that make a click a certain refusal — never the model's
    own filters (edge, confidence, price band) or the auto-pause, which don't
    apply to Market buys.
    """
    if not running:
        return "Press ▶ Start first"
    if not loop_supported:
        return f"Loop isn't wired for {selection} yet"
    if not tick or not tick.get("window_slug"):
        return "Waiting for market data"
    if (tick.get("remaining_seconds") or 0) <= 0:
        return "Window ended — waiting for the next one"
    if open_position_count > 0:
        return "Position open — wait for it to exit (max 1)"
    if kill_armed:
        return "Kill switch armed"
    return None


def _toggle(strategy: str) -> str:
    def _opt(value: str, label: str) -> str:
        active = " active" if strategy == value else ""
        return (
            f"<button class='mode-opt{active}' data-strategy='{value}' "
            f"onclick=\"setExecutionStrategy('{value}')\">{label}</button>"
        )

    return (
        "<div class='mode-toggle exec-toggle' title='Execution strategy'>"
        f"{_opt('model', 'MODEL')}{_opt('market', 'MARKET')}"
        "</div>"
    )


def _buy_button(
    side: str, ask: float | None, shares: float, mode_tag: str, reason: str | None
) -> str:
    off = reason or (None if ask is not None else f"No ask on the {side} book")
    disabled = f" disabled title='{escape(off)}'" if off else ""
    px = f"{ask:.3f}" if ask is not None else "—"
    value = f"≈ ${shares * ask:,.2f} · {shares:g} sh" if ask is not None else f"{shares:g} sh"
    return (
        f"<button class='mo-btn {side.lower()}' data-side='{side}' "
        f"data-ask='{(ask or 0):.4f}' onclick=\"buyMarket('{side}')\"{disabled}>"
        f"<span class='mo-l'>BUY {side.upper()}</span>"
        f"<span class='mo-px'>{px}</span>"
        f"<span class='mo-sub'>{value}</span>"
        f"{mode_tag}"
        "</button>"
    )


def render(
    *,
    strategy: str,
    running: bool,
    mode: str,
    asset: str,
    timeframe: str,
    loop_supported: bool,
    tick: dict[str, Any] | None,
    trade_shares: float | None,
    open_position_count: int,
    kill_armed: bool,
) -> str:
    """Render the EXECUTION card (always a full-width ``card wide``).

    ``strategy`` is the ``execution_strategy`` knob — anything but "market"
    reads as Model. ``running`` is the runner thread's liveness, not the saved
    state row. ``trade_shares`` is the operator's CONTROLS share count (None →
    the venue minimum, same as the loop sizes a Market buy).
    """
    strategy = "market" if strategy == "market" else "model"
    is_live = mode == "live"
    label = selection_label(asset, timeframe)
    window = str((tick or {}).get("window_slug") or "")
    head = (
        f"<section class='card wide' id='exec-card' data-window='{escape(window)}' "
        f"data-asset='{escape(asset)}' data-timeframe='{escape(timeframe)}' "
        f"data-mode='{'live' if is_live else 'paper'}'>"
        "<div class='card-h'>EXECUTION"
        f"<span class='win'>{'LIVE' if is_live else 'paper'} · {escape(label)}</span></div>"
    )
    if strategy == "model":
        return (
            head
            + "<div class='exec-row'>"
            + _toggle(strategy)
            + "<span class='exec-note'>"
            "Model — the strategy enters automatically when its filters pass.</span>"
            "</div></section>"
        )

    shares = trade_shares if trade_shares and trade_shares > 0 else float(DEFAULT_MIN_ORDER_SIZE)
    mode_tag = (
        "<span class='pill live'>LIVE — real order</span>"
        if is_live
        else "<span class='pill paper'>PAPER</span>"
    )
    reason = disabled_reason(
        running=running,
        loop_supported=loop_supported,
        selection=label,
        tick=tick,
        open_position_count=open_position_count,
        kill_armed=kill_armed,
    )
    buttons = "".join(
        _buy_button(side, side_ask(tick, side), shares, mode_tag, reason)
        for side, _, _ in _SIDES
    )
    status = (
        f"<div class='mo-status off'>{escape(reason)}</div>"
        if reason
        else "<div class='mo-status'>Click to buy at the current ask. "
        "Exits follow your exit style.</div>"
    )
    return (
        head
        + "<div class='exec-row'>"
        + _toggle(strategy)
        + "<span class='exec-note'>"
        "Market — you click, the bot buys. Model auto-entries are off.</span>"
        "</div>"
        f"<div class='mo-grid'>{buttons}</div>"
        f"{status}"
        "</section>"
    )
