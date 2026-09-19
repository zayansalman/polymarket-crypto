"""Win-streak instrument for the BTC 5m up/down window (parked research).

Answers one question with the full recorded history rather than intuition:
**how long does a run of consecutive correct 5-minute calls actually last, and
what does a ride-the-stack ladder pay if you stop at N wins?**

The prompt was the "double your stake 17 times and you're a millionaire in 90
minutes" framing. It is worth measuring because the *same* arithmetic governs
any compounding-stake scheme on these markets, and because the answer turns out
to be a book-depth statement, not a probability statement.

Method, and the guards that keep it honest:

* **Real windows, real settlement rule** — every complete 5-minute bucket in
  the Binance 1m archive (Jul 2024 - Jul 2026), resolved Up when
  ``close >= open``, matching ``polymarket_bot/backtest.py`` (ties credit Up,
  per the market rules page). No synthetic coin flips.
* **A-priori sides** — the bet rule is declared up front (always Up, always
  Down, or a seeded random pick). Nothing is fitted to the sample; there is no
  signal here to overfit.
* **The spread is charged to the payout, not the win rate** — paying the ask
  does not change how often the window closes up, it changes what a win
  compounds at (1/price per rung). Measured spread on the recorded book is 1c,
  so the default entry is 50.5c => 1.98x per win, not 2x.
* **Depth is a separate gate** — streak probability says a 17-win ladder is
  rare; the recorded top-of-book says the later rungs are unfillable at any
  probability. Both are reported, because the second one binds first.

Pure read-only: the strategy DB is opened read-only for book snapshots, the
archive is read as parquet, and nothing is written except an optional chart
under ``DATA_DIR``. Not in the live signal path.

Run: ``python tools/ladder_streaks.py [--chart]``
"""

from __future__ import annotations

import argparse
import glob
import math
import os
import sqlite3
from dataclasses import dataclass

import numpy as np
import pandas as pd

from config import DATA_DIR, DB_PATH

ARCHIVE_GLOB = str(DATA_DIR / "binance_archive" / "parquet" / "klines-um-BTCUSDT-1m-*.parquet")
WINDOW_NS = 5 * 60 * 1_000_000_000
BASE_STAKE = 10.0
FAIR_PRICE = 0.500      # mid, i.e. a frictionless ladder
ASK_PRICE = 0.505       # mid + half the measured 1c spread: what a taker pays


@dataclass(frozen=True)
class LadderResult:
    target: int
    payout: float
    ladders: int
    hits: int
    staked: float
    banked: float

    @property
    def net(self) -> float:
        return self.banked - self.staked

    @property
    def roi(self) -> float:
        return self.net / self.staked if self.staked else 0.0


def load_windows() -> pd.DataFrame:
    """Every complete 5-minute window in the archive, with its settlement flag."""
    files = sorted(glob.glob(ARCHIVE_GLOB))
    if not files:
        raise SystemExit(f"no 1m klines under {ARCHIVE_GLOB}")
    klines = (
        pd.concat(
            [pd.read_parquet(f, columns=["open_time", "open", "close"]) for f in files],
            ignore_index=True,
        )
        .drop_duplicates("open_time")
        .sort_values("open_time")
    )
    klines["bucket"] = klines["open_time"].astype("int64") // WINDOW_NS
    grouped = klines.groupby("bucket")
    windows = pd.DataFrame(
        {
            "minutes": grouped.size(),
            "opened_at": grouped["open_time"].first(),
            "open": grouped["open"].first(),
            "close": grouped["close"].last(),
        }
    )
    windows = windows[windows["minutes"] == 5].copy()
    windows["up"] = (windows["close"] >= windows["open"]).to_numpy()
    return windows


def book_ask_sizes() -> np.ndarray:
    """Recorded top-of-book ask sizes from the live paper journal (read-only)."""
    uri = f"file:{DB_PATH}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        rows = conn.execute(
            "select up_ask_size from paper_ticks where up_ask_size is not null"
        ).fetchall()
    return np.array([r[0] for r in rows], dtype=float)


def streak_lengths(wins: np.ndarray) -> np.ndarray:
    """Length of every maximal run of wins, ride-until-you-lose (no stop rule)."""
    runs, current = [], 0
    for won in wins:
        if won:
            current += 1
        else:
            runs.append(current)
            current = 0
    runs.append(current)
    return np.array(runs)


def ride_to_target(wins: np.ndarray, target: int, price: float) -> LadderResult:
    """Bet the whole stack each window; bank at ``target`` wins; restart at $10."""
    payout = BASE_STAKE * (1.0 / price) ** target
    ladders = hits = streak = 0
    starting = True
    for won in wins:
        if starting:
            ladders += 1
            starting = False
        if won:
            streak += 1
            if streak == target:
                hits += 1
                streak = 0
                starting = True
        else:
            streak = 0
            starting = True
    return LadderResult(target, payout, ladders, hits, BASE_STAKE * ladders, payout * hits)


def rung_notional(step: int) -> float:
    return BASE_STAKE * 2 ** (step - 1)


def report(windows: pd.DataFrame, asks: np.ndarray) -> None:
    up = windows["up"].to_numpy()
    print(f"windows        : {len(windows):,}  "
          f"{windows['opened_at'].min()} -> {windows['opened_at'].max()}")
    print(f"Up rate        : {up.mean():.4f}   flat (close==open): "
          f"{(windows['close'] == windows['open']).sum():,}")
    if asks.size:
        print(f"book snapshots : {asks.size}  top-of-book ask shares "
              f"p25 {np.percentile(asks, 25):.0f} / median {np.median(asks):.0f} "
              f"/ p75 {np.percentile(asks, 75):.0f}")
    print()

    for label, wins in (("always Up", up), ("always Down", ~up),
                        ("coin-pick side", (np.random.default_rng(7).random(len(up)) < 0.5) == up)):
        runs = streak_lengths(wins)
        need = math.ceil(math.log(100_000) / math.log(1.0 / ASK_PRICE))
        print(f"--- {label}: {len(runs):,} ladders, win rate {wins.mean():.4f}, "
              f"longest {runs.max()} (need {need} for $1M)")
        print("    reaching >=n: "
              + "  ".join(f"{n}:{(runs >= n).sum():,}" for n in (1, 5, 8, 10, 12, 15, 17)))
    print()

    for price in (FAIR_PRICE, ASK_PRICE):
        print(f"===== stop-and-cash-out, entry {price:.3f} "
              f"({1 / price:.3f}x per win) =====")
        header = (f"{'stop at':>7} {'cash-out':>10} {'ladders':>9} {'hits':>6} "
                  f"{'staked':>12} {'banked':>12} {'net':>12} {'ROI':>7} "
                  f"{'top rung':>9} {'fillable':>9}")
        print(header)
        for target in range(1, 16):
            res = ride_to_target(up, target, price)
            top = rung_notional(target)
            fillable = 100 * (asks >= top / price).mean() if asks.size else float("nan")
            print(f"{res.target:>7} {res.payout:>10,.0f} {res.ladders:>9,} {res.hits:>6,} "
                  f"{res.staked:>12,.0f} {res.banked:>12,.0f} {res.net:>+12,.0f} "
                  f"{100 * res.roi:>6.1f}% {top:>9,.0f} {fillable:>8.1f}%")
        print()


def draw_chart(windows: pd.DataFrame, out_path: str) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter, NullFormatter

    runs = streak_lengths(windows["up"].to_numpy())
    steps = np.arange(0, 19)
    surviving = np.array([(runs >= s).sum() for s in steps], dtype=float)

    surface, ink, ink_dim, muted = "#fcfcfb", "#0b0b0b", "#52514e", "#b5b3ab"
    blue, red = "#2a78d6", "#e34948"

    fig, ax = plt.subplots(figsize=(10.5, 6.4), dpi=200)
    fig.patch.set_facecolor(surface)
    ax.set_facecolor(surface)
    fig.subplots_adjust(top=0.80, left=0.10, right=0.97, bottom=0.12)

    live = surviving > 0
    ax.plot(steps[live], surviving[live], color=blue, lw=2, zorder=4,
            marker="o", ms=5.5, mfc=blue, mec=surface, mew=1.6)
    ax.axvline(17, color=red, lw=1.5, ls=(0, (4, 3)), zorder=2)
    ax.text(16.6, 700_000, "17 wins\n= $1,000,000", color=red, fontsize=11,
            ha="right", va="center", fontweight="700", linespacing=1.3)

    leader = dict(arrowstyle="-", color=muted, lw=1.1, shrinkA=2, shrinkB=6)
    ax.annotate("half of all ladders die\non the very first bet", (1, surviving[1]),
                xytext=(3.3, 700_000), color=ink_dim, fontsize=10.5, ha="left",
                va="center", linespacing=1.35, arrowprops=leader, zorder=5)
    ax.annotate(f"{int(surviving[10])} ladders reach 10", (10, surviving[10]),
                xytext=(11.4, 30_000), color=ink_dim, fontsize=10.5, ha="left",
                va="center", arrowprops=leader, zorder=5)
    ax.annotate(f"the record is {runs.max()} —\nhit twice in two years",
                (15, surviving[15]), xytext=(14.4, 900), color=ink_dim, fontsize=10.5,
                ha="center", va="bottom", linespacing=1.35, arrowprops=leader, zorder=5)

    ax.set_yscale("log")
    ax.set_ylim(0.7, 3_000_000)
    ax.set_xlim(-0.7, 18.7)
    ax.set_xticks(range(0, 19))
    ax.set_yticks([1, 10, 100, 1_000, 10_000, 100_000, 1_000_000])
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{int(v):,}"))
    ax.yaxis.set_minor_formatter(NullFormatter())
    ax.grid(axis="y", color=muted, lw=0.6, alpha=0.35, zorder=0)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(muted)
    ax.spines["bottom"].set_linewidth(0.8)
    ax.tick_params(colors=ink_dim, length=0, labelsize=10)
    ax.tick_params(axis="y", which="minor", length=0)

    fig.text(0.10, 0.945, "Nobody ever gets to the end of the ladder", color=ink,
             fontsize=17, fontweight="700", ha="left", va="center")
    fig.text(0.10, 0.885,
             "Ladders still alive after each consecutive win — every real Bitcoin "
             "5-minute up/down window,\n"
             f"{windows['opened_at'].min():%B %Y} – {windows['opened_at'].max():%B %Y} "
             f"({len(windows):,} windows, {len(runs):,} ladders, always betting Up)",
             color=ink_dim, fontsize=10.5, ha="left", va="center", linespacing=1.5)
    ax.set_xlabel("consecutive wins", color=ink_dim, fontsize=10.5, labelpad=8)
    ax.set_ylabel("ladders still alive", color=ink_dim, fontsize=10.5, labelpad=8)

    fig.savefig(out_path, facecolor=surface)
    print(f"chart -> {out_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--chart", action="store_true",
                        help="also render the survival chart under DATA_DIR")
    args = parser.parse_args()

    windows = load_windows()
    report(windows, book_ask_sizes())
    if args.chart:
        draw_chart(windows, os.path.join(str(DATA_DIR), "ladder_survival.png"))


if __name__ == "__main__":
    main()
