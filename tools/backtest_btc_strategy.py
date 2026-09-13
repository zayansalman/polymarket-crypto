"""Run the BTC strategy backtest and parameter optimizer."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from polymarket_bot.backtest import build_report, format_report, save_report
from config import HISTORY_CSV_PATH, DATA_DIR


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", type=Path, default=HISTORY_CSV_PATH)
    parser.add_argument("--output", type=Path, default=DATA_DIR / "backtests" / "latest.json")
    args = parser.parse_args()

    report = build_report(args.history)
    output = save_report(report, args.output)
    print(format_report(report))
    print()
    print(f"Saved JSON report: {output}")


if __name__ == "__main__":
    main()
