"""Regime-attribution instrument for the shadow forward-tester (issue #120).

Answers one question, rigorously: does any *a-priori* regime carry real,
two-sided edge? It reads SETTLED rows from ``model_shadow_positions``
(read-only) and, per ``(model x regime x side-BET)`` cell, reports expectancy
with a Wilson win-rate band, then applies three guards the project learned the
hard way:

* **A-priori regimes** — time-of-day / edge bands are declared up front, never
  fitted to this sample. (Edge archaeology already mined the conditional
  surface to a null; this tool refuses to mine it again.)
* **Attribute by the side BET, not the outcome** — a regime only counts as
  real edge when BOTH the Up and the Down bets placed inside it are positive.
  An outcome split is a mechanical artifact of directional tilt.
* **Power gate + localized significance** — per-cell sample sizes are reported;
  significance is a one-vs-rest permutation test *per (model, regime)* on
  side-residualized PnL (so a directional tilt can't pose as regime structure),
  run only over regimes with >= ``min_n`` trades (so a thin outlier can't
  manufacture it), and Benjamini-Hochberg FDR-corrected across every
  (model, regime) hypothesis. A regime is a candidate only if its OWN test is
  significant AND it passes the two-sided gate.

By design it runs essentially dormant on a fresh sample: the honest headline
is "no regime clears the bar — underpowered" until the shadow ledger grows.

Pure read-only. The database is opened in SQLite read-only mode; this tool
never writes, settles, or places orders. It is NOT in the live signal path —
it can never flip the live model.

Usage::

    python tools/regime_attribution.py --db data/btc_5m_binary_fair_value.db --axis time
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Reuse the audited stats primitives rather than reimplement them: the Wilson
# win-rate band, the exact-binomial tail, and the fee-adjusted breakeven all
# already live in the shadow performance tool and share the ledger's fee model.
from tools.shadow_performance import (  # noqa: E402
    breakeven_winrate,
    wilson_interval,
)

Row = Mapping[str, object]


# ---------------------------------------------------------------------------
# A-priori regime axes (declared up front — never mined from this sample)
# ---------------------------------------------------------------------------


def time_of_day_band(created_at: str) -> str:
    """Map an ISO-8601 UTC timestamp to its a-priori time-of-day band.

    Bands: ``night`` 00:00-07:59, ``day`` 08:00-15:59, ``evening`` 16:00-23:59
    (UTC). These breakpoints are fixed in advance (the prior 'night bleed'
    observation), so the split cannot be tuned to flatter this sample.
    """
    dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    # Honor the UTC contract: an offset-aware stamp is converted to UTC; a
    # naive stamp is assumed to already be UTC (every producer emits +00:00).
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    hour = dt.hour
    if hour < 8:
        return "night"
    if hour < 16:
        return "day"
    return "evening"


def edge_band(edge: float) -> str:
    """Map a decision-time ``edge`` to its a-priori band.

    ``lo`` < 0.06, ``mid`` 0.06-0.09, ``hi`` >= 0.09 — cutoffs mirror the
    strategy's own edge gate, fixed in advance rather than fitted here.
    """
    if edge < 0.06:
        return "lo"
    if edge < 0.09:
        return "mid"
    return "hi"


def vol_band(sigma_per_second: float) -> str:
    """Map decision-time 1-second volatility to an a-priori band (issue #122).

    ``lo`` < 3e-5, ``mid`` 3e-5-6e-5, ``hi`` >= 6e-5 — round cutoffs bracketing
    the observed BTC-5m median (~4e-5/s), FROZEN up front rather than fitted to
    any outcome. Units-calibration only: the cutoffs set the scale, they were
    not tuned against PnL.
    """
    if sigma_per_second < 3e-5:
        return "lo"
    if sigma_per_second < 6e-5:
        return "mid"
    return "hi"


def basis_band(spot: float, reference: float) -> str:
    """Map decision-time spot-vs-reference dislocation to an a-priori band (#122).

    Banded on ``|spot - reference|`` in basis points of spot: ``near`` < 5bps,
    ``mid`` 5-15bps, ``far`` >= 15bps — round cutoffs bracketing the observed
    ~13bps average, fixed in advance. The MAGNITUDE of how far the window has
    moved from its open is the regime; the *signed* cushion is a separate,
    per-side entry gate, not this axis.
    """
    denom = abs(spot) if spot else 0.0
    if denom == 0.0:
        return "na"
    bps = abs(spot - reference) / denom * 1e4
    if bps < 5.0:
        return "near"
    if bps < 15.0:
        return "mid"
    return "far"


# ---------------------------------------------------------------------------
# Per-cell attribution (model x regime x side BET)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Cell:
    """Settled-trade performance for one ``(model, regime, side-bet)`` cell.

    ``side`` is the bet placed, not the realized winner; ``wins`` counts rows
    whose bet side matched the settled ``outcome``. ``powered`` is the honesty
    gate — a cell below ``min_n`` is excluded from any edge verdict.
    """

    model_id: str
    regime: str
    side: str
    n: int
    wins: int
    win_rate: float
    net_pnl: float
    expectancy: float
    avg_entry: float
    wilson_low: float
    wilson_high: float
    breakeven: float
    powered: bool


def _won(row: Row) -> bool:
    """A row won iff its BET side matches the settled winning ``outcome``."""
    return row["outcome"] is not None and row["side"] == row["outcome"]


def attribute(
    rows: Sequence[Row],
    regime_of: Callable[[Row], str],
    min_n: int = 30,
) -> list[Cell]:
    """Group settled rows into ``(model, regime, side)`` cells.

    ``regime_of`` maps a row to its a-priori regime label (e.g.
    ``lambda r: time_of_day_band(r["created_at"])``). Net PnL is taken from the
    stored, already-fee-netted ``realized_pnl_usd``; expectancy is per trade.
    Cells are returned sorted by ``(model, regime, side)`` for stable output.
    """
    groups: dict[tuple[str, str, str], list[Row]] = {}
    for row in rows:
        key = (str(row["model_id"]), regime_of(row), str(row["side"]))
        groups.setdefault(key, []).append(row)

    cells: list[Cell] = []
    for (model_id, regime, side), grp in groups.items():
        n = len(grp)
        wins = sum(1 for r in grp if _won(r))
        net_pnl = sum(float(r["realized_pnl_usd"] or 0.0) for r in grp)
        shares_total = sum(float(r["shares"] or 0.0) for r in grp)
        if shares_total > 0:
            avg_entry = (
                sum(float(r["entry_price"]) * float(r["shares"] or 0.0) for r in grp)
                / shares_total
            )
        else:
            avg_entry = sum(float(r["entry_price"]) for r in grp) / n
        low, high = wilson_interval(wins, n)
        cells.append(
            Cell(
                model_id=model_id,
                regime=regime,
                side=side,
                n=n,
                wins=wins,
                win_rate=wins / n,
                net_pnl=net_pnl,
                expectancy=net_pnl / n,
                avg_entry=avg_entry,
                wilson_low=low,
                wilson_high=high,
                breakeven=breakeven_winrate(avg_entry),
                powered=n >= min_n,
            )
        )
    cells.sort(key=lambda c: (c.model_id, c.regime, c.side))
    return cells


def two_sided_edge(cells: Sequence[Cell], model_id: str, regime: str) -> bool:
    """Does ``(model_id, regime)`` show *real, two-sided* edge?

    True only when BOTH the Up and the Down bet placed in this regime are
    present, powered, positive in expectancy, and have a Wilson lower bound
    that clears fee breakeven. Requiring both legs is the guard against
    rewarding a one-sided directional tilt — which is a mechanical artifact of
    the period's drift, not edge.
    """
    sides = {
        c.side: c for c in cells if c.model_id == model_id and c.regime == regime
    }
    up, down = sides.get("Up"), sides.get("Down")
    if up is None or down is None:
        return False
    return all(
        c.powered and c.expectancy > 0 and c.wilson_low > c.breakeven
        for c in (up, down)
    )


# ---------------------------------------------------------------------------
# Significance: permutation test + multiple-comparisons (FDR) correction
# ---------------------------------------------------------------------------


def benjamini_hochberg(
    pvalues: Sequence[float], alpha: float = 0.05
) -> list[bool]:
    """Benjamini-Hochberg FDR rejection mask, in the input order.

    Controls the false-discovery rate at ``alpha`` across all ``m`` hypotheses:
    sort the p-values, find the largest rank ``k`` (1-based) with
    ``p_(k) <= k/m * alpha``, and reject every hypothesis up to that rank
    (the step-up property — a smaller p that failed its own rank is still
    rejected if a larger-ranked one passes).
    """
    m = len(pvalues)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: pvalues[i])
    max_k = 0
    for rank, idx in enumerate(order, start=1):
        if pvalues[idx] <= (rank / m) * alpha:
            max_k = rank
    rejected = [False] * m
    for rank, idx in enumerate(order, start=1):
        if rank <= max_k:
            rejected[idx] = True
    return rejected


def _side_residualized_pnl(rows: Sequence[Row]) -> np.ndarray:
    """PnL with each side's own mean removed.

    Subtracting the per-side mean strips out directional tilt, so a regime that
    merely shifts the Up/Down *mix* (more Up bets at night, say) no longer reads
    as regime structure — only a genuine within-side regime effect survives.
    """
    pnl = np.array([float(r["realized_pnl_usd"] or 0.0) for r in rows])
    sides = [str(r["side"]) for r in rows]
    resid = pnl.copy()
    for side in set(sides):
        mask = np.array([s == side for s in sides])
        resid[mask] = pnl[mask] - pnl[mask].mean()
    return resid


def one_vs_rest_p(
    rows: Sequence[Row],
    regime_of: Callable[[Row], str],
    target: str,
    n_perm: int = 2000,
    seed: int = 0,
) -> float:
    """Permutation p-value that ``target``'s PnL differs from the rest.

    Operates on the supplied universe of rows (already restricted to one model's
    testable regimes). Statistic = ``|mean(resid | target) - mean(resid | rest)|``
    on side-residualized PnL; the null shuffles which rows are ``target`` while
    preserving the group size. p = ``(1 + count) / (n_perm + 1)``. Returns 1.0
    when ``target`` or its complement is empty (no contrast to test).
    """
    in_target = np.array([regime_of(r) == target for r in rows])
    n_t = int(in_target.sum())
    if n_t == 0 or n_t == len(rows):
        return 1.0

    resid = _side_residualized_pnl(rows)
    observed = abs(resid[in_target].mean() - resid[~in_target].mean())

    rng = np.random.default_rng(seed)
    idx = np.arange(len(rows))
    count = 0
    for _ in range(n_perm):
        rng.shuffle(idx)
        tgt = idx[:n_t]
        rest = idx[n_t:]
        if abs(resid[tgt].mean() - resid[rest].mean()) >= observed - 1e-12:
            count += 1
    return (1 + count) / (n_perm + 1)


# ---------------------------------------------------------------------------
# Axis selection + orchestration
# ---------------------------------------------------------------------------

_AXES = ("time", "edge", "vol", "basis")


def _regime_fn(axis: str) -> Callable[[Row], str]:
    """Return the a-priori regime labeller for the named axis."""
    if axis == "time":
        return lambda r: time_of_day_band(str(r["created_at"]))
    if axis == "edge":
        return lambda r: edge_band(float(r["edge"]))  # type: ignore[arg-type]
    if axis == "vol":
        return lambda r: vol_band(float(r["sigma_per_second"]))  # type: ignore[arg-type]
    if axis == "basis":
        return lambda r: basis_band(
            float(r["spot_at_decision"]), float(r["reference_at_decision"])
        )  # type: ignore[arg-type]
    raise ValueError(f"unknown axis {axis!r}; expected one of {_AXES}")


def _axis_value_present(row: Row, axis: str) -> bool:
    """Is the column(s) this axis needs non-NULL? Rows recorded before the #122
    migration have NULL vol/basis and are skipped for those axes."""
    if axis == "edge":
        return row["edge"] is not None
    if axis == "vol":
        return row["sigma_per_second"] is not None
    if axis == "basis":
        return (
            row["spot_at_decision"] is not None
            and row["reference_at_decision"] is not None
        )
    return row["created_at"] is not None


@dataclass(frozen=True)
class RegimeResult:
    """One ``(model, regime)`` verdict: its OWN one-vs-rest permutation p (FDR-
    corrected across all model x regime hypotheses), whether it passes the
    two-sided-edge gate, and its per-side cells. Significance is localized to
    this regime — it is not borrowed from the model's other regimes."""

    model_id: str
    regime: str
    permutation_p: float
    fdr_significant: bool
    two_sided: bool
    cells: list[Cell]


@dataclass(frozen=True)
class ModelVerdict:
    """All regime results for one model."""

    model_id: str
    regimes: list[RegimeResult]


@dataclass(frozen=True)
class Analysis:
    """Whole-run result. A ``candidate`` is a ``(model, regime)`` that has BOTH
    a significant one-vs-rest difference (after FDR) AND a two-sided profitable
    regime — the significance is the named regime's own, not the model's."""

    axis: str
    min_n: int
    n_perm: int
    n_settled: int
    models: list[ModelVerdict]
    candidates: list[tuple[str, str]]
    verdict: str


def analyze(
    rows: Sequence[Row],
    axis: str = "time",
    min_n: int = 30,
    n_perm: int = 2000,
    seed: int = 0,
) -> Analysis:
    """Run the full instrument over ``rows`` for one regime ``axis``.

    Builds per-cell attribution, then tests each (model, regime) by one-vs-rest
    permutation on side-residualized PnL — but only over TESTABLE regimes (total
    rows >= ``min_n``) so a thin outlier can't manufacture structure. The
    p-values are FDR-corrected across all (model, regime) hypotheses, and a
    candidate is a regime that is BOTH significant and two-sided profitable.
    With a small or homogeneous sample this returns no candidates by construction.
    """
    regime_of = _regime_fn(axis)
    usable = [r for r in rows if _axis_value_present(r, axis)]
    cells = attribute(usable, regime_of, min_n)

    rows_by_model: dict[str, list[Row]] = {}
    for r in usable:
        rows_by_model.setdefault(str(r["model_id"]), []).append(r)

    tested: list[tuple[str, str]] = []
    pvals: list[float] = []
    for mid in sorted(rows_by_model):
        m_rows = rows_by_model[mid]
        totals: dict[str, int] = {}
        for r in m_rows:
            rg = regime_of(r)
            totals[rg] = totals.get(rg, 0) + 1
        testable = sorted(rg for rg, n in totals.items() if n >= min_n)
        universe = [r for r in m_rows if regime_of(r) in testable]
        for rg in testable:
            tested.append((mid, rg))
            pvals.append(one_vs_rest_p(universe, regime_of, rg, n_perm=n_perm, seed=seed))

    mask = benjamini_hochberg(pvals)
    p_by = dict(zip(tested, pvals))
    sig_by = dict(zip(tested, mask))

    models: list[ModelVerdict] = []
    candidates: list[tuple[str, str]] = []
    for mid in sorted({c.model_id for c in cells}):
        m_cells = [c for c in cells if c.model_id == mid]
        rrs: list[RegimeResult] = []
        for rg in sorted({c.regime for c in m_cells}):
            ts = two_sided_edge(cells, mid, rg)
            fsig = sig_by.get((mid, rg), False)
            if ts and fsig:
                candidates.append((mid, rg))
            rrs.append(
                RegimeResult(
                    model_id=mid, regime=rg, permutation_p=p_by.get((mid, rg), 1.0),
                    fdr_significant=fsig, two_sided=ts,
                    cells=[c for c in m_cells if c.regime == rg],
                )
            )
        models.append(ModelVerdict(mid, rrs))

    if candidates:
        verdict = "REGIME EDGE: " + ", ".join(f"{m}/{rg}" for m, rg in candidates)
    elif not any(c.powered for c in cells):
        verdict = "no regime clears the bar — underpowered (sample too small)"
    else:
        verdict = "no regime clears the bar — no significant two-sided regime edge"

    return Analysis(
        axis=axis, min_n=min_n, n_perm=n_perm, n_settled=len(usable),
        models=models, candidates=candidates, verdict=verdict,
    )


def load_settled_rows(db_path: Path) -> list[sqlite3.Row]:
    """Read every ``state = 'settled'`` shadow row, with the axis columns.

    Opens the database read-only (``mode=ro``) so this tool can never mutate
    live data. Raises ``FileNotFoundError`` if the database is missing.
    """
    if not db_path.exists():
        raise FileNotFoundError(f"database not found: {db_path}")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        # The #122 regime columns are migration-added, so a ledger that predates
        # the migration lacks them. Select ``NULL AS col`` for any that are
        # absent so this read-only tool never errors on an old DB — those rows
        # simply fail _axis_value_present for the vol/basis axes.
        present = {
            r["name"]
            for r in conn.execute("PRAGMA table_info(model_shadow_positions)")
        }
        optional = (
            "sigma_per_second",
            "drift_per_second",
            "spot_at_decision",
            "reference_at_decision",
        )
        opt_select = ", ".join(
            col if col in present else f"NULL AS {col}" for col in optional
        )
        return conn.execute(
            f"""
            SELECT model_id, side, entry_price, shares, outcome,
                   realized_pnl_usd, created_at, edge,
                   {opt_select}
              FROM model_shadow_positions
             WHERE state = 'settled'
            """
        ).fetchall()
    finally:
        conn.close()


def run(
    db_path: Path,
    axis: str = "time",
    min_n: int = 30,
    n_perm: int = 2000,
    seed: int = 0,
) -> Analysis:
    """Load settled rows and analyze them (testable end-to-end entry point)."""
    return analyze(
        load_settled_rows(db_path), axis=axis, min_n=min_n, n_perm=n_perm, seed=seed
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def format_report(a: Analysis) -> str:
    """Render the analysis as a fixed-width text report, verdict first."""
    lines = [
        f"=== regime attribution · axis={a.axis} · settled={a.n_settled} · "
        f"min_n={a.min_n} · perms={a.n_perm} ===",
        f"VERDICT: {a.verdict}",
    ]
    if not a.models:
        lines.append("\n(no settled shadow positions)")
        return "\n".join(lines)

    for m in a.models:
        lines.append(f"\n[{m.model_id}]")
        for r in m.regimes:
            sig = "FDR-sig" if r.fdr_significant else "fdr-ns"
            tags = " ".join(
                x for x in ("2SIDED" if r.two_sided else "",) if x
            )
            cand = "  <<< CANDIDATE" if (r.two_sided and r.fdr_significant) else ""
            lines.append(
                f"  regime={r.regime:<8} one-vs-rest p={r.permutation_p:.4f} "
                f"({sig}) {tags}{cand}"
            )
            for c in r.cells:
                pw = "UNDERPWR" if not c.powered else ""
                wilson = f"[{c.wilson_low * 100:5.1f},{c.wilson_high * 100:5.1f}]%"
                lines.append(
                    f"    {c.side:<5} n={c.n:>4} win={c.win_rate * 100:>5.1f}% "
                    f"exp/trade={c.expectancy:>+8.4f} wilson95={wilson} "
                    f"brk={c.breakeven:.3f} {pw}"
                )
    lines.append(
        "\nexp/trade = mean net-of-fee realized PnL per trade (USD). "
        "one-vs-rest = permutation p that THIS regime differs from the model's "
        "other (testable) regimes, on side-residualized PnL; FDR-corrected "
        "across all model x regime tests. 2SIDED = both sides powered and clear "
        "fee breakeven. A CANDIDATE needs BOTH."
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("data/btc_5m_binary_fair_value.db"),
        help="Path to the SQLite database.",
    )
    parser.add_argument("--axis", choices=_AXES, default="time")
    parser.add_argument("--min-n", type=int, default=30, dest="min_n")
    parser.add_argument("--n-perm", type=int, default=2000, dest="n_perm")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    args = parser.parse_args()

    try:
        result = run(
            args.db, axis=args.axis, min_n=args.min_n, n_perm=args.n_perm, seed=args.seed
        )
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(asdict(result), indent=2))
    else:
        print(format_report(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
