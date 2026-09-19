# WTI daily Up/Down — model survey + first measurements

Parked 2026-09-19 (work done 2026-09-14). Code and results are on this branch under
[`tools/oil_updown/`](../tools/oil_updown/); the data files (parquet, news, HF downloads, ~9 MB)
stay in the gitignored `data/oil_updown/` in the main checkout.

## The market

Polymarket series `oil-daily-up-or-down` ("WTI Crude Oil Up or Down on <date>"), 117 markets
since 2026-03-25, median volume ~$48k. Resolves Up if the **active-month WTI futures close at
5pm ET** is above the prior trading day's 5pm close, priced off Pyth, 50-50 on an exact tie.
Market opens 12:00 UTC the prior day; fee schedule `finance_prices_fees`, rate 0.04, taker only,
so ~1c per share at 0.50. Fair entry is ~0.50 right after the 5pm reference is set; by 9am the
next morning the price has already moved with overnight futures (observed 0.02–0.97).
**Break-even ≈ 52.5%** taking the spread.

## Target and data (verified)

- Yahoo `CL=F` daily Close is the ~2:30pm settle and matches the Polymarket outcome on only
  **85%** of the 116 resolved markets.
- The 5pm close rebuilt from Yahoo `CL=F` 1h bars (last bar with hour < 17 ET) matches **95.7%**.
  That is the target used here (`y_5pm`, 601 days from 2024-04-23, the limit of free 1h history).
- Pyth's own history is not free: `benchmarks.pyth.network` returns 404 and Hermes historical
  updates return 401.
- Long-history proxy `y_settle` (settle-to-settle, 2001+) is **not** the market's question:
  it is predictable from moves that happen after 2:30pm, which the 5pm reference already contains.
  That is why the same model scores 54.6% on `y_settle` and 50.2% on `y_5pm`.

## Measured (walk-forward, refit yearly, no tuning on test years)

Full table in [`tools/oil_updown/scoreboard.csv`](../tools/oil_updown/scoreboard.csv).
Windows: A = settle target 2012+ (n≈3,682), B = 5pm target 2024-04+ (n=600), C = the 116 real
Polymarket outcomes.

| family | best on B (5pm target) |
| --- | --- |
| linear / OLS on 10 return lags | 52.5% [48.5–56.5] |
| logistic on price features | 52.3% [48.3–56.3] |
| LightGBM (price, all features, 5y window) | 49.2–52.0% |
| random forest depth 3 | 50.3% |
| rules (persistence, reversal, 5d/20d momentum, Brent lead) | 48.3–52.0% |

Every interval on B includes 50%. 18 variants were run; none separated from the base rate.

Hugging Face models that exist for oil:

- **`nyanko1999/oil-price-model`** — RandomForest(depth 3) over 138 features, target is the **$10
  price band** (`bin_edges` 55/65/75/85/95/105/inf), not direction. "Same band as today" scores
  86–92%, above the card's 66–70%. Its tweet/TF-IDF features (Trump, Khamenei, Araghchi, IDF,
  WhiteHouse, mfa_russia…) are not published, so only a price-only run was possible: the band it
  predicts implies a direction on 36% of days and those calls hit **51.1%** (n=569).
  Pickles were opened with an allowlist unpickler, [`tools/oil_updown/safe_load.py`](../tools/oil_updown/safe_load.py).
- **`DrAdrianDC/wti-lstm-autoencoder`** — anomaly flags, no direction content: P(up) after a flag
  52.0% [44.6–59.3] on 2020–2026. But the day after a flag moves **3.72% vs 2.01%** — a volatility
  signal, useful as an input, not a side.
- **`Captain-1337/CrudeBERT`** — not run. The 438 MB weight download was declined mid-session.

## Unfinished

- Deep-sequence (LSTM/GRU/TCN/Transformer) and zero-shot foundation-model runs were **stopped
  part-way**; `preds/seq_*.parquet` in `data/oil_updown/` are from that aborted run and must not be
  read as results.
- `scrape_news.py` (oilprice.com archive, minute timestamps back to 2020) was stopped after 860
  headlines. No news, sentiment or LLM test was run.

## What the literature says (details in [`notes/ARXIV_NOTES.md`](../tools/oil_updown/notes/ARXIV_NOTES.md))

- No public model has an honest post-cutoff daily oil direction result above ~52–55%; every >60%
  claim checked had a leak.
- The strongest lead is not direction at all: **price the contract intraday** and trade the gap.
  arXiv 2606.19517 found Polymarket BTC threshold contracts sat 5.6–6.3 pts above option-implied
  fair value, mean-reverting with a ~4h half-life, gap largest at low probabilities.
  Oil version: fair P(Up) = Φ( ln(S_now/S_ref) / (σ·√τ) ), σ from OVX or a vol forecast.
- For that σ: arXiv 2607.05291 over 50 assets incl. CL — only IBM **TTM** beats Log-HAR, and
  Log-HAR + TTM averaged is better than either. Toto blew up on crude.
- LLM agents lose to market prices in the financial category (TimeSeek, arXiv 2604.04220) and are
  only competitive near 50c early in a market's life.
- Only LLM-on-oil paper (arXiv 2603.11408) is weekly, tuned on its own folds, and its useful
  features were sentiment **intensity/uncertainty/dispersion**, not polarity.

## Next, if picked up

1. Confirm roll-day label mechanics: ~12 days/yr the "prior close" may be the expiring contract,
   in which case the CL1–CL2 spread decides the label before the market moves.
2. Build the intraday fair-value check against Polymarket's own price history (needs per-market
   `prices-history`; only ~20 of 116 returned data with `interval=max`, so pull with explicit
   start/end timestamps).
3. Only then revisit next-day direction models, and test LLMs strictly after their training cutoff.
