"""FADE 1H MOMENTUM ON 15M card: what the strategy has made, then what it is doing now.

Profit first: net P&L, return on staked, cents per share, max drawdown, settled windows and
open exposure head the card, before anything about the model. No win rate: a strategy that
buys cheap can lose most windows and still make money, so a win rate would mislead.

Then the state: the strategy switch, PAPER or LIVE (this strategy has no live order path) and
the kill switch; the loop's own state (a loop that stopped or a pass that failed says so in the
header too); the last pass, every error of the latest pass (or the last error before it); the
bankroll, the dials and the ended windows still waiting to settle.

Then one block per coin for its current 15m window:

- the minutes left and the market;
- the inputs in plain words: the 15m move (the live price against the price to beat, the
  opening TWAP-60s print), how jumpy the coin is, the snap-back stretch, the hour's move and
  the 1h market's price, the trailing trend, and any note the inputs carry;
- the maths' chance of Up against the market's price, the chance traded on, and the factor
  waterfall of what moved it from even;
- the order the maths chose: a scaled passive limit order buying one side, a resting sell that
  reduces a position held, or "no order" when nothing adds growth; the growth each option adds;
  every child order (resting, or planned but not resting) with its price level, shares, depth
  ahead, fill chance and win chance if filled; what this pass kept, placed, cancelled or held
  back; the position held and what the maths does with it;
- the decision's one-sentence story in a trader's words, with a link to how the strategy works.

Paper only. When LIVE is selected the card says this strategy has no live order path (not
built, not authorised), which is what the runner does: it places nothing and keeps settling.

Pure ``render(...) -> str``, no DB and no awaits: ``execution_view.py`` loads the ledger, the
runner's status and the switch and passes them in. Numbers are plain HTML (no KaTeX: the card
is replaced every few seconds). It has no controls and no browser dialogs; the switch is on
MY STRATEGIES and the dials are in SETTINGS.
"""

from __future__ import annotations

import json
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
DEFAULT_POLL_S = 60.0  # the runner's default pass interval, when Settings cannot be read
LATE_PASSES = 3  # a last pass older than this many intervals (plus a minute) is called late
MAX_ERRORS = 6  # distinct errors of the latest pass listed before "and N more"

# The runner's per-coin actions (its constants) plus "switched_off", which it reports in its
# status without writing a decision row. "orders" is replaced by the order itself (BUY UP,
# SELL DOWN) when the decision says which.
ORDERS = "orders"
NO_ORDER = "no_order"
ACTIONS: dict[str, tuple[str, str]] = {
    ORDERS: ("ORDERS", "on"),
    NO_ORDER: ("NO ORDER", "off"),
    "not_placed": ("NOT PLACED", "warn"),
    "refused": ("REFUSED", "down"),
    "no_inputs": ("NO INPUTS", "warn"),
    "coin_off": ("COIN OFF", "off"),
    "no_model": ("NO PRICE", "off"),
    "model_error": ("MODEL ERROR", "down"),
    "switched_off": ("SWITCHED OFF", "off"),
}
# Decision rows written before the actions were renamed.
LEGACY_ACTIONS = {"bid": ORDERS, "no_bid": NO_ORDER}

# The runner's executor states (``executor.PAPER_STATE`` and friends).
EXECUTOR_PILLS: dict[str, tuple[str, str]] = {
    "paper": ("PAPER", "paper"),
    "live_not_authorised": ("LIVE · NOT BUILT / NOT AUTHORISED", "live"),
    "kill_switch": ("KILL SWITCH", "down"),
    "mode_unknown": ("MODE UNKNOWN", "warn"),
}
LIVE_MESSAGE = (
    "LIVE is selected, but this strategy has no live order path: it is not built, and live "
    "trading is not authorised for any market. No new orders are placed; paper orders already "
    "filled keep settling."
)

# What the runner's overall state means, when it is worth a line.
RUNNER_STATES: dict[str, tuple[str, str]] = {
    "not_started": ("The loop has not run yet in this process.", "warn"),
    "setting_up": ("Setting up: no new orders until the dials are seeded and orders left over "
                   "from a restart are stopped.", "warn"),
    "no_executor": ("No executor this pass, so no new orders.", "warn"),
    "pass_failed": ("The last pass failed before it finished (see the errors); the next pass "
                    "tries again.", "down"),
    "stopped": ("The loop has stopped: no fills are checked, no window settles and no order "
                "is placed until it starts again.", "warn"),
    "stopped_on_error": ("The loop died on an error (see the errors): no fills are checked, no "
                         "window settles and no order is placed until the app restarts.",
                         "down"),
}
# Loop states that also get a pill in the header, so they are seen with the card folded.
LOOP_PILLS: dict[str, tuple[str, str]] = {
    "stopped_on_error": ("LOOP DIED", "down"),
    "stopped": ("LOOP STOPPED", "warn"),
    "pass_failed": ("PASS FAILED", "down"),
}

# Why ended windows have not settled yet (``executor.SettleReport``), in plain words.
SETTLE_WAITING = (
    ("resolution", "waiting for the venue's result"),
    ("tape", "waiting for the trade tape"),
    ("retry_later", "looked up again after a failed lookup"),
    ("no_market_id", "with no market id recorded (cannot settle)"),
    ("backlog", "left for the next pass"),
)

DIAL_SOURCES = {
    "prior": "the prior (follows the market)",
    "fit_sep17_20": "the starting fit (Sep 17-20 tape)",
    "live": "learned from settled windows",
}

SPOT_FEEDS = {"chainlink": "Chainlink", "binance": "Binance"}

# The waterfall: points of Up chance from each cause, in order. They add up to 100 (p - 1/2).
WATERFALL = (
    ("leg_pts", "Move so far"),
    ("snapback_pts", "Snap-back"),
    ("momentum_pts", "1h momentum"),
    ("anchor_pts", "Market anchor"),
)

# The options the maths weighs (``decide`` factors ``growth_<action>_<side>``), in order.
GROWTH_OPTIONS = (
    ("growth_buy_up", "buy Up"),
    ("growth_buy_down", "buy Down"),
    ("growth_sell_up", "sell Up held"),
    ("growth_sell_down", "sell Down held"),
)

_ROUND = 6
_EPS = 1e-9


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


def _maps(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [v for v in value if isinstance(v, Mapping)]


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


def _span(seconds: float) -> str:
    d = max(0.0, seconds)
    if d < 90:
        return f"{d:.0f}s"
    if d < 5400:
        return f"{d / 60:.0f}m"
    return f"{d / 3600:.1f}h"


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


def _usd(v: Any) -> str:
    return f"${_f(v) or 0.0:,.2f}"


def _growth(g: Any) -> str:
    """Expected log growth an option adds to the account, as a percent (or "nothing")."""
    v = _f(g)
    if v is None:
        return "—"
    if v <= _EPS:
        return "nothing"
    return f"+{100.0 * v:.3g}%"


def _pill(label: str, css: str) -> str:
    return f"<span class='pill {css}'>{escape(label)}</span>"


def _kv(label: str, value: str, sub: str = "") -> str:
    """One labelled line: ``value`` is trusted HTML, ``sub`` plain text."""
    return (
        f"<div><span>{escape(label)}</span><b>{value}"
        + (f"<span class='fade-sub'>{escape(sub)}</span>" if sub else "")
        + "</b></div>"
    )


def _line(text: str, tone: str = "") -> str:
    """One plain-text line under a coin (escaped here)."""
    return f"<div class='fade-reason{' ' + tone if tone else ''}'>{escape(text)}</div>"


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n:,} {one if n == 1 else (many or one + 's')}"


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
                refusals.setdefault(key, str(row.get("reason") or "The orders were refused."))
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


def depth_ahead_now(order: Mapping[str, Any]) -> tuple[float | None, list[tuple[float, float]]]:
    """An open order's depth still ahead of it (shares), and the price levels it sits in.

    The ledger keeps the depth ahead level by level (``levels_ahead_json``) and uses each level
    up as the tape trades through it; an order written without levels has its whole
    ``depth_ahead`` at its own price.
    """
    levels: list[tuple[float, float]] = []
    text = order.get("levels_ahead_json")
    if isinstance(text, str) and text:
        try:
            for px, size in json.loads(text):
                p, n = _f(px), _f(size)
                if p is not None and n is not None and n > 0:
                    levels.append((p, n))
        except (TypeError, ValueError):
            levels = []
        else:
            return math.fsum(n for _, n in levels), levels
    depth = _f(order.get("depth_ahead"))
    price = _f(order.get("price"))
    if depth is not None and price is not None and depth > 0:
        levels = [(price, depth)]
    return depth, levels


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
    sold = _f(summary.get("sold_shares")) or 0.0
    proceeds = _f(summary.get("sale_proceeds_usd")) or 0.0
    exposure = _f(summary.get("open_exposure_usd")) or 0.0
    open_windows = int(_f(summary.get("open_windows")) or 0)
    resting = _f(summary.get("resting_usd")) or 0.0
    resting_n = int(_f(summary.get("resting_orders")) or 0)
    offered = _f(summary.get("resting_sell_shares")) or 0.0
    net_sub = "settled; passive fills pay no fee"
    if sold > 0:
        net_sub += f"; {sold:,.2f} shares sold before settling for ${proceeds:,.2f}"
    resting_sub = f"{_plural(resting_n, 'child order')} resting (${resting:,.2f} of buys"
    resting_sub += f", {offered:,.2f} shares offered)" if offered > 0 else ")"
    return (
        "<div class='fade-strip'>"
        + s.stat("Net P&L", s.money(net, True), s.cls(net), net_sub)
        + s.stat("Return on staked", s.pct(ret, True) if ret is not None else "—", s.cls(ret),
                 f"on ${staked:,.2f} staked" if staked else "nothing settled yet")
        + s.stat("Per share", f"{cps:+.2f}c" if cps is not None else "—", s.cls(cps),
                 f"over {shares:,.2f} settled shares bought" if shares else "")
        + s.stat("Max drawdown", f"-${dd:,.2f}" if dd > 0 else s.money(0.0),
                 "down" if dd > 0 else "flat", "window by window, from the peak")
        + s.stat("Settled windows", f"{settled:,}", "",
                 f"{int(_f(summary.get('windows_observed')) or 0):,} windows seen in all")
        + s.stat("Open exposure", s.money(exposure), "",
                 f"{_plural(open_windows, 'window')} unsettled · {resting_sub}")
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


def _errors_this_pass(status: Mapping[str, Any]) -> list[str]:
    out: list[str] = []
    for e in status.get("errors") or []:
        text = str(e or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def _state_lines(status: Mapping[str, Any], enabled: bool | None, mode: str | None,
                 dials: Mapping[str, Any] | None, now: float, poll_s: float | None) -> str:
    lines: list[str] = []
    _, css, message = _executor_view(status, mode)
    if message:
        lines.append(f"<div class='{'down' if css in ('live', 'down') else 'warn'}'>"
                     f"{escape(message)}</div>")
    if enabled is False:
        lines.append("<div class='warn'>Switched off on MY STRATEGIES: no new orders. Fills "
                     "and settlement keep running.</div>")

    state = str(status.get("state") or "not_started")
    if state in RUNNER_STATES:
        text, tone = RUNNER_STATES[state]
        lines.append(f"<div class='{tone}'>{escape(text)}</div>")

    last = _f(status.get("last_pass_ts"))
    earlier = bool(status.get("from_earlier_run"))
    if last is not None:
        passes = int(_f(status.get("passes")) or 0)
        lines.append(f"<div>Last pass {_ago(last, now)}, {_clock(last)}"
                     + (f" · pass {passes:,}" if passes else "")
                     + (" (from an earlier run of the app)" if earlier else "") + "</div>")
        interval = poll_s if poll_s is not None and poll_s > 0 else DEFAULT_POLL_S
        if (not earlier and state not in LOOP_PILLS
                and now - last > LATE_PASSES * interval + 60.0):
            lines.append(f"<div class='warn'>No pass for {_span(now - last)}, though one runs "
                         f"every {interval:.0f}s: the loop may be stuck.</div>")
    else:
        lines.append("<div>No pass has run yet.</div>")

    errors = _errors_this_pass(status)
    error = status.get("last_error")
    if errors:
        head = "An error" if len(errors) == 1 else f"{len(errors)} errors"
        if state in LOOP_PILLS or last is None:
            # Outside a finished pass (the loop died, or a pass broke off): say when.
            if _f(status.get("last_error_ts")) is not None:
                head += f" {_ago(status.get('last_error_ts'), now)}"
        else:
            head += " in the latest pass"
        lines.append(f"<div class='down'>{escape(head)}:</div>")
        for e in errors[:MAX_ERRORS]:
            lines.append(f"<div class='down fade-err'>{escape(e)}</div>")
        if len(errors) > MAX_ERRORS:
            lines.append(f"<div class='down fade-err'>and {len(errors) - MAX_ERRORS} more</div>")
    elif error:
        lines.append(f"<div class='down'>Last error {_ago(status.get('last_error_ts'), now)}: "
                     f"{escape(str(error))} · the latest pass ran clean</div>")
    else:
        lines.append("<div>No errors.</div>")

    facts: list[str] = []
    bank = _map(status.get("bankroll"))
    start, free = _f(bank.get("start_usd")), _f(bank.get("free_usd"))
    if start is not None:
        facts.append(f"bankroll ${start:,.2f} to start"
                     + (f", ${free:,.2f} free to buy with" if free is not None else ""))
    if dials:
        version = dials.get("version")
        source = str(dials.get("source") or "")
        n_windows = int(_f(dials.get("n_windows")) or 0)
        facts.append(f"dials v{escape(str(version))}: "
                     f"{escape(DIAL_SOURCES.get(source, source or 'unknown'))}"
                     + (f", {n_windows:,} windows" if n_windows else ""))
    waiting = _map(status.get("settle_waiting"))
    for key, words in SETTLE_WAITING:
        n = int(_f(waiting.get(key)) or 0)
        if n:
            facts.append(f"{_plural(n, 'ended window')} {words}")
    if facts:
        lines.append("<div>" + " · ".join(facts) + "</div>")
    note = status.get("learn_note")
    if note:
        lines.append(f"<div>Last learning step: {escape(str(note))}</div>")
    return "<div class='fade-state'>" + "".join(lines) + "</div>"


# ---------------------------------------------------------------------------
# One coin: inputs and chances
# ---------------------------------------------------------------------------


def _spot_feed(inputs: Mapping[str, Any]) -> str:
    feed = str(_map(inputs.get("settings")).get("spot_feed") or "chainlink")
    return "chainlink" if feed.startswith("chainlink") else feed


def _inputs_col(inputs: Mapping[str, Any], factors: Mapping[str, Any]) -> str:
    der = _map(inputs.get("derived"))
    rows: list[str] = []

    # The move so far as the model sees it: the live price against the price to beat.
    feed = _spot_feed(inputs)
    leg_pct = _f(factors.get("leg_pct"))
    if leg_pct is not None:
        d = leg_pct / 100.0
    else:
        d = _f(der.get("d_binance" if feed == "binance" else "d"))
    leg_sub = (f"{SPOT_FEEDS.get(feed, feed)} now {_px(_map(inputs.get(feed)).get('value'))} "
               f"against the price to beat {_px(inputs.get('start_ref'))}, the TWAP-60s print "
               "at the open")
    close = _f(der.get("close_abar"))
    if close is not None:
        leg_sub += (f"; in the closing minute the 60-second average it settles on is "
                    f"{_pct(close)} so far")
    rows.append(_kv("15m move", "flat" if d is not None and abs(d) < 5e-7 else _pct(d),
                    leg_sub))

    sigma, h = _f(der.get("sigma")), _f(der.get("h"))
    if h is None and _f(factors.get("minutes_left")) is not None:
        h = (_f(factors.get("minutes_left")) or 0.0) / 60.0
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
        pull = ("the snap-back pulls toward Down" if stretch > 0 else
                "the snap-back pulls toward Up" if stretch < 0 else "no pull either way")
        rows.append(_kv("Stretch", _pct(stretch),
                        f"how far the last twelve 15m candles ran, the latest weighted most; "
                        f"{pull}"))

    rows.append(_kv("1h move", _pct(der.get("x")),
                    f"Binance since the hour opened at {_px(inputs.get('hour_open'))}; the 1h "
                    "market settles on this candle"))
    hour_up = _f(der.get("hour_up"))
    rows.append(_kv("1h market", f"Up {_cents(hour_up)}" if hour_up is not None else "—",
                    f"bid {_cents(inputs.get('hour_up_bid'))} / ask "
                    f"{_cents(inputs.get('hour_up_ask'))}"))
    trend = der.get("mu_l") if der.get("mu_l") is not None else factors.get("mu_L")
    rows.append(_kv("Trend", _pct(trend), "the coin over the last hour"))
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
    if market is None:
        market = _f(factors.get("market_up"))
    p_model, p = _f(row.get("p_model")), _f(row.get("p"))
    traded_sub = "the model anchored to the market by the dials"
    if p is not None and market is not None:
        traded_sub += f"; {100.0 * (p - market):+.1f} pts against the market's price"
    rows = [
        _kv("Model", f"Up {_prob(p_model)}" if p_model is not None else "—",
            "the maths' chance the window settles Up"),
        _kv("Market", f"Up {_cents(market)}" if market is not None else "—",
            f"the 15m Up book's mid: bid {_cents(up_book.get('best_bid'))} / ask "
            f"{_cents(up_book.get('best_ask'))}"),
        _kv("Traded on", f"Up {_prob(p)}" if p is not None else "—", traded_sub),
    ]
    return ("<div class='de-col'><div class='de-h'>Chances</div>"
            f"<div class='fade-kv'>{''.join(rows)}</div>"
            f"{_waterfall(factors, p)}</div>")


# ---------------------------------------------------------------------------
# One coin: the order
# ---------------------------------------------------------------------------


def _order_side(item: Mapping[str, Any]) -> str:
    side = str(item.get("order_side") or "").upper()
    if side in ("BUY", "SELL"):
        return side
    return "BUY"  # rows written before sells existed were all buys


def _key(order_side: str, side: Any, price: Any) -> tuple[str, str, float]:
    return (order_side, str(side or ""), round(_f(price) or 0.0, _ROUND))


def _chosen(row: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """(action "buy" | "sell" | "none", side) of the decision's order; (None, None) when the
    row carries no decision from the maths."""
    order = _map(_map(row.get("factors")).get("order"))
    action = order.get("action")
    if action in ("buy", "sell", "none"):
        side = order.get("side") or row.get("side")
        return str(action), (str(side) if side else None)
    if row.get("p") is None:
        return None, None
    # A row written before the order was recorded: its child orders say what it was.
    children = _maps(row.get("child_orders"))
    if row.get("side") and children:
        return ("sell" if _order_side(children[0]) == "SELL" else "buy"), str(row["side"])
    return "none", None


def _held(row: Mapping[str, Any], positions: Sequence[Mapping[str, Any]]
          ) -> tuple[str | None, float]:
    """The side and the net shares held in the window: from the fills, else from what the
    maths was told."""
    for p in positions:
        n = _f(p.get("shares")) or 0.0
        if n > _EPS:
            return str(p.get("side") or ""), n
    record = _map(row.get("hedge"))
    if record.get("held_side") and (_f(record.get("held_shares")) or 0.0) > _EPS:
        return str(record["held_side"]), float(_f(record.get("held_shares")) or 0.0)
    return None, 0.0


def _headline(action: str | None, side: str | None, n_orders: int, held_side: str | None,
              held: float) -> str:
    if action in ("buy", "sell") and n_orders == 0:
        return (f"{action.capitalize()} {side} adds growth, but no child order is left to place "
                "after the sizing.")
    if action == "buy":
        what = (f"Buy {side}: a scaled passive limit order, "
                f"{_plural(n_orders, 'child order')} at price levels at or under the best bid")
        if held_side == side and held > 0:
            what += f", adding to the {held:,.2f} held"
        return what + "."
    if action == "sell":
        return (f"Sell {side}: reduce the {held:,.2f} {side} shares held with a resting sell "
                "at or over the best ask.")
    if action == "none":
        if held_side and held > 0:
            return (f"No order: neither adding to the {held:,.2f} {held_side} shares held nor "
                    "selling them adds growth.")
        return "No order: neither side adds growth."
    return "No decision from the maths, so no orders."


def _growth_line(factors: Mapping[str, Any]) -> str:
    parts = [f"{label} {_growth(factors.get(key))}" for key, label in GROWTH_OPTIONS
             if _f(factors.get(key)) is not None]
    if not parts:
        return ""
    return _line("Expected growth each option adds to the account: " + " · ".join(parts) + ".")


def _order_row(order_side: str, side: Any, price: Any, shares: Any, depth: float | None,
               levels: Sequence[tuple[float, float]], p_fill: Any, q_fill: Any,
               state: str) -> str:
    verb = "Sell" if order_side == "SELL" else "Buy"
    title = " · ".join(f"{_sh(n)} at {_cents(px)}" for px, n in levels)
    depth_html = f"{depth:,.0f}" if depth is not None else "—"
    if title:
        depth_html = f"<span title='{escape(title)}'>{depth_html}</span>"
    return (f"<tr><td>{verb} {escape(str(side or ''))} at {_cents(price)}</td>"
            f"<td>{_sh(shares)}</td><td>{depth_html}</td><td>{_prob(p_fill)}</td>"
            f"<td>{_prob(q_fill)}</td><td>{escape(state)}</td></tr>")


def _orders_table(row: Mapping[str, Any], action: str, orders: Sequence[Mapping[str, Any]],
                  live: Mapping[str, Any] | None) -> str:
    planned: dict[tuple[str, str, float], Mapping[str, Any]] = {}
    for c in _maps(row.get("child_orders")):
        if (_f(c.get("shares")) or 0.0) > _EPS:
            planned.setdefault(_key(_order_side(c), c.get("side") or row.get("side"),
                                    c.get("price")), c)

    def rank(o: Mapping[str, Any]) -> tuple[bool, float, float, float]:
        price = _f(o.get("price")) or 0.0
        sell = _order_side(o) == "SELL"
        return (sell, price if sell else -price, _f(o.get("placed_ts")) or 0.0,
                _f(o.get("id")) or 0.0)

    rows: list[str] = []
    matched: set[tuple[str, str, float]] = set()
    for o in sorted(orders, key=rank):
        key = _key(_order_side(o), o.get("side"), o.get("price"))
        plan = planned.get(key, {})
        matched.add(key)
        filled = _f(o.get("filled_shares")) or 0.0
        remaining = max(0.0, (_f(o.get("shares")) or 0.0) - filled)
        depth, levels = depth_ahead_now(o)
        state = "resting" if filled <= _EPS else f"resting, {_sh(filled)} filled"
        rows.append(_order_row(key[0], o.get("side"), o.get("price"), remaining, depth, levels,
                               plan.get("p_fill"), plan.get("q_fill"), state))
    this_pass = live if live and live.get("decision_id") == row.get("id") else {}
    if action == "not_placed":
        missing = "not placed"
    elif action == "refused":
        missing = "refused"
    elif this_pass.get("held_back"):
        missing = "held back"
    else:
        missing = "not resting"
    for key, c in planned.items():
        if key in matched:
            continue
        depth = _f(c.get("depth_ahead"))
        rows.append(_order_row(key[0], key[1], c.get("price"), c.get("shares"), depth, (),
                               c.get("p_fill"), c.get("q_fill"), missing))
    if not rows:
        return ""
    return ("<table class='de-tail-tbl fade-orders'><thead><tr><th>Child order</th>"
            "<th>Shares</th><th>Depth ahead</th><th>Fill chance</th><th>Wins if filled</th>"
            "<th>State</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>")


def _sizing_line(row: Mapping[str, Any], chosen: str | None) -> str:
    stake, kelly = _f(row.get("stake_usd")), _f(row.get("kelly_f"))
    if chosen != "buy" or not stake:
        return ""
    sizing = _map(_map(row.get("factors")).get("sizing"))
    bank = _f(sizing.get("bankroll_usd"))
    text = f"The buys cost ${stake:,.2f} if every child order fills"
    if kelly is not None and bank:
        text += (f": {100.0 * kelly:.1f}% of the ${bank:,.2f} this coin sized from (the free "
                 "cash plus what the other coins hold)")
    joint = _f(sizing.get("joint_scale"))
    if joint is not None and joint < 1.0 - 1e-6:
        text += f"; sized down to {joint:.0%} with the other coins buying in this pass"
    return _line(text + ".")


def _pass_lines(row: Mapping[str, Any], live: Mapping[str, Any] | None) -> list[str]:
    if not live or live.get("decision_id") != row.get("id"):
        return []
    out: list[str] = []
    counts = [f"{int(_f(live.get(k)) or 0)} {w}" for k, w in
              (("kept", "kept in the queue"), ("placed", "placed"), ("cancelled", "cancelled"))
              if live.get(k) is not None]
    if counts:
        out.append(_line("This pass: " + ", ".join(counts) + "."))
    for text in live.get("held_back") or []:
        if text:
            out.append(_line(f"Held back: {text}", "warn"))
    return out


def _position_lines(row: Mapping[str, Any], positions: Sequence[Mapping[str, Any]]
                    ) -> list[str]:
    out: list[str] = []
    for p in positions:
        side = escape(str(p.get("side") or ""))
        bought = _f(p.get("bought_shares"))
        if bought is None:  # a row from before sells existed
            bought = _f(p.get("shares")) or 0.0
        parts = [f"bought {_sh(bought)} at avg {_cents(p.get('avg_price'))} for "
                 f"{_usd(p.get('cost_usd'))}"]
        sold = _f(p.get("sold_shares")) or 0.0
        if sold > _EPS:
            parts.append(f"sold {_sh(sold)} for {_usd(p.get('proceeds_usd'))}")
        offered = _f(p.get("offered_shares")) or 0.0
        if offered > _EPS:
            parts.append(f"{_sh(offered)} offered for sale")
        out.append(f"<div class='fade-reason'>Held in this window: {_sh(p.get('shares'))} "
                   f"{side} ({'; '.join(parts)}).</div>")
    record = _map(row.get("hedge"))
    if not record.get("held_side"):
        return out
    if not positions:
        out.append(_line(f"Held in this window: {_sh(record.get('held_shares'))} "
                         f"{record['held_side']}."))
    sale = _map(record.get("sell"))
    if sale:
        out.append(_line(
            f"Reducing it: a resting sell of {_sh(sale.get('shares'))} "
            f"{sale.get('side') or record['held_side']} at {_cents(sale.get('price'))}, sized "
            f"by the maths and never more than the shares held; it fills "
            f"{_prob(sale.get('p_fill'))} of the time, and "
            f"{sale.get('side') or record['held_side']} still wins "
            f"{_prob(sale.get('q_fill'))} if it does."))
    elif record.get("note"):
        out.append(_line(str(record["note"])))
    return out


def _orders_col(row: Mapping[str, Any], action: str, orders: Sequence[Mapping[str, Any]],
                positions: Sequence[Mapping[str, Any]], live: Mapping[str, Any] | None,
                factors: Mapping[str, Any]) -> str:
    chosen, side = _chosen(row)
    held_side, held = _held(row, positions)
    n_orders = sum(1 for c in _maps(row.get("child_orders"))
                   if (_f(c.get("shares")) or 0.0) > _EPS)
    heading = {"buy": f"Buy {side}", "sell": f"Sell {side}", "none": "No order"}.get(
        chosen or "", "")
    lines = [f"<div class='fade-choice'>{escape(_headline(chosen, side, n_orders, held_side, held))}"
             "</div>"]
    table = _orders_table(row, action, orders, live)
    if table:
        lines.append(table)
    lines.append(_growth_line(factors))
    lines.append(_sizing_line(row, chosen))
    lines.extend(_pass_lines(row, live))
    lines.extend(_position_lines(row, positions))
    return ("<div class='de-col'><div class='de-h'>Orders"
            + (f" · {escape(heading)}" if heading else "")
            + "</div>" + "".join(lines) + "</div>")


# ---------------------------------------------------------------------------
# One coin
# ---------------------------------------------------------------------------


def _action_pill(action: str, row: Mapping[str, Any]) -> str:
    action = LEGACY_ACTIONS.get(action, action)
    if action == ORDERS:
        chosen, side = _chosen(row)
        if chosen in ("buy", "sell") and side:
            return _pill(f"{chosen.upper()} {side.upper()}", "on")
    if action in ACTIONS:
        label, css = ACTIONS[action]
        return _pill(label, css)
    return _pill(action.upper(), "warn") if action else ""


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
    action = LEGACY_ACTIONS.get(action, action)
    if refused:
        action = "refused"
    if fresher:
        action = str(live.get("action") or action)
    pill = _action_pill(action, row)
    if pill:
        head.append(pill)

    if fresher and live.get("reason") and live.get("action") != row.get("action"):
        body.append(_line(str(live["reason"]), "warn"))
    if live is not None and live.get("error"):
        body.append(_line(str(live["error"]), "down"))

    if current:
        factors_all = _map(row.get("factors"))
        chosen, _ = _chosen(row)
        if inputs.get("status") == "ok":
            factors = _map(factors_all.get("model"))
            here_orders = [o for o in orders if o.get("window_slug") == slug]
            here_pos = [p for p in positions if p.get("window_slug") == slug]
            body.append("<div class='fade-grid'>"
                        + _inputs_col(inputs, factors)
                        + _chances_col(row, inputs, factors)
                        + _orders_col(row, action, here_orders, here_pos, live, factors)
                        + "</div>")
            story = factors_all.get("explanation")
            if story:
                body.append(f"<div class='fade-story'>{escape(str(story))} "
                            f"<a class='strategy-doc-link' href='{DOCS_URL}' target='_blank' "
                            "rel='noopener'>How the maths works</a></div>")
        reason = row.get("reason")
        # "No order" is already the column's headline; any other reason is said here.
        if reason and not fresher and not (row.get("action") == NO_ORDER and chosen == "none"):
            tone = "warn" if row.get("action") in ("no_inputs", "not_placed") else ""
            body.append(_line(str(reason), tone))
        if inputs.get("status") == "problem":
            also = [str(_map(a).get("message") or "") for a in (inputs.get("also") or [])]
            also = [m for m in also if m]
            if also:
                body.append(_line("Also: " + " ".join(also), "warn"))
        # A warning is a failure that did not stop the coin (a database read or write); a
        # note is a plain fact about the inputs (a default that was assumed).
        for warning in inputs.get("warnings") or []:
            if warning:
                body.append(_line(f"Warning: {warning}", "warn"))
        for note in inputs.get("notes") or []:
            if note:
                body.append(_line(f"Note: {note}"))
        if refused:
            body.append(_line(f"Refused: {refused}", "down"))

    elsewhere = [o for o in orders if o.get("window_slug") != slug]
    if elsewhere:
        body.append(_line(f"{_plural(len(elsewhere), 'child order')} resting in another "
                          "window.", "warn"))
    waiting = [p for p in positions if p.get("window_slug") != slug]
    if waiting:
        held = math.fsum(_f(p.get("shares")) or 0.0 for p in waiting)
        cost = math.fsum((_f(p.get("cost_usd")) or 0.0) - (_f(p.get("proceeds_usd")) or 0.0)
                         for p in waiting)
        body.append(_line(f"Held in ended windows, awaiting the result: {_sh(held)} shares "
                          f"(${cost:,.2f} net cost)."))
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
    poll_s: float | None = None,
    load_error: str | None = None,
    now: float | None = None,
) -> str:
    """The card's HTML.

    ``summary``: ``ledger.summary()``. ``decisions``: decision rows, any order, from
    ``ledger.latest_decisions()`` plus a few ``recent_decisions`` (so a refusal row can be
    matched to the decision it refused). ``orders``: ``ledger.open_orders()``. ``positions``:
    ``ledger.open_positions()``. ``status``: ``runner.status()``. ``dials``:
    ``ledger.dials()``. ``enabled``: the strategy switch. ``mode``: the global PAPER/LIVE
    selection (used until the runner has reported its own state). ``poll_s``: the pass
    interval from Settings (to tell a late pass). ``load_error``: a read that failed, shown
    on the card rather than hidden.
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
    loop = LOOP_PILLS.get(str(status.get("state") or ""))
    head = (f"<summary class='card-h'><span class='fold-title'>{TITLE}</span>"
            f"<span class='win fade-pills'>{_pill(label, css)} {switch} "
            + (_pill(*loop) + " " if loop else "")
            + f"<span>last pass {_ago(live_ts, now) if live_ts is not None else 'not yet'}"
            "</span></span></summary>")

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
        + _state_lines(status, enabled, mode, dials, now, poll_s)
        + "".join(blocks)
        + "<div class='fade-foot'>Paper only: every child order rests on the passive side of "
          "the book (a buy at or under the best bid, a sell at or over the best ask) and fills "
          "only when the real trade tape reaches it, after the depth ahead. "
          f"<a class='strategy-doc-link' href='{DOCS_URL}' target='_blank' rel='noopener'>"
          "How this strategy works</a></div>"
        + "</details>"
    )
