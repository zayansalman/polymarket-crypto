# Backtesting

The repo includes a repeatable local backtest for the BTC 5m Binary Pricing Model
strategy.

```bash
./.venv/bin/python tools/backtest_btc_strategy.py
```

The report is saved to:

```text
./data/backtests/latest.json
```

`data/` is ignored so personal trading history and generated reports stay
local.

## Methodology

The current backtest is a **trade-history conditional backtest**:

- Reads the local exported Polymarket CSV.
- Keeps historical BTC Up/Down buy rows.
- Parses each 5-minute market window from the market name.
- Fetches/caches Binance BTCUSDT 1-second candles for that window.
- Computes reference price, trade-time spot, recent volatility, fair side
  probability, edge, confidence, and hold-to-resolution settlement PnL.
- Runs a grid search over entry edge, confidence, time-left, and max-entry-price
  filters.

## Important Limitation

This is not a full-market backtest. It only evaluates opportunities that
appear in the user's historical buy log. It cannot measure markets that were
skipped, full CLOB fill quality, or quote-path exits after entry.

A full-market replay harness already exists at `polymarket_exec/backtest/harness.py`,
but it is **not wired into the live path** — nothing in `polymarket_bot/` or the
dashboard imports it (it is exercised only by tests). The live backtest that
the dashboard and CLI actually use is `polymarket_bot/backtest.py` (the
trade-history conditional grid above), and the dashboard renders the
precomputed report from `data/backtests/latest.json` rather than running a
replay live. Wiring the full-market harness into the live tooling remains
open work tracked in `docs/ROADMAP.md`.

## Current Optimized Profile

The latest local run favored keeping the existing 4.5 percentage-point edge
floor, lowering the late-entry cutoff to 60 seconds, and using a 0.50
confidence floor for sizing. The edge threshold still prevents weak signals;
the lower confidence floor mainly lets accepted trades scale more naturally
from $1 to $5.
