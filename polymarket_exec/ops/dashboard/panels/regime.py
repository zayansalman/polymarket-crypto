"""Market regime panel: band chips, a few key numbers, strategy feasibility.

Pure ``render(snapshot, asset) -> str`` over the dict
``panels/_data.latest_regime`` returns (a decoded ``regime_snapshots`` row),
per the panels convention. A full-width card: the headline and band chips
on top, eight key numbers, then one row per strategy family with its
feasibility label, the measured numbers it rests on, and the rules that
fired (rule ids resolved to prose via the classifier's registry) — so the
operator reads *why*, not just *what*. Every degradation the monitor
recorded is listed verbatim. Feasibility rows are rendered in neutral
tones on purpose: green is reserved for PnL, and none of this is edge.
"""
from __future__ import annotations

from html import escape
from typing import Any

from polymarket_bot.regime.classify import RULES
from polymarket_bot.regime.features import ANNUALIZE as _ANNUALIZE

from . import _shared as s

_FIT_CLASS = {"feasible": "", "degraded": "dim", "blocked": "down"}

_BAND_CLASS = {
    "high": "down", "expensive": "down", "stale": "down", "expanding": "down",
    "quiet": "down", "jump": "down", "big_down": "down",
}

_CAVEAT = (
    "Feasibility of each family's mechanics on today's numbers — not edge. "
    "Regime switching within a family was falsified here "
    "(docs/archive/FINDINGS.md §3–4)."
)


def _ann(v: float | None) -> str:
    return f"{v * _ANNUALIZE * 100:.0f}% ann" if isinstance(v, (int, float)) else "—"


def _ratio(v: float | None) -> str:
    return f"{v:.2f}×" if isinstance(v, (int, float)) else "—"


def _usd(v: float | None) -> str:
    if not isinstance(v, (int, float)):
        return "—"
    if v >= 1e9:
        return f"${v / 1e9:.2f}B"
    if v >= 1e6:
        return f"${v / 1e6:.1f}M"
    if v >= 1e3:
        return f"${v / 1e3:.0f}k"
    return f"${v:,.0f}"


def _num(v: float | None, digits: int = 2) -> str:
    return f"{v:.{digits}f}" if isinstance(v, (int, float)) else "—"


def _cents(v: float | None) -> str:
    return f"{v * 100:.1f}¢" if isinstance(v, (int, float)) else "—"


def _chip(axis: str, band: Any) -> str:
    band = str(band or "unknown")
    cls = _BAND_CLASS.get(band, "flat")
    return (
        f"<span class='pill {cls}' title='{escape(axis)}'>"
        f"{escape(axis.replace('_', ' '))}: {escape(band.replace('_', ' '))}</span>"
    )


def _metrics_line(metrics: dict[str, Any]) -> str:
    parts = []
    for key, val in metrics.items():
        if isinstance(val, (int, float)):
            parts.append(f"{key.replace('_', ' ')} {val:g}")
    return " · ".join(parts)


def _fit_row(fit: dict[str, Any]) -> str:
    label = str(fit.get("fit", "degraded"))
    cls = _FIT_CLASS.get(label, "")
    reasons = [RULES.get(str(r), str(r)) for r in (fit.get("reasons") or [])]
    metrics = _metrics_line(fit.get("metrics") or {})
    detail = " · ".join(x for x in (metrics, "; ".join(reasons)) if x)
    detail_html = (
        f"<div class='dim' style='font-size:10.5px;margin-top:2px'>{escape(detail)}</div>"
        if detail
        else ""
    )
    return (
        "<div style='padding:6px 0;border-top:1px dashed var(--border-strong)'>"
        "<div style='display:flex;justify-content:space-between;gap:12px'>"
        f"<span class='mono'>{escape(str(fit.get('label') or fit.get('strategy_id') or ''))}</span>"
        f"<b class='mono {cls}'>{escape(label.upper())}</b>"
        "</div>"
        f"{detail_html}"
        "</div>"
    )


def _quality_line(quality: list[Any]) -> str:
    items = []
    for q in quality:
        if isinstance(q, dict):
            code = str(q.get("code", ""))
            detail = str(q.get("detail", ""))
            items.append(f"{code} ({detail})" if detail else code)
        else:
            items.append(str(q))
    return "; ".join(items)


def render_unavailable() -> str:
    """Placeholder when loading or rendering the card failed (the failure is logged
    by the caller) — distinct from "no scan yet" so a fault is never mistaken for
    an empty history."""
    return (
        "<section class='card wide'><div class='card-h'>MARKET REGIME"
        "<span class='win'>advisory</span></div>"
        "<div class='chart-empty'>regime card unavailable — see logs "
        "(regime.card_failed)</div></section>"
    )


def render(snapshot: dict[str, Any] | None, asset: str | None = None) -> str:
    if not snapshot:
        what = f" for {escape(asset.upper())}" if asset else ""
        return (
            "<section class='card wide'><div class='card-h'>MARKET REGIME"
            "<span class='win'>advisory</span></div>"
            f"<div class='chart-empty'>no regime scan yet{what} — the monitor runs every "
            "minute while the dashboard is up</div></section>"
        )
    f: dict[str, Any] = snapshot.get("features") or {}
    bands: dict[str, str] = snapshot.get("bands") or {}
    fits: list[dict[str, Any]] = [x for x in (snapshot.get("fits") or []) if isinstance(x, dict)]
    quality: list[Any] = snapshot.get("quality") or []
    snap_asset = str(snapshot.get("asset") or "").upper()
    timeframe = str(snapshot.get("timeframe") or "")
    grade = str(snapshot.get("grade") or "")
    age = s.ago(snapshot.get("created_at"))

    chips = "".join(
        _chip(axis, bands.get(axis, "unknown"))
        for axis in ("volatility", "vol_trend", "volume", "move", "jumps", "book", "session")
    )
    quality_html = (
        f"<div class='dim' style='margin:6px 0;font-size:10.5px'>data grade {escape(grade)}: "
        + escape(_quality_line(quality))
        + "</div>"
        if quality
        else f"<div class='dim' style='margin:6px 0;font-size:10.5px'>data grade {escape(grade)}</div>"
    )
    kv = (
        "<div class='kv tight'>"
        f"<div><span>Vol 1h (GK)</span><b>{_ann(f.get('vol_1h_gk'))}</b></div>"
        f"<div><span>Vol 24h (GK)</span><b>{_ann(f.get('vol_24h_gk'))}</b></div>"
        f"<div><span>Vol vs same-hour</span><b>{_ratio(f.get('vol_ratio_seasonal'))}</b></div>"
        f"<div><span>Volume vs same-hour</span><b>{_ratio(f.get('volume_ratio_seasonal'))}</b></div>"
        f"<div><span>Volume 1h</span><b>{_usd(f.get('volume_1h_usd'))}</b></div>"
        f"<div><span>Move 1h (z)</span><b class='{s.cls(f.get('move_z_1h'))}'>{_num(f.get('move_z_1h'))}</b></div>"
        f"<div><span>Overround (taker)</span><b>{_cents(f.get('overround'))}</b></div>"
        f"<div><span>Maker capture</span><b>{_cents(f.get('maker_capture'))}</b></div>"
        "</div>"
    )
    fits_html = (
        "<div style='margin-top:10px'><div class='de-h'>STRATEGY FEASIBILITY</div>"
        + "".join(_fit_row(x) for x in fits)
        + f"<div class='dim' style='font-size:10px;margin-top:6px'>{escape(_CAVEAT)}</div>"
        + "</div>"
    )
    rec = str(snapshot.get("recommendation") or "")
    return (
        "<section class='card wide'><div class='card-h'>MARKET REGIME"
        f"<span class='win'>{escape(snap_asset)} {escape(timeframe)} · "
        f"{escape(str(snapshot.get('symbol') or ''))} · {age} ago · advisory</span></div>"
        f"<div class='decision' style='margin-top:0;border-top:none;padding-top:0'>"
        f"{escape(str(snapshot.get('headline') or ''))}</div>"
        f"<div style='display:flex;flex-wrap:wrap;gap:6px;margin:8px 0'>{chips}</div>"
        f"{quality_html}"
        f"{kv}"
        f"<div class='mono' style='margin-top:8px;font-size:11px'>{escape(rec)}</div>"
        f"{fits_html}"
        "</section>"
    )
