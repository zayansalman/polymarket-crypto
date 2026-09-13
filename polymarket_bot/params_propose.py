"""Layer 2 — propose tuned strategy parameters from the existing backtest grid.

Runs ``polymarket_bot.backtest.build_report`` (the same harness behind the dashboard),
picks the recommended params, writes them to ``$DATA_DIR/params_proposed.json``
for operator review. Never auto-applies — see ``params_apply`` for that step.

Run::

    python -m polymarket_bot.params_propose
    python -m polymarket_bot.params_propose --history /path/to/history.csv

Output: prints the current-vs-recommended delta, persists the proposal.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
from pathlib import Path

from polymarket_bot import backtest
from polymarket_bot.params import ActiveParams, load_active, save_proposed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--history", default=None, help="path to history CSV")
    args = ap.parse_args()

    history_path = Path(args.history) if args.history else None
    report = backtest.build_report(history_path=history_path)

    rec = report["recommended"]
    current = report["current"]
    active = asyncio.run(load_active())

    proposed = ActiveParams(
        entry_edge_min=float(rec["params"]["entry_edge_min"]),
        entry_edge_max=active.entry_edge_max,  # not tuned by the grid yet
        min_confidence=float(rec["params"]["min_confidence"]),
        min_remaining_seconds=int(rec["params"]["min_remaining_seconds"]),
        max_entry_price=float(rec["params"]["max_entry_price"]),
        min_entry_price=active.min_entry_price,  # not tuned by the grid yet
        source="proposed",
        proposed_at=datetime.now(UTC).isoformat(timespec="seconds"),
        backtest_meta={
            "opportunities": report["opportunities"],
            "recommended_trades": rec.get("trades"),
            "recommended_pnl": rec.get("total_pnl_usd"),
            "recommended_roi": rec.get("roi"),
            "recommended_win_rate": rec.get("win_rate"),
            "current_trades": current.get("trades"),
            "current_pnl": current.get("total_pnl_usd"),
            "current_roi": current.get("roi"),
            "history": report.get("history_path"),
        },
    )

    print("=== current (dashboard/runtime knobs) ===")
    print(f"  entry_edge_min       = {active.entry_edge_min:.3f}")
    print(f"  min_confidence       = {active.min_confidence:.2f}")
    print(f"  min_remaining_seconds= {active.min_remaining_seconds}")
    print(f"  max_entry_price      = {active.max_entry_price:.2f}")
    print(f"  source               = {active.source}")
    print()
    print("=== proposed (backtest grid recommendation) ===")
    print(f"  entry_edge_min       = {proposed.entry_edge_min:.3f}")
    print(f"  min_confidence       = {proposed.min_confidence:.2f}")
    print(f"  min_remaining_seconds= {proposed.min_remaining_seconds}")
    print(f"  max_entry_price      = {proposed.max_entry_price:.2f}")
    print()
    print("=== backtest summary ===")
    m = proposed.backtest_meta
    print(
        f"  current  : trades={m['current_trades']} pnl=${m['current_pnl']:+.2f} roi={m['current_roi'] * 100:+.1f}%"
    )
    print(
        f"  proposed : trades={m['recommended_trades']} pnl=${m['recommended_pnl']:+.2f} roi={m['recommended_roi'] * 100:+.1f}% win={m['recommended_win_rate'] * 100:.1f}%"
    )

    path = save_proposed(proposed)
    print()
    print(f"persisted proposal -> {path}")
    print("To apply (operator-gated): python -m polymarket_bot.params_apply --confirm")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
