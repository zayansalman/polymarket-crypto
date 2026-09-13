"""Load the user's exported Polymarket history for BTC sizing context."""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from statistics import mean, median

from config import HISTORY_CSV_PATH


@dataclass
class BtcHistoryStats:
    path: str
    found: bool
    total_rows: int = 0
    btc_rows: int = 0
    buys: int = 0
    sells: int = 0
    redeems: int = 0
    buy_usdc_total: float = 0.0
    buy_usdc_avg: float = 0.0
    buy_usdc_median: float = 0.0
    buy_usdc_min: float = 0.0
    buy_usdc_max: float = 0.0
    one_to_five_buy_share: float = 0.0


def load_btc_history_stats(path: Path | None = None) -> BtcHistoryStats:
    """Summarize exported BTC Up/Down rows if the CSV is present."""
    csv_path = path or HISTORY_CSV_PATH
    stats = BtcHistoryStats(path=str(csv_path), found=csv_path.exists())
    if not stats.found:
        return stats

    buy_amounts: list[float] = []
    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            stats.total_rows += 1
            if "Bitcoin Up or Down" not in (row.get("marketName") or ""):
                continue
            stats.btc_rows += 1
            action = (row.get("action") or "").lower()
            amount = _as_float(row.get("usdcAmount"))
            if action == "buy":
                stats.buys += 1
                buy_amounts.append(amount)
            elif action == "sell":
                stats.sells += 1
            elif action == "redeem":
                stats.redeems += 1

    if buy_amounts:
        stats.buy_usdc_total = sum(buy_amounts)
        stats.buy_usdc_avg = mean(buy_amounts)
        stats.buy_usdc_median = median(buy_amounts)
        stats.buy_usdc_min = min(buy_amounts)
        stats.buy_usdc_max = max(buy_amounts)
        stats.one_to_five_buy_share = sum(1 for x in buy_amounts if 1 <= x <= 5.01) / len(
            buy_amounts
        )
    return stats


def _as_float(value: str | None) -> float:
    try:
        return float(value or 0)
    except ValueError:
        return 0.0
