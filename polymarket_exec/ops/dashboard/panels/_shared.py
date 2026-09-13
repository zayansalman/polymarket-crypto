"""Shared rendering primitives for dashboard panels.

Formatters, class-name helpers, time-ago helpers, and the inline SVG
charts. Pure functions — no DB, no globals — so panel rendering stays
testable without spinning up the journal.
"""
from __future__ import annotations

from datetime import UTC, datetime
from html import escape
from typing import Any

# Institutional light palette: color reserved for signal (buy/sell, PnL, breach);
# everything else grayscale.
ACCENT = "#14171c"  # was Bloomberg amber; no decorative accent in the new palette — this now means "primary text/ink"
GREEN = "#1b7a43"
RED = "#b3261e"
DIM = "#6b7078"


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def money(v: float | None, signed: bool = False) -> str:
    if v is None:
        return "—"
    p = "+" if signed and v > 0 else ""
    return f"{p}${v:,.2f}"


def pct(v: float | None, signed: bool = False) -> str:
    if v is None:
        return "—"
    p = "+" if signed and v > 0 else ""
    return f"{p}{v * 100:.1f}%"


def cls(v: float | None) -> str:
    if v is None or abs(v) < 1e-9:
        return "flat"
    return "up" if v > 0 else "down"


def ago(ts: str | None) -> str:
    if not ts:
        return "never"
    try:
        p = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if p.tzinfo is None:
            p = p.replace(tzinfo=UTC)
    except ValueError:
        return ts
    a = max(0, int((datetime.now(UTC) - p).total_seconds()))
    if a < 60:
        return f"{a}s"
    if a < 3600:
        return f"{a // 60}m"
    return f"{a // 3600}h{(a % 3600) // 60}m"


def bk(v: float | None) -> str:
    return f"{v:.2f}" if v is not None else "—"


def side_mid(tick: dict[str, Any], side: str) -> float | None:
    """Current mark for a position side = mid of its book, falling back to the
    recorded market price. Mid (not the cross) is the conservative live mark
    used for open-position unrealized P&L (#113). Returns None when the side has
    no usable quote."""
    if (side or "").upper() == "UP":
        bid, ask, fallback = (
            tick.get("up_best_bid"), tick.get("up_best_ask"), tick.get("market_up_price"),
        )
    else:
        bid, ask, fallback = (
            tick.get("down_best_bid"), tick.get("down_best_ask"), tick.get("market_down_price"),
        )
    if isinstance(bid, (int, float)) and isinstance(ask, (int, float)) and ask >= bid:
        return (bid + ask) / 2.0
    if isinstance(fallback, (int, float)) and fallback > 0:
        return float(fallback)
    return None


def stat(label: str, value: str, cls_: str = "", sub: str = "", *, flash: str | None = None) -> str:
    flash_attr = f" data-flash='{flash}'" if flash else ""
    return (
        f"<div class='stat'><div class='stat-l'>{escape(label)}</div>"
        f"<div class='stat-v {cls_}'{flash_attr}>{value}</div>"
        + (f"<div class='stat-s'>{escape(sub)}</div>" if sub else "")
        + "</div>"
    )


def parse_feed_source(raw: Any) -> dict[str, str]:
    """Parse 'spot=...;ref=...;vol=...;quotes=...' into a dict."""
    if not isinstance(raw, str):
        return {}
    out: dict[str, str] = {}
    for chunk in raw.split(";"):
        if "=" not in chunk:
            continue
        k, v = chunk.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def tick_age_seconds(ts: str | None) -> int | None:
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return max(0, int((datetime.now(UTC) - parsed).total_seconds()))


# ---------------------------------------------------------------------------
# Inline SVG charts
# ---------------------------------------------------------------------------


def svg_equity(curve: list[float], w: int = 320, h: int = 84) -> str:
    """Cumulative-PnL equity curve as an SVG area+line; zero baseline marked."""
    if len(curve) < 2:
        return f"<div class='chart-empty' style='height:{h}px'>awaiting trades</div>"
    lo, hi = min(curve + [0.0]), max(curve + [0.0])
    span = (hi - lo) or 1.0
    pad = 6
    n = len(curve)

    def x(i: int) -> float:
        return pad + i * (w - 2 * pad) / (n - 1)

    def y(v: float) -> float:
        return pad + (hi - v) * (h - 2 * pad) / span

    pts = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(curve))
    zero_y = y(0.0)
    last = curve[-1]
    stroke = GREEN if last >= 0 else RED
    area = f"{pad},{y(0.0):.1f} " + pts + f" {x(n - 1):.1f},{y(0.0):.1f}"
    return (
        f"<svg viewBox='0 0 {w} {h}' class='spark' preserveAspectRatio='none'>"
        f"<polygon points='{area}' fill='{stroke}' opacity='0.10'/>"
        f"<line x1='{pad}' y1='{zero_y:.1f}' x2='{w - pad}' y2='{zero_y:.1f}' "
        f"stroke='{DIM}' stroke-width='0.5' stroke-dasharray='2 3'/>"
        f"<polyline points='{pts}' fill='none' stroke='{stroke}' stroke-width='1.6'/>"
        f"<circle cx='{x(n - 1):.1f}' cy='{y(last):.1f}' r='2.4' fill='{stroke}'/>"
        "</svg>"
    )


def svg_calibration(buckets: list[tuple[float, float, int]], s: int = 120) -> str:
    """Reliability: predicted (x) vs realized win-rate (y) vs the diagonal."""
    pad = 10
    dots = []
    for pred, real, n in buckets:
        cx = pad + pred * (s - 2 * pad)
        cy = s - pad - real * (s - 2 * pad)
        r = 1.8 + min(n, 12) * 0.35
        dots.append(f"<circle cx='{cx:.1f}' cy='{cy:.1f}' r='{r:.1f}' fill='{ACCENT}' opacity='0.85'/>")
    if not dots:
        return f"<div class='chart-empty' style='height:{s}px'>no settled trades</div>"
    return (
        f"<svg viewBox='0 0 {s} {s}' class='calib'>"
        f"<rect x='{pad}' y='{pad}' width='{s - 2 * pad}' height='{s - 2 * pad}' "
        f"fill='none' stroke='{DIM}' stroke-width='0.5' opacity='0.4'/>"
        f"<line x1='{pad}' y1='{s - pad}' x2='{s - pad}' y2='{pad}' "
        f"stroke='{DIM}' stroke-width='0.6' stroke-dasharray='3 3'/>"
        + "".join(dots)
        + "</svg>"
    )


def gauge(prob: float | None) -> str:
    """Horizontal fair-up probability bar (Up green / Down red split)."""
    if prob is None:
        return "<div class='gauge'><div class='gauge-empty'>no signal</div></div>"
    up = max(0.0, min(1.0, prob)) * 100
    return (
        "<div class='gauge'>"
        f"<div class='gauge-fill' style='width:{up:.0f}%'></div>"
        f"<div class='gauge-tick' style='left:50%'></div>"
        f"<span class='gauge-lbl'>fair Up {up:.0f}%</span>"
        "</div>"
    )
