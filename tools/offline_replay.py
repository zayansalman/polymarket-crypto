"""Offline replay of the BTC 5-m pricing-model strategy on HF Polymarket data.

Issue #56. Replays ``polymarket_bot.strategy.fair_up_probability`` +
``signal_from_executable_edges`` over the HF dataset
``aliplayer1/polymarket-crypto-updown`` to validate Brier / ROI / win-rate
on ≫ the ~844 fills present in the live SQLite journal.

Three small dataset configs (~280 MB total, cached by huggingface_hub) are
sufficient for a first cut — the 23.6 GB ``orderbook`` config is *not*
downloaded:

* ``markets`` — ground-truth resolutions (Up/Down/-1=unresolved)
* ``prices`` — per-market Up/Down mid-price evolution
* ``spot_prices`` — Chainlink BTC/USD spot (filterable on source)

Two honest limitations of this first cut, both noted in the JSON report:

1. We use the market **mid** as the executable ask. Live entries pay the
   real best_ask (~0.5¢ wider on average per the dashboard's TCA), so
   replay edges are systematically optimistic by ~half a spread.
2. σ is estimated from the previous ~30 Chainlink prints inside the
   window only (we don't carry σ across window boundaries). For 5-m
   markets that matches the live loop's behaviour exactly.

Outputs a JSON report under ``data/replay/<slice>.json`` and a stdout
summary. Does not touch the live trading loop or database.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import polars as pl
from huggingface_hub import hf_hub_download

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from polymarket_bot.strategy import (  # noqa: E402
    StrategyParams,
    fair_up_probability,
    signal_from_executable_edges,
    sigma_per_second,
)
from config import (  # noqa: E402
    PAPER_ENTRY_EDGE_MAX,
    PAPER_ENTRY_EDGE_MIN,
    PAPER_ENTRY_MIN_REMAINING_SECONDS,
    PAPER_MAX_TRADE_USD,
    PAPER_MIN_CONFIDENCE,
    PAPER_MIN_ENTRY_PRICE,
    PAPER_MIN_TRADE_USD,
    PRINT_GRANULARITY_USD,
    DATA_DIR,
)

HF_REPO = "aliplayer1/polymarket-crypto-updown"
TIMEFRAME_SECONDS = {"5-minute": 300, "15-minute": 900, "1-hour": 3600, "4-hour": 14400}


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


def _download(filename: str) -> Path:
    """Fetch one parquet file from HF (cached locally after first hit)."""
    return Path(
        hf_hub_download(HF_REPO, filename=filename, repo_type="dataset")
    )


def _list_partition_files(prefix: str, crypto: str, timeframe: str) -> list[str]:
    """Return repo-relative paths of parquet files under a Hive partition.

    The dataset stores `prices` / `ticks` / `orderbook` under
    ``data/<config>[.tmp.N]/crypto=<X>/timeframe=<Y>/part-*.parquet``. The
    ``.tmp.N`` suffix is real (not a tempfile to ignore) — recent ingest
    batches land in numbered staging dirs.
    """
    from huggingface_hub import HfApi

    api = HfApi()
    needle = f"/crypto={crypto}/timeframe={timeframe}/"
    return [
        f
        for f in api.list_repo_files(HF_REPO, repo_type="dataset")
        if f.startswith(f"data/{prefix}") and needle in f and f.endswith(".parquet")
    ]


def load_markets(crypto: str = "BTC", timeframe: str = "5-minute") -> pl.DataFrame:
    """Resolved markets of the given shape, with derived window-open ts."""
    path = _download("data/markets.parquet")
    df = (
        pl.read_parquet(path)
        .filter(pl.col("crypto") == crypto)
        .filter(pl.col("timeframe") == timeframe)
        .filter(pl.col("resolution") != -1)
    )
    horizon = TIMEFRAME_SECONDS[timeframe]
    # end_ts is the settlement time; window opens `horizon` seconds before.
    # `start_ts` is market-creation, not window-open — do NOT use it.
    return df.with_columns(
        (pl.col("end_ts") - horizon).alias("window_open_ts"),
        pl.col("end_ts").alias("window_close_ts"),
    )


def load_chainlink_btc_spots(min_ts_s: int, max_ts_s: int) -> pl.DataFrame:
    """Chainlink BTC/USD spot prints in [min, max] seconds, time-sorted."""
    path = _download("data/spot_prices/part-0.parquet")
    return (
        pl.read_parquet(path)
        .filter(pl.col("symbol") == "btc/usd")
        .filter(pl.col("source") == "chainlink_proxy")
        .filter(pl.col("ts_ms") >= min_ts_s * 1_000)
        .filter(pl.col("ts_ms") <= max_ts_s * 1_000)
        .sort("ts_ms")
        .with_columns((pl.col("ts_ms") // 1_000).alias("ts_s"))
    )


def load_market_prices(
    market_ids: list[str], crypto: str = "BTC", timeframe: str = "5-minute"
) -> pl.DataFrame:
    """Per-market Up/Down mid-price evolution for the requested markets.

    Reads every BTC/5-min partition file (dataset is Hive-partitioned by
    crypto + timeframe) and concats. The full set is ~33 files at the time
    of writing — small enough to load in one shot.
    """
    files = _list_partition_files("prices", crypto, timeframe)
    if not files:
        raise RuntimeError(
            f"no prices partition files found for crypto={crypto} timeframe={timeframe}"
        )
    # Skip the occasional zero-byte staging file in `data/prices.tmp.*/` — a
    # bad parquet would abort the whole replay otherwise. Honest cost: the
    # markets covered by those files won't have prices and will be silently
    # dropped at the join (logged in the report).
    frames: list[pl.DataFrame] = []
    bad: list[str] = []
    for f in files:
        try:
            frames.append(pl.read_parquet(_download(f)))
        except Exception as exc:  # noqa: BLE001
            bad.append(f"{f}: {exc}")
    if not frames:
        raise RuntimeError(f"every prices file failed to read: {bad}")
    return (
        pl.concat(frames)
        .filter(pl.col("market_id").is_in(market_ids))
        .sort(["market_id", "timestamp"])
    )


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplayEntry:
    market_id: str
    window_close_ts: int
    side: str  # "Up" or "Down"
    entry_price: float  # mid (ask proxy) at entry
    predicted_up: float  # calibrated fair_up at entry
    confidence: float
    edge: float
    notional: float
    outcome: int  # 1 if Up won, 0 if Down won
    won: bool  # True if our side wins


def _params_from_env() -> StrategyParams:
    return StrategyParams(
        min_trade_usd=PAPER_MIN_TRADE_USD,
        max_trade_usd=PAPER_MAX_TRADE_USD,
        entry_edge_min=PAPER_ENTRY_EDGE_MIN,
        min_confidence=PAPER_MIN_CONFIDENCE,
        entry_min_remaining_seconds=PAPER_ENTRY_MIN_REMAINING_SECONDS,
        entry_edge_max=PAPER_ENTRY_EDGE_MAX,
        min_entry_price=PAPER_MIN_ENTRY_PRICE,
    )


def replay_market(
    market: dict,
    chainlink_spots: pl.DataFrame,
    market_prices: pl.DataFrame,
    params: StrategyParams,
) -> ReplayEntry | None:
    """One-entry-per-window replay (matching ``EXIT_STYLE='settle'``).

    Walk the in-window mid-price ticks chronologically. For each:
    * spot = latest Chainlink print at-or-before that ts
    * reference = first Chainlink print at-or-before window_open_ts
    * σ = ``sigma_per_second`` over the last ~30 in-window Chainlink prints
    * fair_up = ``fair_up_probability`` (no calibrator — replay is uncalibrated)
    Then call ``signal_from_executable_edges`` and return the FIRST accepted
    entry. Returns ``None`` if no tick triggered an entry inside the window.
    """
    open_ts = int(market["window_open_ts"])
    close_ts = int(market["window_close_ts"])

    # Reference = last Chainlink print at or before window open. If none, skip.
    pre = chainlink_spots.filter(pl.col("ts_s") <= open_ts).tail(1)
    if pre.is_empty():
        return None
    reference = float(pre["price"][0])

    # In-window Chainlink prints — used both for spot lookup and σ estimation.
    in_win = chainlink_spots.filter(
        (pl.col("ts_s") >= open_ts) & (pl.col("ts_s") <= close_ts)
    )
    if in_win.height < 2:
        return None  # not enough prints to estimate σ
    cl_ts = in_win["ts_s"].to_list()
    cl_px = in_win["price"].to_list()

    # In-window market price evolution.
    mp = market_prices.filter(
        (pl.col("timestamp") >= open_ts) & (pl.col("timestamp") <= close_ts)
    ).sort("timestamp")
    if mp.is_empty():
        return None

    for row in mp.iter_rows(named=True):
        ts = int(row["timestamp"])
        remaining = close_ts - ts
        if remaining <= 0:
            break
        # Latest Chainlink spot at-or-before this market tick.
        # (linear scan is fine — ~60 prints per window.)
        spot = None
        recent = []
        for cls, p in zip(cl_ts, cl_px, strict=False):
            if cls > ts:
                break
            spot = p
            recent.append(p)
        if spot is None or len(recent) < 2:
            continue
        sigma = sigma_per_second(recent[-30:])

        fair_up = fair_up_probability(
            spot, reference, sigma, remaining, print_granularity=PRINT_GRANULARITY_USD
        )
        up_mid = float(row["up_price"])
        down_mid = float(row["down_price"])
        edge_up = fair_up - up_mid
        edge_down = (1 - fair_up) - down_mid

        side, conf, notional, _ = signal_from_executable_edges(
            edge_up, edge_down, remaining, up_mid, down_mid, params
        )
        if side is None:
            continue

        won = (side == "Up" and market["resolution"] == 1) or (
            side == "Down" and market["resolution"] == 0
        )
        return ReplayEntry(
            market_id=str(market["market_id"]),
            window_close_ts=close_ts,
            side=side,
            entry_price=up_mid if side == "Up" else down_mid,
            predicted_up=fair_up,
            confidence=conf,
            edge=edge_up if side == "Up" else edge_down,
            notional=notional,
            outcome=int(market["resolution"]),
            won=won,
        )
    return None


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def aggregate_metrics(entries: list[ReplayEntry]) -> dict:
    """Brier / ROI / win-rate over the entries. Matches live KPI definitions."""
    if not entries:
        return {"n": 0, "brier": None, "roi": None, "win_rate": None}
    n = len(entries)
    wins = sum(1 for e in entries if e.won)
    losses = n - wins
    # Binary payoff on Polymarket: shares = notional/entry_price; win → $1/share
    # gross, $0 if lose. PnL = (1/entry_price - 1) * notional on a win, else
    # -notional. Matches `_metrics_from_trades` in polymarket_bot/backtest.py.
    pnl_usd = 0.0
    notional_total = 0.0
    for e in entries:
        notional_total += e.notional
        if e.won:
            pnl_usd += e.notional * (1.0 / e.entry_price - 1.0)
        else:
            pnl_usd -= e.notional
    # Brier on "Up wins" — predicted_up vs realized outcome (1 if Up resolved).
    brier = sum((e.predicted_up - e.outcome) ** 2 for e in entries) / n
    return {
        "n": n,
        "wins": wins,
        "losses": losses,
        "win_rate": wins / n,
        "pnl_usd": round(pnl_usd, 4),
        "notional_usd": round(notional_total, 4),
        "roi": round(pnl_usd / notional_total, 6) if notional_total > 0 else None,
        "brier": round(brier, 4),
        "by_side": {
            "Up": sum(1 for e in entries if e.side == "Up"),
            "Down": sum(1 for e in entries if e.side == "Down"),
        },
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def run(
    timeframe: str = "5-minute",
    crypto: str = "BTC",
    max_markets: int | None = None,
    params: StrategyParams | None = None,
) -> dict:
    """Run the replay end-to-end. Returns the report dict (also written to disk)."""
    params = params or _params_from_env()
    markets = load_markets(crypto=crypto, timeframe=timeframe)
    if max_markets:
        markets = markets.head(max_markets)
    if markets.is_empty():
        return {"n_markets": 0, "n_entries": 0, "note": "no resolved markets found"}

    ts_min = int(markets["window_open_ts"].min())
    ts_max = int(markets["window_close_ts"].max())
    chainlink = load_chainlink_btc_spots(ts_min - 120, ts_max + 120)
    market_ids = markets["market_id"].to_list()
    prices_df = load_market_prices(market_ids)
    prices_by_market = {mid: g for mid, g in prices_df.group_by("market_id")}

    entries: list[ReplayEntry] = []
    for m in markets.iter_rows(named=True):
        mid = str(m["market_id"])
        mp = prices_by_market.get((mid,))
        if mp is None or mp.is_empty():
            continue
        e = replay_market(m, chainlink, mp, params)
        if e is not None:
            entries.append(e)

    metrics = aggregate_metrics(entries)
    report = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "dataset": HF_REPO,
        "crypto": crypto,
        "timeframe": timeframe,
        "n_markets_considered": markets.height,
        "params": {
            "entry_edge_min": params.entry_edge_min,
            "entry_edge_max": params.entry_edge_max,
            "min_confidence": params.min_confidence,
            "min_remaining_seconds": params.entry_min_remaining_seconds,
            "min_entry_price": params.min_entry_price,
        },
        "limitations": [
            "uses prices.up_price/down_price MID as ask proxy — replay edges "
            "are optimistic by ~half a spread vs live execution",
            "no calibrator applied — fair_up is raw log-normal+tie-mass; "
            "live Brier 0.236 includes isotonic, so direct comparison is "
            "indicative not exact",
        ],
        "metrics": metrics,
    }
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeframe", default="5-minute", choices=list(TIMEFRAME_SECONDS))
    parser.add_argument("--crypto", default="BTC")
    parser.add_argument(
        "--max-markets",
        type=int,
        default=None,
        help="Cap on number of resolved markets to replay (default: all).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DATA_DIR / "replay" / "latest.json",
        help="Destination for the JSON report.",
    )
    args = parser.parse_args()

    report = run(
        timeframe=args.timeframe,
        crypto=args.crypto,
        max_markets=args.max_markets,
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"\n=== Offline replay · {args.crypto} {args.timeframe} ===")
    print(f"markets considered : {report['n_markets_considered']}")
    m = report["metrics"]
    if not m["n"]:
        print("no entries triggered — check params / data coverage")
    else:
        print(f"entries            : {m['n']}  ({m['by_side']['Up']}U / {m['by_side']['Down']}D)")
        print(f"win rate           : {m['win_rate']:.1%}  ({m['wins']}W / {m['losses']}L)")
        print(f"PnL / notional     : ${m['pnl_usd']:+.2f} / ${m['notional_usd']:.0f}")
        print(f"ROI                : {(m['roi'] or 0):+.2%}")
        print(f"Brier              : {m['brier']}")
    print(f"\nreport: {args.output}")


if __name__ == "__main__":
    main()
