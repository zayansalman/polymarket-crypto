# Tsinghua-Kronos BTC 24h: scoring and recreating the Kronos team's published BTCUSDT forecasts

Name "Tsinghua-Kronos BTC 24h" chosen by Zayan (operator), 2026-09-16.

## What the source is

- **Model family:** Kronos, Shi et al., "Kronos: A Foundation Model for the Language of Financial Markets", arXiv 2508.02739 (Tsinghua University), code github.com/shiyu-coder/Kronos (MIT).
- **Forecast being studied:** the Kronos team's own live demo, github.com/shiyu-coder/Kronos-demo (`update_predictions.py`). The repo was pointed to by Zayan on 2026-09-16; Claude found the demo repo and its history the same day.
- **What the demo does, every hour (runs at HH:00:05 UTC):**
  1. Fetches 384 Binance spot BTCUSDT 1h candles and drops the still-forming one, leaving 383.
  2. Samples 30 paths of the next 24 hours with Kronos, T=1.0, top_p=0.95.
  3. Publishes the **upside probability**: the share of paths whose close 24 hours ahead is above the last closed hourly close.
  4. Commits the number to `index.html`. There are 8,563 such commits from 2025-07-11 to 2026-07-04, after which updates stop.
- **Model eras** (from the demo repo's own history):

| Era | From (UTC, code push) | Model | Sampling | Note |
|---|---|---|---|---|
| small_T0.6 | 2025-07-11 | Kronos-small + Tokenizer-base | T=0.6, top_p=0.9 | |
| small_T1.0 | 2025-08-20 07:33 | Kronos-small + Tokenizer-base | T=1.0, top_p=0.95 | |
| mini_prefix | 2025-08-30 10:17 | Kronos-mini + Tokenizer-2k | T=1.0, top_p=0.95 | Attention dropout stayed on during forecasting |
| mini | 2025-09-16 02:33 (commit eba16695, "Bug fix") | Kronos-mini + Tokenizer-2k | T=1.0, top_p=0.95 | Dropout fixed; this is the era studied |

- **Model weights:** Kronos-mini weights have not changed since 2025-07-01 (Hugging Face `NeoQuasar/Kronos-mini`, snapshot f4e68697d9d5aed55cef5c96aabc3376bcad9f81; later commits only edit the README). The same holds for `NeoQuasar/Kronos-Tokenizer-2k` (snapshot 26966d0035065a0cae0ebad7af8ece35bc1fb51c).

## Files

- `scripts/fetch_binance_btcusdt_1h_spot_klines.py`: downloads Binance spot BTCUSDT 1h candles from data.binance.vision to `data/btcusdt_1h_spot.csv` (11,328 candles, 2025-06-01 to 2026-09-15, no gaps).
- `scripts/extract_published_kronos_demo_forecasts.py`: reads every published forecast out of the demo repo's git history (partial clone, `data/kronos-demo-repo`) into `data/published_forecasts.csv`.
- `scripts/score_published_kronos_demo_forecasts.py`: scores each published forecast against Binance, per era, over two windows:
  - the 24-hour direction the demo forecasts;
  - the direction of hour A, which is how Polymarket's hourly BTC market resolves. The forecast appears about 25 s into hour A.

  95% ranges come from a 24-hour block bootstrap. Output: `outputs/published_forecasts_score.txt`.
- `scripts/score_noon_et_daily_market_subset.py`: scores only the forecasts published during the 12:00 America/New_York hour. That is the exact window of Polymarket's daily BTC Up/Down market, which settles on the Binance 1-minute close at noon ET vs the previous day's noon ET (see `polymarket_bot/daily/market.py`). Output: `outputs/noon_et_daily_market_subset.txt`.
- `scripts/recreate_tsinghua_kronos_btc_24h_forecast.py`: reruns the demo's forecast for chosen hours with the demo's own model code at eba16695 (`data/kronos_demo_code`) and the pinned weights. Outputs: `outputs/recreated_*.csv`.

Run order: fetch, extract, score, noon subset, recreate. `data/` is git-ignored and rebuilt by the scripts. The demo repo is cloned with `git clone --filter=blob:limit=50k --no-checkout https://github.com/shiyu-coder/Kronos-demo.git data/kronos-demo-repo`. Weights come via `huggingface_hub.snapshot_download` at the snapshots above.

## Results (2026-09-16)

Published forecasts, era "mini" (2025-09-16 to 2026-07-04, 6,990 hourly forecasts):

| Scored against | Hit rate (95% range) | AUC | Brier score | Brier if always 50% |
|---|---|---|---|---|
| BTC higher 24 hours later | 51.4% (48.1-54.7), n=6,673 | 0.514 | 0.305 | 0.250 |
| Hour A up (Polymarket hourly market rule) | 50.5% (49.4-51.6), n=6,673 | 0.505 | 0.308 | 0.250 |
| Noon-ET forecasts only (Polymarket daily market window) | 52.2% (46.3-58.1), n=276 | | | |

- **Calibration:** confidence did not match outcomes. Whether the forecast said about 9% or about 88%, BTC was higher 24 hours later 45-51% of the time. So the probabilities scored worse than always saying 50%.
- **Confident forecasts only:** restricting to forecasts at least 20, 30 or 40 points from 50% gave 50.8%, 50.5% and 51.3%.

Recreation check: `outputs/recreated_timing_check.csv` holds three hours whose last close matched the published inputs exactly.

| Hour | Recreated | Published |
|---|---|---|
| 1 | 43% | 33% |
| 2 | 3% | 3% |
| 3 | 93% | 100% |

With 30 paths, differences of about ±9 points are expected.

Wider check (`scripts/compare_recreated_with_published.py`, output `outputs/recreated_vs_published.txt`): 270 hours spread over the whole Kronos-mini era, one every 25 hours. Inputs were identical for all 270. Correlation with the published numbers was 0.896, the mean gap -0.002 and the mean absolute gap 0.086. 13 of 270 gaps (4.8%) were beyond two sampling standard errors, which is what sampling alone predicts (about 5%). The recreation reproduces the published forecast. The median run was 8.8 s of CPU on the operator's M2.
