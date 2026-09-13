"""Tick-replay backtest for the shadow roster over the FULL quote history (#144).

The shadow race only covers the days the runner existed; the tick journal
(``paper_ticks``) reaches further back with everything a signal needs:
both books, the model fair, spot/reference, sigma, remaining seconds. This
tool replays those ticks through the CURRENT roster signal functions,
settles fee-true, and reports standings — turning pre-race history into an
out-of-sample test for gates designed on race data.

Honesty guards built in:
* **Outcome reconstruction is validated** against every window with a known
  outcome (settled shadow rows) before it is trusted for unlabeled windows.
* **The harness is validated** by requiring replayed v0/v2 entries on the
  race days to reproduce the recorded shadow ledger.
* Fills at the executable ask + the venue taker fee (``net_pnl_per_share``)
  — identical accounting to the shadow ledger and (post-#133) the live book.

Read-only against the ledger. Never imported by the runtime.

Usage::

    .venv/bin/python tools/replay_race.py [--db PATH] [--grid]
"""

from __future__ import annotations

import argparse
import asyncio
import math
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config as _config  # noqa: E402
from polymarket_bot import strategy  # noqa: E402
from polymarket_bot.shadow import runner as shadow_runner  # noqa: E402
from polymarket_bot.shadow.fees import net_pnl_per_share  # noqa: E402
from polymarket_bot.shadow.types import ShadowSignal, SnapshotView  # noqa: E402

WINDOW_SECONDS = 300
SHARES = shadow_runner.SHADOW_SHARES


def _params() -> strategy.StrategyParams:
    """The production strategy params — the same mapping the paper loop uses
    (polymarket_bot/paper.py::_strategy_params, minus the runtime sizing override)."""
    from polymarket_bot import runtime_knobs as _knobs

    async def _load() -> strategy.StrategyParams:
        return strategy.StrategyParams(
            min_trade_usd=await _knobs.get("paper_min_trade_usd"),
            max_trade_usd=await _knobs.get("paper_max_trade_usd"),
            entry_edge_min=await _knobs.get("paper_entry_edge_min"),
            min_confidence=await _knobs.get("paper_min_confidence"),
            entry_min_remaining_seconds=await _knobs.get("paper_entry_min_remaining_seconds"),
            entry_edge_max=await _knobs.get("paper_entry_edge_max"),
            min_entry_price=await _knobs.get("paper_min_entry_price"),
        )

    return asyncio.run(_load())


@dataclass
class Trade:
    window_slug: str
    model_id: str
    side: str
    entry_price: float
    edge: float
    created_at: str
    pnl: float | None = None


def load_ticks(db_path: Path) -> dict[str, list[sqlite3.Row]]:
    """Ticks with an executable two-sided book and a pricing-model value, per window."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT created_at, window_slug, remaining_seconds, spot_price, "
        "reference_price, sigma_per_second, fair_up_prob, up_best_bid, "
        "up_best_ask, down_best_ask, down_best_bid "
        "FROM paper_ticks "
        "WHERE up_best_ask IS NOT NULL AND down_best_ask IS NOT NULL "
        "AND fair_up_prob IS NOT NULL AND spot_price IS NOT NULL "
        "AND reference_price IS NOT NULL AND remaining_seconds IS NOT NULL "
        "ORDER BY window_slug, created_at"
    ).fetchall()
    conn.close()
    by_window: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        by_window[r["window_slug"]].append(r)
    return by_window


def known_outcomes(db_path: Path) -> dict[str, str]:
    """Window -> outcome from the settled shadow ledger (ground truth)."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rows = conn.execute(
        "SELECT DISTINCT window_slug, outcome FROM model_shadow_positions "
        "WHERE state='settled' AND outcome IN ('Up','Down')"
    ).fetchall()
    conn.close()
    return {w: o for w, o in rows}


def reconstruct_outcomes(
    ticks: dict[str, list[sqlite3.Row]],
    known: dict[str, str],
) -> tuple[dict[str, str], float, int]:
    """Outcome per window from the NEXT window's reference print.

    Consecutive 5m windows share the settlement feed: window N+1's
    ``reference_price`` is that feed's print at window N's close, so
    ``outcome(N) = Up iff ref(N+1) >= ref(N)`` — the venue's own ``>=`` tie
    rule on the venue's own feed. Validated at 100.00% agreement on 564
    ground-truth outcomes (vs 93.95% for last-tick spot reconstruction).

    Returns (labels, agreement_vs_known, n_checked); ground truth always
    wins where available, and the agreement MUST be inspected by the caller
    before unlabeled-window results are trusted.
    """
    refs: dict[int, tuple[str, float]] = {}
    for slug, rows in ticks.items():
        try:
            ts = int(slug.rsplit("-", 1)[-1])
        except (TypeError, ValueError):
            continue
        refs[ts] = (slug, float(rows[0]["reference_price"]))
    labels: dict[str, str] = {}
    agree = checked = 0
    for ts, (slug, ref) in refs.items():
        nxt = refs.get(ts + WINDOW_SECONDS)
        rec = None if nxt is None else ("Up" if nxt[1] >= ref else "Down")
        if slug in known:
            if rec is not None:
                checked += 1
                agree += known[slug] == rec
            labels[slug] = known[slug]  # ground truth always wins
        elif rec is not None:
            labels[slug] = rec
    return labels, (agree / checked if checked else float("nan")), checked


def _view_from_tick(t: sqlite3.Row) -> SnapshotView:
    up_bid, up_ask = t["up_best_bid"], t["up_best_ask"]
    market_up = (
        (float(up_bid) + float(up_ask)) / 2.0 if up_bid is not None else float(up_ask)
    )
    return SnapshotView(
        window_slug=t["window_slug"],
        remaining_seconds=int(t["remaining_seconds"]),
        spot=float(t["spot_price"]),
        reference=float(t["reference_price"]),
        up_ask=float(t["up_best_ask"]),
        down_ask=float(t["down_best_ask"]),
        market_up_price=market_up,
        fair_up=float(t["fair_up_prob"]),
        sigma_per_second=float(t["sigma_per_second"] or 0.0),
        feed_source="replay",
        quote_source="replay",
        drift_per_second=None,
        # Bid quotes populated here for spread-guard evaluation (#149).
        # None in all live/paper/shadow paths — zero production impact.
        up_bid=float(t["up_best_bid"]) if t["up_best_bid"] is not None else None,
        down_bid=float(t["down_best_bid"]) if t["down_best_bid"] is not None else None,
    )


def replay(
    ticks: dict[str, list[sqlite3.Row]],
    outcomes: dict[str, str],
    models: dict[str, Callable[[SnapshotView, strategy.StrategyParams], ShadowSignal | None]],
    params: strategy.StrategyParams,
) -> list[Trade]:
    """First-signal-per-(window, model), settled fee-true — shadow semantics."""
    trades: list[Trade] = []
    for slug, rows in sorted(ticks.items()):
        outcome = outcomes.get(slug)
        if outcome is None:
            continue
        done: set[str] = set()
        for t in rows:
            if len(done) == len(models):
                break
            view = _view_from_tick(t)
            for model_id, fn in models.items():
                if model_id in done:
                    continue
                sig = fn(view, params)
                if sig is None:
                    continue
                done.add(model_id)
                pnl = SHARES * net_pnl_per_share(sig.entry_price, sig.side == outcome)
                trades.append(
                    Trade(
                        window_slug=slug,
                        model_id=model_id,
                        side=sig.side,
                        entry_price=sig.entry_price,
                        edge=sig.edge,
                        created_at=t["created_at"],
                        pnl=pnl,
                    )
                )
    return trades


def summarize(trades: list[Trade], label: str) -> None:
    by_model: dict[str, list[Trade]] = defaultdict(list)
    for tr in trades:
        by_model[tr.model_id].append(tr)
    print(f"\n=== {label} ===")
    print(f"{'model':24}{'n':>6}{'total':>9}{'exp':>9}{'ci95':>16}{'win':>7}{'maxDD':>8}")
    for m, ts in sorted(by_model.items(), key=lambda kv: -sum(t.pnl or 0 for t in kv[1])):
        pnls = [t.pnl or 0.0 for t in ts]
        n = len(pnls)
        tot = sum(pnls)
        exp = tot / n if n else 0.0
        sd = math.sqrt(sum((p - exp) ** 2 for p in pnls) / (n - 1)) if n > 1 else 0.0
        half = 1.96 * sd / math.sqrt(n) if n else 0.0
        win = sum(1 for p in pnls if p > 0) / n if n else 0.0
        cum = peak = dd = 0.0
        for p in pnls:
            cum += p
            peak = max(peak, cum)
            dd = min(dd, cum - peak)
        print(
            f"{m:24}{n:>6}{tot:>9.2f}{exp:>+9.4f}"
            f"  [{exp - half:+.3f},{exp + half:+.3f}]{win:>7.3f}{dd:>8.2f}"
        )


def harness_check(trades: list[Trade], db_path: Path) -> None:
    """Replayed v2 on race days must reproduce the recorded shadow ledger."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rec = {
        w: (s, p)
        for w, s, p in conn.execute(
            "SELECT window_slug, side, entry_price FROM model_shadow_positions "
            "WHERE model_id='cushion_favorite_v2' AND state='settled'"
        )
    }
    conn.close()
    rep = {t.window_slug: t for t in trades if t.model_id == "cushion_favorite_v2"}
    common = set(rec) & set(rep)
    if not common:
        print("\nHARNESS CHECK: no overlap with recorded shadow rows — cannot validate")
        return
    side_ok = sum(1 for w in common if rec[w][0] == rep[w].side)
    px_ok = sum(1 for w in common if abs(rec[w][1] - rep[w].entry_price) <= 0.011)
    print(
        f"\nHARNESS CHECK vs recorded shadow v2: {len(common)} common windows | "
        f"side match {side_ok/len(common):.1%} | entry within 1 tick {px_ok/len(common):.1%} | "
        f"replay-only {len(rep) - len(common)}, recorded-only {len(rec) - len(common)}"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=_config.DB_PATH)
    ap.add_argument("--grid", action="store_true", help="v7 parameter-fragility grid")
    ap.add_argument("--split", default="2026-06-18", help="OOS boundary date (gates designed on data >= this)")
    args = ap.parse_args()

    ticks = load_ticks(args.db)
    known = known_outcomes(args.db)
    outcomes, agreement, n_checked = reconstruct_outcomes(ticks, known)
    print(
        f"windows with ticks: {len(ticks)} | labeled: {len(outcomes)} | "
        f"reconstruction agreement vs {n_checked} known outcomes: {agreement:.2%}"
    )
    if n_checked >= 50 and agreement < 0.99:
        print("WARNING: reconstruction below 99% — treat unlabeled-window results as noisy")

    params = _params()
    from polymarket_bot.shadow.signals import cushion_fresh_v7  # noqa: E402

    models: dict[str, Callable] = dict(shadow_runner._MODELS)
    trades = replay(ticks, outcomes, models, params)
    harness_check(trades, args.db)

    pre = [t for t in trades if t.created_at < args.split]
    post = [t for t in trades if t.created_at >= args.split]
    summarize(trades, "FULL PERIOD (fee-true, first-signal-per-window)")
    summarize(pre, f"PRE-RACE < {args.split}  (out-of-sample for v7's gates)")
    summarize(post, f"RACE ERA >= {args.split}  (in-sample for v7's gates)")

    if args.grid:
        print("\n=== v7 FRAGILITY GRID (params must sit on a plateau, not a spike) ===")
        from polymarket_bot.shadow.signals import (  # noqa: E402
            cushion_fresh_v7_f45,
            cushion_fresh_v7_f45_spread,
        )
        grid_models: dict[str, Callable] = {}
        for fresh in (45, 60, 90):
            for cap in (0.06, 0.065, 0.07):
                name = f"v7[f{fresh},c{cap}]"
                grid_models[name] = (
                    lambda v, p, _f=fresh, _c=cap: cushion_fresh_v7(
                        v, p, max_age_seconds=_f, edge_cap=_c
                    )
                )
        grid_models["v0+fresh60"] = lambda v, p: (
            None
            if (WINDOW_SECONDS - v.remaining_seconds) > 60
            else shadow_runner._v0_control(v, p)
        )
        grid_models["v2+fresh60(no cap)"] = lambda v, p: cushion_fresh_v7(
            v, p, max_age_seconds=60, edge_cap=1.0
        )
        # Pre-registered hypotheses from issue #149 (day-5 race observation):
        # H1: tighten freshness to ≤45s (46–60s bucket negative in both fresh models)
        # H2: add spread guard ≤1 tick (all ≥2-tick spread entries negative in every model)
        grid_models["v7_f45[#149-H1]"] = cushion_fresh_v7_f45
        grid_models["v7_f45_spread1c[#149-H2]"] = cushion_fresh_v7_f45_spread
        gtrades = replay(ticks, outcomes, grid_models, params)
        summarize([t for t in gtrades if t.created_at < args.split], "grid · PRE-RACE (OOS)")
        summarize([t for t in gtrades if t.created_at >= args.split], "grid · RACE ERA (IS)")


if __name__ == "__main__":
    main()
