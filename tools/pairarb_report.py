"""Report on the two-sided pair shadow ledger (#182).

Answers the only question that matters for a maker strategy: **is the edge real,
or are we being picked off?**

Four sections, in order of how much they can embarrass the strategy:

1. **Adverse selection** — the decisive metric. We quote both legs of every
   window, so each leg is an offer that either fills or does not, and either
   wins or loses. A fair quoter fills at roughly the same rate on legs that end
   up winning as on legs that end up losing. A quoter that is being picked off
   fills disproportionately on the losing side: your Down bid gets hit precisely
   because price is falling. The gap between those two fill rates is the cost of
   being a maker, and it is invisible in headline PnL.

2. **Hedged vs stranded** — a completed pair is deterministic; a stranded leg is
   a naked directional bet. If PnL is only positive because stranded legs went
   the right way, this is a punt with extra steps, not a market-making edge.

3. **Daily decomposition + drawdown** — never a bare total. ``lessons.md``
   documents crowning a strategy on a 5-day sum while it was mid -$26 intraday
   drawdown; aggregates hide regime breaks.

4. **Capacity** — edge per dollar deployed. At these clip sizes capacity binds
   long before edge does.

Usage::

    python tools/pairarb_report.py
    python tools/pairarb_report.py --db data/pairarb_shadow.db
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _rows(db: sqlite3.Connection, sql: str) -> list[sqlite3.Row]:
    db.row_factory = sqlite3.Row
    return list(db.execute(sql))


def report(db_path: Path) -> int:
    if not db_path.exists():
        print(f"no ledger at {db_path} — run tools/pairarb_shadow.py first")
        return 1
    db = sqlite3.connect(db_path)
    windows = _rows(db, "SELECT * FROM pair_windows ORDER BY start_ts")
    if not windows:
        print("ledger is empty — no windows have settled yet")
        return 0

    n = len(windows)
    quoted = [w for w in windows if (w["quoted_up"] or 0) > 0]
    filled = [w for w in windows if (w["up_filled"] or 0) > 0 or (w["down_filled"] or 0) > 0]
    both = [w for w in windows if (w["up_filled"] or 0) > 0 and (w["down_filled"] or 0) > 0]
    pnl = sum(w["pnl"] or 0 for w in windows)
    pairs = sum(w["pairs"] or 0 for w in windows)
    stranded = sum(w["stranded"] or 0 for w in windows)

    span_h = (windows[-1]["start_ts"] - windows[0]["start_ts"]) / 3600 if n > 1 else 0
    print("=" * 78)
    print(f"PAIR SHADOW LEDGER — {n} windows over {span_h:.1f}h")
    print("=" * 78)
    print(f"  quoted           {len(quoted):>6}   ({100 * len(quoted) / n:.0f}% of windows)")
    print(f"  any fill         {len(filled):>6}   ({100 * len(filled) / max(n, 1):.0f}%)")
    print(f"  both legs filled {len(both):>6}   ({100 * len(both) / max(n, 1):.0f}%)")
    print(f"  hedged pairs     {pairs:>9.1f} shares")
    print(f"  stranded         {stranded:>9.1f} shares")
    if pairs + stranded > 0:
        hedge_rate = 2 * pairs / (2 * pairs + stranded)
        print(f"  hedged share of filled volume: {100 * hedge_rate:.1f}%")

    # ---------------------------------------------------------------- #
    # 1. Adverse selection — the decisive number
    # ---------------------------------------------------------------- #
    print("\n" + "-" * 78)
    print("ADVERSE SELECTION  (does a leg fill BECAUSE it is about to lose?)")
    print("-" * 78)
    win_off = win_fill = lose_off = lose_fill = 0
    for w in quoted:
        up_won = bool(w["resolved_up"])
        for leg_won, leg_filled in (
            (up_won, (w["up_filled"] or 0) > 0),
            (not up_won, (w["down_filled"] or 0) > 0),
        ):
            if leg_won:
                win_off += 1
                win_fill += int(leg_filled)
            else:
                lose_off += 1
                lose_fill += int(leg_filled)
    if win_off and lose_off:
        wr = win_fill / win_off
        lr = lose_fill / lose_off
        print(f"  fill rate on legs that WON : {100 * wr:5.1f}%   ({win_fill}/{win_off})")
        print(f"  fill rate on legs that LOST: {100 * lr:5.1f}%   ({lose_fill}/{lose_off})")
        gap = lr - wr
        print(f"  >>> adverse-selection gap  : {100 * gap:+5.1f} pp", end="  ")
        if gap > 0.10:
            print("SEVERE — being picked off; maker edge is likely illusory")
        elif gap > 0.03:
            print("present — quote wider or shorter, and re-measure")
        elif gap < -0.03:
            print("favourable — verify this is not a small-sample artifact")
        else:
            print("~neutral — fills look outcome-independent")
        if min(win_off, lose_off) < 100:
            print(
                f"  (n={min(win_off, lose_off)} on the smaller arm — underpowered; "
                "treat as directional, not established)"
            )
    else:
        print("  not enough quoted windows yet")

    # ---------------------------------------------------------------- #
    # 2. Hedged vs stranded PnL attribution
    # ---------------------------------------------------------------- #
    print("\n" + "-" * 78)
    print("PnL ATTRIBUTION  (is the profit from the hedge or from the punt?)")
    print("-" * 78)
    hedged_pnl = sum(
        (w["pairs"] or 0) * (1.0 - ((w["up_vwap"] or 0) + (w["down_vwap"] or 0)))
        for w in windows
        if (w["pairs"] or 0) > 0
    )
    print(f"  from hedged pairs  ${hedged_pnl:+10.2f}   (deterministic at entry)")
    print(f"  from stranded legs ${pnl - hedged_pnl:+10.2f}   (directional risk)")
    print(f"  TOTAL              ${pnl:+10.2f}")
    if pnl > 0 and hedged_pnl <= 0:
        print("  >>> WARNING: all profit is directional. This is not a maker edge.")

    # ---------------------------------------------------------------- #
    # 3. Daily decomposition + drawdown
    # ---------------------------------------------------------------- #
    print("\n" + "-" * 78)
    print("DAILY DECOMPOSITION  (aggregates hide regime breaks — lessons.md)")
    print("-" * 78)
    by_day: dict[str, list[sqlite3.Row]] = {}
    for w in windows:
        day = time.strftime("%Y-%m-%d", time.gmtime(w["start_ts"] or 0))
        by_day.setdefault(day, []).append(w)
    print(f"  {'day':<12} {'wins':>6} {'pnl':>10} {'pairs':>9} {'stranded':>9}")
    for day, rows in sorted(by_day.items()):
        print(
            f"  {day:<12} {len(rows):>6} {sum(r['pnl'] or 0 for r in rows):>10.2f} "
            f"{sum(r['pairs'] or 0 for r in rows):>9.1f} "
            f"{sum(r['stranded'] or 0 for r in rows):>9.1f}"
        )
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for w in windows:
        equity += w["pnl"] or 0
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    print(f"\n  max drawdown: ${max_dd:.2f}   final equity: ${equity:+.2f}")

    # ---------------------------------------------------------------- #
    # 4. Capacity
    # ---------------------------------------------------------------- #
    print("\n" + "-" * 78)
    print("CAPACITY")
    print("-" * 78)
    deployed = sum(
        (w["pairs"] or 0) * ((w["up_vwap"] or 0) + (w["down_vwap"] or 0))
        + (w["stranded"] or 0) * max(w["up_vwap"] or 0, w["down_vwap"] or 0)
        for w in windows
    )
    print(f"  capital deployed   ${deployed:10.2f}")
    if deployed > 0:
        print(f"  return on deployed {100 * pnl / deployed:+9.2f}%")
        if span_h > 0:
            print(f"  deployed per hour  ${deployed / span_h:10.2f}")

    # Per-asset breakdown — the alt books carry the edge, so this matters.
    print("\n  by asset:")
    print(f"  {'asset':<8} {'wins':>5} {'pnl':>9} {'fills':>6} {'hedged%':>8}")
    by_asset: dict[str, list[sqlite3.Row]] = {}
    for w in windows:
        by_asset.setdefault(w["asset"] or "?", []).append(w)
    for asset, rows in sorted(by_asset.items(), key=lambda x: -sum(r["pnl"] or 0 for r in x[1])):
        p = sum(r["pairs"] or 0 for r in rows)
        s = sum(r["stranded"] or 0 for r in rows)
        nf = sum(1 for r in rows if (r["up_filled"] or 0) or (r["down_filled"] or 0))
        hr = f"{100 * 2 * p / (2 * p + s):.0f}%" if (p + s) > 0 else "-"
        print(
            f"  {asset:<8} {len(rows):>5} {sum(r['pnl'] or 0 for r in rows):>9.2f} "
            f"{nf:>6} {hr:>8}"
        )
    print("=" * 78)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default="data/pairarb_shadow.db")
    a = p.parse_args()
    return report(Path(a.db))


if __name__ == "__main__":
    raise SystemExit(main())
