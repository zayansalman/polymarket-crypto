# Pre-registered: aggressive order flow and next-hour BTC reversal

Written 2026-09-14 before running.

## Data
Binance spot BTCUSDT 1h klines with taker-buy base volume, 2023-10-12 to 2026-09-13 UTC (25,627 hours, no gaps).
- Discovery: before 2025-10-18 14:00 UTC.
- Validation: from 2025-10-18 14:00 UTC.

## Definitions (all known at the open of hour H, built from hour H-1 and earlier)
- Flow imbalance: imb = 2 * taker_buy_base / volume - 1, from -1 (all aggressive sellers) to +1 (all aggressive buyers).
- Flow z: (imb - trailing 168h mean) / trailing 168h std. The window includes H-1.
- Flow in the move's direction: fz = z * sign(close - open of H-1). Positive means the move was pushed by aggressive orders on its own side.
- Move size: |close - open| of H-1 divided by the trailing 24h mean |move|.
- Reversal bet: bet against H-1's direction. It wins if hour H closes the other way (a flat close counts as down).
- Cutoffs are quintiles computed on discovery only.

## Tests (fixed)
1. Dose-response: reversal win rate by quintile of fz, in both periods.
2. H1, flow-driven move: fz in the top quintile. Compare reversal win rate with all other hours.
3. H2, flow-driven big move: fz top quintile AND move size top quintile.
4. H3, flow alone: |z| top quintile; bet against the flow's sign.
5. H4, absorbed move: flow opposite the move (fz bottom quintile); reversal win rate.

95% Wilson intervals. Benjamini-Hochberg FDR at q = 0.10 across H1-H4 in discovery.

## Bar
Counts as a lead only if it passes FDR in discovery, holds the same direction in validation with a validation interval above the all-hours reversal rate, and shows a roughly monotonic dose-response.
Reference lines: 52.25% taker break-even at 50c (fee + 1c spread), 50% maker.
