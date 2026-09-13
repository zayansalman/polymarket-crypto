"""Print a BTC paper trading snapshot."""
from __future__ import annotations

import asyncio
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from polymarket_bot.history import load_btc_history_stats
from polymarket_bot.paper import load_paper_summary
from polymarket_bot.backtest import format_report
from config import DATA_DIR
from db import init_db


async def main() -> None:
    await init_db()
    paper = await load_paper_summary()
    history = load_btc_history_stats()
    win_rate = "n/a" if paper.win_rate is None else f"{paper.win_rate:.0%}"
    avg_pnl = "n/a" if paper.avg_pnl_usd is None else f"${paper.avg_pnl_usd:+.2f}"
    avg_hold = "n/a" if paper.avg_hold_seconds is None else f"{paper.avg_hold_seconds:.0f}s"

    print("# BTC 5m Binary Pricing Model Snapshot")
    print()
    print(f"- Risk state: {paper.risk_state}")
    print(f"- Last tick: {paper.last_tick_at or 'never'}")
    print(f"- Last window: {paper.last_window_slug or 'n/a'}")
    print(f"- Latest signal: {paper.last_signal}")
    print(f"- Open exposure: ${paper.open_exposure_usd:.2f}")
    print(f"- Open positions: {paper.open_positions}")
    print(f"- Closed trades: {paper.closed_positions}")
    print(f"- Closed notional: ${paper.closed_notional_usd:.2f}")
    print(f"- Total paper PnL: ${paper.total_pnl_usd:+.2f}")
    print(f"- Win rate: {win_rate}")
    print(f"- Average PnL/trade: {avg_pnl}")
    print(f"- Average hold: {avg_hold}")
    print(f"- Feed source: {paper.last_feed_source or 'n/a'}")
    print()
    if history.found:
        print("## History Baseline")
        print()
        print(f"- Export path: {history.path}")
        print(f"- BTC rows: {history.btc_rows} of {history.total_rows}")
        print(f"- BTC buys/sells/redeems: {history.buys}/{history.sells}/{history.redeems}")
        print(f"- Average buy size: ${history.buy_usdc_avg:.2f}")
        print(f"- Median buy size: ${history.buy_usdc_median:.2f}")
        print(f"- $1-$5 buy share: {history.one_to_five_buy_share:.0%}")
    else:
        print("## History Baseline")
        print()
        print(f"- Optional CSV not found at {history.path}")
    report_path = DATA_DIR / "backtests" / "latest.json"
    if report_path.exists():
        import json

        print()
        print(format_report(json.loads(report_path.read_text(encoding="utf-8"))))


if __name__ == "__main__":
    asyncio.run(main())
