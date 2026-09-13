"""One-shot fee-true race standings + deploy-bar tracker (issue #150).

The 6-hour race-loop iteration (``tasks/race_loop.md``) re-derives the same
standings SQL by hand each time. This tool packages that assessment into a
single read-only command: per-model fee-true standings since the race's
common start, a live-book summary, the bot's state/heartbeat, and a
deploy-bar tracker (how many more settled trades until the leader's 95% CI
clears zero at its current point estimate).

Every number matches the shadow ledger's own accounting: ``realized_pnl_usd``
is already NET of the Polymarket taker fee (``0.07 * p * (1 - p)`` per share,
charged on entry — see :mod:`polymarket_bot.shadow.fees`), so totals and means read
straight off that column, and the fee-adjusted breakeven win-rate each model
must beat is ``p + 0.07 * p * (1 - p)``.

Guardrails (BINDING — this tool is part of the read-only assessment path):

* **Snapshot, never touch the live DB.** The ledger is copied to a temp file
  and opened with ``immutable=1``; a bot writing concurrently cannot be
  blocked or corrupted, and a direct read-only open (which the sandbox
  refuses on the live path) is never attempted.
* **Zero bot interaction.** No writes, no settles, no orders, no dashboard
  POSTs. Pure reporting.

Usage::

    python tools/race_status.py [--db data/btc_5m_binary_fair_value.db]
                                [--since 2026-07-02T14:50:50]
                                [--trades-per-day 9] [--json]
"""
from __future__ import annotations

import argparse
import json
import math
import random
import shutil
import sqlite3
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config as _config  # noqa: E402

# The race's common start: the first tick at which the current four-model
# ablation roster (v0 / v2 / v7 / v8) was all live together (#142/#144). All
# standings are scoped to settled rows at or after this instant so every model
# is judged over the same window set.
COMMON_START = "2026-07-02T14:50:50"

# 95% two-sided normal critical value.
Z_95 = 1.959963984540054

# Bootstrap resamples for the mean-PnL CI. Fixed seed → reproducible report.
BOOTSTRAP_N = 10_000
BOOTSTRAP_SEED = 42

# A "CLEARS NOW" verdict claim needs a real sample behind it. One winning
# trade has sd=0 and a degenerate CI "above zero" — without this floor the
# tracker would print a deploy-bar pass on n=1 (observed with f45's first
# settled trade, 2026-07-10). 30 matches the attribution instrument's min_n.
MIN_N_FOR_CLEARS = 30

# Tick-cadence health (#157). The paper loop journals a tick every ~5s, so a
# healthy 10-minute window holds ~120 ticks. The #147 watchdog only checks the
# heartbeat, which stays fresh even when a flapping settlement feed collapses
# journaling to a trickle (2026-07-09: ~2-4 ticks/10min for ~6h, race silently
# under-accruing while state read "running"). Flag a running loop that has
# journaled fewer than this in the last 10 minutes so the gap is visible.
CADENCE_WINDOW_SECONDS = 600
MIN_TICKS_PER_WINDOW = 30  # ~25% of the ~120 expected; well clear of a brief blip


def taker_fee_per_share(price: float, fee_rate: float = 0.07) -> float:
    """Polymarket entry taker fee per share (mirrors polymarket_bot.shadow.fees)."""
    return fee_rate * price * (1.0 - price)


def breakeven_winrate(entry_price: float, fee_rate: float = 0.07) -> float:
    """Win-rate a side priced at ``entry_price`` must beat net of fees."""
    return entry_price + taker_fee_per_share(entry_price, fee_rate)


# ---------------------------------------------------------------------------
# Stats (stdlib only)
# ---------------------------------------------------------------------------


def mean_std(xs: list[float]) -> tuple[float, float]:
    """Sample mean and (ddof=1) standard deviation; std is 0 for n<2."""
    n = len(xs)
    if n == 0:
        return (0.0, 0.0)
    mu = sum(xs) / n
    if n < 2:
        return (mu, 0.0)
    var = sum((x - mu) ** 2 for x in xs) / (n - 1)
    return (mu, math.sqrt(var))


def t_ci(xs: list[float], z: float = Z_95) -> tuple[float, float]:
    """Normal-approx 95% CI for the mean (z-interval; n is in the hundreds)."""
    n = len(xs)
    if n == 0:
        return (0.0, 0.0)
    mu, sd = mean_std(xs)
    if n < 2:
        return (mu, mu)
    half = z * sd / math.sqrt(n)
    return (mu - half, mu + half)


def bootstrap_ci(
    xs: list[float], n_resamples: int = BOOTSTRAP_N, seed: int = BOOTSTRAP_SEED
) -> tuple[float, float]:
    """Percentile bootstrap 95% CI for the mean — robust to PnL's fat tails."""
    n = len(xs)
    if n == 0:
        return (0.0, 0.0)
    if n < 2:
        return (xs[0], xs[0])
    rng = random.Random(seed)
    means = []
    for _ in range(n_resamples):
        means.append(sum(xs[rng.randrange(n)] for _ in range(n)) / n)
    means.sort()
    lo = means[int(0.025 * n_resamples)]
    hi = means[int(0.975 * n_resamples)]
    return (lo, hi)


def max_drawdown(xs: list[float]) -> float:
    """Most-negative peak-to-trough excursion of the cumulative PnL path."""
    cum = peak = dd = 0.0
    for x in xs:
        cum += x
        peak = max(peak, cum)
        dd = min(dd, cum - peak)
    return dd


def required_n_for_ci_gt_zero(mean: float, sd: float, z: float = Z_95) -> int | None:
    """Smallest n at which a z-interval's lower bound clears zero.

    Solving ``mean - z * sd / sqrt(n) > 0`` for ``n`` gives
    ``n > (z * sd / mean) ** 2``. Only defined for a positive mean; a
    non-positive point estimate never clears zero and returns ``None``.
    """
    if mean <= 0 or sd <= 0:
        return None
    return math.ceil((z * sd / mean) ** 2)


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


@dataclass
class ModelStats:
    model_id: str
    n: int
    total: float
    mean: float
    sd: float
    t_lo: float
    t_hi: float
    boot_lo: float
    boot_hi: float
    win_rate: float
    breakeven_wr: float
    max_dd: float
    today_n: int
    today_total: float
    req_n: int | None


def _snapshot(db_path: Path) -> Path:
    """Copy the ledger to a temp file for an immutable, race-free read."""
    tmp = Path(tempfile.mkstemp(prefix="race_status_", suffix=".db")[1])
    shutil.copy2(db_path, tmp)
    return tmp


def _connect_immutable(snapshot: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{snapshot}?immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def gather_models(conn: sqlite3.Connection, since: str, today: str) -> list[ModelStats]:
    rows = conn.execute(
        "SELECT model_id, created_at, side, entry_price, realized_pnl_usd "
        "FROM model_shadow_positions "
        "WHERE state='settled' AND created_at >= ? "
        "ORDER BY model_id, created_at",
        (since,),
    ).fetchall()

    by_model: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        by_model.setdefault(r["model_id"], []).append(r)

    out: list[ModelStats] = []
    for model_id, mrows in by_model.items():
        pnls = [float(r["realized_pnl_usd"]) for r in mrows]
        pxs = [float(r["entry_price"]) for r in mrows]
        n = len(pnls)
        mu, sd = mean_std(pnls)
        t_lo, t_hi = t_ci(pnls)
        boot_lo, boot_hi = bootstrap_ci(pnls)
        wins = sum(1 for p in pnls if p > 0)
        avg_px = sum(pxs) / n if n else 0.0
        today_rows = [r for r in mrows if str(r["created_at"])[:10] == today]
        today_pnls = [float(r["realized_pnl_usd"]) for r in today_rows]
        out.append(
            ModelStats(
                model_id=model_id,
                n=n,
                total=sum(pnls),
                mean=mu,
                sd=sd,
                t_lo=t_lo,
                t_hi=t_hi,
                boot_lo=boot_lo,
                boot_hi=boot_hi,
                win_rate=wins / n if n else 0.0,
                breakeven_wr=breakeven_winrate(avg_px),
                max_dd=max_drawdown(pnls),
                today_n=len(today_pnls),
                today_total=sum(today_pnls),
                req_n=required_n_for_ci_gt_zero(mu, sd),
            )
        )
    out.sort(key=lambda s: s.mean, reverse=True)
    return out


@dataclass
class LiveBook:
    n: int
    total: float
    by_day: list[tuple[str, int, float]]
    # Maker/taker attribution of ENTRY placements (#137): 'matched' = crossed
    # at placement (taker fee paid), 'live' = rested on the book (maker if
    # later filled — no fee on that portion). Zeroes when live_orders is
    # absent (minimal DBs) or carries no placement responses.
    entries_matched: int = 0
    entries_rested: int = 0


def _placement_split(conn: sqlite3.Connection) -> tuple[int, int]:
    """(matched, rested) counts among live SUBMITTED ENTRY orders.

    Derived via json_extract from the journaled placement response so it works
    on any snapshot age — including ledgers that predate the #137
    placement_status column migration. Resilient to a missing table.
    """
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='live_orders'"
    ).fetchone()
    if not has_table:
        return (0, 0)
    rows = conn.execute(
        "SELECT json_extract(details_json, '$.response.status') ps, COUNT(*) n "
        "FROM live_orders "
        "WHERE mode='live' AND intent='ENTRY' AND status='SUBMITTED' "
        "GROUP BY ps",
    ).fetchall()
    counts = {str(r["ps"]): int(r["n"]) for r in rows if r["ps"] is not None}
    return (counts.get("matched", 0), counts.get("live", 0))


def gather_live_book(conn: sqlite3.Connection) -> LiveBook:
    rows = conn.execute(
        "SELECT substr(opened_at,1,10) day, COUNT(*) n, "
        "ROUND(SUM(realized_pnl_usd),2) pnl "
        "FROM paper_positions WHERE mode='live' "
        "GROUP BY day ORDER BY day",
    ).fetchall()
    by_day = [(str(r["day"]), int(r["n"]), float(r["pnl"] or 0.0)) for r in rows]
    total_row = conn.execute(
        "SELECT COUNT(*) n, ROUND(SUM(realized_pnl_usd),2) pnl "
        "FROM paper_positions WHERE mode='live'",
    ).fetchone()
    matched, rested = _placement_split(conn)
    return LiveBook(
        n=int(total_row["n"] or 0),
        total=float(total_row["pnl"] or 0.0),
        by_day=by_day,
        entries_matched=matched,
        entries_rested=rested,
    )


@dataclass
class BotState:
    mode: str
    state: str
    updated_at: str
    last_tick: str
    last_shadow: str
    accruing: bool
    ticks_last_10min: int
    cadence_ok: bool


def gather_bot_state(conn: sqlite3.Connection) -> BotState:
    cfg = {
        r["key"]: (r["value"], r["updated_at"])
        for r in conn.execute(
            "SELECT key, value, updated_at FROM config WHERE key IN "
            "('polymarket_bot.mode','polymarket_bot.state','polymarket_bot.updated_at')",
        ).fetchall()
    }
    last_tick = conn.execute(
        "SELECT MAX(created_at) m FROM paper_ticks",
    ).fetchone()["m"]
    last_shadow = conn.execute(
        "SELECT MAX(created_at) m FROM model_shadow_positions",
    ).fetchone()["m"]
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(seconds=CADENCE_WINDOW_SECONDS)).isoformat()
    ticks_last_10min = conn.execute(
        "SELECT COUNT(*) c FROM paper_ticks WHERE created_at >= ?",
        (cutoff,),
    ).fetchone()["c"]
    state = cfg.get("polymarket_bot.state", ("?", ""))[0]
    # "Accruing" means the loop is both marked running AND has produced a tick
    # within the last two window-lengths (10 min) of the snapshot.
    accruing = False
    if state == "running" and last_tick:
        try:
            age = (now - datetime.fromisoformat(str(last_tick))).total_seconds()
            accruing = age < CADENCE_WINDOW_SECONDS
        except ValueError:
            accruing = False
    # Cadence health (#157): a running loop should journal ~120 ticks / 10min.
    # Below the floor while "running" means a journaling stall the heartbeat
    # watchdog cannot see. Not applicable to a stopped bot.
    cadence_ok = state != "running" or ticks_last_10min >= MIN_TICKS_PER_WINDOW
    return BotState(
        mode=cfg.get("polymarket_bot.mode", ("?", ""))[0],
        state=state,
        updated_at=cfg.get("polymarket_bot.updated_at", ("", ""))[0],
        last_tick=str(last_tick or "—"),
        last_shadow=str(last_shadow or "—"),
        accruing=accruing,
        ticks_last_10min=int(ticks_last_10min),
        cadence_ok=cadence_ok,
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_text(
    models: list[ModelStats],
    live: LiveBook,
    bot: BotState,
    since: str,
    trades_per_day: float,
) -> str:
    lines: list[str] = []
    lines.append("=" * 92)
    lines.append(f"RACE STANDINGS — settled, fee-true, since {since}")
    lines.append("=" * 92)
    hdr = (
        f"{'model':<22}{'n':>4}{'total':>9}{'mean':>9}"
        f"{'t-CI95':>19}{'WR':>7}{'beWR':>7}{'maxDD':>8}{'today':>11}"
    )
    lines.append(hdr)
    for s in models:
        today = f"{s.today_n}/{s.today_total:+.1f}" if s.today_n else "—"
        lines.append(
            f"{s.model_id:<22}{s.n:>4}{s.total:>9.2f}{s.mean:>9.4f}"
            f"{'[%+.3f,%+.3f]' % (s.t_lo, s.t_hi):>19}"
            f"{s.win_rate:>7.3f}{s.breakeven_wr:>7.3f}{s.max_dd:>8.2f}{today:>11}"
        )

    lines.append("")
    lines.append("DEPLOY-BAR TRACKER (95% CI lower bound > 0, net of fees)")
    leader = models[0] if models else None
    for s in models:
        base = (
            f"  {s.model_id:<22} mean {s.mean:+.4f} (sd {s.sd:.2f}) · "
            f"boot95 [{s.boot_lo:+.3f},{s.boot_hi:+.3f}] · have {s.n} · "
        )
        if s.t_lo > 0 and s.n >= MIN_N_FOR_CLEARS:
            # The interval already excludes zero — the bar is met right now.
            lines.append(base + "CLEARS NOW")
        elif s.t_lo > 0:
            # Degenerate small-sample interval (e.g. one winning trade → sd 0
            # → CI collapses above zero). A verdict claim on a handful of
            # trades is exactly the false signal this tracker must not emit.
            lines.append(
                base + f"CI>0 but n<{MIN_N_FOR_CLEARS} — sample too small to mean anything"
            )
        elif s.req_n is None:
            # Non-positive point estimate can never clear as n grows.
            lines.append(base + "mean ≤ 0 — does not clear on current estimate")
        else:
            remaining = max(0, s.req_n - s.n)
            eta_days = remaining / trades_per_day if trades_per_day > 0 else float("inf")
            lines.append(base + f"need ~{s.req_n} (≈{eta_days:.0f}d more)")
    if leader is not None:
        lines.append(
            f"  (ETA assumes {trades_per_day:.0f} settled trades/day; "
            f"leader = {leader.model_id})"
        )

    lines.append("")
    lines.append("LIVE BOOK (real money)")
    lines.append(f"  lifetime: {live.n} fills, ${live.total:+.2f}")
    if live.by_day:
        recent = live.by_day[-3:]
        tail = "  ".join(f"{d} {n}×${p:+.2f}" for d, n, p in recent)
        lines.append(f"  recent days: {tail}")
    placements = live.entries_matched + live.entries_rested
    if placements:
        maker_pct = live.entries_rested / placements * 100
        lines.append(
            f"  entry placements: {live.entries_matched} crossed (taker) / "
            f"{live.entries_rested} rested (maker-eligible, fee-free) — "
            f"{maker_pct:.0f}% maker share (#137)"
        )

    lines.append("")
    lines.append("BOT STATE")
    accr = "YES" if bot.accruing else "NO"
    lines.append(
        f"  mode={bot.mode} state={bot.state} (updated {bot.updated_at})"
    )
    lines.append(f"  last tick={bot.last_tick} · last shadow row={bot.last_shadow}")
    lines.append(f"  race accruing: {accr}")
    cadence_mark = "✓" if bot.cadence_ok else "⚠️  DEGRADED"
    lines.append(
        f"  tick cadence: {bot.ticks_last_10min} in last 10min "
        f"(~120 healthy) {cadence_mark}"
    )
    if not bot.accruing:
        lines.append(
            "  ⚠️  NO NEW RACE DATA — the shadow race only advances while the "
            "PAPER loop runs. Restart it from the dashboard."
        )
    elif not bot.cadence_ok:
        # Alive (recent tick) but journaling has stalled — the #147 heartbeat
        # watchdog cannot see this; the race is under-accruing (#157).
        lines.append(
            "  ⚠️  JOURNALING STALL — loop is alive but tick cadence has "
            "collapsed (feed flapping?). The race is under-accruing even though "
            "it reads 'accruing: YES'."
        )
    return "\n".join(lines)


def render_json(
    models: list[ModelStats], live: LiveBook, bot: BotState, since: str
) -> str:
    return json.dumps(
        {
            "since": since,
            "models": [asdict(s) for s in models],
            "live_book": asdict(live),
            "bot": asdict(bot),
        },
        indent=2,
        default=str,
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=_config.DB_PATH)
    ap.add_argument("--since", default=COMMON_START, help="race common start (ISO)")
    ap.add_argument(
        "--trades-per-day",
        type=float,
        default=9.0,
        help="settled-trade rate for the deploy-bar ETA (leader ~9/day)",
    )
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    if not args.db.exists():
        sys.exit(f"ledger not found: {args.db}")

    snapshot = _snapshot(args.db)
    try:
        conn = _connect_immutable(snapshot)
        try:
            today = _today_utc()
            models = gather_models(conn, args.since, today)
            live = gather_live_book(conn)
            bot = gather_bot_state(conn)
        finally:
            conn.close()
    finally:
        snapshot.unlink(missing_ok=True)

    if args.json:
        print(render_json(models, live, bot, args.since))
    else:
        print(render_text(models, live, bot, args.since, args.trades_per_day))


if __name__ == "__main__":
    main()
