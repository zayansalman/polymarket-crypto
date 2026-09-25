"""FADE 1H MOMENTUM ON 15M card: what the strategy has made, then what it is doing now.

Profit first: net P&L, return on staked, cents per share, max drawdown, settled windows and
open exposure head the card, before anything about the model. No win rate: a strategy that
buys cheap can lose most windows and still make money, so a win rate would mislead.

Then what decides whether anything can be placed at all (the strategy switch, PAPER or LIVE,
the kill switch), the last pass and the last error, and one block per coin for its current
15m window:

- the inputs in plain words: the 15m leg so far against the price to beat, how jumpy the coin
  is, the snap-back stretch, the hour's leg and the 1h market's price, the trailing trend;
- the model's chance of Up, the market's price and the chance traded on, with the waterfall of
  what moved it from even;
- the side, every rung (resting, or planned but not placed) with its fill chance, any hedge,
  and the decision's one-sentence story.

Paper only. When LIVE is selected the card says this strategy has no live order path (not
built, not authorised), which is what the runner does: it places nothing and keeps settling.

Pure ``render(...) -> str``, no DB and no awaits: ``execution_view.py`` loads the ledger, the
runner's status and the switch and passes them in. Numbers are plain HTML (no KaTeX: the card
is replaced every few seconds). It has no controls and no browser dialogs; the switch is on
MY STRATEGIES and the dials are in SETTINGS.
"""

from __future__ import annotations

import math
import time
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from html import escape
from typing import Any

from . import _shared as s

STRATEGY = "fade_1h_momentum_15m"
DOCS_URL = f"/strategy-docs/{STRATEGY}"
MARKET_URL = "https://polymarket.com/event/"
TITLE = "FADE 1H MOMENTUM ON 15M"
ASSETS = ("btc", "eth", "sol", "xrp")  # the runner's coins, in its order
WINDOW_S = 900

# One pill per decision action (the runner's action names, plus "switched_off", which the
# runner reports in its status without writing a decision row).
ACTIONS: dict[str, tuple[str, str]] = {
    "bid": ("BIDDING", "on"),
    "no_bid": ("NO BID", "off"),
    "not_placed": ("NOT PLACED", "warn"),
    "refused": ("REFUSED", "down"),
    "no_inputs": ("NO INPUTS", "warn"),
    "coin_off": ("COIN OFF", "off"),
    "no_model": ("NO PRICE", "off"),
    "model_error": ("MODEL ERROR", "down"),
    "switched_off": ("SWITCHED OFF", "off"),
}

# The runner's executor states (``executor.PAPER_STATE`` and friends).
EXECUTOR_PILLS: dict[str, tuple[str, str]] = {
    "paper": ("PAPER", "paper"),
    "live_not_authorised": ("LIVE · NOT BUILT / NOT AUTHORISED", "live"),
    "kill_switch": ("KILL SWITCH", "down"),
    "mode_unknown": ("MODE UNKNOWN", "warn"),
}
LIVE_MESSAGE = (
    "LIVE is selected, but this strategy has no live order path: it is not built, and live "
    "trading is not authorised for any market. No new bids are placed; paper bids already "
    "filled keep settling."
)

# What the runner's overall state means, when it is worth a line.
RUNNER_STATES: dict[str, tuple[str, str]] = {
    "not_started": ("The loop has not run yet in this process.", "warn"),
    "setting_up": ("Setting up: no new bids until the dials are seeded and bids left over "
                   "from a restart are stopped.", "warn"),
    "stopped": ("The loop has stopped.", "warn"),
    "stopped_on_error": ("The loop stopped on an error (see the last error).", "down"),
    "no_executor": ("No executor this pass, so no new bids.", "warn"),
}

DIAL_SOURCES = {
    "prior": "the prior (follows the market)",
    "fit_sep17_20": "the starting fit (Sep 17-20 tape)",
    "live": "learned from settled windows",
}

# The waterfall: points of Up chance from each cause, in order. They add up to 100 (p - 1/2).
WATERFALL = (
    ("leg_pts", "Leg so far"),
    ("snapback_pts", "Snap-back"),
    ("momentum_pts", "1h momentum"),
    ("anchor_pts", "Market anchor"),
)

_ROUND = 6


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def _f(value: Any) -> float | None:
    """A finite float, or None."""
    if isinstance(value, bool):
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _map(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


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
        return ""
    return datetime.fromtimestamp(t, UTC).strftime("%H:%M:%S UTC")


def _left(seconds: float) -> str:
    sec = max(0, int(seconds))
    if sec < 60:
        return f"{sec}s left"
    return f"{sec // 60}m {sec % 60:02d}s left"


def _pct(x: Any, digits: int = 2) -> str:
    """A log return (or any fraction) as a signed percent."""
    v = _f(x)
    return "—" if v is None else f"{100.0 * v:+.{digits}f}%"


def _prob(p: Any) -> str:
    v = _f(p)
    return "—" if v is None else f"{100.0 * v:.1f}%"


def _cents(price: Any) -> str:
    v = _f(price)
    if v is None:
        return "—"
    c = 100.0 * v
    return f"{c:.0f}c" if abs(c - round(c)) < 1e-6 else f"{c:.1f}c"


def _px(value: Any) -> str:
    """A coin price in dollars, with the decimals its size needs."""
    v = _f(value)
    if v is None:
        return "—"
    if abs(v) >= 1000:
        return f"${v:,.2f}"
    if abs(v) >= 1:
        return f"${v:,.4f}"
    return f"${v:.5f}"


def _sh(n: Any) -> str:
    v = _f(n)
    return "—" if v is None else f"{v:,.2f}"


def _pill(label: str, css: str) -> str:
    return f"<span class='pill {css}'>{escape(label)}</span>"


def _kv(label: str, value: str, sub: str = "") -> str:
    """One labelled line: ``value`` is trusted HTML, ``sub`` plain text."""
    return (
        f"<div><span>{escape(label)}</span><b>{value}"
        + (f"<span class='fade-sub'>{escape(sub)}</span>" if sub else "")
        + "</b></div>"
    )


# ---------------------------------------------------------------------------
# Picking each coin's current decision (pure)
# ---------------------------------------------------------------------------


def current_decisions(rows: Iterable[Mapping[str, Any]]
                      ) -> dict[str, tuple[Mapping[str, Any], str | None]]:
    """Each coin's newest real decision, and the refusal that followed it, if any.

    When the executor refuses a plan the runner writes a follow-up row (action "refused")
    that carries only the id of the decision it refused. The coin's current state is then the
    refused decision (its inputs and maths) plus the refusal's reason.
    """
    by_id: dict[int, Mapping[str, Any]] = {}
    for row in rows or ():
        if not isinstance(row, Mapping):
            continue
        try:
            by_id[int(row.get("id") or 0)] = row
        except (TypeError, ValueError):
            continue
    out: dict[str, tuple[Mapping[str, Any], str | None]] = {}
    refusals: dict[tuple[str, int], str] = {}
    for rid in sorted(by_id, reverse=True):
        row = by_id[rid]
        asset = str(row.get("asset") or "")
        if not asset:
            continue
        if row.get("action") == "refused" and _map(row.get("inputs")).get("status") == "refused":
            target = _map(row.get("inputs")).get("decision_id")
            if target is not None:
                key = (asset, int(_f(target) or 0))
                refusals.setdefault(key, str(row.get("reason") or "The bids were refused."))
            continue
        if asset in out:
            continue
        out[asset] = (row, refusals.get((asset, rid)))
    return out


def _window_end(inputs: Mapping[str, Any], slug: str | None) -> float | None:
    end = _f(inputs.get("window_end"))
    if end is not None:
        return end
    if slug and "-15m-" in slug:
        start = _f(slug.rsplit("-", 1)[-1])
        if start is not None:
            return start + WINDOW_S
    return None


# ---------------------------------------------------------------------------
# Top of the card: profit, then state
# ---------------------------------------------------------------------------


def _profit_strip(summary: Mapping[str, Any]) -> str:
    net = _f(summary.get("net_pnl_usd")) or 0.0
    ret = _f(summary.get("return_on_staked"))
    cps = _f(summary.get("cents_per_share"))
    dd = _f(summary.get("max_drawdown_usd")) or 0.0
    settled = int(_f(summary.get("settled_windows")) or 0)
    shares = _f(summary.get("settled_shares")) or 0.0
    staked = _f(summary.get("staked_usd")) or 0.0
    exposure = _f(summary.get("open_exposure_usd")) or 0.0
    open_windows = int(_f(summary.get("open_windows")) or 0)
    resting = _f(summary.get("resting_usd")) or 0.0
    resting_n = int(_f(summary.get("resting_orders")) or 0)
    return (
        "<div class='fade-strip'>"
        + s.stat("Net P&L", s.money(net, True), s.cls(net),
                 "settled, after fees (resting fills pay none)")
        + s.stat("Return on staked", s.pct(ret, True) if ret is not None else "—", s.cls(ret),
                 f"on ${staked:,.2f} staked" if staked else "nothing settled yet")
        + s.stat("Per share", f"{cps:+.2f}c" if cps is not None else "—", s.cls(cps),
                 f"over {shares:,.2f} settled shares" if shares else "")
        + s.stat("Max drawdown", f"-${dd:,.2f}" if dd > 0 else s.money(0.0),
                 "down" if dd > 0 else "flat", "window by window, from the peak")
        + s.stat("Settled windows", f"{settled:,}", "",
                 f"{int(_f(summary.get('windows_observed')) or 0):,} windows seen in all")
        + s.stat("Open exposure", s.money(exposure), "",
                 f"{open_windows} window(s) filled, unsettled · ${resting:,.2f} in "
                 f"{resting_n} resting bid(s)")
        + "</div>"
    )


def _executor_view(status: Mapping[str, Any], mode: str | None) -> tuple[str, str, str | None]:
    """(pill label, pill class, message or None) for PAPER / LIVE / kill switch."""
    ex = _map(status.get("executor"))
    state = str(ex.get("state") or "")
    if state in EXECUTOR_PILLS:
        label, css = EXECUTOR_PILLS[state]
        message = None if state == "paper" else str(ex.get("message") or "") or None
        if state == "live_not_authorised" and not message:
            message = LIVE_MESSAGE
        return label, css, message
    if state:
        return state.upper(), "warn", str(ex.get("message") or "") or None
    # The runner has not reported yet: say what the global selection means for it.
    if (mode or "paper") == "live":
        label, css = EXECUTOR_PILLS["live_not_authorised"]
        return label, css, LIVE_MESSAGE
    return EXECUTOR_PILLS["paper"][0], EXECUTOR_PILLS["paper"][1], None


def _state_lines(status: Mapping[str, Any], enabled: bool | None, mode: str | None,
                 dials: Mapping[str, Any] | None, now: float) -> str:
    lines: list[str] = []
    _, css, message = _executor_view(status, mode)
    if message:
        lines.append(f"<div class='{'down' if css in ('live', 'down') else 'warn'}'>"
                     f"{escape(message)}</div>")
    if enabled is False:
        lines.append("<div class='warn'>Switched off on MY STRATEGIES: no new bids. Fills and "
                     "settlement keep running.</div>")

    state = str(status.get("state") or "not_started")
    if state in RUNNER_STATES:
        text, tone = RUNNER_STATES[state]
        lines.append(f"<div class='{tone}'>{escape(text)}</div>")

    last = _f(status.get("last_pass_ts"))
    if last is not None:
        passes = int(_f(status.get("passes")) or 0)
        earlier = " (from an earlier run of the app)" if status.get("from_earlier_run") else ""
        lines.append(f"<div>Last pass {_ago(last, now)}, {_clock(last)}"
                     + (f" · pass {passes:,}" if passes else "") + f"{escape(earlier)}</div>")
    else:
        lines.append("<div>No pass has run yet.</div>")

    error = status.get("last_error")
    if error:
        this_pass = [e for e in (status.get("errors") or []) if e]
        tail = ("" if this_pass else " · the latest pass ran clean")
        more = f" (+{len(this_pass) - 1} more in the latest pass)" if len(this_pass) > 1 else ""
        lines.append(f"<div class='down'>Last error {_ago(status.get('last_error_ts'), now)}: "
                     f"{escape(str(error))}{more}{tail}</div>")
    else:
        lines.append("<div>No errors.</div>")

    facts: list[str] = []
    bank = _map(status.get("bankroll"))
    start, free = _f(bank.get("start_usd")), _f(bank.get("free_usd"))
    if start is not None:
        facts.append(f"bankroll ${start:,.2f} to start"
                     + (f", ${free:,.2f} free" if free is not None else ""))
    if dials:
        version = dials.get("version")
        source = str(dials.get("source") or "")
        n_windows = int(_f(dials.get("n_windows")) or 0)
        facts.append(f"dials v{escape(str(version))}: "
                     f"{escape(DIAL_SOURCES.get(source, source or 'unknown'))}"
                     + (f", {n_windows:,} windows" if n_windows else ""))
    waiting = _map(status.get("settle_waiting"))
    for key, words in (("resolution", "waiting for the venue's result"),
                       ("tape", "waiting for the trade tape")):
        n = int(_f(waiting.get(key)) or 0)
        if n:
            facts.append(f"{n} ended window(s) {words}")
    if facts:
        lines.append("<div>" + " · ".join(facts) + "</div>")
    return "<div class='fade-state'>" + "".join(lines) + "</div>"


# ---------------------------------------------------------------------------
# One coin
# ---------------------------------------------------------------------------


def _inputs_col(inputs: Mapping[str, Any], factors: Mapping[str, Any]) -> str:
    der = _map(inputs.get("derived"))
    twap = _map(inputs.get("twap60"))
    rows: list[str] = []

    d = _f(der.get("d"))
    leg_sub = f"price to beat {_px(inputs.get('start_ref'))}, TWAP-60s now {_px(twap.get('value'))}"
    close = _f(der.get("close_abar"))
    if close is not None:
        leg_sub += f"; the closing minute averages {_pct(close)} so far"
    rows.append(_kv("15m leg", "flat" if d is not None and abs(d) < 5e-7 else _pct(d), leg_sub))

    sigma, h = _f(der.get("sigma")), _f(der.get("h"))
    if sigma is not None:
        sub = "from the last 60 one-minute moves"
        if h is not None and h > 0:
            sub = (f"about ±{100.0 * sigma * math.sqrt(h):.2f}% over the "
                   f"{60.0 * h:.0f} min left; " + sub)
        rows.append(_kv("Jumpiness", f"{100.0 * sigma:.2f}% an hour", sub))
    else:
        rows.append(_kv("Jumpiness", "—"))

    stretch = _f(factors.get("stretch_M"))
    if stretch is None:
        rows.append(_kv("Stretch", "—", "the model has not priced this window"))
    else:
        pull = ("pulls back toward Down" if stretch > 0 else
                "pulls back toward Up" if stretch < 0 else "no pull either way")
        rows.append(_kv("Stretch", _pct(stretch),
                        f"the last 12 15m candles, the latest weighted most; {pull}"))

    x = _f(der.get("x"))
    rows.append(_kv("1h leg", _pct(x),
                    f"since the hour opened at {_px(inputs.get('hour_open'))} on Binance "
                    "(the 1h market settles on it)"))
    hour_up = _f(der.get("hour_up"))
    rows.append(_kv("1h market", f"Up {_cents(hour_up)}" if hour_up is not None else "—",
                    f"bid {_cents(inputs.get('hour_up_bid'))} / ask "
                    f"{_cents(inputs.get('hour_up_ask'))}"))
    rows.append(_kv("Trend", _pct(der.get("mu_l")), "the coin over the last hour"))
    return ("<div class='de-col'><div class='de-h'>Inputs</div>"
            f"<div class='fade-kv'>{''.join(rows)}</div></div>")


def waterfall_parts(factors: Mapping[str, Any]) -> list[tuple[str, float]] | None:
    """The waterfall's (label, points) in order, or None when the row does not carry it."""
    parts = []
    for key, label in WATERFALL:
        v = _f(factors.get(key))
        if v is None:
            return None
        parts.append((label, v))
    return parts


def _waterfall(factors: Mapping[str, Any], p: float | None) -> str:
    parts = waterfall_parts(factors)
    if parts is None:
        return ""
    cum = [50.0]
    for _, v in parts:
        cum.append(cum[-1] + v)
    final = 100.0 * p if p is not None else cum[-1]
    lo, hi = min(cum + [final]), max(cum + [final])
    span = max(hi - lo, 10.0) * 1.1
    mid = 0.5 * (hi + lo)
    lo, hi = max(0.0, mid - span / 2), min(100.0, mid + span / 2)
    if hi - lo < 1e-9:
        lo, hi = 0.0, 100.0

    def x(v: float) -> float:
        return min(100.0, max(0.0, (v - lo) / (hi - lo) * 100.0))

    def bar(a: float, b: float, css: str) -> str:
        left = x(min(a, b))
        width = min(max(x(max(a, b)) - left, 0.8), 100.0 - left)
        return (f"<span class='fade-wf-track'><span class='fade-wf-mid' "
                f"style='left:{x(50.0):.1f}%'></span><span class='fade-wf-bar {css}' "
                f"style='left:{left:.1f}%;width:{width:.1f}%'></span></span>")

    rows = ["<div class='fade-wf-row fade-wf-head'><span>from even</span><span></span>"
            "<span>pts</span><span>Up</span></div>"]
    for (label, v), a, b in zip(parts, cum, cum[1:]):
        css = s.cls(v)
        rows.append(f"<div class='fade-wf-row'><span class='fade-wf-l'>{escape(label)}</span>"
                    f"{bar(a, b, css)}<span class='fade-wf-v {css}'>{v:+.1f}</span>"
                    f"<span class='fade-wf-c'>{b:.1f}%</span></div>")
    net = final - 50.0
    rows.append(f"<div class='fade-wf-row fade-wf-total'><span class='fade-wf-l'>Traded on"
                f"</span>{bar(50.0, final, s.cls(net))}<span class='fade-wf-v {s.cls(net)}'>"
                f"{net:+.1f}</span><span class='fade-wf-c'>{final:.1f}%</span></div>")
    if abs(cum[-1] - final) > 0.1:
        rows.append(f"<div class='fade-reason warn'>The parts add up to {cum[-1]:.1f}%, not "
                    f"the {final:.1f}% traded on.</div>")
    return "<div class='fade-wf'>" + "".join(rows) + "</div>"


def _chances_col(row: Mapping[str, Any], inputs: Mapping[str, Any],
                 factors: Mapping[str, Any]) -> str:
    der = _map(inputs.get("derived"))
    up_book = _map(inputs.get("up_book"))
    market = _f(der.get("market_up"))
    p_model, p = _f(row.get("p_model")), _f(row.get("p"))
    side = row.get("side")
    rows = [
        _kv("Model", f"Up {_prob(p_model)}" if p_model is not None else "—",
            "the maths' chance the window settles Up"),
        _kv("Market", f"Up {_cents(market)}" if market is not None else "—",
            f"the 15m Up book: bid {_cents(up_book.get('best_bid'))} / ask "
            f"{_cents(up_book.get('best_ask'))}"),
        _kv("Traded on", f"Up {_prob(p)}" if p is not None else "—",
            "the model anchored to the market by the dials"),
        _kv("Side", escape(str(side)) if side else "—",
            "where the chance traded on leans" if side else "no decision"),
    ]
    return ("<div class='de-col'><div class='de-h'>Chances</div>"
            f"<div class='fade-kv'>{''.join(rows)}</div>"
            f"{_waterfall(factors, p)}</div>")


def _key(kind: Any, side: Any, price: Any) -> tuple[str, str, float]:
    return (str(kind or ""), str(side or ""), round(_f(price) or 0.0, _ROUND))


def _bids_col(row: Mapping[str, Any], orders: Sequence[Mapping[str, Any]],
              positions: Sequence[Mapping[str, Any]], live: Mapping[str, Any] | None) -> str:
    action = str(row.get("action") or "")
    ladder = [r for r in (row.get("ladder") or []) if isinstance(r, Mapping)]
    hedge = _map(row.get("hedge"))
    hedge_bid = _map(hedge.get("bid"))
    planned: dict[tuple[str, str, float], Mapping[str, Any]] = {}
    for r in ladder:
        planned.setdefault(_key(r.get("kind") or "entry", r.get("side") or row.get("side"),
                                r.get("price")), r)
    if hedge_bid:
        planned.setdefault(_key("hedge", hedge_bid.get("side"), hedge_bid.get("price")),
                           hedge_bid)

    lines: list[str] = []
    rest_rows: list[str] = []
    matched: set[tuple[str, str, float]] = set()
    for o in sorted(orders, key=lambda o: (str(o.get("kind")) != "entry",
                                           -(_f(o.get("price")) or 0.0))):
        key = _key(o.get("kind"), o.get("side"), o.get("price"))
        plan = planned.get(key, {})
        matched.add(key)
        filled = _f(o.get("filled_shares")) or 0.0
        remaining = (_f(o.get("shares")) or 0.0) - filled
        state = "resting" if filled <= 0 else f"partial, {_sh(filled)} filled"
        rest_rows.append(_rung_row(o.get("kind"), o.get("side"), o.get("price"), remaining,
                                   plan.get("p_fill"), plan.get("q_fill"),
                                   o.get("depth_ahead"), state))
    not_there = {"not_placed": "not placed", "refused": "refused"}.get(action,
                                                                         "no longer resting")
    for key, r in planned.items():
        if key in matched:
            continue
        rest_rows.append(_rung_row(key[0], key[1], r.get("price"), r.get("shares"),
                                   r.get("p_fill"), r.get("q_fill"), r.get("depth_ahead"),
                                   not_there))
    if rest_rows:
        lines.append(
            "<table class='de-tail-tbl fade-rungs'><thead><tr><th>Bid</th><th>Shares</th>"
            "<th>Fill chance</th><th>Wins if filled</th><th>Queue ahead</th><th>State</th>"
            "</tr></thead>"
            "<tbody>" + "".join(rest_rows) + "</tbody></table>")
    elif row.get("side"):
        lines.append("<div class='fade-reason'>No bids resting for this window.</div>")
    else:
        lines.append("<div class='fade-reason'>No decision, so no bids.</div>")

    stake, kelly = _f(row.get("stake_usd")), _f(row.get("kelly_f"))
    sizing = _map(_map(row.get("factors")).get("sizing"))
    bank = _f(sizing.get("bankroll_usd"))
    if stake:
        lines.append(f"<div class='fade-reason'>Ladder ${stake:,.2f}"
                     + (f", {100.0 * kelly:.1f}% of the ${bank:,.2f} free bankroll"
                        if kelly is not None and bank else "")
                     + "</div>")
    if live and live.get("decision_id") == row.get("id"):
        counts = [f"{int(_f(live.get(k)) or 0)} {w}" for k, w in
                  (("kept", "kept in the queue"), ("placed", "placed"),
                   ("cancelled", "cancelled")) if live.get(k) is not None]
        if counts:
            lines.append("<div class='fade-reason'>This pass: " + ", ".join(counts) + ".</div>")

    lines.append(_hedge_line(hedge))
    lines.append(_held_line(positions))
    return ("<div class='de-col'><div class='de-h'>Bids"
            + (f" · {escape(str(row.get('side')))}" if row.get("side") else "")
            + "</div>" + "".join(lines) + "</div>")


def _rung_row(kind: Any, side: Any, price: Any, shares: Any, p_fill: Any, q_fill: Any,
              depth: Any, state: str) -> str:
    label = f"{escape(str(side or ''))} {_cents(price)}"
    if str(kind) == "hedge":
        label = f"hedge {label}"
    depth_v = _f(depth)
    return (f"<tr><td>{label}</td><td>{_sh(shares)}</td><td>{_prob(p_fill)}</td>"
            f"<td>{_prob(q_fill)}</td>"
            f"<td>{f'{depth_v:,.0f}' if depth_v is not None else '—'}</td>"
            f"<td>{escape(state)}</td></tr>")


def _hedge_line(hedge: Mapping[str, Any]) -> str:
    if not hedge:
        return ""
    held = str(hedge.get("held") or "")
    parts = [f"Hedge: holding {_sh(hedge.get('held_shares'))} {escape(held)} unpaired"]
    paired = _f(hedge.get("paired_shares")) or 0.0
    if paired > 0:
        parts[0] += f" ({_sh(paired)} paired)"
    if hedge.get("side"):
        shares = _f(hedge.get("shares"))
        if shares:
            parts.append(f"rest {_sh(shares)} {escape(str(hedge['side']))} at "
                         f"{_cents(hedge.get('price'))}; {escape(held)} still wins "
                         f"{_prob(hedge.get('p_held_given_fill'))} if it fills")
        else:
            parts.append(f"the maths gives no hedge at {_cents(hedge.get('price'))} "
                         f"({_sh(hedge.get('optimal_shares') or 0.0)} shares is below the "
                         "venue's minimum or zero)")
    if hedge.get("note"):
        parts.append(escape(str(hedge["note"])))
    return "<div class='fade-reason'>" + "; ".join(parts) + ".</div>"


def _held_line(positions: Sequence[Mapping[str, Any]]) -> str:
    if not positions:
        return ""
    parts = []
    for p in positions:
        hedge_sh = _f(p.get("hedge_shares")) or 0.0
        parts.append(f"{_sh(p.get('shares'))} {escape(str(p.get('side') or ''))} at avg "
                     f"{_cents(p.get('avg_price'))} (${_f(p.get('cost_usd')) or 0.0:,.2f}"
                     + (f", {_sh(hedge_sh)} as hedge" if hedge_sh > 0 else "") + ")")
    return "<div class='fade-reason'>Filled in this window: " + "; ".join(parts) + ".</div>"


def _coin_block(asset: str, pick: tuple[Mapping[str, Any], str | None] | None,
                orders: Sequence[Mapping[str, Any]], positions: Sequence[Mapping[str, Any]],
                live: Mapping[str, Any] | None, live_ts: float | None, now: float) -> str:
    coin = escape(asset.upper())
    head = [f"<b>{coin}</b>"]
    body: list[str] = []
    row, refused = pick if pick is not None else ({}, None)
    row_ts = _f(row.get("ts"))
    # The runner's latest pass is newer than this row when it wrote no row for the coin
    # (switched off) or the coin's step failed after recording: say what it did last.
    fresher = (live is not None and live_ts is not None
               and (row_ts is None or live_ts >= row_ts)
               and live.get("decision_id") != row.get("id"))

    inputs = _map(row.get("inputs"))
    slug = str(row.get("window_slug") or inputs.get("window_slug") or "")
    end = _window_end(inputs, slug)
    # A row with no window (its inputs failed before one was known) is the coin's latest
    # word all the same; the header says how old it is.
    current = bool(row) and (end is None or end > now)

    if current:
        if slug:
            head.append(f"<a class='mk-link' href='{MARKET_URL}{escape(slug)}' "
                        f"target='_blank' rel='noopener'>{escape(slug)}</a>")
        quarter = _f(_map(inputs.get("derived")).get("quarter"))
        if quarter is not None:
            head.append(f"quarter {int(quarter)} of the hour")
        if end is not None:
            head.append(f"<span class='fade-left'>{_left(end - now)}</span>")
        head.append(f"decided {_ago(row_ts, now)}")
    elif row:
        head.append(f"no decision for the current window (the last one, {_ago(row_ts, now)}, "
                    "was for a window that has ended)")
    else:
        head.append("no decision recorded yet")

    action = str(row.get("action") or "") if current else ""
    if refused:
        action = "refused"
    if fresher:
        action = str(live.get("action") or action)
    if action in ACTIONS:
        label, css = ACTIONS[action]
        head.append(_pill(label, css))
    elif action:
        head.append(_pill(action.upper(), "warn"))

    if fresher and live.get("reason") and live.get("action") != row.get("action"):
        body.append(f"<div class='fade-reason warn'>{escape(str(live['reason']))}</div>")
    if live is not None and live.get("error"):
        body.append(f"<div class='fade-reason down'>{escape(str(live['error']))}</div>")

    if current:
        if inputs.get("status") == "ok":
            factors = _map(_map(row.get("factors")).get("model"))
            here_orders = [o for o in orders if o.get("window_slug") == slug]
            here_pos = [p for p in positions if p.get("window_slug") == slug]
            body.append("<div class='fade-grid'>"
                        + _inputs_col(inputs, factors)
                        + _chances_col(row, inputs, factors)
                        + _bids_col(row, here_orders, here_pos, live)
                        + "</div>")
            story = _map(row.get("factors")).get("explanation")
            if story:
                body.append(f"<div class='fade-story'>{escape(str(story))}</div>")
        reason = row.get("reason")
        if reason and not fresher:
            tone = "warn" if row.get("action") in ("no_inputs", "not_placed") else ""
            body.append(f"<div class='fade-reason {tone}'>{escape(str(reason))}</div>")
        if inputs.get("status") == "problem":
            also = [str(_map(a).get("message") or "") for a in (inputs.get("also") or [])]
            also = [m for m in also if m]
            if also:
                body.append(f"<div class='fade-reason warn'>Also: {escape(' '.join(also))}"
                            "</div>")
        if refused:
            body.append(f"<div class='fade-reason down'>Refused: {escape(refused)}</div>")

    elsewhere = [o for o in orders if o.get("window_slug") != slug]
    if elsewhere:
        body.append(f"<div class='fade-reason warn'>{len(elsewhere)} bid(s) resting in another "
                    "window.</div>")
    waiting = [p for p in positions if p.get("window_slug") != slug]
    if waiting:
        cost = sum(_f(p.get("cost_usd")) or 0.0 for p in waiting)
        body.append(f"<div class='fade-reason'>Filled in earlier windows, awaiting the result: "
                    f"{_sh(sum(_f(p.get('shares')) or 0.0 for p in waiting))} shares "
                    f"(${cost:,.2f}).</div>")
    return ("<div class='fade-coin'>"
            f"<div class='fade-coin-h'>{' · '.join(head)}</div>"
            + "".join(body) + "</div>")


# ---------------------------------------------------------------------------
# The card
# ---------------------------------------------------------------------------


def render(
    *,
    summary: Mapping[str, Any] | None = None,
    decisions: Sequence[Mapping[str, Any]] | None = None,
    orders: Sequence[Mapping[str, Any]] | None = None,
    positions: Sequence[Mapping[str, Any]] | None = None,
    status: Mapping[str, Any] | None = None,
    dials: Mapping[str, Any] | None = None,
    enabled: bool | None = None,
    mode: str | None = None,
    load_error: str | None = None,
    now: float | None = None,
) -> str:
    """The card's HTML.

    ``summary``: ``ledger.summary()``. ``decisions``: decision rows, any order, from
    ``ledger.latest_decisions()`` plus a few ``recent_decisions`` (so a refusal row can be
    matched to the decision it refused). ``orders``: ``ledger.open_orders()``. ``positions``:
    ``ledger.open_positions()``. ``status``: ``runner.status()``. ``dials``:
    ``ledger.dials()``. ``enabled``: the strategy switch. ``mode``: the global PAPER/LIVE
    selection (used until the runner has reported its own state). ``load_error``: a read
    that failed, shown on the card rather than hidden.
    """
    now = time.time() if now is None else float(now)
    summary = summary or {}
    status = status or {}
    orders = [o for o in (orders or []) if isinstance(o, Mapping)]
    positions = [p for p in (positions or []) if isinstance(p, Mapping)]
    picks = current_decisions(decisions or [])
    live_assets = _map(status.get("assets"))
    live_ts = _f(status.get("last_pass_ts"))

    label, css, _ = _executor_view(status, mode)
    switch = ("" if enabled is None else
              _pill("SWITCH ON", "on") if enabled else _pill("SWITCH OFF", "off"))
    last = _f(status.get("last_pass_ts"))
    head = (f"<summary class='card-h'><span class='fold-title'>{TITLE}</span>"
            f"<span class='win fade-pills'>{_pill(label, css)} {switch} "
            f"<span>last pass {_ago(last, now) if last is not None else 'not yet'}</span>"
            "</span></summary>")

    blocks = []
    for asset in ASSETS:
        blocks.append(_coin_block(
            asset, picks.get(asset),
            [o for o in orders if o.get("asset") == asset],
            [p for p in positions if p.get("asset") == asset],
            _map(live_assets.get(asset)) or None, live_ts, now,
        ))

    error = (f"<div class='fade-reason down'>Could not read this strategy's records: "
             f"{escape(load_error)}</div>" if load_error else "")
    return (
        "<details class='card wide fade-card fold' data-fold='fade-1h' open>"
        + head
        + error
        + _profit_strip(summary)
        + _state_lines(status, enabled, mode, dials, now)
        + "".join(blocks)
        + "<div class='fade-foot'>Paper only: every bid rests below the ask and fills only "
          "when the real trade tape reaches it. "
          f"<a class='strategy-doc-link' href='{DOCS_URL}' target='_blank' rel='noopener'>"
          "How this strategy works</a></div>"
        + "</details>"
    )
