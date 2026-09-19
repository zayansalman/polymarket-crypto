# WTI daily Up/Down — model shortlist (2026-09-14)

Target: Polymarket "WTI Up or Down" = active-month WTI 5pm-ET close vs prior 5pm close.
Nothing below is tested yet unless marked. Full catalogs: llm.md, gbm.md, deep_sequence.md, linear_stats.md.

Big picture: no public model shows an honest daily oil direction result above ~52–55%.
Every >60% claim checked had a leak (same-day inputs, random splits, price-level "accuracy").
Break-even on Polymarket ≈ 52.5% (entry ~50c right after 5pm, 0.04·p·(1−p) taker fee, spread).

## Already tested (walk-forward, 600 Polymarket-matched days 2024-04..2026-09)
- Linear / logistic / LightGBM / RF (18 variants): 49–52.5%, all CIs include 50%.
- nyanko1999/oil-price-model: predicts $10 price bands, not direction. Price-only implied direction 51.1% (n=569).
- DrAdrianDC/wti-lstm-autoencoder: no direction signal; next-day move ~1.85x larger after a flag.
- seq_* preds in preds/ are from a run stopped mid-way — treat as unfinished.

## LLM
1. CrudeBERT — headline sentiment; published 52.5% next-day WTI (2012–21, not held out). CPU, clean test 2022+.
2. Local LLM "bullish/bearish for WTI?" per headline, averaged per day — Gemma 4 E2B (cutoff Jan 2025), Olmo 3 7B (Dec 2024), Phi-4-mini (Jun 2024), SmolLM3-3B (Jun 2025). Test only after cutoff.
3. Dai et al. arXiv 2603.11408 setup moved to daily (LLM sentiment dims + LightGBM); weekly AUC 0.65, never compared to always-up.
4. Point-in-time LLMs (PIT-4B, DatedGPT, ChronoBERT yearly) — leakage control.
5. EXAONE Finance 1.0 — synthetic-only training, no look-ahead; non-commercial.
Note: TimeSeek — 10 frontier LLM agents all lost to Kalshi market prices in finance. Score vs the Polymarket price, not 50%.

## GBM / trees
1. SelinaPhan0205/OilPricePrediction — next-day LightGBM, saved model; 54.7% walk-forward 2020–25 (58% in high OVX); features chosen with test visible.
2. trungdangtapcode/Oil-Prediction-Data-Mining-ML — leak-safe pipeline (conflict, GDELT, EIA, FRED); 54.5% / AUC 0.559, 2023–26.
3. Ddhamani123/wti-direction-ml — XGBoost + Cushing surprise; ships OOS preds 2015–25 (scorable without retraining); ~53% then ~50% in 2025.
4. ziqianz360/oil-price-prediction — LightGBM 55.4% on 386 days (unstable).
5. PeterLP123/wti-return-forecasting — best release-timing template; 54.6% (p≈0.1).
6. Tabular foundation models (TabICL, Mitra, TabDPT) on a rolling window — calibrated probabilities.
Features worth adding: OVX, CL1–CL4 curve, crack spread, COT, EIA stocks/surprise, daily GPR, EPU.

## Deep sequence
Pretrained (zero-shot, CPU-OK on M2):
1. Chronos-2 (120M, Apache) — covariates (Brent, products, DXY, OVX), quantiles → P(up).
2. TiRex-2 (38M) — top of GIFT-Eval/fev-bench.
3. Kronos-small/base (MIT) — OHLCV candles incl. futures; pretraining to Jun 2024 → test from Jul 2024.
4. TimesFM 2.5 (Apache) / 3.0 (non-commercial).
5. FinText yearly Chronos/TimesFM vintages — pre-2024 backtests without look-ahead.
6. IBM TTM r2.1/r3 (1–35M) — fast, easy to fine-tune with covariates.
Trained:
1. Momentum Transformer / TFT / LSTM (MIT) — futures panel incl. crude.
2. VLSTM / TFT / xLSTM (Oxford 2026 futures benchmark) — WTI +3.8%/yr pre-cost.
3. NeuralForecast/Darts TFT, TimeXer, NHITS with quantile loss + covariates.
4. PeterLP123 probabilistic LSTM — 54.6% on 348 days (Jul 2024–Mar 2026).

## Linear / statistical / rules
1. Roll-day label mechanics — ~12 days/yr the "prior close" may be the old contract; curve spread could decide the label. Confirm first.
2. Intraday baseline Φ(move so far / remaining vol) — only if betting during the day.
3. EIA release-day premium (Wednesdays; holiday weeks shift).
4. API Tuesday 4:30pm → EIA Wednesday.
5. Daily reversal (commodity autocorr ≈ −0.04 post-2006) — low prior, needed baseline.
6. COT hedger flow (weekly); 7. OPEC+ decision days; 8. curve shape; 9. OVX / variance risk premium; 10. day-of-week sanity check.
Low priors: GARCH-in-mean, Markov switching, single-asset momentum, BMA, RL agents.

## Free data verified
FRED (WTI, Brent, OVXCLS, USEPUINDXD), CBOE OVX, CFTC COT, daily GPR, EIA release calendar, Yahoo CL=F (daily + 1h/730d),
oilprice.com archive (minute timestamps, 2020+). Pyth history needs a paid key.
