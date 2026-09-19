# Deep sequence models trained on crypto — survey (2026-09-19)

Parked. **Desk research only — no code, no data, no backtest, nothing measured here.**
Every number below is copied from a model card or a paper, not reproduced.

Backlog issue: #257

## What prompted this

Question was simply: which deep sequence models have actually been trained on
crypto price series, with weights you can download? The repo itself has no such
model wired in — `polymarket_bot/chronos_signal.py` is a stub that always
returns "no signal", and the 5-minute family it was designed for is gone.

## Candidates found

### Kairos — the only one with a published short-horizon direction number
- [`Shadowell/Kairos-base-crypto`](https://huggingface.co/Shadowell/Kairos-base-crypto)
  (~102M, MIT) and [`Shadowell/Kairos-small-crypto`](https://huggingface.co/Shadowell/Kairos-small-crypto) (~25M).
- Kronos + a 32-dim exogenous bypass channel + a quantile return head, fine-tuned on
  **BTC/USDT + ETH/USDT 1-minute K-lines, 2024-01 → 2026-04** (Binance Vision spot mirror).
- Lookback 256 minutes, predicts 30 minutes. Tokenizer stays `NeoQuasar/Kronos-Tokenizer-base`.
- Author's own test set (2026-01-01 → 2026-04-16, 304,710 1-min bars), direction hit rate:

  | horizon | base: baseline → finetuned | small: baseline → finetuned |
  |---|---|---|
  | 1 min  | 50.78% → 50.37% | 50.58% → 49.53% |
  | 5 min  | 51.61% → 50.95% | 49.87% → 50.51% |
  | 30 min | 52.49% → 52.92% | 49.04% → 51.68% |

  "baseline" = stock Kronos weights with a randomly initialised exog/return head.
  Fine-tuning helped only at 30 minutes; at 1 and 5 minutes the base model got worse.
- Caveat the card states itself: the 5 crypto-native exogenous features
  (funding rate, funding z, OI change, basis, BTC dominance) are **padded to zero** —
  they were never fed real values. The other 27 dims are real.
- Training recipe / pitfalls: https://github.com/Shadowell/Kairos

### Kronos family (the pretraining base)
- [`NeoQuasar/Kronos-{mini,small,base}`](https://huggingface.co/NeoQuasar/Kronos-base) —
  4.1M / 24.7M / 102M, MIT, `arxiv:2508.02739` (AAAI 2026).
- Pretrained on 12B+ K-lines from 45 exchanges: equities, futures, forex **and crypto**,
  1-minute through daily. So crypto is in the pretraining mix, not the whole diet.
- Community BTC fine-tunes: [`lc2004/kronos_base_model_BTCUSDT_1h_finetune`](https://huggingface.co/lc2004/kronos_base_model_BTCUSDT_1h_finetune),
  [`..._4h_finetune`](https://huggingface.co/lc2004/kronos_base_model_BTCUSDT_4h_finetune),
  `tuan83/kronos-finetuned-latest` (24.7M, no card).

### FinCast
- [`Vincent05R/FinCast`](https://huggingface.co/Vincent05R/FinCast), 1B params, Apache-2.0,
  CIKM 2025, arXiv 2508.19609. Decoder-only + MoE + point/quantile (PQ) loss.
- Pretrained on 20B financial time points spanning **crypto**, forex, futures, stocks, macro.
- Paper claims ~20% lower MSE zero-shot vs TimesFM / Chronos-T5 / TimesMoE on 3,632 series.
  **No crypto-specific or direction accuracy published.** MSE, not calibration.
- Code: https://github.com/vincent05r/FinCast-fts. 1B params is heavy for an 8 GB Mac —
  this is an HF-compute job, not a local one.

### CryptoMamba
- https://github.com/MShahabSepehri/CryptoMamba — 136k params (tiny), state-space model,
  arXiv 2501.01010, IEEE ICBC 2025. Checkpoints in-repo (`checkpoints/cmamba_v.ckpt`).
- **Daily** BTC close, one-day-ahead point forecast. Smallest footprint of its baselines
  (vs Bi-LSTM 569k, S-Mamba 330k).

### Looked at and dropped
- `matthewnguyen1005/BTCUSDT-1m-*` — repo holds a trained **tokenizer only**, no base model.
- `XelotX/btc_usdt_full-model-artifacts-{1m,5m,15m,1h}` — genetic-algorithm strategy
  artifacts and simulation JSON, not a sequence model.
- `funasoft/StockLlama-*-BTCUSDT-*` — no card, 2025 training window, 0–2 downloads.
- `web3face/BitcoinPricePrediction`, `arova-syams/Bitcoin-Price-Prediction-with-LSTM` — toys.
- CryptoBERT / FinBERT-crypto / CryptoTrader-LM — text models, not price sequence models.

## If picked up

1. Pick the market first, then the horizon. Kairos is the only 1-minute-native option;
   Kronos base fine-tunes are hourly; CryptoMamba is daily.
2. Live inference needs only the trailing window from Binance's public API
   (Kairos: last 256 1-min candles; hourly Kronos: last ~500 1h candles). No archive.
3. Measure calibration (reliability curve, Brier), not just hit rate — the bet needs a
   probability that beats the posted price, not a direction.
4. Sample many paths for `P(up)`, and compare against the venue's **real** open, not the
   model's own reconstructed open.
5. Break-even on Polymarket taker fills is the spread — roughly 52.25% on the hourly
   BTC book. Any hit rate below that is not tradeable as a taker.
6. Kairos's zeroed funding/OI/basis channels are the obvious first experiment: feed them
   real values from the venue-flow feeds instead of zeros.

## Sources

- Kronos paper: https://arxiv.org/abs/2508.02739
- FinCast paper: https://arxiv.org/pdf/2508.19609
- CryptoMamba paper: https://arxiv.org/pdf/2501.01010
