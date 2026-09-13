"""Performance / alpha panel: combined equity curve + LIVE/PAPER mini-cards.

The combined view (equity, net P&L, ROI, win rate, expectancy, profit
factor, max DD) sits on top. The two mini-cards below split LIVE vs PAPER
using each mode's own recent-N window — a low-volume mode never gets crowded
out by a high-volume mode (the bug that left LIVE blank when paper trades
dominated the recent slice).
"""
from __future__ import annotations

from html import escape
from typing import Any

from . import _shared as s


def _recon_line(recon: dict[str, Any] | None) -> str:
    """A 'Reconciled vs Polymarket' footer — the real account truth (#102).

    The mini-cards above are the bot's recorded ledger (now corrected to real
    fills). This line is the whole-account ground truth from the Polymarket
    Data API, which the per-window rows can't show: full-history account PnL
    (incl. non-bot trades) and current open value.
    """
    if not recon:
        return ""

    def _f(key: str) -> float | None:
        try:
            return float(recon[key])
        except (KeyError, TypeError, ValueError):
            return None

    btc = _f("real_btc_pnl_lifetime")
    acct = _f("real_account_pnl_lifetime")
    openv = _f("open_positions_value")
    asof = escape(str(recon.get("asof", "")))[:19]
    return (
        "<div class='perf-recon'>"
        "<span class='perf-recon-h'>Reconciled vs Polymarket</span>"
        f"<span>BTC bot <b class='{s.cls(btc)}'>{s.money(btc, True)}</b></span>"
        f"<span title='Whole Polymarket account — INCLUDES your non-bot trades'>"
        f"account (incl. non-bot) <b class='{s.cls(acct)}'>{s.money(acct, True)}</b></span>"
        f"<span>open value <b>{s.money(openv)}</b></span>"
        f"<span class='perf-recon-asof'>as-of {asof}Z · data-api</span>"
        "</div>"
    )


def _freshness_badge(recon: dict[str, Any] | None) -> str:
    """Whether the HEADLINE metrics are reconciled to real fills (#113).

    Net P&L / ROI / win-rate are booked at the bot's *assumed* fills (zero-fee,
    at entry). ``tools/reconcile_live_ledger.py --apply`` corrects the ledger to
    real Polymarket fills and writes the ``recon.*`` snapshot read here, so
    its presence is the signal that the headline numbers were last grounded to
    reality — and its absence means they are pure assumed-fill.
    """
    if not recon:
        return (
            "<span class='pill warn perf-fresh' title='Headline P&amp;L uses "
            "ASSUMED fills (zero-fee, booked at entry) — NOT reconciled to real "
            "Polymarket fills. Run tools/reconcile_live_ledger.py to correct.'>"
            "assumed-fill</span>"
        )
    asof = escape(str(recon.get("asof", "")))[:10]  # YYYY-MM-DD
    return (
        "<span class='pill perf-fresh' title='Ledger reconciled to real "
        "Polymarket fills as-of this snapshot; trades since are assumed-fill.'>"
        f"reconciled {asof}</span>"
    )


def render(
    *,
    style: str,
    perf: dict[str, Any],
    perf_live: dict[str, Any],
    perf_paper: dict[str, Any],
    recon: dict[str, Any] | None = None,
) -> str:
    if not perf.get("n"):
        return (
            "<section class='card'><div class='card-h'>PERFORMANCE / ALPHA</div>"
            "<div class='chart-empty'>awaiting first settled trade this session</div></section>"
        )
    wl = f"{perf['wins']}W / {perf['losses']}L"
    pf_s = f"{perf['profit_factor']:.2f}" if perf["profit_factor"] else "∞"

    def _mini(label_html: str, p: dict[str, Any]) -> str:
        # Always render the same row layout for LIVE and PAPER so the two
        # mini-cards stay visually parallel. Empty state shows zeros + a
        # 'n=0' hint instead of a different layout that visually breaks
        # symmetry with the populated mode.
        n = p.get("n") or 0
        if n:
            pnl_v = p["pnl"]
            roi_v = p["roi"]
            win_v = p["win_rate"]
            exp_v = p["expectancy"]
            wl_s = f"{p['wins']}W / {p['losses']}L"
        else:
            pnl_v = roi_v = win_v = exp_v = 0.0
            wl_s = "0W / 0L"
        sub = f"recent {n}" if n else "no closed trades yet"
        return (
            "<div class='perf-mini'>"
            f"<div class='perf-mini-h'>{label_html}<span class='perf-mini-sub'>{escape(sub)}</span></div>"
            "<div class='perf-mini-row'>"
            f"<span>P&amp;L</span><b class='{s.cls(pnl_v)}'>{s.money(pnl_v, True)}</b>"
            "</div>"
            "<div class='perf-mini-row'>"
            f"<span>ROI</span><b class='{s.cls(roi_v)}'>{s.pct(roi_v, True)}</b>"
            "</div>"
            "<div class='perf-mini-row'>"
            f"<span>Win rate</span><b>{s.pct(win_v)} <em>({wl_s})</em></b>"
            "</div>"
            "<div class='perf-mini-row'>"
            f"<span>Expectancy</span><b class='{s.cls(exp_v)}'>{s.money(exp_v, True)}</b>"
            "</div>"
            "</div>"
        )

    live_label = "<span class='pill live'>● LIVE</span>"
    paper_label = "<span class='pill paper'>PAPER</span>"
    return (
        "<section class='card'><div class='card-h'>PERFORMANCE / ALPHA"
        f"<span class='win'>recent {perf['n']} · {style} · live+paper</span>"
        f"{_freshness_badge(recon)}</div>"
        f"<div class='equity'>{s.svg_equity(perf['equity'])}</div>"
        "<div class='statrow'>"
        f"{s.stat('Net P&L', s.money(perf['pnl'], True), s.cls(perf['pnl']))}"
        f"{s.stat('ROI', s.pct(perf['roi'], True), s.cls(perf['roi']))}"
        f"{s.stat('Win rate', s.pct(perf['win_rate']), '', wl)}"
        f"{s.stat('Expectancy', s.money(perf['expectancy'], True), s.cls(perf['expectancy']), 'per trade')}"
        f"{s.stat('Profit factor', pf_s, s.cls((perf['profit_factor'] or 2) - 1))}"
        f"{s.stat('Max DD', s.money(perf['max_dd']), 'down' if perf['max_dd'] < 0 else '')}"
        "</div>"
        "<div class='perf-split'>"
        f"{_mini(live_label, perf_live)}"
        f"{_mini(paper_label, perf_paper)}"
        "</div>"
        f"{_recon_line(recon)}"
        "</section>"
    )
