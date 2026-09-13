"""Reconcile the live paper-ledger against the REAL Polymarket account (issue #102).

The live executor books *assumed* fills/resolution/zero-fees (issue #103), so
``paper_positions(mode='live')`` drifts from what actually happened on the
venue. This tool corrects the historical ledger to ground truth pulled from the
Polymarket **Data API** (public, keyed by the funder wallet — no private key,
no orders).

Per window, real **economic** PnL is::

    pnl = sell_proceeds + redeem_proceeds + open_current_value - buy_cost

where a losing binary generates no REDEEM (its shares are worthless and sit as a
$0 open position), a winner REDEEMs at $1/share, and a not-yet-redeemed/unresolved
position carries its current open value. A DB window with **no** matching venue
buy is a *phantom* (an assumed fill that never executed) and is voided.

Inputs (read-only; already fetched):
    data/polymarket_activity_full.json   — /activity snapshot (buys/sells/redeems)
    data/polymarket_positions_full.json  — /positions snapshot (currentValue);
                                            fetched here if absent.

Usage::

    .venv/bin/python tools/reconcile_live_ledger.py --dry-run     # show the diff
    .venv/bin/python tools/reconcile_live_ledger.py --apply        # write (back up first!)
"""

from __future__ import annotations

import argparse
import collections
import csv
import json
import sqlite3
import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config as _config  # noqa: E402

DATA = ROOT / "data"
ACTIVITY = DATA / "polymarket_activity_full.json"
POSITIONS = DATA / "polymarket_positions_full.json"
CSV_OUT = DATA / "reconcile_corrections.csv"
RECON_TAG = "recon:dataapi"
_UA = {"User-Agent": "Mozilla/5.0"}


# ---------------------------------------------------------------------------
# Ground-truth loaders (read-only)
# ---------------------------------------------------------------------------


def _fetch_positions(addr: str) -> list[dict]:
    out: list[dict] = []
    off = 0
    while True:
        url = (
            f"https://data-api.polymarket.com/positions?user={addr}"
            f"&limit=100&offset={off}&sizeThreshold=0"
        )
        req = urllib.request.Request(url, headers=_UA)
        with urllib.request.urlopen(req, timeout=30) as r:
            batch = json.load(r)
        if not batch:
            break
        out.extend(batch)
        off += 100
        if len(batch) < 100:
            break
    return out


def load_truth(addr: str) -> tuple[dict, dict]:
    """Return (per-window aggregates, conditionId -> open currentValue)."""
    activity = json.loads(ACTIVITY.read_text())
    if POSITIONS.exists():
        positions = json.loads(POSITIONS.read_text())
    else:
        positions = _fetch_positions(addr)
        POSITIONS.write_text(json.dumps(positions))

    open_val: dict[str, float] = collections.defaultdict(float)
    for p in positions:
        open_val[p.get("conditionId")] += float(p.get("currentValue") or 0)

    win: dict[str, dict] = collections.defaultdict(
        lambda: {"buy": 0.0, "bsh": 0.0, "sell": 0.0, "red": 0.0, "conds": set()}
    )
    for r in activity:
        w = r.get("eventSlug") or r.get("slug")
        if not w:
            continue
        g = win[w]
        usd = float(r.get("usdcSize") or 0)
        sz = float(r.get("size") or 0)
        typ, side = r.get("type"), (r.get("side") or "").upper()
        if r.get("conditionId"):
            g["conds"].add(r["conditionId"])
        if typ == "TRADE" and side == "BUY":
            g["buy"] += usd
            g["bsh"] += sz
        elif typ == "TRADE" and side == "SELL":
            g["sell"] += usd
        elif typ == "REDEEM":
            g["red"] += usd
    return win, open_val


def economic(win: dict, open_val: dict, slug: str) -> tuple[float, float, float, float]:
    """Return (cost, shares, proceeds_incl_open, pnl) for a window."""
    g = win[slug]
    ov = sum(open_val.get(c, 0.0) for c in g["conds"])
    proceeds = g["sell"] + g["red"] + ov
    return g["buy"], g["bsh"], proceeds, proceeds - g["buy"]


def lifetime_pnls(addr_activity: list[dict]) -> tuple[float, float]:
    """Return (btc_only, whole_account) realized cash PnL from activity."""

    def flow(rows: list[dict]) -> float:
        return sum(
            float(r.get("usdcSize") or 0)
            * (1 if r.get("type") == "REDEEM" or (r.get("side") or "").upper() == "SELL" else -1)
            for r in rows
            if r.get("type") in ("TRADE", "REDEEM")
        )

    # Isolate the bot's BTC trades by the structural slug prefix, not the title
    # substring (#113). The bot only ever trades ``btc-updown-5m-{ts}`` markets
    # (the hardcoded discovery contract), so the slug is the robust discriminator
    # — immune to null/renamed titles and to non-bot markets that merely mention
    # Bitcoin. Verified on real data: 648/648 BTC rows match, 0 false positives.
    btc = [
        r
        for r in addr_activity
        if (r.get("eventSlug") or r.get("slug") or "").startswith("btc-updown-5m-")
    ]
    return round(flow(btc), 4), round(flow(addr_activity), 4)


# ---------------------------------------------------------------------------
# Reconcile
# ---------------------------------------------------------------------------


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat()


def _newest_live_epoch(db_path: Path) -> int:
    """Unix-seconds of the most recent live position's opened_at (0 if none)."""
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT MAX(opened_at) FROM paper_positions WHERE mode='live'"
    ).fetchone()
    conn.close()
    if not row or not row[0]:
        return 0
    try:
        return int(datetime.fromisoformat(row[0]).timestamp())
    except ValueError:
        return 0


def build_plan(db_path: Path, win: dict, open_val: dict) -> tuple[list[dict], list[dict]]:
    """Return (corrections, phantoms) for the live closed positions."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    live = conn.execute(
        "SELECT position_id, window_slug, side, entry_price, exit_price, shares, "
        "notional_usd, realized_pnl_usd, exit_reason FROM paper_positions "
        "WHERE mode='live' AND state='closed' ORDER BY opened_at"
    ).fetchall()
    conn.close()

    corrections: list[dict] = []
    phantoms: list[dict] = []
    for r in live:
        g = win.get(r["window_slug"])
        if not g or g["bsh"] <= 0:
            phantoms.append(dict(r))
            continue
        cost, sh, proceeds, pnl = economic(win, open_val, r["window_slug"])
        db_pnl = r["realized_pnl_usd"] or 0.0
        disagree = (db_pnl > 0.5 and pnl < -0.5) or (db_pnl < -0.5 and pnl > 0.5)
        corrections.append(
            {
                "position_id": r["position_id"],
                "window_slug": r["window_slug"],
                "side": r["side"],
                "db_entry": r["entry_price"],
                "db_exit": r["exit_price"],
                "db_shares": r["shares"],
                "db_notional": round(r["notional_usd"], 4),
                "db_pnl": round(db_pnl, 4),
                "real_entry": round(cost / sh, 6),
                "real_exit": round(proceeds / sh, 6),
                "real_shares": round(sh, 6),
                "real_notional": round(cost, 6),
                "real_pnl": round(pnl, 6),
                "delta_pnl": round(db_pnl - pnl, 4),
                "resolution_disagree": disagree,
                "exit_reason": r["exit_reason"] or "",
            }
        )
    return corrections, phantoms


def apply_plan(
    db_path: Path,
    corrections: list[dict],
    phantoms: list[dict],
    recon_keys: dict[str, str],
    updated_at: str,
) -> None:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("BEGIN")
    for c in corrections:
        er = c["exit_reason"]
        if RECON_TAG not in er:
            tag = f" | {RECON_TAG}"
            if c["resolution_disagree"]:
                tag += f" RESOLUTION-DISAGREE db={c['db_pnl']:+.2f} real={c['real_pnl']:+.2f}"
            er = er + tag
        cur.execute(
            "UPDATE paper_positions SET entry_price=?, exit_price=?, shares=?, "
            "notional_usd=?, realized_pnl_usd=?, exit_reason=? WHERE position_id=?",
            (
                c["real_entry"],
                c["real_exit"],
                c["real_shares"],
                c["real_notional"],
                c["real_pnl"],
                er,
                c["position_id"],
            ),
        )
    for p in phantoms:
        er = p["exit_reason"] or ""
        if RECON_TAG not in er:
            er = er + f" | {RECON_TAG} PHANTOM-no-venue-fill"
        cur.execute(
            "UPDATE paper_positions SET state='void', realized_pnl_usd=0, "
            "notional_usd=0, shares=0, exit_price=0, exit_reason=? WHERE position_id=?",
            (er, p["position_id"]),
        )
    for k, v in recon_keys.items():
        cur.execute(
            "INSERT INTO config(key, value, updated_at) VALUES(?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (k, str(v), updated_at),
        )
    conn.commit()
    conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="print the diff, write CSV, no DB writes")
    g.add_argument("--apply", action="store_true", help="apply the corrections to the DB")
    ap.add_argument("--db", type=Path, default=_config.DB_PATH)
    ap.add_argument("--asof", default=None, help="ISO timestamp recorded in recon.asof")
    ap.add_argument(
        "--force", action="store_true",
        help="apply even when the snapshot looks stale vs the ledger (skips the staleness guard)",
    )
    args = ap.parse_args()

    addr = _config.POLYMARKET_FUNDER
    if not addr:
        raise SystemExit("POLYMARKET_FUNDER not set — cannot identify the account.")

    win, open_val = load_truth(addr)
    activity = json.loads(ACTIVITY.read_text())
    btc_pnl, acct_pnl = lifetime_pnls(activity)
    open_total = round(sum(open_val.values()), 4)

    corrections, phantoms = build_plan(args.db, win, open_val)
    db_sum = sum(c["db_pnl"] for c in corrections) + sum((p["realized_pnl_usd"] or 0) for p in phantoms)
    real_sum = sum(c["real_pnl"] for c in corrections)
    disagrees = [c for c in corrections if c["resolution_disagree"]]

    with CSV_OUT.open("w", newline="") as f:
        cols = [k for k in corrections[0] if k != "exit_reason"]
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(corrections)

    print(f"corrections: {len(corrections)}  phantoms: {len(phantoms)}  "
          f"resolution-disagreements (flagged): {len(disagrees)}")
    print(f"DB live PnL (closed): ${db_sum:+.2f}  ->  corrected: ${real_sum:+.2f}")
    print(f"phantom fictional PnL removed: ${sum(p['realized_pnl_usd'] or 0 for p in phantoms):+.2f}")
    print(f"REAL lifetime: BTC ${btc_pnl:+.2f} | account ${acct_pnl:+.2f} | open value ${open_total:+.2f}")
    print(f"diff CSV -> {CSV_OUT}")

    if args.dry_run:
        print("\nDRY RUN — no DB writes. Re-run with --apply to commit.")
        return

    # Staleness guard (#103): refuse to --apply when the ledger holds live trades
    # newer than the Data-API snapshot — the API has not indexed them yet, so they
    # would be voided as phantoms (the near-miss that put 2 fresh real trades in
    # the phantom list mid-session). Stop the bot and re-pull fresh data first.
    newest_activity = max((int(r.get("timestamp") or 0) for r in activity), default=0)
    newest_db = _newest_live_epoch(args.db)
    if not args.force and newest_db and newest_activity and newest_db > newest_activity + 120:
        raise SystemExit(
            f"STALE snapshot: newest live position ({_iso(newest_db)}) is newer than the "
            f"activity snapshot ({_iso(newest_activity)}). Stop the bot, re-pull fresh data, "
            f"then --apply (or pass --force if the recent windows are already settled)."
        )

    asof = args.asof or datetime.now(UTC).isoformat()
    recon_keys = {
        "recon.real_btc_pnl_lifetime": btc_pnl,
        "recon.real_account_pnl_lifetime": acct_pnl,
        "recon.open_positions_value": open_total,
        "recon.corrected_live_pnl": round(real_sum, 4),
        "recon.phantoms_voided": len(phantoms),
        "recon.source": "polymarket-data-api",
        "recon.asof": asof,
        "recon.note": "live ledger reconciled to real fills/redemptions; phantoms voided (#102)",
    }
    apply_plan(args.db, corrections, phantoms, recon_keys, asof)
    print(f"\nAPPLIED to {args.db}. Wrote {len(recon_keys)} recon.* keys.")


if __name__ == "__main__":
    main()
