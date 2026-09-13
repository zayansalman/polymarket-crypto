"""Per-model performance comparison for the shadow forward-tester.

Reads SETTLED rows from ``model_shadow_positions`` and prints one row per
``model_id``: trade count, win%, average entry price (a price-implied
breakeven win-rate), gross and net-of-fee ROI, average net PnL per share, a
Wilson 95% confidence interval on the realized win-rate, and a one-sided
binomial p-value testing the model's win-rate against the FEE-ADJUSTED
breakeven win-rate it must beat to make money net of the Polymarket taker
fee.

The stored ``realized_pnl_usd`` is already NET of the 0.07*p*(1-p) per-share
taker fee charged on entry (the LEDGER settles each shadow position that
way), so net ROI reads straight off that column; gross ROI re-derives the
pre-fee payoff from the binary outcome so the fee drag is visible as the gap
between the two.

A model is flagged ``EDGE?`` when the LOWER bound of its net-of-fee Wilson
interval clears its fee-adjusted breakeven win-rate — i.e. even the
pessimistic end of the win-rate estimate is profitable after fees. That is a
screening signal, not a guarantee: it is the win-rate bound, not a PnL
confidence interval, and small samples have wide bounds.

Pure read-only. The database is opened in SQLite read-only mode; this tool
never writes, settles, or places orders.

Usage::

    python tools/shadow_performance.py --db data/btc_5m_binary_fair_value.db
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:  # canonical fee math lives in the shadow package (built by the fees agent)
    from polymarket_bot.shadow.fees import breakeven_winrate
except Exception:  # pragma: no cover - exercised only before the package lands
    # Standalone fallback matching the shared contract exactly:
    #   fee_per_share(p) = 0.07 * p * (1 - p), charged on entry. Solving the
    #   zero-expected-net-PnL condition for the win-rate gives
    #   w* = p + fee_per_share(p) = p + 0.07 * p * (1 - p).
    def breakeven_winrate(entry_price: float, fee_rate: float = 0.07) -> float:
        """Win-rate a side priced at ``entry_price`` must beat net of fees."""
        p = entry_price
        return p + fee_rate * p * (1.0 - p)


# 95% two-sided normal critical value (also the one-sided 97.5% point); kept
# as a literal so the stats helpers need only ``math`` from the stdlib.
Z_95 = 1.959963984540054


# ---------------------------------------------------------------------------
# Stats helpers (stdlib only)
# ---------------------------------------------------------------------------


def wilson_interval(wins: int, n: int, z: float = Z_95) -> tuple[float, float]:
    """Wilson score confidence interval for a binomial proportion.

    Preferred over the normal (Wald) interval at the small samples a fresh
    shadow tester produces: it never escapes [0, 1] and stays sensible when
    ``wins`` is 0 or ``n``. Returns ``(0.0, 0.0)`` for an empty sample so
    callers can treat "no data" as a degenerate, non-clearing band.
    """
    if n <= 0:
        return (0.0, 0.0)
    phat = wins / n
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = (phat + z2 / (2.0 * n)) / denom
    half = (z * math.sqrt((phat * (1.0 - phat) + z2 / (4.0 * n)) / n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def binomial_sf_ge(k: int, n: int, p0: float) -> float:
    """One-sided p-value ``P(X >= k)`` for ``X ~ Binomial(n, p0)``.

    The exact probability of seeing at least ``k`` wins out of ``n`` trades
    if the true win-rate were only the breakeven ``p0`` — small values are
    evidence the model beats breakeven. Computed by exact summation over the
    upper tail (``n`` is in the hundreds at most here). Monotonically
    non-increasing in ``k`` for fixed ``n`` and ``p0``.
    """
    if n <= 0:
        return 1.0
    p0 = min(max(p0, 0.0), 1.0)
    k = max(0, min(k, n + 1))
    if k <= 0:
        return 1.0
    if k > n:
        return 0.0
    # Upper tail is cheaper when k is past the midpoint, but n is tiny; sum
    # the shorter side for numerical tidiness either way.
    return sum(
        math.comb(n, i) * (p0**i) * ((1.0 - p0) ** (n - i)) for i in range(k, n + 1)
    )


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelStats:
    """Aggregated settled-trade performance for one ``model_id``."""

    model_id: str
    n: int
    wins: int
    win_rate: float
    avg_entry: float
    gross_roi: float
    net_roi: float
    avg_net_pnl_per_share: float
    wilson_low: float
    wilson_high: float
    breakeven: float
    binomial_p: float
    edge: bool


def _net_pnl_per_share(entry_price: float, won: bool) -> float:
    """Net PnL per share for a side bought at ``entry_price`` (fee on entry)."""
    fee = 0.07 * entry_price * (1.0 - entry_price)
    payoff = (1.0 - entry_price) if won else (-entry_price)
    return payoff - fee


def _gross_pnl_per_share(entry_price: float, won: bool) -> float:
    """Pre-fee PnL per share — the binary payoff with no fee deducted."""
    return (1.0 - entry_price) if won else (-entry_price)


def compute_model_stats(rows: list[sqlite3.Row]) -> list[ModelStats]:
    """Aggregate settled shadow rows into a sorted list of per-model stats.

    Each row contributes ``shares`` of exposure at its ``entry_price``. Net
    PnL comes straight from the stored, already-net ``realized_pnl_usd``;
    gross PnL is re-derived from the outcome so the fee drag is the gap.
    Notional for ROI is ``entry_price * shares`` (what you paid). Sorted by
    net ROI descending so the strongest candidate is first.
    """
    by_model: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        by_model.setdefault(row["model_id"], []).append(row)

    stats: list[ModelStats] = []
    for model_id, model_rows in by_model.items():
        n = len(model_rows)
        wins = 0
        cost = 0.0
        gross_pnl = 0.0
        net_pnl = 0.0
        shares_total = 0.0
        entry_weighted = 0.0
        for row in model_rows:
            entry = float(row["entry_price"])
            shares = float(row["shares"] or 0.0)
            # The ledger stores the winning SIDE ("Up"/"Down") in `outcome`;
            # a row won iff its own side matches that winning side.
            won = row["outcome"] is not None and row["side"] == row["outcome"]
            wins += 1 if won else 0
            cost += entry * shares
            shares_total += shares
            entry_weighted += entry * shares
            gross_pnl += _gross_pnl_per_share(entry, won) * shares
            # Trust the stored net PnL; fall back to the contract formula only
            # if the LEDGER left it NULL on a settled row.
            stored = row["realized_pnl_usd"]
            net_pnl += (
                float(stored)
                if stored is not None
                else _net_pnl_per_share(entry, won) * shares
            )

        win_rate = wins / n if n else 0.0
        # Share-weighted average entry: the price whose breakeven the book
        # actually faces. Falls back to an unweighted mean if shares are all 0.
        avg_entry = (
            (entry_weighted / shares_total)
            if shares_total > 0
            else (sum(float(r["entry_price"]) for r in model_rows) / n if n else 0.0)
        )
        gross_roi = (gross_pnl / cost) if cost > 0 else 0.0
        net_roi = (net_pnl / cost) if cost > 0 else 0.0
        avg_net_pps = (net_pnl / shares_total) if shares_total > 0 else 0.0

        low, high = wilson_interval(wins, n)
        breakeven = breakeven_winrate(avg_entry)
        # One-sided test of the OBSERVED win count against the breakeven
        # win-rate: P(X >= wins | n, breakeven). Small ⇒ unlikely to be a
        # break-even model that got lucky.
        binom_p = binomial_sf_ge(wins, n, breakeven)
        # EDGE? — the pessimistic end of the win-rate band still clears the
        # fee-adjusted breakeven. Require a non-trivial sample to avoid a
        # one-trade fluke tripping the flag.
        edge = n >= 2 and low > breakeven

        stats.append(
            ModelStats(
                model_id=model_id,
                n=n,
                wins=wins,
                win_rate=win_rate,
                avg_entry=avg_entry,
                gross_roi=gross_roi,
                net_roi=net_roi,
                avg_net_pnl_per_share=avg_net_pps,
                wilson_low=low,
                wilson_high=high,
                breakeven=breakeven,
                binomial_p=binom_p,
                edge=edge,
            )
        )

    stats.sort(key=lambda s: s.net_roi, reverse=True)
    return stats


# ---------------------------------------------------------------------------
# Data access (read-only)
# ---------------------------------------------------------------------------


def load_settled_rows(db_path: Path) -> list[sqlite3.Row]:
    """Read every ``state = 'settled'`` row from the shadow positions table.

    Opens the database read-only (``mode=ro``) so this tool can never mutate
    live data. Raises ``FileNotFoundError`` if the database is missing.
    """
    if not db_path.exists():
        raise FileNotFoundError(f"database not found: {db_path}")
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(
            """
            SELECT model_id, side, entry_price, notional_usd, shares,
                   outcome, realized_pnl_usd
              FROM model_shadow_positions
             WHERE state = 'settled'
            """
        )
        return cur.fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def format_report(stats: list[ModelStats]) -> str:
    """Render the per-model comparison as a fixed-width text table."""
    header = (
        f"{'model_id':<22} {'n':>4} {'win%':>6} {'avg_entry':>9} "
        f"{'gross_roi':>10} {'net_roi':>10} {'net_pnl/sh':>11} "
        f"{'wilson95':>17} {'breakeven':>9} {'binom_p':>9}  flag"
    )
    lines = [header, "-" * len(header)]
    if not stats:
        lines.append("(no settled shadow positions)")
        return "\n".join(lines)
    for s in stats:
        flag = "EDGE?" if s.edge else ""
        wilson = f"[{s.wilson_low * 100:5.1f},{s.wilson_high * 100:5.1f}]%"
        lines.append(
            f"{s.model_id:<22} {s.n:>4} {s.win_rate * 100:>5.1f}% "
            f"{s.avg_entry:>9.3f} "
            f"{s.gross_roi * 100:>+9.1f}% {s.net_roi * 100:>+9.1f}% "
            f"{s.avg_net_pnl_per_share:>+11.4f} "
            f"{wilson:>17} {s.breakeven:>9.3f} {s.binomial_p:>9.4f}  {flag}"
        )
    return "\n".join(lines)


def run(db_path: Path) -> list[ModelStats]:
    """Load settled rows and compute per-model stats (testable entry point)."""
    return compute_model_stats(load_settled_rows(db_path))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("data/btc_5m_binary_fair_value.db"),
        help="Path to the SQLite database (default: data/btc_5m_binary_fair_value.db).",
    )
    args = parser.parse_args()

    try:
        stats = run(args.db)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"\n=== shadow forward-tester: per-model performance ({args.db}) ===\n")
    print(format_report(stats))
    print(
        "\nnet_roi/net_pnl are net of the 0.07*p*(1-p) taker fee; "
        "breakeven = fee-adjusted win-rate the avg-entry side must beat.\n"
        "EDGE? = net-of-fee Wilson 95% lower bound clears that breakeven "
        "(screening signal, not proof).\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
