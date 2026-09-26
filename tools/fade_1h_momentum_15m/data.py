"""Data layer for the Fade 1h Momentum on 15m historical test.

Pre-registration: tasks/2026-09-21-fade-1h-momentum-on-15m.md, section 8.

Two sources:

- Binance SPOT 1-minute klines for BTC/ETH/SOL/XRP, 2026-02-28 .. 2026-09-21 UTC,
  built once into ``data/fade_1h_momentum_15m/spot1m.db`` (table ``k1m``) by
  :func:`build_spot`. Whole months come from the monthly bulk zips, the rest
  from daily zips, the REST API only when a zip is missing. Every zip is checked
  against its published SHA-256. Re-running skips what is already stored.
- The Polymarket tapes (read-only): ``m15.db`` (15m markets, both sides of every
  match), ``wallets.db`` (family ``1h``) and ``maker15.db`` (taker side of each
  m15 match).

Time convention (the look-ahead trap): a kline with open time ``t`` has
``o`` = price AT ``t`` and ``c`` = price at ``t + 60``. A decision at instant
``T`` may use ``o`` of the minute opening at ``T`` and ``c`` of minutes with
``t + 60 <= T``, nothing else.

    PYTHON_DOTENV_DISABLED=1 python3 tools/fade_1h_momentum_15m/data.py              # build + integrity report
    PYTHON_DOTENV_DISABLED=1 python3 tools/fade_1h_momentum_15m/data.py --report-only
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
import sqlite3
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple

import numpy as np

REPO = Path(__file__).resolve().parents[2]
OUT_DIR = REPO / "data" / "fade_1h_momentum_15m"
SPOT_DB = OUT_DIR / "spot1m.db"

SYMBOLS = {"btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT", "xrp": "XRPUSDT"}
SPOT_START = int(datetime(2026, 2, 28, tzinfo=UTC).timestamp())  # February's tail: March's trailing history
SPOT_END = int(datetime(2026, 9, 21, tzinfo=UTC).timestamp())  # exclusive: last candle opens 2026-09-20 23:59

BULK = "https://data.binance.vision/data/spot"
REST = "https://api.binance.com/api/v3/klines"
UA = {"User-Agent": "Mozilla/5.0"}

Q15 = 900  # a 15m window, seconds
H1 = 3600


# --------------------------------------------------------------------------- paths


def pm_dir() -> Path:
    """Folder holding m15.db / wallets.db / maker15.db.

    ``FADE_PM_DIR`` overrides. Otherwise this checkout's ``data/wallet_research``,
    falling back to the main checkout's when running from a git worktree (the
    databases are gitignored and live only in the main checkout).
    """
    if env := os.environ.get("FADE_PM_DIR"):
        return Path(env)
    here = REPO / "data" / "wallet_research"
    if (here / "m15.db").exists():
        return here
    git = REPO / ".git"
    if git.is_file():  # worktree: "gitdir: <main>/.git/worktrees/<name>"
        gitdir = Path(git.read_text().split("gitdir:", 1)[1].strip())
        return gitdir.parents[2] / "data" / "wallet_research"
    return here


@lru_cache(maxsize=None)
def _ro(path: str) -> sqlite3.Connection:
    """One cached read-only connection per database file."""
    if not Path(path).exists():
        raise FileNotFoundError(path)
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _pm(db: str | Path | sqlite3.Connection) -> sqlite3.Connection:
    """'m15' | 'wallets' | 'maker15' | a path | an open connection."""
    if isinstance(db, sqlite3.Connection):
        return db
    if str(db) in ("m15", "wallets", "maker15"):
        db = pm_dir() / f"{db}.db"
    return _ro(str(Path(db).resolve()))


# --------------------------------------------------------------------------- build


def _ts(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp())


def _day(ts: int) -> date:
    return datetime.fromtimestamp(ts, UTC).date()


def _units(start: int, end: int) -> list[tuple[str, str, int, int]]:
    """Download units covering [start, end): (kind, label, lo, hi).

    A calendar month wholly inside the range is one monthly zip; any partial
    month is split into daily zips.
    """
    if start % 86400 or end % 86400:
        raise ValueError("start and end must be UTC midnights")
    out: list[tuple[str, str, int, int]] = []
    d, stop = _day(start), _day(end)
    while d < stop:
        first = d.replace(day=1)
        nxt = (first + timedelta(days=32)).replace(day=1)
        if d == first and nxt <= stop:
            out.append(("monthly", f"{d:%Y-%m}", _ts(d), _ts(nxt)))
            d = nxt
            continue
        while d < min(nxt, stop):
            out.append(("daily", f"{d:%Y-%m-%d}", _ts(d), _ts(d + timedelta(days=1))))
            d += timedelta(days=1)
    return out


def _days_of(lo: int, hi: int) -> list[tuple[str, str, int, int]]:
    return [("daily", f"{_day(t):%Y-%m-%d}", t, t + 86400) for t in range(lo, hi, 86400)]


def _get(url: str, retries: int = 4) -> bytes | None:
    """Body of ``url``, None on 404, raises after ``retries`` transient failures."""
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if attempt == retries - 1:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == retries - 1:
                raise
        time.sleep(2 ** attempt)
    return None


def _to_sec(x: int) -> int:
    """Binance open time -> unix seconds. Bulk spot files from 2025-01 are in microseconds."""
    if x >= 10**14:
        return x // 1_000_000
    if x >= 10**11:
        return x // 1000
    return x


def _zip_rows(blob: bytes, lo: int, hi: int) -> Iterator[tuple[int, float, float, float, float, float]]:
    """Stream (t, o, h, l, c, v) rows with lo <= t < hi out of one kline zip."""
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        name = next(n for n in zf.namelist() if n.endswith(".csv"))
        with zf.open(name) as fh:
            for rec in csv.reader(io.TextIOWrapper(fh, "ascii")):
                if not rec or not rec[0].isdigit():  # header row in some files
                    continue
                t = _to_sec(int(rec[0]))
                if t % 60:
                    raise ValueError(f"{name}: open time {rec[0]} is not on a minute")
                if lo <= t < hi:
                    yield t, float(rec[1]), float(rec[2]), float(rec[3]), float(rec[4]), float(rec[5])


def _rest_rows(sym: str, lo: int, hi: int) -> Iterator[tuple[int, float, float, float, float, float]]:
    s = lo * 1000
    while s < hi * 1000:
        body = _get(f"{REST}?symbol={sym}&interval=1m&startTime={s}&endTime={hi * 1000 - 1}&limit=1000")
        rows = json.loads(body) if body else []
        if not rows:
            return
        for r in rows:
            yield _to_sec(int(r[0])), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])
        s = int(rows[-1][0]) + 60_000
        time.sleep(0.1)


def _open_spot_rw(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("create table if not exists k1m(sym TEXT, t INTEGER, o REAL, h REAL, l REAL, c REAL, v REAL, "
               "PRIMARY KEY(sym, t))")
    db.execute("create table if not exists loaded(sym TEXT, unit TEXT, source TEXT, url TEXT, n_rows INTEGER, "
               "sha256_ok INTEGER, loaded_at INTEGER, PRIMARY KEY(sym, unit))")
    return db


def _store(db: sqlite3.Connection, sym: str, rows: Iterator[tuple]) -> int:
    before = db.total_changes
    db.executemany("insert or ignore into k1m values (?,?,?,?,?,?,?)", ((sym, *r) for r in rows))
    return db.total_changes - before


def build_spot(syms: list[str] | None = None, start: int = SPOT_START, end: int = SPOT_END,
               db_path: Path = SPOT_DB, verbose: bool = True) -> dict[str, int]:
    """Download Binance spot 1m klines into ``k1m``; idempotent. Returns rows added per symbol.

    One zip is in memory at a time. A unit is skipped when it is recorded in
    ``loaded`` or already has every minute stored.
    """
    db = _open_spot_rw(db_path)
    added: dict[str, int] = {}
    for sym in syms or list(SYMBOLS.values()):
        added[sym] = 0
        done = {u for (u,) in db.execute("select unit from loaded where sym=?", (sym,))}
        queue = _units(start, end)
        while queue:
            kind, label, lo, hi = queue.pop(0)
            have = db.execute("select count(*) from k1m where sym=? and t>=? and t<?", (sym, lo, hi)).fetchone()[0]
            if label in done or have == (hi - lo) // 60:
                continue
            url = f"{BULK}/{kind}/klines/{sym}/1m/{sym}-1m-{label}.zip"
            blob = _get(url)
            if blob is None and kind == "monthly":
                queue[:0] = _days_of(lo, hi)  # monthly not published: fall back to its days
                continue
            if blob is None:
                source, sha_ok = "rest", None
                n = _store(db, sym, _rest_rows(sym, lo, hi))
                url = REST
            else:
                source = kind
                chk = _get(url + ".CHECKSUM")
                sha_ok = None if chk is None else int(chk.split()[0].decode() == hashlib.sha256(blob).hexdigest())
                if sha_ok == 0:
                    raise ValueError(f"checksum mismatch: {url}")
                n = _store(db, sym, _zip_rows(blob, lo, hi))
                del blob
            db.execute("insert or replace into loaded values (?,?,?,?,?,?,?)",
                       (sym, label, source, url, n, sha_ok, int(time.time())))
            db.commit()
            added[sym] += n
            if verbose:
                print(f"  {sym} {label:10} {source:7} +{n:6d} rows (had {have})", flush=True)
    db.close()
    return added


# --------------------------------------------------------------------------- spot loaders


class Bars(NamedTuple):
    """1-minute candles; ``t`` = open time (unix s). o at t, c at t + 60."""

    t: np.ndarray
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray  # noqa: E741 - candle low
    c: np.ndarray
    v: np.ndarray


class Quarters(NamedTuple):
    """15m candles on UTC quarter-hours; ``t`` = open time. NaN where no minute exists.

    ``o`` is the open of the quarter's first stored minute and ``c`` the close of
    its last; they are the exact quarter open/close only when ``complete``.
    """

    t: np.ndarray
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray  # noqa: E741 - candle low
    c: np.ndarray
    v: np.ndarray
    n_min: np.ndarray  # minutes present, 0..15
    complete: np.ndarray  # all 15 minutes present


def _sym(sym: str) -> str:
    return SYMBOLS.get(sym.lower(), sym.upper())


def load_1m(sym: str, start_ts: int, end_ts: int, db_path: Path = SPOT_DB) -> Bars:
    """Minutes with open time in [start_ts, end_ts), sorted. ``sym``: 'btc' or 'BTCUSDT'."""
    cur = _ro(str(db_path)).execute(
        "select t, o, h, l, c, v from k1m where sym=? and t>=? and t<? order by t", (_sym(sym), start_ts, end_ts))
    a = np.fromiter(cur, dtype=[("t", "i8"), ("o", "f8"), ("h", "f8"), ("l", "f8"), ("c", "f8"), ("v", "f8")])
    return Bars(*(np.ascontiguousarray(a[f]) for f in a.dtype.names))


def candles_15m(sym: str, start_ts: int, end_ts: int, db_path: Path = SPOT_DB) -> Quarters:
    """Every UTC quarter-hour with open time in [ceil15(start_ts), end_ts), aggregated exactly from 1m.

    open = o of the minute at the quarter's open, close = c of the minute opening
    at +14, high/low = extremes, volume = sum. The grid is complete (empty
    quarters are NaN rows), so index i is always quarter ``t[0] + 900 * i``.
    """
    q0 = -(-start_ts // Q15) * Q15
    nq = max(0, -(-(end_ts - q0) // Q15))
    t = q0 + Q15 * np.arange(nq, dtype=np.int64)
    b = load_1m(sym, q0, q0 + Q15 * nq, db_path)
    grid = {f: np.full((nq, 15), np.nan) for f in "ohlcv"}
    qi, mi = (b.t - q0) // Q15, ((b.t - q0) % Q15) // 60
    for f in "ohlcv":
        grid[f][qi, mi] = getattr(b, f)
    present = ~np.isnan(grid["o"])
    n_min = present.sum(axis=1)
    rows = np.arange(nq)
    first = np.argmax(present, axis=1)
    last = 14 - np.argmax(present[:, ::-1], axis=1)
    empty = n_min == 0
    o = np.where(empty, np.nan, grid["o"][rows, first])
    c = np.where(empty, np.nan, grid["c"][rows, last])
    with np.errstate(all="ignore"):
        h = np.where(empty, np.nan, np.nanmax(np.where(present, grid["h"], -np.inf), axis=1))
        lo = np.where(empty, np.nan, np.nanmin(np.where(present, grid["l"], np.inf), axis=1))
    v = np.where(empty, np.nan, np.nansum(grid["v"], axis=1))
    return Quarters(t, o, h, lo, c, v, n_min, n_min == 15)


# --------------------------------------------------------------------------- Polymarket loaders


class Market(NamedTuple):
    cid: str
    slug: str
    asset: str  # btc / eth / sol / xrp
    start_ts: int
    end_ts: int
    winner: int  # winning oidx: 0 = Up, 1 = Down


class Print(NamedTuple):
    ts: int
    oidx: int  # 0 = Up, 1 = Down
    side: str  # BUY / SELL, from this wallet's point of view
    price: float  # on oidx's token; Up-equivalent of a Down print is 1 - price
    size: float
    txh: str
    wallet: str


def _markets(db: str, family: str, length: int) -> list[Market]:
    cur = _pm(db).execute(
        "select cid, slug, series, end_ts, winner from markets "
        "where family=? and fetched=1 and winner is not null order by end_ts, series", (family,))
    return [Market(cid, slug, series.split("-", 1)[0], end - length, end, int(w)) for cid, slug, series, end, w in cur]


def markets_15m() -> list[Market]:
    """The settled 15m markets in m15.db (window [end_ts - 900, end_ts))."""
    return _markets("m15", "15m", Q15)


def markets_1h() -> list[Market]:
    """The settled hourly markets in wallets.db family '1h' (window [end_ts - 3600, end_ts))."""
    return _markets("wallets", "1h", H1)


def prints(db: str | Path | sqlite3.Connection, cid: str) -> list[Print]:
    """Every trade row of one market, sorted by ts (then tape order).

    m15.db holds BOTH sides of every match (the taker's row and each maker's
    row), so summing sizes counts volume twice; use :func:`taker_keys` to keep
    one side. ``db``: 'm15' | 'wallets' | a path | a connection.
    """
    cur = _pm(db).execute(
        "select ts, oidx, side, price, size, txh, wallet from trades where cid=? order by ts, rowid", (cid,))
    return [Print(int(ts), int(o), s, float(p), float(z), (txh or "").lower(), (w or "").lower())
            for ts, o, s, p, z, txh, w in cur]


def taker_keys(cid: str) -> set[tuple[str, str]]:
    """(txh, wallet) of the taker side of each m15 match. A print whose key is absent was a maker fill."""
    cur = _pm("maker15").execute("select txh, wallet from tk where cid=?", (cid,))
    return {((txh or "").lower(), (w or "").lower()) for txh, w in cur}


# --------------------------------------------------------------------------- integrity report


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), UTC).strftime("%Y-%m-%d %H:%M")


def _wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return float("nan"), float("nan")
    p = k / n
    d = 1 + z * z / n
    mid = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return mid - half, mid + half


def p_two_sided(t: float) -> float:
    """Two-sided p of a t-statistic read against the standard normal."""
    return math.erfc(abs(t) / math.sqrt(2.0))


def holm(p: list[float]) -> list[float]:
    """Holm step-down adjusted p-values (family-wise error), in the input order."""
    n = len(p)
    order = sorted(range(n), key=lambda i: p[i])
    out, run = [0.0] * n, 0.0
    for rank, i in enumerate(order):
        run = max(run, min(1.0, (n - rank) * p[i]))
        out[i] = run
    return out


def bh(p: list[float]) -> list[float]:
    """Benjamini-Hochberg adjusted p-values (false-discovery rate), in the input order."""
    n = len(p)
    order = sorted(range(n), key=lambda i: p[i], reverse=True)
    out, run = [0.0] * n, 1.0
    for k, i in enumerate(order):
        run = min(run, p[i] * n / (n - k))
        out[i] = run
    return out


def spot_integrity(start: int = SPOT_START, end: int = SPOT_END) -> dict:
    out: dict = {}
    expected = (end - start) // 60
    src = _ro(str(SPOT_DB)).execute(
        "select sym, source, count(*), sum(n_rows), sum(sha256_ok=1) from loaded group by sym, source").fetchall()
    for sym in SYMBOLS.values():
        b = load_1m(sym, start, end)
        n = len(b.t)
        gaps = []
        if n:
            edges = np.concatenate(([start - 60], b.t, [end]))
            d = np.diff(edges) // 60 - 1  # missing minutes before each stored minute (and after the last)
            for i in np.flatnonzero(d > 0):
                gaps.append((int(edges[i] + 60), int(d[i])))
        gaps.sort(key=lambda g: -g[1])
        bad_hl = int(np.sum((b.h < np.maximum(b.o, b.c)) | (b.l > np.minimum(b.o, b.c))))
        cont = np.flatnonzero(np.diff(b.t) == 60)
        jump_bp = 1e4 * np.abs(b.o[cont + 1] / b.c[cont] - 1)
        o_ne_prev_c = int(np.sum(jump_bp > 0))
        out[sym] = {
            "rows": n, "expected": expected, "missing_minutes": expected - n,
            "first_t": int(b.t[0]) if n else None, "last_t": int(b.t[-1]) if n else None,
            "first": _iso(b.t[0]) if n else None, "last": _iso(b.t[-1]) if n else None,
            "n_gaps": len(gaps),
            "longest_gaps": [{"from": _iso(g0), "minutes": g1} for g0, g1 in gaps[:5]],
            "hl_inconsistent_rows": bad_hl,
            "open_differs_from_prev_close": o_ne_prev_c, "adjacent_pairs": len(cont),
            "open_vs_prev_close_bp_when_different": {
                "median": float(np.median(jump_bp[jump_bp > 0])) if o_ne_prev_c else 0.0,
                "p99": float(np.percentile(jump_bp[jump_bp > 0], 99)) if o_ne_prev_c else 0.0,
                "max": float(jump_bp.max()) if len(jump_bp) else 0.0},
            "sources": {s: {"units": u, "rows": r, "sha256_ok": ok} for sy, s, u, r, ok in src if sy == sym},
        }
    return out


def settlement_agreement() -> dict:
    """Binance direction (close >= open, ties Up) vs the Polymarket winner, every market in the tapes."""
    res: dict = {}
    for label, mkts in (("15m", markets_15m()), ("1h", markets_1h())):
        by_asset: dict[str, list[Market]] = {}
        for m in mkts:
            by_asset.setdefault(m.asset, []).append(m)
        res[label] = {"n_markets": len(mkts), "by_asset": {}, "disagreements": []}
        pooled: list[tuple[float, bool, int]] = []  # (|move| bp, agree, winner) over every compared market
        for asset, ms in sorted(by_asset.items()):
            lo, hi = min(m.start_ts for m in ms), max(m.end_ts for m in ms)
            b = load_1m(asset, lo, hi)
            idx = {int(t): i for i, t in enumerate(b.t)}
            n = agree = skipped = interior_gap = 0
            moves, dis_moves = [], []
            for m in ms:
                i0, i1 = idx.get(m.start_ts), idx.get(m.end_ts - 60)
                if i0 is None or i1 is None:  # open or close minute missing: direction unknown
                    skipped += 1
                    continue
                interior_gap += not all(t in idx for t in range(m.start_ts, m.end_ts, 60))
                op, cl = b.o[i0], b.c[i1]
                up = 0 if cl >= op else 1
                move_bp = 1e4 * (cl / op - 1)
                n += 1
                agree += up == m.winner
                moves.append(abs(move_bp))
                pooled.append((abs(move_bp), up == m.winner, m.winner))
                if up != m.winner:
                    dis_moves.append(abs(move_bp))
                    res[label]["disagreements"].append({
                        "slug": m.slug, "asset": asset, "window_utc": _iso(m.start_ts),
                        "binance_open": op, "binance_close": cl, "move_bp": round(move_bp, 3),
                        "binance_side": "Up" if up == 0 else "Down",
                        "polymarket_winner": "Up" if m.winner == 0 else "Down"})
            w = _wilson(agree, n)
            res[label]["by_asset"][asset] = {
                "n": n, "agree": agree, "rate": agree / n if n else None,
                "wilson95": [round(w[0], 4), round(w[1], 4)],
                "skipped_open_or_close_minute_missing": skipped,
                "included_with_interior_gap": interior_gap,
                "median_abs_move_bp_all": float(np.median(moves)) if moves else None,
                "median_abs_move_bp_disagree": float(np.median(dis_moves)) if dis_moves else None}
        tot_n = sum(v["n"] for v in res[label]["by_asset"].values())
        tot_a = sum(v["agree"] for v in res[label]["by_asset"].values())
        res[label]["all"] = {"n": tot_n, "agree": tot_a, "rate": tot_a / tot_n if tot_n else None,
                             "wilson95": [round(x, 4) for x in _wilson(tot_a, tot_n)]}
        ties = [w for mv, _, w in pooled if mv == 0.0]
        res[label]["binance_exact_ties"] = {"n": len(ties), "polymarket_up": ties.count(0)}
        res[label]["by_abs_move_bp"] = []
        for band, lo_bp, hi_bp in (("=0", -1.0, 0.0), ("(0,1]", 0.0, 1.0), ("(1,2]", 1.0, 2.0),
                                   ("(2,5]", 2.0, 5.0), ("(5,10]", 5.0, 10.0), (">10", 10.0, math.inf)):
            sel = [ok for mv, ok, _ in pooled if lo_bp < mv <= hi_bp]
            res[label]["by_abs_move_bp"].append({"band": band, "n": len(sel), "agree": sum(sel),
                                                 "rate": sum(sel) / len(sel) if sel else None})
        res[label]["disagreements"].sort(key=lambda d: abs(d["move_bp"]))
    return res


def tape_checks() -> dict:
    """Loader sanity: slug start time vs end_ts - 900, and taker coverage of m15."""
    ms = markets_15m()
    slug_bad = [m.slug for m in ms if not m.slug.endswith(str(m.start_ts))]
    covered = {c for (c,) in _pm("maker15").execute("select cid from done")}
    no_taker = [c for (c,) in _pm("maker15").execute("select cid from done where n_taker=0")]
    return {"m15_markets": len(ms), "slug_start_mismatch": len(slug_bad),
            "maker15_done_covers_all": all(m.cid in covered for m in ms),
            "maker15_markets_with_zero_taker_rows": len(no_taker),
            "h1_markets": len(markets_1h())}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--report-only", action="store_true", help="skip the download, report on what is stored")
    ap.add_argument("--json", default=str(OUT_DIR / "integrity.json"))
    args = ap.parse_args()

    if not args.report_only:
        print(f"building {SPOT_DB} ({_iso(SPOT_START)} .. {_iso(SPOT_END)} UTC)")
        added = build_spot()
        print("rows added:", added)

    report = {"spot": spot_integrity(), "tapes": tape_checks(), "agreement": settlement_agreement()}
    Path(args.json).parent.mkdir(parents=True, exist_ok=True)
    Path(args.json).write_text(json.dumps(report, indent=1, default=float))

    print("\nspot 1m integrity")
    for sym, r in report["spot"].items():
        gaps = ", ".join(f"{g['from']} ({g['minutes']}m)" for g in r["longest_gaps"][:3]) or "none"
        print(f"  {sym:8} rows {r['rows']:7d}/{r['expected']}  missing {r['missing_minutes']:4d} in {r['n_gaps']} gaps  "
              f"{r['first']} .. {r['last']}  longest: {gaps}")
    print("\ntapes", report["tapes"])
    for label in ("15m", "1h"):
        a = report["agreement"][label]
        print(f"\nBinance direction vs Polymarket winner, {label} ({a['n_markets']} markets)")
        for asset, r in a["by_asset"].items():
            dis = r["median_abs_move_bp_disagree"]
            print(f"  {asset}: {r['agree']}/{r['n']} = {r['rate']:.4f}  95% {r['wilson95']}  "
                  f"skipped {r['skipped_open_or_close_minute_missing']}  "
                  f"median |move| all {r['median_abs_move_bp_all']:.2f}bp, "
                  f"disagreements {'-' if dis is None else f'{dis:.2f}bp'}")
        print(f"  all: {a['all']['agree']}/{a['all']['n']} = {a['all']['rate']:.4f}  95% {a['all']['wilson95']}")
        print(f"  Binance exact ties (close == open): {a['binance_exact_ties']}")
        print("  by |Binance move|: " + "  ".join(
            f"{r['band']}bp {r['agree']}/{r['n']}" for r in a["by_abs_move_bp"]))
        for d in a["disagreements"][:8]:
            print(f"    {d['slug']:40} {d['window_utc']}  {d['move_bp']:+8.3f}bp  "
                  f"Binance {d['binance_side']:4} Polymarket {d['polymarket_winner']}")
    print(f"\nwrote {args.json}")


if __name__ == "__main__":
    main()
