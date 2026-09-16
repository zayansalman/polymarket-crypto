# Pre-registered test: Qlib-style factors for next-hour BTC direction

Written 2026-09-14, before any model is fit or any test-period result is seen.

## Question
At the open of UTC hour H, can Qlib's Alpha158 price/volume factors predict whether Binance spot BTCUSDT hour H closes above its open, better than a coin flip, on hours never used for fitting or choosing anything?

## Data
- Binance spot BTCUSDT 1h OHLCV + quote volume, 2023-10-19 14:00 to 2026-09-13 13:00 UTC (25,440 hours, no gaps).
- Target: up_H = close_H > open_H.
- Features for hour H use only candles up to and including H-1.

## Split (fixed)
- Fit period: hours before 2025-10-18 14:00 UTC. The last 10% of it (by time) is the inner validation slice, used only for early stopping and to set the confidence threshold.
- Test period: 2025-10-18 14:00 UTC to the end (about 7,920 hours), the same out-of-sample start as the Kronos test. Nothing is tuned on it.

## Features
- Alpha158 as defined in microsoft/qlib `Alpha158DL.get_feature_config` (default windows 5, 10, 20, 30, 60), computed on hourly bars. VWAP0 uses quote volume / volume.
- Extra: hour-of-day and weekday (sin/cos).
- Processing (Qlib's single-asset-safe steps only): robust z-score with median/MAD from the fit period only, clip to ±3, fill missing with 0. No cross-sectional steps.

## Models (hyperparameters fixed here, not tuned)
1. Logistic regression (torch, L2 weight decay 1e-3, Adam lr 1e-3, early stop on inner validation log loss, patience 5).
2. MLP 158+4 -> 64 -> 64 -> 1, ReLU, dropout 0.3, same optimizer and early stopping.

Training schemes:
- A. Static: fit once on the fit period.
- B. Rolling (Qlib-style): refit at the start of each calendar month of the test period on all hours before that month, with the same inner validation rule (last 10% of the hours available then).

So 4 variants in total (2 models x 2 schemes). All 4 are reported. The best of 4 is optimistic by selection.

## Baselines (same test hours)
Coin flip; always-up; repeat last hour's direction; bet against last hour's direction.

## Metrics
- Hit rate with 95% Wilson interval.
- Brier score vs 0.25 and log loss vs 0.6931.
- Calibration by probability bucket.
- Confident subset: hours where |p - 0.5| is above the 80th percentile of |p - 0.5| on the inner validation slice. Report hit rate and n.
- Break-even reference: about 51.75% hit rate as a taker at a 50-cent price (fee 0.07*p*(1-p)); 50% as a maker.

## Success bar (fixed)
A variant counts as promising only if its test hit rate's lower 95% bound is above 50% AND its Brier score beats 0.25. Beating 51.75% (lower bound) is the bar for taker trading. Anything else is reported as no edge.
