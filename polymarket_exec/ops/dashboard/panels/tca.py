"""TCA panel: quoted spread, half-spread, edge capture, Brier calibration."""
from __future__ import annotations

from typing import Any

from . import _shared as s


def render(*, perf: dict[str, Any], spread: float | None) -> str:
    half = (spread / 2) if spread else None
    capture = None
    if perf.get("signaled_edge") and perf.get("n"):
        capture = perf["roi"] / perf["signaled_edge"] if perf["signaled_edge"] else None
    spread_s = f"{spread * 100:.1f}¢" if spread else "—"
    half_s = f"-{half * 100:.1f}¢" if half else "—"
    capture_s = f"{capture * 100:.0f}%" if capture is not None else "—"
    brier_s = f"{perf['brier']:.3f}" if perf.get("brier") is not None else "—"
    return (
        "<section class='card'><div class='card-h'>TCA · COST &amp; CAPTURE</div>"
        "<div class='kv tight'>"
        f"<div><span>Quoted spread</span><b>{spread_s}</b></div>"
        f"<div><span>Taker half-spread</span><b class='down'>{half_s}</b></div>"
        f"<div><span>Signaled edge</span><b class='up'>{s.pct(perf.get('signaled_edge'))}</b></div>"
        f"<div><span>Realized ROI</span><b class='{s.cls(perf.get('roi'))}'>{s.pct(perf.get('roi'), True)}</b></div>"
        f"<div><span>Edge capture</span><b>{capture_s}</b></div>"
        f"<div><span>Brier (calib.)</span><b>{brier_s}</b></div>"
        "</div>"
        f"<div class='calib-wrap'><div class='calib-cap'>calibration · predicted → realized</div>{s.svg_calibration(perf.get('cal_buckets', []))}</div>"
        "</section>"
    )
