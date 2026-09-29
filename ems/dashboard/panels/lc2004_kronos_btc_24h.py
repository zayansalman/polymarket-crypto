"""lc2004-Kronos BTC 24h forecast card: the model's chance that today's daily BTC market settles Up.

Display only: the card places no orders and has no controls. The operator reads it and trades
by hand on Polymarket (Zayan (operator), 2026-09-29: "this doesnt need to trade for me actually
just show its prediction in the regime overview or somewhere in the dashboard and ill go
manaulla place a trade").

Top to bottom: anything wrong, in plain words (switched off, missing weights with the one-line
fix, a failed run, what the loop is waiting for, the last error); the headline (the model's
chance of Up and how many of the 30 paths end above the strike); the market now (both books,
and the model's fair value minus each ask in cents, a measurement and not a rule); the window
(market link, strike, the last BTC close the model saw, time to settle, hours ahead); the
spread of the 30 paths against the strike; when the forecast was made and when the next one
runs; and this window's forecast for every hour so far.

Pure ``render(...) -> str``, no DB and no awaits: ``execution_view.lc2004_forecast_data`` reads
the ledger, the runner's status and the Settings switch and passes them in. Every value is
HTML-escaped. Times are ET, the market's own clock (the window runs noon to noon ET).

Claude, 2026-09-29.
"""

from __future__ import annotations

import math
import time
from collections.abc import Mapping, Sequence
from datetime import datetime
from html import escape
from typing import Any
from zoneinfo import ZoneInfo

from ems.lc2004_kronos_btc_24h.maths import PATHS, path_spread

TITLE = "lc2004-Kronos BTC 24h forecast"
FOLD = "lc2004-kronos-btc-24h"
MARKET_URL = "https://polymarket.com/event/"
ET = ZoneInfo("America/New_York")
HOUR_S = 3600
MAX_ERRORS = 6
ERROR_CELL_CHARS = 90

LOOP_STATES: dict[str, tuple[str, str]] = {
    "not_started": ("The forecast loop has not run yet in this process (its first pass is "
                    "15 s after the app starts).", "warn"),
    "pass_failed": ("A step of the last pass failed (see below); the next pass tries again.",
                    "down"),
    "stopped": ("The forecast loop has stopped: nothing new is forecast until the app "
                "restarts.", "warn"),
    "stopped_on_error": ("The forecast loop died on an error (see below): nothing new is "
                         "forecast until the app restarts.", "down"),
}
LOOP_PILLS: dict[str, tuple[str, str]] = {
    "pass_failed": ("PASS FAILED", "down"),
    "stopped": ("LOOP STOPPED", "warn"),
    "stopped_on_error": ("LOOP DIED", "down"),
}
# The worker's own "missing library" text (ems/kronos_forecast/worker.py).
MISSING_LIBRARY = "Kronos worker is missing"
KRONOS_PYTHON_FIX = ("Set KRONOS_PYTHON in .env to the absolute path of a Python that has "
                     "torch and einops installed outside the user site (see .env.example), "
                     "then restart the app.")


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


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
    return f"<span class='pill {escape(css)}'>{escape(label)}</span>"


def _et(ts: Any, fmt: str = "%H:%M ET") -> str:
    t = _f(ts)
    if t is None:
        return "—"
    return datetime.fromtimestamp(t, ET).strftime(fmt)


def _span(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < HOUR_S:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // HOUR_S}h {(s % HOUR_S) // 60:02d}m"


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


def _usd(value: Any, signed: bool = False) -> str:
    v = _f(value)
    if v is None:
        return "—"
    return f"{'+' if signed and v > 0 else '-' if v < 0 else ''}${abs(v):,.2f}"


def _price(value: Any) -> str:
    """A book price to the market's tick: 0.53, or 0.004 once the tick drops to 0.001.

    Two places for a whole cent, three otherwise, so a sub-cent price near the extremes is
    never rounded to one that is not in the book (Claude, 2026-09-29, for a review finding).
    """
    v = _f(value)
    if v is None:
        return "—"
    text = f"{v:.3f}"
    return text[:-1] if text.endswith("0") else text


def _pct(value: Any, digits: int = 0) -> str:
    v = _f(value)
    return "—" if v is None else f"{100 * v:.{digits}f}%"


def _sign_class(value: float | None) -> str:
    return "pos" if value is not None and value > 0 else "neg" if value is not None and (
        value < 0) else ""


def _kv(label: str, value: str, sub: str = "") -> str:
    """One labelled line: ``value`` is trusted HTML, ``sub`` plain text."""
    return (f"<div><span>{escape(label)}</span><b>{value}"
            + (f"<span class='lk-sub'>{escape(sub)}</span>" if sub else "") + "</b></div>")


def _line(text: str, tone: str = "") -> str:
    return f"<div class='{escape(tone)}'>{escape(text)}</div>"


def _candle_close(row: Mapping[str, Any]) -> float | None:
    """When the forecast's last 1h candle closed: the hour the forecast speaks from."""
    open_ms = _f(row.get("last_candle_open_ms"))
    return None if open_ms is None else open_ms / 1000 + HOUR_S


def _ok(row: Mapping[str, Any]) -> bool:
    return not row.get("error") and _f(row.get("q_up")) is not None


# ---------------------------------------------------------------------------
# Pieces
# ---------------------------------------------------------------------------


def _problems(*, status: Mapping[str, Any], current: bool, enabled: bool | None,
              missing_weights: str | None, latest: Mapping[str, Any] | None,
              load_error: str | None, now: float) -> str:
    lines = []
    if load_error:
        lines.append(_line(f"Could not read the forecasts: {load_error}", "down"))
    if enabled is False:
        lines.append(_line("Switched off in Settings: no forecast runs until it is switched "
                           "back on.", "warn"))
    state = str(status.get("state") or "not_started")
    if state in LOOP_STATES and not (enabled is False and state == "not_started"):
        text, tone = LOOP_STATES[state]
        lines.append(_line(text, tone))
    if missing_weights:
        lines.append(_line(f"No model weights, so no forecast runs: {missing_weights}", "down"))
    if current and enabled is not False:
        for text in status.get("waiting") or []:
            lines.append(_line(str(text), "warn"))
        if status.get("forecast_note"):
            lines.append(_line(str(status["forecast_note"]), "warn"))
    if latest is not None and latest.get("error"):
        error = str(latest["error"])
        lines.append(_line(f"The {_et(_candle_close(latest))} forecast failed: {error}", "down"))
        if any(text in error for text in (MISSING_LIBRARY, "einops", "KRONOS_PYTHON")):
            lines.append(_line(KRONOS_PYTHON_FIX, "down lk-fix"))
    errors = [str(e) for e in status.get("errors") or []]
    for error in errors[:MAX_ERRORS]:
        lines.append(_line(error, "down lk-err"))
    if len(errors) > MAX_ERRORS:
        lines.append(_line(f"and {len(errors) - MAX_ERRORS} more", "down lk-err"))
    if not errors and status.get("last_error"):
        lines.append(_line(f"Last error ({_ago(status.get('last_error_ts'), now)}): "
                           f"{status['last_error']}", "warn"))
    return "<div class='lk-state'>" + "".join(lines) + "</div>" if lines else ""


def _no_forecast_yet(*, status: Mapping[str, Any], current: bool, enabled: bool | None,
                     missing: str | None, strike: float | None) -> str:
    """Why this window has no forecast yet, in the order that actually blocks it."""
    if enabled is False:
        return "The forecast is switched off in Settings."
    if missing:
        return "The first one starts within a minute of the model weights being installed."
    if current and status.get("forecast_running"):
        return "The first one is running now; it takes a few minutes."
    if strike is None:
        return ("The first one runs once the 12:00 ET noon candle has closed and the strike "
                "is known.")
    if current and status.get("next_forecast_after"):
        return f"The next one starts after {_et(status['next_forecast_after'])}."
    return "The first one starts on the next pass."


def _headline(good: Mapping[str, Any] | None, latest: Mapping[str, Any] | None,
              why_none: str = "") -> str:
    if good is None:
        return ("<div class='lk-headline lk-none'>Model: no forecast for this window yet."
                f"</div><div class='lk-sub'>{escape(why_none)}</div>")
    q_up = _f(good.get("q_up")) or 0.0
    k = int(_f(good.get("paths_above")) or 0)
    n = int(_f(good.get("paths")) or PATHS)
    p_raw, se = _f(good.get("p_raw")), _f(good.get("sampling_se"))
    sub = [f"Down {100 * (1 - q_up):.0f}%",
           f"raw share {k}/{n} = {_pct(p_raw, 1)}"
           + (f", sampling error ±{100 * se:.1f} pts" if se is not None else ""),
           f"P(Up) = (paths above + 1) / {n + 2}"]
    if latest is not None and latest is not good and latest.get("error"):
        sub.append(f"from the {_et(_candle_close(good))} run; the newest run failed")
    return (f"<div class='lk-headline'>Model: Up {100 * q_up:.0f}% "
            f"<span>({k} of {n} paths end above the strike)</span></div>"
            f"<div class='lk-sub'>{escape(' · '.join(sub))}</div>")


def _fair_line(label: str, fair: float | None, ask: float | None) -> str:
    if fair is None:
        return _kv(f"{label} vs ask", "—", "no forecast yet")
    if ask is None:
        return _kv(f"{label} vs ask", f"model {fair:.2f} vs ask —", "no ask in the book")
    cents = round((fair - ask) * 100)
    css = _sign_class(float(cents))
    return _kv(f"{label} vs ask",
               f"model {fair:.2f} vs ask {_price(ask)}, "
               f"<span class='{css}'>{cents:+d}c</span>",
               "model's fair value minus the ask, a measurement")


def _market_now(books: Mapping[str, Any] | None, q_up: float | None, now: float) -> str:
    rows = []
    if not books:
        rows.append(_kv("Books", "not read yet", "read every minute by the forecast loop"))
        rows.append(_fair_line("Up", q_up, None))
        rows.append(_fair_line("Down", None if q_up is None else 1 - q_up, None))
        return "<div class='lk-kv'><div class='lk-h'>Market now</div>" + "".join(rows) + "</div>"
    for label, key in (("Up", "up"), ("Down", "down")):
        top = books.get(key)
        if not isinstance(top, Mapping):
            rows.append(_kv(f"{label} book", "could not be read", "tried again next minute"))
            continue
        rows.append(_kv(f"{label} book", f"bid {_price(top.get('bid'))} / ask "
                                         f"{_price(top.get('ask'))}"))
    up_ask = _f(_map(books.get("up")).get("ask"))
    down_ask = _f(_map(books.get("down")).get("ask"))
    rows.append(_fair_line("Up", q_up, up_ask))
    rows.append(_fair_line("Down", None if q_up is None else 1 - q_up, down_ask))
    rows.append(_kv("Read", _ago(books.get("ts"), now), "the book, not the model"))
    return "<div class='lk-kv'><div class='lk-h'>Market now</div>" + "".join(rows) + "</div>"


def _window_block(window: Mapping[str, Any], market: Mapping[str, Any], strike: float | None,
                  good: Mapping[str, Any] | None, now: float, strike_failed: bool) -> str:
    slug = str(market.get("slug") or window.get("slug") or "")
    link = (f"<a class='lk-link' href='{escape(MARKET_URL + slug)}' target='_blank' "
            f"rel='noopener'>{escape(slug)}</a>" if slug else "—")
    start, end = _f(window.get("start_ts")), _f(window.get("end_ts"))
    rows = [_kv("Market", link, str(market.get("question") or ""))]
    if strike is not None:
        rows.append(_kv("Strike", _usd(strike),
                        f"Binance BTCUSDT 1m close at 12:00 ET, {_et(start, '%a %b %d')}"))
    elif strike_failed:
        rows.append(_kv("Strike", "could not be read",
                        "the Binance read failed (see above); tried again next minute"))
    else:
        rows.append(_kv("Strike", "waiting", "for the 12:00 ET 1m candle to close"))
    if good is not None:
        close_ts = _candle_close(good)
        last = _f(good.get("last_close"))
        vs = (f", {_usd(last - strike, signed=True)} vs the strike"
              if last is not None and strike is not None else "")
        rows.append(_kv("Last BTC close", _usd(last),
                        f"the 1h candle closing {_et(close_ts)}{vs}"))
    if end is not None:
        rows.append(_kv("Settles", _et(end, "%a %b %d %H:%M ET"),
                        f"{_span(end - now)} left" if end > now else "ended"))
    if good is not None:
        rows.append(_kv("Hours ahead", f"{int(_f(good.get('horizon_hours')) or 0)}",
                        "hourly steps from the last close to 12:00 ET"))
    return "<div class='lk-kv'><div class='lk-h'>The window</div>" + "".join(rows) + "</div>"


def _spread_block(good: Mapping[str, Any] | None) -> str:
    rows = []
    closes = good.get("final_closes") if good is not None else None
    strike = _f(good.get("strike")) if good is not None else None
    spread = None
    if isinstance(closes, Sequence) and closes and strike is not None:
        try:
            spread = path_spread([float(c) for c in closes], strike)
        except (TypeError, ValueError):
            spread = None
    if spread is None:
        rows.append(_kv("Paths", "—", "no successful forecast in this window yet"))
    else:
        for label, key in (("10th pct", "p10"), ("Median", "p50"), ("90th pct", "p90")):
            diff = spread[f"{key}_minus_strike"]
            rows.append(_kv(label, f"{_usd(spread[key])} <span class='{_sign_class(diff)}'>"
                                   f"{_usd(diff, signed=True)}</span>",
                            "final close, and vs the strike"))
    return ("<div class='lk-kv'><div class='lk-h'>Spread of the 30 paths</div>"
            + "".join(rows) + "</div>")


def _run_block(good: Mapping[str, Any] | None, status: Mapping[str, Any], current: bool,
               enabled: bool | None, missing: str | None) -> str:
    rows = []
    if good is not None:
        secs = _f(good.get("seconds"))
        rows.append(_kv("Updated", _et(good.get("created_ts")),
                        f"worker took {secs:.0f} s" if secs is not None else ""))
    running = _map(status.get("forecast_running"))
    if running:
        candle = _f(running.get("candle_open_ms"))
        rows.append(_kv("Running", f"since {_et(running.get('started_ts'))}",
                        f"for the 1h candle closing {_et(candle / 1000 + HOUR_S)}"
                        if candle is not None else ""))
    elif (current and enabled is not False and not missing
          and status.get("next_forecast_after")):
        rows.append(_kv("Next", f"after {_et(status['next_forecast_after'])}",
                        "a few seconds after the next hourly close"))
    if not rows:
        return ""
    return "<div class='lk-kv'><div class='lk-h'>Forecast</div>" + "".join(rows) + "</div>"


def _history(rows: Sequence[Mapping[str, Any]]) -> str:
    if not rows:
        return ""
    body = []
    for row in reversed(rows):  # newest first
        if _ok(row):
            n = int(_f(row.get("paths")) or PATHS)
            above = f"{int(_f(row.get('paths_above')) or 0)} of {n}"
            p_up = _pct(row.get("q_up"))
            note = ""
        else:
            above, p_up = "failed", "—"
            note = str(row.get("error") or "no error text")[:ERROR_CELL_CHARS]
        body.append(
            "<tr>"
            f"<td>{_et(_candle_close(row))}</td>"
            f"<td>{int(_f(row.get('horizon_hours')) or 0)}</td>"
            f"<td>{escape(above)}</td>"
            f"<td>{escape(p_up)}</td>"
            f"<td>{_price(row.get('up_ask'))}</td>"
            f"<td>{_price(row.get('down_ask'))}</td>"
            f"<td class='lk-note-cell'>{escape(note)}</td>"
            "</tr>")
    return ("<div class='lk-h'>This window, hour by hour</div>"
            "<table class='lk-table'><thead><tr><th>Time (ET)</th><th>Hours left</th>"
            "<th>Paths above</th><th>P(Up)</th><th>Up ask</th><th>Down ask</th><th></th>"
            "</tr></thead><tbody>" + "".join(body) + "</tbody></table>")


# ---------------------------------------------------------------------------
# The card
# ---------------------------------------------------------------------------


def render(
    *,
    window: Mapping[str, Any] | None = None,
    status: Mapping[str, Any] | None = None,
    forecast: Mapping[str, Any] | None = None,
    history: Sequence[Mapping[str, Any]] | None = None,
    enabled: bool | None = None,
    missing_weights: str | None = None,
    load_error: str | None = None,
    now: float | None = None,
) -> str:
    """The card's HTML.

    ``window``: the current window (``slug``, ``start_ts``, ``end_ts``). ``status``:
    ``runner.status()``. ``forecast``: the window's newest forecast, failed or not.
    ``history``: every forecast of the window, oldest first. ``enabled``: the Settings
    switch. ``missing_weights``: ``kronos_forecast.client.missing_weights()``. ``load_error``:
    a read that failed, shown on the card rather than hidden.
    """
    now = time.time() if now is None else float(now)
    status = status or {}
    window = window or {}
    rows = [r for r in (history or []) if isinstance(r, Mapping)]
    latest = forecast if isinstance(forecast, Mapping) else (rows[-1] if rows else None)
    # The headline is the newest forecast that worked; a newer failed one is shown as a problem.
    good = latest if latest is not None and _ok(latest) else next(
        (r for r in reversed(rows) if _ok(r)), None)
    # The runner's status speaks for this window only while it saw the same one.
    current = bool(window.get("slug")) and _map(status.get("window")).get("slug") == (
        window.get("slug"))
    missing = missing_weights if missing_weights is not None else (
        status.get("missing_weights") if current else None)
    strike = _f(good.get("strike")) if good is not None else None
    if strike is None and current:
        strike = _f(status.get("strike"))
    market = _map(status.get("market")) if current else {}
    books = _map(status.get("books")) if current else {}
    q_up = _f(good.get("q_up")) if good is not None else None

    pills = [_pill("DISPLAY ONLY", "off")]
    if enabled is False:
        pills.append(_pill("SWITCHED OFF", "warn"))
    if status.get("forecast_running"):
        pills.append(_pill("MODEL RUNNING", "on"))
    loop = LOOP_PILLS.get(str(status.get("state") or ""))
    if loop:
        pills.append(_pill(*loop))
    head = (f"<summary class='card-h'><span class='fold-title'>{escape(TITLE)}</span>"
            f"<span class='win lk-pills'>{' '.join(pills)} "
            f"<span>last pass {_ago(status.get('last_pass_ts'), now)}</span></span></summary>")
    grid = "".join([
        _market_now(books or None, q_up, now),
        _window_block(window, market, strike, good, now,
                      current and strike is None and bool(status.get("strike_failed"))),
        _spread_block(good),
        _run_block(good, status, current, enabled, missing),
    ])
    return (
        f"<details class='card wide lk-card fold' data-fold='{FOLD}' open>"
        + head
        + "<div class='lk-display'>Display only — places no orders.</div>"
        + _problems(status=status, current=current, enabled=enabled, missing_weights=missing,
                    latest=latest, load_error=load_error, now=now)
        + "<div class='lk-block'>"
        + _headline(good, latest, _no_forecast_yet(status=status, current=current,
                                                   enabled=enabled, missing=missing,
                                                   strike=strike))
        + "</div>"
        + f"<div class='lk-grid'>{grid}</div>"
        + _history(rows)
        + "<div class='lk-foot'>Every hour the lc2004 Kronos fine-tune reads the last 512 "
          "closed Binance BTCUSDT 1h candles and samples 30 price paths to 12:00 ET. P(Up) "
          "counts the paths that end above the strike, with one path added to each side "
          "(Laplace), so 30 paths give 1/32 to 31/32. It covers the sampling noise of 30 "
          "paths, not the model's own error.</div>"
        + "</details>"
    )
