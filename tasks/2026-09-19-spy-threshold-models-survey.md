# SPY "closes above X" — model survey notes (parked 2026-09-19)

Backlog issue: #256

Desk research only. **No code, no data, no backtest.** Nothing here has been run or measured.
Parked at this point so it is recoverable later.

The market shape in question: *will SPY close above a given strike*. That needs a **probability**,
so the only models that are directly usable are the ones that output a distribution over the
closing price. A model that returns a single number needs an extra error model bolted on before it
can price anything.

## The two models named, as they actually exist on the Hub

### `ColonelParrot/benchstreet-chronos-t5-small-sp500`
- Fine-tune of `amazon/chronos-t5-small`. 46.2M params, T5 seq2seq, MIT licence.
- Uploaded 17 Jul 2025, ~52 downloads. Part of a personal benchmark project ("benchstreet"),
  not a lab release. There is a sibling `ColonelParrot/benchstreet-timesfm-2.0-500m-torch-sp500`.
- Training data per the card: 20 years of S&P 500 **daily closing prices**. Prices, not returns.
- Config: context length 730 days, **prediction horizon 730 days**, min 60 past observations,
  20 sample paths per inference. Tokenizer: mean-scale uniform binning, 4096 tokens, clipped to
  [-15, 15].
- **No accuracy, no benchmark numbers, no usage example on the card.**
- Because it is Chronos, it samples paths, so `P(close > strike)` is just the fraction of sampled
  paths above the strike. That is the one property that makes it usable at all here.
- Two mismatches to resolve before testing: the horizon is tuned for 730 days, not 1 day, and the
  card says S&P 500, not SPY — index level vs ETF price are different series (dividends, scale).

### `StockLlama/StockLlama-tuned-SPY-2023-01-01_2024-08-24`
- 176.9M params, class `StockLlamaForForecasting`, custom architecture. Uploaded 31 Aug 2024,
  15 downloads. Base is `StockLlama/StockLlama-base-v1` (5.4K downloads, Apache-2.0), pretrained on
  `Q-bert/NASDAQ-Daily-Close-Random-100`.
- **The SPY tune's training window is 2023-01-01 → 2024-08-24 and it has not been updated since.**
  That is ~20 months of data covering one regime. Anything after Aug 2024 is genuinely
  out-of-sample, which is useful, but the model is two years stale.
- Appears to be a single-target forecasting head — a point price, not a distribution.
  **Not verified**; check the output shape before assuming. If it is a point forecast, it cannot
  answer a threshold question without fitting a residual distribution on top, and that residual fit
  becomes the actual model.
- There is a `Q-bert/StockLlama-LoRA-SPY-2024-07-01_2024-9-31` adapter too, and the family has
  BTC/ETH/DOGE tunes built the same way.

Local compute is fine for both — 46M and 177M params run on the 8 GB box. Unlike the weather
models, this does not need HF compute.

## What the literature says (alphaXiv)

**Pretrained TSFMs for financial return forecasting — arXiv 2606.27100.** Benchmarks TimeGPT,
TimesFM-2.5, Moirai-2.0, Chronos and Chronos-2 against NBEATS, NHITS, PatchTST, iTransformer, KAN
on five liquid US equities, 512-day lookback, 20-business-day horizon, rolling origin, MAE.
- TSFMs won 8 of 10 tasks; Moirai-2.0 and TimesFM-2.5 had the best average ranks.
- **Only 2 of the results beat a random walk at p < 0.05** (Chronos on AMZN, Moirai-2.0 on GOOG).
- Authors' conclusion: useful priors that cut model-development cost, not reliable alpha.
- Takeaway for us: **random walk is the baseline that has to be beaten**, and the bar is high. Any
  test here must report the model against a random-walk / drift-free benchmark, not in isolation.

**Forecast collapse in TSFMs — arXiv 2608.14106.** TimesFM and Chronos plus 12 deep architectures
(97 configs) on Finance1K — 1,000 US equities, hourly, 2015 to early 2026.
- On equity *returns* the forecasts go nearly flat: raw amplitude < 0.05 relative to the target;
  for low-predictability targets (R² < 0.05) the median amplitude is under 6% of the target's
  standard deviation. The same models are fine on trading volume, so it is the target, not the model.
- Cause given: low signal-to-noise puts a mathematical ceiling on optimal forecast amplitude, and
  per-series MSE-style objectives ignore cross-series structure.
- Their fix, CalibRank (MSE + information-coefficient ranking term): roughly tripled cross-sectional
  correlation, Sharpe 0.052 → 0.170, median IC gain 0.0489.
- **Most important consequence for a threshold market:** a collapsed forecast means the predicted
  distribution sits on top of the current price with too little spread moved. Read `P(> strike)` off
  that and you get something close to a fixed number regardless of input. Check for this first —
  plot the model's implied probability against the strike distance and see if it actually moves.

**Multivariate financial forecasting with Chronos — arXiv 2605.21504.** Chronos-2, asks whether
multivariate inputs beat univariate on economic/financial series. Read the result before assuming
extra features help; a newer Chronos-2 may also be the better starting point than a personal
fine-tune of chronos-t5-small.

**ProbFM — arXiv 2601.10591** (JPMorganChase). Probabilistic TSFM with uncertainty decomposition,
aimed squarely at zero-shot financial forecasting and at the fact that TSFM uncertainty estimates
are not trustworthy as-is.

**Calibration, the piece that actually decides this.** A threshold market is a pure calibration
problem — the question is never "what is the price" but "is 30% really 30%". Relevant and unread:
- *Beyond Point Forecasts: survey on probabilistic forecasting* — arXiv 2609.13345
- *Retrieval-Corrected Conformal Prediction for Time Series* — arXiv 2608.10553
- *Isotonic Conformal Prediction* — arXiv 2607.16675, and *binary isotonic regression* — arXiv 2607.27301
  (isotonic regression is the standard way to recalibrate a probability against realised outcomes)
- *Regime-aware conformal calibration* — arXiv 2608.17079, *regime-weighted conformal VaR* — arXiv 2602.03903

## If this is picked up again

1. Settlement first, as always: what exactly does the market settle on — SPY's official close, a
   specific print, an index level? The chronos model was trained on S&P 500, not SPY.
2. The baseline to beat is not the other model, it is the **random walk** (and, for anything
   short-dated, the **option-implied distribution** from SPY/SPX strikes, which already is a
   market-consensus probability of closing above a price and is free to compute). If the model
   cannot beat those, the rest does not matter.
3. Test the collapse failure mode before anything else: sweep the strike, plot the model's implied
   probability, confirm it responds to strike distance and to the input window.
4. Of the two named, only the Chronos fine-tune gives a distribution directly. StockLlama needs a
   residual model added, and its training window ended Aug 2024.
5. Measure calibration, not accuracy — reliability curve and Brier score against realised closes,
   then isotonic recalibration on a held-out slice.
6. Mismatched horizon is the obvious thing to fix on the Chronos side: the card's 730-day setup is
   not the setup a daily threshold market needs.
