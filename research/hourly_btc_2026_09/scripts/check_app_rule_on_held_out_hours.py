"""Run the app's own rule code on the held-out hours and compare with the research result.

The research (PREREG_minute.md rule R5) measured 220 bets and a 57.27% win rate on the held-out
hours from 2025-10-18 14:00 UTC. This replays the app's decide() for every one of those hours
on the same Binance data (Claude, 2026-09-16; the check first appeared as Task 2 Step 5 of the
hourly engine plan, 2026-09-14).

Usage: python3 check_app_rule_on_held_out_hours.py [DATA_DIR]   (run from the repo root)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from polymarket_bot.hourly import mean_reversion as rule  # noqa: E402
from polymarket_bot.hourly.market import Candle  # noqa: E402

DATA = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "data"
HELD_OUT_FROM = pd.Timestamp("2025-10-18 14:00")


def load(name: str) -> list[Candle]:
    d = pd.read_csv(DATA / name, parse_dates=["ts"])
    return [
        Candle(int(r.ts.value // 1_000_000), r.open, r.high, r.low, r.close, r.volume, r.amount,
               r.taker_buy_base)
        for r in d.itertuples()
    ]


def main() -> None:
    spot, perp = load("btc_1h_flow.csv"), load("btc_perp_1h_flow.csv")
    start_ms = int(HELD_OUT_FROM.value // 1_000_000)
    first = next(i for i, c in enumerate(spot) if c.open_time_ms >= start_ms)
    bets = wins = 0
    for i in range(first, len(spot)):
        assert spot[i - 1].open_time_ms == perp[i - 1].open_time_ms, "spot/perp rows out of line"
        decision = rule.decide(spot[i - rule.WINDOW:i], perp[i - rule.WINDOW:i])
        if decision.side:
            bets += 1
            up = spot[i].close > spot[i].open
            wins += int(up == (decision.side == "Up"))
    print(f"app rule on held-out hours: bets {bets} win rate {wins / bets:.4f} "
          f"({wins} wins) | research: bets 220 win rate 0.5727")


if __name__ == "__main__":
    main()
