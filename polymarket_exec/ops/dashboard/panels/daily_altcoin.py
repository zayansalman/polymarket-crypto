"""Daily altcoin Up/Down scanner panel (issue #185).

Pure ``render(...) -> str``, no DB access — data is loaded in ``ems.py`` via
``panels/_data.py``, per this repo's dashboard convention. Shows the open
position (if any), a settled-trade equity curve, and a plain-language
explanation of the mechanism, since this strategy has no Start/Stop control
of its own (it auto-runs in-process whenever the dashboard is up — see
``app.py``'s lifespan).
"""
from __future__ import annotations

from html import escape
from typing import Any

import config as _config

from . import _shared as s


def _mechanism_blurb(*, scan_interval_seconds: float, trade_usd: float) -> str:
    assets = ", ".join(a.upper() for a in _config.DAILY_ASSETS)
    return (
        "Scans Polymarket's daily (24h) Up/Down market across "
        f"{escape(assets)} roughly every {int(scan_interval_seconds)}s, "
        f"sizes a flat ${trade_usd:.0f} paper position on whichever "
        "asset currently shows the strongest edge versus a realized-volatility "
        "fair-value model. Paper only — no live gate exists for this strategy."
    )


def _open_row(row: dict[str, Any]) -> str:
    age = s.ago(row.get("created_at"))
    edge = row.get("edge")
    side = str(row.get("side", ""))
    return (
        "<div>"
        f"<span>{escape(str(row.get('asset', '')).upper())} · opened {age} ago</span>"
        f"<b class='{s.cls(1.0 if side == 'Up' else -1.0)}'>"
        f"{escape(side)} @ {row.get('entry_price'):.3f}"
        f" · edge {s.pct(edge, True) if edge is not None else '—'}</b>"
        "</div>"
    )


def render(
    *,
    open_positions: list[dict[str, Any]],
    perf: dict[str, Any],
    scan_interval_seconds: float,
    trade_usd: float,
) -> str:
    if open_positions:
        open_html = "<div class='de-kv'>" + "".join(_open_row(r) for r in open_positions) + "</div>"
    else:
        open_html = "<div class='dim'>no open position — waiting for a qualifying edge</div>"

    if perf.get("n"):
        equity_html = s.svg_equity(perf["equity"])
        stats_html = (
            "<div class='kv'>"
            f"<div><span>Settled</span><b>{perf['n']}</b></div>"
            f"<div><span>Net PnL</span><b class='{s.cls(perf['pnl'])}'>{s.money(perf['pnl'], True)}</b></div>"
            f"<div><span>Win rate</span><b>{s.pct(perf['win_rate'])}</b></div>"
            f"<div><span>Expectancy</span><b class='{s.cls(perf['expectancy'])}'>{s.money(perf['expectancy'], True)}</b></div>"
            "</div>"
        )
    else:
        equity_html = "<div class='chart-empty' style='height:84px'>awaiting first settled trade</div>"
        stats_html = ""

    return (
        "<section class='card'><div class='card-h'>DAILY ALTCOIN SCANNER</div>"
        f"<div class='dim' style='margin-bottom:8px'>"
        f"{_mechanism_blurb(scan_interval_seconds=scan_interval_seconds, trade_usd=trade_usd)}</div>"
        f"{open_html}"
        f"{equity_html}"
        f"{stats_html}"
        "</section>"
    )
