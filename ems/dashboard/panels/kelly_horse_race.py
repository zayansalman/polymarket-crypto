"""KELLY HORSE-RACE card: what it has made per mode, what it is doing now, and each window.

Head: the endpoints (PAPER on, LIVE's state, the kill switch), the strategy switch, the loop
when it stopped or failed, and the last pass.

Then, per mode (paper, live): windows settled with a fill, their P&L and the share won, and
orders placed, filled and blocked. Then the state: the loop's own state, each endpoint's
message, what the current window is waiting for, every error of the latest pass, and a
warning when a per-trade cap is below the notional cap (draws above it are blocked).

Then the latest decision factor by factor, the way the maths made it: the price to beat and
where it came from, the price now and the move, the hour's drift and volatility, the time
left, the chance of Up, the die and the side, the book, the size draw, and each mode's order
(its state, shares filled, P&L); or why the window has no order. Then the recent windows.

Pure ``render(...) -> str``, no DB and no awaits: ``execution_view.py`` loads the ledger, the
runner's status and the switch and passes them in. No controls and no browser dialogs; the
switch is on MY STRATEGIES and the knobs are in SETTINGS.
"""

from __future__ import annotations

import math
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from html import escape
from typing import Any

STRATEGY = "kelly_horse_race"
DOCS_URL = f"/strategy-docs/{STRATEGY}"
TITLE = "KELLY HORSE-RACE"
MODES = ("paper", "live")
MAX_ERRORS = 6

ENDPOINT_PILLS: dict[str, dict[str, tuple[str, str]]] = {
    "paper": {"on": ("PAPER ON", "paper"), "kill_switch": ("KILL SWITCH", "down")},
    "live": {"on": ("LIVE ON", "live"), "not_built": ("LIVE NOT BUILT", "off"),
             "kill_switch": ("KILL SWITCH", "down")},
}
LOOP_PILLS: dict[str, tuple[str, str]] = {
    "stopped_on_error": ("LOOP DIED", "down"),
    "stopped": ("LOOP STOPPED", "warn"),
    "pass_failed": ("PASS FAILED", "down"),
}
RUNNER_STATES: dict[str, tuple[str, str]] = {
    "not_started": ("The loop has not run yet in this process.", "warn"),
    "switched_off": ("Switched off: nothing new is decided; fills and settlement go on.", ""),
    "no_endpoint": ("No endpoint takes orders this pass (the kill switch, or every endpoint "
                    "off), so nothing new is decided.", "warn"),
    "pass_failed": ("A step of the last pass failed (see the errors); the next pass tries "
                    "again.", "down"),
    "stopped": ("The loop has stopped: no fills are checked and nothing is decided.", "warn"),
    "stopped_on_error": ("The loop died on an error (see the errors): nothing runs until the "
                         "app restarts.", "down"),
}
ORDER_STATES: dict[str, tuple[str, str]] = {
    "resting": ("RESTING", "on"), "filled": ("FILLED", "on"), "cancelled": ("CANCELLED", "off"),
    "expired": ("EXPIRED", "off"), "blocked": ("BLOCKED", "warn"),
    "rejected": ("REFUSED", "down"),
}


def _f(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _map(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _pill(label: str, css: str) -> str:
    return f"<span class='pill {css}'>{escape(label)}</span>"


def _ago(ts: Any, now: float) -> str:
    t = _f(ts)
    if t is None:
        return "never"
    d = max(0.0, now - t)
    if d < 90:
        return f"{d:.0f}s ago"
    if d < 5400:
        return f"{d / 60:.0f}m ago"
    return f"{d / 3600:.1f}h ago"


def _clock(ts: Any) -> str:
    t = _f(ts)
    if t is None:
        return "—"
    return datetime.fromtimestamp(t, UTC).strftime("%H:%M UTC")


def _money(v: Any, signed: bool = False) -> str:
    x = _f(v)
    if x is None:
        return "—"
    return f"${x:+,.2f}" if signed else f"${x:,.2f}"


def _num(v: Any, fmt: str = ".4f") -> str:
    x = _f(v)
    return "—" if x is None else format(x, fmt)


def _kv(label: str, value: str, sub: str = "") -> str:
    """One labelled line: ``value`` is trusted HTML, ``sub`` plain text."""
    return (f"<div><span>{escape(label)}</span><b>{value}"
            + (f"<span class='kelly-sub'>{escape(sub)}</span>" if sub else "") + "</b></div>")


def _line(text: str, tone: str = "") -> str:
    return f"<div class='{tone}'>{escape(text)}</div>"


def _pnl_class(v: Any) -> str:
    x = _f(v)
    return "pos" if x is not None and x > 0 else "neg" if x is not None and x < 0 else ""


# ---------------------------------------------------------------------------
# Pieces
# ---------------------------------------------------------------------------


def _endpoint_pills(status: Mapping[str, Any]) -> str:
    points = _map(status.get("endpoints"))
    pills = []
    for mode in MODES:
        state = str(_map(points.get(mode)).get("state") or "")
        label, css = ENDPOINT_PILLS[mode].get(state, (f"{mode.upper()} ?", "warn"))
        if mode == "live" and state == "kill_switch" and "paper" in points:
            continue  # one KILL SWITCH pill is enough
        pills.append(_pill(label, css))
    return " ".join(pills)


def _record(summary: Mapping[str, Any]) -> str:
    cells = []
    for mode in MODES:
        s = _map(summary.get(mode))
        settled = int(_f(s.get("settled")) or 0)
        win = _f(s.get("win_rate"))
        pnl = _f(s.get("pnl_usd")) or 0.0
        cells.append(
            f"<div class='kelly-rec'><span class='kelly-mode'>{mode.upper()}</span>"
            f"<b class='{_pnl_class(pnl)}'>{_money(pnl, signed=True)}</b>"
            f"<span>{settled} settled with a fill"
            + (f" · {100 * win:.0f}% won" if win is not None else "") + "</span>"
            f"<span>{int(_f(s.get('placed')) or 0)} placed · {int(_f(s.get('filled')) or 0)} "
            f"filled · {int(_f(s.get('blocked')) or 0)} blocked · "
            f"{int(_f(s.get('rejected')) or 0)} refused</span></div>"
        )
    windows = _map(summary.get("windows"))
    decided, done = int(_f(windows.get("decided")) or 0), int(_f(windows.get("settled")) or 0)
    side_won = int(_f(windows.get("side_won")) or 0)
    cells.append(
        "<div class='kelly-rec'><span class='kelly-mode'>WINDOWS</span>"
        f"<b>{decided}</b><span>decided with a side · {done} settled</span>"
        + (f"<span>the die's side won {side_won} of {done} ({100 * side_won / done:.0f}%)"
           "</span>" if done else "<span>none settled yet</span>")
        + "</div>"
    )
    return "<div class='kelly-strip'>" + "".join(cells) + "</div>"


def _state_lines(status: Mapping[str, Any], enabled: bool | None, caps: Mapping[str, Any],
                 now: float) -> str:
    lines = []
    state = str(status.get("state") or "not_started")
    if state in RUNNER_STATES:
        text, tone = RUNNER_STATES[state]
        lines.append(_line(text, tone))
    if enabled is False and state != "switched_off":
        lines.append(_line("The switch is off: it applies on the next pass.", "warn"))
    for mode in MODES:
        point = _map(_map(status.get("endpoints")).get(mode))
        if point.get("message"):
            lines.append(_line(str(point["message"]), "" if point.get("active") else "warn"))
    window = _map(status.get("window"))
    if window.get("waiting"):
        lines.append(_line(f"This window is waiting: {window.get('message') or window['waiting']}",
                           "warn"))
    cap = _f(caps.get("max_notional_usd"))
    for mode in MODES:
        trade_cap = _f(caps.get(f"{mode}_max_trade_usd"))
        if cap is not None and trade_cap is not None and trade_cap < cap - 1e-9:
            lines.append(_line(
                f"The {mode} per-trade cap (${trade_cap:,.2f}) is below this strategy's "
                f"${cap:,.2f} notional cap, so {mode} draws above ${trade_cap:,.2f} are "
                "blocked and recorded as blocked. Both are in SETTINGS.", "warn"))
    errors = [str(e) for e in status.get("errors") or []]
    for error in errors[:MAX_ERRORS]:
        lines.append(_line(error, "down kelly-err"))
    if len(errors) > MAX_ERRORS:
        lines.append(_line(f"and {len(errors) - MAX_ERRORS} more", "down kelly-err"))
    if not errors and status.get("last_error"):
        lines.append(_line(f"Last error ({_ago(status.get('last_error_ts'), now)}): "
                           f"{status['last_error']}", "warn"))
    return "<div class='kelly-state'>" + "".join(lines) + "</div>" if lines else ""


def _order_line(mode: str, order: Mapping[str, Any] | None) -> str:
    if not order:
        return _kv(mode.upper(), "no order", "not active when the window was decided")
    label, css = ORDER_STATES.get(str(order.get("state")), (str(order.get("state")), "warn"))
    size, filled = _f(order.get("size")) or 0.0, _f(order.get("filled_size")) or 0.0
    parts = [_pill(label, css), f" {filled:.2f} of {size:.2f} filled"]
    pnl = _f(order.get("pnl_usd"))
    if pnl is not None:
        parts.append(f" · <span class='{_pnl_class(pnl)}'>{_money(pnl, signed=True)}</span>")
    sub = str(order.get("reason") or "")
    return _kv(mode.upper(), "".join(parts), sub)


def _decision_block(d: Mapping[str, Any], now: float) -> str:
    slug = escape(str(d.get("window_slug") or ""))
    end = _f(d.get("window_end"))
    left = f"{max(0.0, end - now) / 60:.0f} min left" if end is not None and end > now else (
        "ended")
    head = (f"<div class='kelly-h'><b>{slug}</b> <span>{_clock(d.get('window_start'))}–"
            f"{_clock(end)} · {left}</span>"
            + (f" · settled {escape(str(d['outcome']).upper())}" if d.get("outcome") else "")
            + "</div>")
    if d.get("side") is None:
        return ("<div class='kelly-block'>" + head
                + _line(f"No order this window: {d.get('reason') or 'no reason recorded'}",
                        "kelly-reason warn") + "</div>")
    k, x = _f(d.get("k_price")), _f(d.get("x_price"))
    move = math.log(x / k) if k and x else None
    tau = _f(d.get("tau_h"))
    p = _f(d.get("p_up"))
    inputs = "".join([
        _kv("Price to beat", f"{_num(k, ',.2f')}", str(d.get("k_source") or "")),
        _kv("Price now", f"{_num(x, ',.2f')}",
            f"move ln(X/K) {move:+.5f}" if move is not None else ""),
        _kv("Hour drift", _num(d.get("r60"), "+.5f"), "r60: the last hour's log return"),
        _kv("Hour vol", _num(d.get("sigma_h"), ".5f"), "sigma_h per square-root hour"),
        _kv("Time left", f"{60 * tau:.1f} min" if tau is not None else "—",
            f"tau {tau:.4f} h" if tau is not None else ""),
    ])
    z = _f(d.get("z"))
    maths = "".join([
        _kv("z", _num(z, "+.3f") if z is not None else "no volatility left",
            "(ln(X/K) + r60·tau) / (sigma_h·√tau)"),
        _kv("P(Up)", f"{100 * p:.1f}%" if p is not None else "—", "Φ(z)"),
        _kv("Die", f"u1 {_num(d.get('u1'))} → {escape(str(d.get('side')).upper())}",
            "Up if u1 < P(Up)"),
    ])
    reason = d.get("reason")
    book = "".join([
        _kv("Book", f"bid {_num(d.get('best_bid'), '.2f')} × {_num(d.get('bid_size'), ',.0f')}"
            f" · ask {_num(d.get('best_ask'), '.2f')}",
            f"tick {_num(d.get('tick_size'), 'g')} · min {_num(d.get('min_order_size'), 'g')}"
            " shares"),
        _kv("Size", (f"u2 {_num(d.get('u2'))} → {_num(d.get('shares'), '.2f')} shares at "
                     f"{_num(d.get('price'), '.2f')} = {_money(d.get('notional_usd'))}")
            if not reason else f"u2 {_num(d.get('u2'))}",
            f"up to {_money(d.get('max_notional_usd'))}"),
    ])
    orders = _map(d.get("orders"))
    sent = ("".join(_order_line(mode, _map(orders.get(mode)) or None) for mode in MODES)
            if not reason else _line(f"No order: {reason}", "kelly-reason warn"))
    return ("<div class='kelly-block'>" + head
            + "<div class='kelly-grid'>"
            + f"<div class='kelly-kv'>{inputs}</div><div class='kelly-kv'>{maths}</div>"
            + f"<div class='kelly-kv'>{book}{sent}</div>"
            + "</div></div>")


def _recent_table(decisions: Sequence[Mapping[str, Any]]) -> str:
    if not decisions:
        return ""
    rows = []
    for d in decisions:
        orders = _map(d.get("orders"))
        cells = [
            f"<td>{_clock(d.get('window_start'))}</td>",
            f"<td>{escape(str(d.get('side') or '—'))}</td>",
            f"<td>{_num(d.get('p_up'), '.3f')}</td>",
            f"<td>{_num(d.get('price'), '.2f')}</td>",
            f"<td>{_num(d.get('shares'), '.2f')}</td>",
        ]
        for mode in MODES:
            o = _map(orders.get(mode))
            if not o:
                cells.append("<td>—</td>")
                continue
            label, css = ORDER_STATES.get(str(o.get("state")), (str(o.get("state")), "warn"))
            pnl = _f(o.get("pnl_usd"))
            cells.append(
                f"<td>{_pill(label, css)} {(_f(o.get('filled_size')) or 0):.2f}"
                + (f" <span class='{_pnl_class(pnl)}'>{_money(pnl, signed=True)}</span>"
                   if pnl is not None else "") + "</td>")
        cells.append(f"<td>{escape(str(d.get('outcome') or '…'))}</td>")
        rows.append("<tr>" + "".join(cells) + "</tr>")
    return ("<table class='kelly-table'><thead><tr><th>Window</th><th>Side</th><th>P(Up)</th>"
            "<th>Price</th><th>Shares</th><th>Paper</th><th>Live</th><th>Result</th></tr>"
            "</thead><tbody>" + "".join(rows) + "</tbody></table>")


# ---------------------------------------------------------------------------
# The card
# ---------------------------------------------------------------------------


def render(
    *,
    summary: Mapping[str, Any] | None = None,
    decisions: Sequence[Mapping[str, Any]] | None = None,
    status: Mapping[str, Any] | None = None,
    caps: Mapping[str, Any] | None = None,
    enabled: bool | None = None,
    load_error: str | None = None,
    now: float | None = None,
) -> str:
    """The card's HTML.

    ``summary``: ``ledger.summary()``. ``decisions``: ``ledger.recent()``, newest first, each
    with its orders by mode. ``status``: ``runner.status()``. ``caps``: the notional cap and
    each mode's per-trade cap, from SETTINGS. ``enabled``: the strategy switch.
    ``load_error``: a read that failed, shown on the card rather than hidden.
    """
    now = time.time() if now is None else float(now)
    status = status or {}
    decisions = [d for d in (decisions or []) if isinstance(d, Mapping)]
    switch = ("" if enabled is None else
              _pill("SWITCH ON", "on") if enabled else _pill("SWITCH OFF", "off"))
    loop = LOOP_PILLS.get(str(status.get("state") or ""))
    head = (f"<summary class='card-h'><span class='fold-title'>{TITLE}</span>"
            f"<span class='win kelly-pills'>{_endpoint_pills(status)} {switch} "
            + (_pill(*loop) + " " if loop else "")
            + f"<span>last pass {_ago(status.get('last_pass_ts'), now)}</span></span>"
            "</summary>")
    error = (f"<div class='kelly-reason down'>Could not read this strategy's records: "
             f"{escape(load_error)}</div>" if load_error else "")
    latest = _decision_block(decisions[0], now) if decisions else (
        "<div class='kelly-block'>" + _line("No window decided yet.", "kelly-reason") + "</div>")
    return (
        "<details class='card wide kelly-card fold' data-fold='kelly-horse-race' open>"
        + head + error
        + _record(summary or {})
        + _state_lines(status, enabled, caps or {}, now)
        + latest
        + _recent_table(decisions[1:])
        + "<div class='kelly-foot'>One passive limit buy per BTC 15m window at the chosen "
          "side's best bid; on paper it fills only when the real trade tape reaches it, after "
          "the shares resting ahead of it. "
          f"<a class='strategy-doc-link' href='{DOCS_URL}' target='_blank' rel='noopener'>"
          "How this strategy works</a></div>"
        + "</details>"
    )
