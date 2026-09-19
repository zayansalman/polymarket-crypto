# oil_updown — research bench for the Polymarket daily WTI Up/Down market

Parked. Write-up and findings: [`tasks/2026-09-14-wti-updown-models.md`](../../tasks/2026-09-14-wti-updown-models.md).
Nothing here is wired into the bot; it is a standalone bench that runs from its own venv.

Data files are **not** in git. They live in the gitignored `data/oil_updown/` of the main checkout
(`dataset.parquet`, `wti_cl.parquet`, `wti_1h.parquet`, `market_ohlcv.parquet`, `pm_oil_events.json`,
`pm_entry_prices.csv`, `news_oilprice.jsonl`, `preds/`, plus HF downloads under `nyanko/` and `lstm_ae/`).

## Run order

```bash
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python pandas numpy scikit-learn lightgbm statsmodels yfinance \
    huggingface_hub requests scipy pyarrow
.venv/bin/python build_dataset.py     # needs the raw parquet files in the working dir
.venv/bin/python classic_models.py    # writes preds/<model>.parquet
.venv/bin/python score.py             # prints + writes scoreboard.csv
```

`build_dataset.py` expects `wti_cl.parquet`, `wti_1h.parquet`, `market_closes.parquet` and
`market_ohlcv.parquet` in the working directory (yfinance downloads; see the write-up for tickers).

## Files

| file | what it does |
| --- | --- |
| `build_dataset.py` | daily table: `y_settle`, `y_5pm` targets + 57 features, all lagged to the prior close |
| `classic_models.py` | walk-forward rules, linear/logistic, LightGBM, random forest → `preds/` |
| `score.py` | scores every `preds/*.parquet` on windows A (settle 2012+), B (5pm 2024-04+), C (116 real Polymarket outcomes) |
| `safe_load.py` | allowlist unpickler for third-party sklearn/joblib pickles (no code execution) |
| `scrape_news.py` | polite oilprice.com archive scraper (title, minute timestamp, excerpt); stopped at 860 rows |
| `scoreboard.csv` | results as of 2026-09-14 |
| `notes/` | model catalogs by family (`llm`, `gbm`, `deep_sequence`, `linear_stats`), `SHORTLIST.md`, `ARXIV_NOTES.md` |

## Caveats

- The target that matters is `y_5pm` (window B/C). `y_settle` is easier for the wrong reason — see
  the write-up.
- `preds/seq_*.parquet` in the data folder are from an aborted deep-sequence run; not results.
- Free 1h history is ~730 days, so the 5pm target starts 2024-04-23.
