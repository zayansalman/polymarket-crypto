"""Slow-market forecasting-skill pilot: journal + scoring (issue #162).

Implements the pre-registered pilot in ``docs/archive/PIVOT_2026-07.md`` §4: log a
probability forecast on a slow Polymarket market, snapshot the executable
quotes alongside it, and — once markets resolve — score forecasting skill
against the market itself.

The protocol the tool enforces or reminds:

* **Forecast before price.** Decide your probability BEFORE looking at the
  book (anchoring is the known failure mode). The tool takes the forecast as
  a required argument and timestamps it; the eyes-discipline is yours.
* **No crypto.** The pilot excludes the crypto category by pre-registration —
  the 5m program already established that game is unwinnable at our latency
  (and it carries the highest taker fee, 0.07).
* **Success bar** (pre-registered, #162): positive Brier skill vs the
  market-implied probability AND fee-true simulated PnL 95% CI > 0 over
  ≥ 30 resolutions. Anything less = no demonstrated skill = do not fund.

Storage is a **dedicated SQLite file** (default ``data/forecast_journal.db``)
— deliberately not the bot's ledger, so this tool can never contend with or
corrupt the running race.

Usage::

    python tools/forecast_journal.py add --market will-x-happen \
        --question "Will X happen by Sept?" --category geopolitics \
        --forecast 0.62 --yes-bid 0.44 --yes-ask 0.47 [--resolves-by 2026-09-01]
    python tools/forecast_journal.py resolve --id 3 --outcome yes
    python tools/forecast_journal.py list
    python tools/forecast_journal.py report
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_DB = ROOT / "data" / "forecast_journal.db"

# Polymarket Fee Structure V2 (2026-03-30): taker fee rate by category.
# Crypto is present only to REFUSE it loudly — the pilot pre-registration
# excludes it (docs/archive/PIVOT_2026-07.md §4).
CATEGORY_FEE = {
    "geopolitics": 0.0,
    "world": 0.0,
    "sports": 0.03,
    "finance": 0.04,
    "politics": 0.04,
    "mentions": 0.04,
    "tech": 0.04,
    "economics": 0.05,
    "culture": 0.05,
    "weather": 0.05,
    "other": 0.05,
    "crypto": 0.07,
}

# Pre-registered pilot parameters (#162 / PIVOT memo §4).
DIVERGENCE_MIN = 0.05   # simulate a position only when |forecast − mid| ≥ 5c
TARGET_N = 30           # resolutions needed before the success bar is judged
Z_95 = 1.959963984540054

_SCHEMA = """
CREATE TABLE IF NOT EXISTS forecasts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  market_id TEXT NOT NULL,
  question TEXT,
  category TEXT NOT NULL,
  fee_rate REAL NOT NULL,
  forecast_yes REAL NOT NULL,
  yes_bid REAL,
  yes_ask REAL,
  resolves_by TEXT,
  outcome TEXT,
  resolved_at TEXT
);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Scoring math (pure, tested)
# ---------------------------------------------------------------------------


def taker_fee_per_share(price: float, fee_rate: float) -> float:
    """Per-share entry fee: ``fee_rate * p * (1 - p)`` (0 for fee-free cats)."""
    return fee_rate * price * (1.0 - price)


def brier(prob_yes: float, outcome_yes: bool) -> float:
    """Squared error of a probability forecast against the binary outcome."""
    y = 1.0 if outcome_yes else 0.0
    return (prob_yes - y) ** 2


def market_implied(yes_bid: float, yes_ask: float) -> float:
    """The market's own P(yes): the mid of the YES book."""
    return (yes_bid + yes_ask) / 2.0


def simulated_pnl_per_share(
    forecast_yes: float,
    yes_bid: float,
    yes_ask: float,
    outcome_yes: bool,
    fee_rate: float,
) -> float | None:
    """Fee-true PnL of the position the forecast implies, or None if no trade.

    Pre-registered rule: trade only when the forecast diverges from the
    market mid by ≥ ``DIVERGENCE_MIN``. Forecast above mid → buy YES at the
    YES ask. Forecast below mid → buy NO; in a binary book the executable NO
    ask is the complement of the YES bid (selling YES to the bid ≡ buying NO
    at ``1 − yes_bid``). Entry pays the category taker fee on the price paid.
    """
    mid = market_implied(yes_bid, yes_ask)
    edge = forecast_yes - mid
    if abs(edge) < DIVERGENCE_MIN:
        return None
    if edge > 0:  # we think YES is cheap
        price = yes_ask
        won = outcome_yes
    else:  # we think NO is cheap
        price = 1.0 - yes_bid
        won = not outcome_yes
    gross = (1.0 - price) if won else -price
    return gross - taker_fee_per_share(price, fee_rate)


def mean_ci(xs: list[float]) -> tuple[float, float, float]:
    """(mean, lo, hi) 95% z-interval; degenerate values for n < 2."""
    n = len(xs)
    if n == 0:
        return (0.0, 0.0, 0.0)
    mu = sum(xs) / n
    if n < 2:
        return (mu, mu, mu)
    sd = math.sqrt(sum((x - mu) ** 2 for x in xs) / (n - 1))
    half = Z_95 * sd / math.sqrt(n)
    return (mu, mu - half, mu + half)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_add(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    category = args.category.strip().lower()
    if category == "crypto":
        sys.exit(
            "REFUSED: the pilot pre-registration excludes crypto (see "
            "docs/archive/PIVOT_2026-07.md §4 — that game was measured and lost)."
        )
    if category not in CATEGORY_FEE:
        sys.exit(f"unknown category {category!r}; one of {sorted(CATEGORY_FEE)}")
    if not (0.0 < args.forecast < 1.0):
        sys.exit("forecast must be a probability strictly between 0 and 1")
    for name in ("yes_bid", "yes_ask"):
        v = getattr(args, name)
        if v is not None and not (0.0 <= v <= 1.0):
            sys.exit(f"{name} must be within [0, 1]")
    if (
        args.yes_bid is not None
        and args.yes_ask is not None
        and args.yes_bid > args.yes_ask
    ):
        sys.exit("crossed quote: yes_bid > yes_ask")
    cur = conn.execute(
        "INSERT INTO forecasts (created_at, market_id, question, category, "
        "fee_rate, forecast_yes, yes_bid, yes_ask, resolves_by) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            _now(),
            args.market,
            args.question,
            category,
            CATEGORY_FEE[category],
            args.forecast,
            args.yes_bid,
            args.yes_ask,
            args.resolves_by,
        ),
    )
    conn.commit()
    print(
        f"logged #{cur.lastrowid}: P(yes)={args.forecast:.2f} on {args.market!r} "
        f"({category}, fee {CATEGORY_FEE[category]:.2f})"
    )
    if args.yes_bid is None or args.yes_ask is None:
        print(
            "NOTE: no quote snapshot — this row scores Brier only; it cannot "
            "enter the simulated-PnL leg of the success bar."
        )


def cmd_resolve(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    outcome = args.outcome.strip().lower()
    if outcome not in ("yes", "no"):
        sys.exit("outcome must be 'yes' or 'no'")
    row = conn.execute(
        "SELECT id, outcome FROM forecasts WHERE id = ?", (args.id,)
    ).fetchone()
    if row is None:
        sys.exit(f"no forecast with id {args.id}")
    if row["outcome"] is not None:
        sys.exit(f"forecast {args.id} already resolved as {row['outcome']!r}")
    conn.execute(
        "UPDATE forecasts SET outcome = ?, resolved_at = ? WHERE id = ?",
        (outcome, _now(), args.id),
    )
    conn.commit()
    print(f"resolved #{args.id} as {outcome.upper()}")


def cmd_list(conn: sqlite3.Connection, _args: argparse.Namespace) -> None:
    rows = conn.execute("SELECT * FROM forecasts ORDER BY id").fetchall()
    if not rows:
        print("journal is empty — log the first forecast with 'add'")
        return
    for r in rows:
        status = (r["outcome"] or "open").upper()
        quote = (
            f"bid {r['yes_bid']:.2f}/ask {r['yes_ask']:.2f}"
            if r["yes_bid"] is not None and r["yes_ask"] is not None
            else "no quote"
        )
        print(
            f"#{r['id']:>3} [{status:<4}] P(yes)={r['forecast_yes']:.2f} vs {quote} "
            f"· {r['category']} · {r['market_id']} · {r['created_at']}"
        )


def cmd_report(conn: sqlite3.Connection, _args: argparse.Namespace) -> None:
    rows = conn.execute(
        "SELECT * FROM forecasts WHERE outcome IS NOT NULL ORDER BY id"
    ).fetchall()
    open_n = conn.execute(
        "SELECT COUNT(*) c FROM forecasts WHERE outcome IS NULL"
    ).fetchone()["c"]

    print("=" * 78)
    print(f"FORECAST PILOT REPORT — resolved {len(rows)} / target ≥{TARGET_N} "
          f"(open: {open_n})")
    print("=" * 78)
    if not rows:
        print("nothing resolved yet — no skill claim possible either way")
        return

    ours, markets, pnls, traded = [], [], [], 0
    for r in rows:
        y = r["outcome"] == "yes"
        ours.append(brier(r["forecast_yes"], y))
        if r["yes_bid"] is None or r["yes_ask"] is None:
            continue
        markets.append(brier(market_implied(r["yes_bid"], r["yes_ask"]), y))
        pnl = simulated_pnl_per_share(
            r["forecast_yes"], r["yes_bid"], r["yes_ask"], y, r["fee_rate"]
        )
        if pnl is not None:
            traded += 1
            pnls.append(pnl)

    our_brier = sum(ours) / len(ours)
    print(f"our Brier: {our_brier:.4f} over {len(ours)} resolutions "
          f"(lower is better; 0.25 = coin-flip forecaster)")
    if markets:
        mkt_brier = sum(markets) / len(markets)
        skill = mkt_brier - our_brier
        print(f"market Brier: {mkt_brier:.4f} over {len(markets)} quoted rows")
        print(f"Brier skill vs market: {skill:+.4f} "
              f"({'we beat the market' if skill > 0 else 'the market beats us'})")
    if pnls:
        mu, lo, hi = mean_ci(pnls)
        print(f"simulated fee-true PnL (divergence ≥{DIVERGENCE_MIN:.2f}): "
              f"{traded} trades, mean {mu:+.4f}/share, CI95 [{lo:+.4f}, {hi:+.4f}]")
    else:
        print("no simulated trades yet (no quoted rows diverged "
              f"≥{DIVERGENCE_MIN:.2f} from mid)")

    print()
    if len(rows) < TARGET_N:
        print(f"VERDICT: underpowered — {TARGET_N - len(rows)} more resolutions "
              "needed before the pre-registered bar is judged")
    else:
        skill_ok = bool(markets) and (sum(markets) / len(markets)) - our_brier > 0
        pnl_ok = bool(pnls) and mean_ci(pnls)[1] > 0
        if skill_ok and pnl_ok:
            print("VERDICT: PASSES the pre-registered bar (Brier skill > 0 AND "
                  "PnL CI > 0) — fund small per docs/archive/PIVOT_2026-07.md")
        else:
            print("VERDICT: FAILS the pre-registered bar — no demonstrated "
                  "skill; do not fund (close option C)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="log a forecast (decide it BEFORE looking at the book)")
    p_add.add_argument("--market", required=True, help="market slug or condition id")
    p_add.add_argument("--question", default=None)
    p_add.add_argument("--category", required=True, help=f"one of {sorted(CATEGORY_FEE)}")
    p_add.add_argument("--forecast", type=float, required=True, help="your P(yes), 0-1")
    p_add.add_argument("--yes-bid", dest="yes_bid", type=float, default=None)
    p_add.add_argument("--yes-ask", dest="yes_ask", type=float, default=None)
    p_add.add_argument("--resolves-by", dest="resolves_by", default=None)

    p_res = sub.add_parser("resolve", help="record a market's resolution")
    p_res.add_argument("--id", type=int, required=True)
    p_res.add_argument("--outcome", required=True, help="yes | no")

    sub.add_parser("list", help="show the journal")
    sub.add_parser("report", help="Brier skill + simulated PnL vs the pre-registered bar")

    args = ap.parse_args()
    conn = connect(args.db)
    try:
        {"add": cmd_add, "resolve": cmd_resolve, "list": cmd_list, "report": cmd_report}[
            args.cmd
        ](conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
