# DOGE price models on Hugging Face — survey (parked 2026-09-19)

Backlog issue: #257

Desk research only. **No code, no data, no backtest, nothing downloaded.** One question was asked:
what exists on Hugging Face trained on DOGE historical price data. The Hub was searched through the
Hugging Face MCP connector on 2026-09-19 (`dogecoin`, `doge`, `DOGEUSDT`, `DOGE-USD`,
`doge price prediction`, `doge lstm`, `dogecoin forecast`, `DOGE crypto`).

## What exists — three DOGE-only price models, all hobby uploads

| Repo | What it is | Trained on | Reported result |
| --- | --- | --- | --- |
| [`phamluan/crypto-dogecoin-predictor`](https://hf.co/phamluan/crypto-dogecoin-predictor) | RandomForest + GradientBoosting + LinearRegression + LSTM, 23 TA features | 365 days of **daily** CoinGecko DOGE prices, trained 2025-10-24 | RMSE 0.0018 (LR), 0.0090 (GB), 0.0117 (RF), 0.0146 (LSTM); MAE 0.0013–0.0116 |
| [`StockLlama/StockLlama-tuned-DOGE-USD-2023-01-01_2024-08-24`](https://hf.co/StockLlama/StockLlama-tuned-DOGE-USD-2023-01-01_2024-08-24) | 708 MB transformer (`model.safetensors` + `scalers/`) | daily DOGE-USD, 2023-01-01 → 2024-08-24 | **none** — auto-generated card, every field "[More Information Needed]" |
| [`handecarkci/Dogecoin-Price-Predictor`](https://hf.co/handecarkci/Dogecoin-Price-Predictor) | RandomForestRegressor pickle + Streamlit app (card in Turkish) | daily OHLC + volume + market cap, next-day close | R² ≈ 0.984, RMSE ≈ 0.0021; `sample_input.json` is empty, no training code in the repo |

Two LoRA adapters also exist (`Q-bert/StockLlama-LoRA-DOGE-USD-*`, same 2023-01-01 → 2024-08-24
window, 0 downloads). Nothing else on the Hub is a DOGE price model — the rest of the `doge` hits are
the SmallDoge LLM family, DogeAI/DoGe chat models, a `dogeum` robotics account, and an SDXL LoRA of
the Shiba Inu.

## Why none of this is usable as-is

- **All three predict a price level, not direction, on a daily horizon.** Errors are quoted in
  dollars on a ~$0.2 asset. A one-day-ahead price is not a Polymarket Up/Down answer.
- **The reported numbers are in-sample-shaped and unvalidated.** `phamluan`'s best model is
  *linear regression* — on a price-level target with `price_lag_1` among the 23 features, that is
  the signature of a model repeating yesterday's price, and every other model losing to it is the
  tell. Same for R² 0.984 on `handecarkci`. Neither publishes a locked out-of-sample window, a
  walk-forward split, or a direction hit rate.
- **No base rate anywhere.** None of the three reports what always-up or persistence scores on the
  same days, so there is no excess to read off.
- **StockLlama has no card at all** — no training procedure, no metrics, no usage. 708 MB of weights
  and nothing that says what they do. The Mac has 8 GB of RAM and a near-full disk, so this is not a
  casual download.

## Data that is worth keeping

| Dataset | Contents |
| --- | --- |
| [`nickbett/bybit-linear-perps-dogeusdt`](https://hf.co/datasets/nickbett/bybit-linear-perps-dogeusdt) | Bybit DOGEUSDT linear perps, 100M–1B rows, parquet (uploaded 2026-08-28) |
| [`Miaowuawa/DOGE-USDT-2019-2024-Binance`](https://hf.co/datasets/Miaowuawa/DOGE-USDT-2019-2024-Binance) | Binance DOGE/USDT 2019–2024, 1M–10M rows, CSV |
| [`jalvart/dogecoin-tick-level-trade-data-free-sample`](https://hf.co/datasets/jalvart/dogecoin-tick-level-trade-data-free-sample) | tick-level DOGE/USDT trades, 1-week sample (2025-09-29 → 10-06) |
| [`dataforge-labs/dogecoin-network-propagation`](https://hf.co/datasets/dataforge-labs/dogecoin-network-propagation) | Dogecoin peer-network block propagation latency, 1M–10M rows |
| [`EMEliasMi8859/DogeUSDT_30m_start_2025_jan_18`](https://hf.co/datasets/EMEliasMi8859/DogeUSDT_30m_start_2025_jan_18) | DOGE/USDT 30m bars from 2025-01-18, 10K–100K rows |

## What's left if this is picked up again

- Settle the horizon first. The DOGE markets this project has actually touched are **hourly**
  (see the 2026-09-14 thin-book test), and every model above is daily. A daily-close model does not
  answer an hourly question, so this survey found nothing aimed at the market that exists.
- If any of the three is tested, score **direction** on dates after the training window
  (`phamluan` after 2025-10-24, StockLlama after 2024-08-24) and report the hit rate beside
  always-up and persistence on the same days.
- The exchange datasets are the durable part of this survey. A DOGE bench would be built from
  `nickbett/bybit-linear-perps-dogeusdt` or `Miaowuawa/DOGE-USDT-2019-2024-Binance`, not from these
  models.
- Whatever gets built, the market mid is the baseline to beat, not 50% — same rule as the gold/silver
  and WTI surveys (#254, #255).
