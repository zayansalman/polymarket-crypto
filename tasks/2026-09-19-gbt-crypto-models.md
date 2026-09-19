# Gradient-boosted trees for medium-speed crypto signals — fit check + model hunt (parked 2026-09-19)

Backlog issue: #260

Desk research only. **No code, no training, no backtest.** Nothing here has been measured. Parked
at this point so it is recoverable later.

Two questions were asked: are gradient-boosted trees (XGBoost / LightGBM) a sensible fit for
medium-speed signals (15-minute to hourly windows, not tick-level), and does a trained crypto model
already exist that could be used instead of training one.

## Why trees fit this shape of problem

- The inputs are tabular and few: time left in the window, distance from the window's reference
  price, recent realized volatility, momentum over several lookbacks, funding. Trees pick up the
  interactions between those without them being specified.
- Training takes seconds on CPU for tens of thousands of rows; inference is well under a
  millisecond, so the live loop's decision budget is untouched. No GPU.
- Boosting handles the small, noisy, non-stationary tabular regime better than a deep sequence model
  at this data size.

Requirements that come with it, none of them measured yet:

- Feed the existing closed-form fair value in as an input so the model learns a correction on top of
  the math rather than relearning it.
- Calibrate the output probabilities (the repo already has an isotonic calibrator pattern in the
  v0-era code).
- Split walk-forward by time. A random split leaks.
- Polymarket settles on **Chainlink**, not Binance. Labels built from Binance closes are noisy
  exactly at the line, which is where the trades are.
- **The baseline is the market price, not 50%.** Predicting direction is not the bar; beating what
  the book is charging after fees is.

## Data actually on hand (checked 2026-09-14)

| Source | Coverage | Use |
| --- | --- | --- |
| `data/binance_archive/parquet` | 1m klines + funding, BTC and ETH, **2024-07 → 2026-07** (25 months) | ~18k hourly / ~70k 15-minute windows of labels and features |
| same, `aggTrades` | BTC only, 3 months (2024-07 → 2024-09) | order-flow features only over a short span |
| `data/btc_5m_binary_fair_value.db` → `paper_ticks` | **76 rows** | far too few to train on; this is the only place Polymarket prices at decision time are stored |

The gap that matters: there is no stored history of **Polymarket prices at decision time**. Binance
history can teach a model which way BTC went; only recorded book snapshots can say whether that
beats the price on offer. Recording more ticks is the prerequisite for any of this being scoreable.

## Model hunt — what exists already

Searched Hugging Face (models matching bitcoin / crypto trading / BTCUSDT, sorted by downloads) and
the web/GitHub. **Nothing on Hugging Face is a documented gradient-boosted tree trained on crypto.**
The BTC-shaped repos there are LLM finetunes, sentiment classifiers, or PyTorch sequence models
(`XelotX/btc_usdt_full-model-artifacts-{1m,5m,15m,1h}` are `.pt` nets; `shanaka95/BTCUSDT_spot_predictions`
is a 69 MB `.pt`).

| Repo | What it is | Verdict |
| --- | --- | --- |
| [`masterputra169/polymarket-btc-15-minutes`](https://github.com/masterputra169/polymarket-btc-15-minutes) | Ships **trained** `public/ml/xgboost_model.json` (2.3 MB) and `lightgbm_model.json` (5.7 MB), plus a full retrain pipeline (`backtest/ml_training/mltrain/`: LightGBM stage, Platt calibration, purged CV, meta-labelling, pruning, Optuna sweeps, fee model, tests). Trained on ~45k Polymarket BTC 15-minute markets, 86% real labels, 180-day window, 79 features (54 base + 25 engineered), ensemble weights XGB 0.75 / LGBM 0.25. Author reports test accuracy 84.07%, AUC 0.9248, holdout 94.12%. Node/TypeScript bot + React dashboard; inference is a hand-written tree traversal in `Mlpredictor.ts`. | The only real candidate. Model files are **JSON**, so loading them executes no code. Numbers are self-reported and unverified — the 94% holdout is high enough to want a leakage check. 1 star, last pushed 2026-09-09. |
| [`zongowo111/crypto-trading-bot`](https://huggingface.co/zongowo111/crypto-trading-bot) | ~65 `.pkl` models across BTC/ETH/SOL/etc at 15m/1h/4h plus scalers | Empty README, model type unknown, **pickle executes code on load**. Skip. |
| [`Marjathirtyfour391/polymarket-…-xgboost-lightgbm`](https://github.com/Marjathirtyfour391/polymarket-trading-bot-ai-model-btc-5m-15m-1h-stacked-ensemble-xgboost-lightgbm) | Keyword-stuffed name, no model files, created 2026-08-14, 0 stars, a 487 KB `.zip` hidden in `docs/images/` | Matches the fake-Polymarket-bot malware pattern. **Do not clone or run.** |
| [FreqAI](https://www.freqtrade.io/en/stable/freqai/) | 18 pre-configured LightGBM / XGBoost / CatBoost / PyTorch prediction models inside Freqtrade, with live retraining | Templates, not weights. You train your own. Worth reading for the retrain-on-a-schedule plumbing. |

### The 79 features of the one usable model

Documented in its README and `mltrain/features.py`:

- **BTC price:** returns at 1m/5m/15m/30m/1h/4h, z-score, momentum.
- **Technical:** RSI, MACD, VWAP, Bollinger, ATR, Heiken Ashi, EMA cross, StochRSI.
- **Polymarket:** token price, bid/ask spread, time to settlement, order-book imbalance.
- **Volume:** delta, funding rate, VPIN estimate.
- **Regime:** choppy / trending / mean-reverting classification with a confidence.
- **25 engineered:** products and interactions of the above (`rsi_x_trending`, `delta_1m_atr_adj`,
  `vol_weighted_momentum`, `trend_alignment_score`, …), with a fixed positional order that is part
  of the model contract (`norm_browser.json` is written positionally).

The Polymarket-side inputs it needs — token price, spread, time to settlement, book imbalance — are
all already in our `paper_ticks` schema, which is why this is worth a second look rather than a
rewrite.

## What's left if this is picked up again

1. **Record first.** Without a growing table of decision-time Polymarket prices there is nothing to
   score any model against. 76 rows is not a dataset.
2. Reproduce the 79 features exactly (order matters) before judging the shipped model — or ignore
   the weights and reuse only their training pipeline on our own recorded data.
3. Check their labels for leakage before trusting 94% holdout: confirm no feature is computed from
   inside the settlement window.
4. Train the plain version first: LightGBM on the Binance archive, fair value as an input, isotonic
   calibration, walk-forward by time, logged beside the Polymarket mid on paper — no gating, just
   observe ([[feedback_no-verdicting]]).
5. Note the horizon question: their model is BTC **15-minute**. The 5-minute family is out of scope
   as of 2026-09-19. Hourly is the horizon worth its own training run.
