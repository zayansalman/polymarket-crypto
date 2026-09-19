# OpenMarket — a public logistic regression trained on Polymarket BTC 15m markets

Parked 2026-09-19. Search-only session: nothing was built, run, or measured here. This
note records what exists publicly so the next attempt at "logistic regression on
hand-built features" starts from someone else's measured result instead of from zero.

## What it is

`OpenMarket` (Gregory Young, University of Colorado Boulder, 2026-07-28) started as a
trading system against Polymarket's **BTC 15-minute** Up/Down markets, using Binance
BTC/USDT order flow. It shipped as a dataset + negative-result paper.

- Paper: arXiv 2607.26245 — <https://www.alphaxiv.org/abs/2607.26245>
- Code: <https://github.com/gregyoung14/openmarket> (Rust, Apache-2.0, frozen at v0.5.2)
- Model: <https://hf.co/gregyoung14/openmarket-models> (`v0.2.1/binary_outcome_model.json`)
- Data: <https://hf.co/datasets/gregyoung14/openmarket-btc-polymarket>

## The model (weights are public, ~5 KB of JSON)

Walk-forward logistic regression per market + Platt scaling. 43 standardised features,
shipped with `feature_names` / `means` / `stds` / `weights` / `intercept`, so it can be
scored in a few lines without their Rust pipeline.

Feature families: clock (`secs_in`, `secs_left`), price path (`price_vs_open`, returns
and realized vol at 15/30/60/180s, volume, trade counts), order flow (`imbalance_*`,
`ofi_accel`, `path_eff`, `autocorr`), regime one-hots, and the **Polymarket book itself**
(`up_best_bid/ask`, `down_best_bid/ask`, spreads, `mid_up`, `mid_down`,
`market_mid_prior_up`).

Where the weight actually sits (standardised, so comparable): `market_mid_prior_up`
+0.202, `mid_up` +0.202, `mid_down` −0.202, the four book prices ±0.202, and
`price_vs_open` +0.204. Everything else is ≤0.05 — `drift_prob_up` +0.053,
`imbalance_180s` −0.049, `combined_prob_up` +0.038. **The model is mostly re-reading the
market's own price plus distance from the window open.**

## Measured (their numbers, walk-forward OOS, 559 windows / ~356k rows)

| | AUC | Brier | ECE | Log loss |
|---|---|---|---|---|
| Polymarket mid prior | 0.8405 | 0.163 | 0.014 | 0.485 |
| Logistic + Platt | 0.8377 | 0.165 | 0.026 | 0.495 |
| `drift_prob_up` only (diagnostic) | 0.7725 | 0.218 | 0.145 | 0.792 |
| `imbalance_60s` sigmoid (diagnostic) | 0.5863 | 0.246 | 0.031 | 0.685 |

Taking every trade the model scored +EV: **−0.117 payoff units per trade**, 260,617
trades, hit rate 49.4% — under their assumed 1% fee and 0.5% slippage (not Polymarket's
actual `0.07·p·(1−p)` taker fee, so the PnL number is not directly ours).

An in-sample ranking lift over the mid (ΔAUC +0.0014) does **not** survive walk-forward.

## Other microstructure facts from the corpus (useful regardless of the model)

- Polymarket BTC 15m top of book is **one tick wide 91.9%** of the time (median 0.01).
- Polymarket quotes respond to ≥5bps Binance moves after a **median 347 ms** (collector
  clock, synchronisation-free measurement).
- Median cross-venue lead–lag 16–19 ms, flat across disagreement regimes.
- Forecasts are slightly better in high realized vol (Brier 0.159 vs 0.164) and worse
  when quotes widen (Brier 0.185 at spreads ≥0.015).
- Public-feed trade-direction inference is only ~59% reliable, so every `imbalance_*`
  feature inherits that noise.

## The dataset

727,098,247 deduplicated rows, 8.69 GiB Parquet, 54 observed Polymarket days between
2026-02-12 and 2026-05-15, plus 2,936,031 explicit lead–lag pairs. Splits:
`v0.4.3-unified` (analytic), `v0.2-full` (per-snapshot), `v0.1-sample` (quickstart).
Tables: `polymarket_ticks_ms`, `binance_trades`, `binance_ticks_ms`, `lag_pairs_ms`,
`market_meta`, candles 1s→1h.

This is the one thing we do not have and cannot backfill: **months of recorded
Polymarket book history**. Our own DB only holds what we recorded live.

## Gaps against our setup

- 15-minute BTC markets, not the timeframes we trade now.
- Prices off Binance; our settlement reference is Chainlink/Pyth depending on series.
- Fee/slippage assumptions are theirs (1% + 0.5%), coarser than the real taker fee.
- Their features assume a websocket book feed at millisecond resolution.

## If picked up again

1. Score the released weights on our own recorded ticks — cheap, no training, and tells
   us whether the "market mid + distance from open" combination behaves the same on the
   timeframes we trade.
2. Use the `v0.1-sample` split first (8.69 GiB unified split vs 8 GB RAM on this Mac —
   see the low-memory constraint; the heavy split belongs on HF compute).
3. If we want our own coefficients, the honest comparison is always **against the book
   mid at the same cutoff timestamp**, walk-forward, never a random row split — adjacent
   high-frequency rows are autocorrelated.

## Also checked, not useful

- arXiv 2506.05764 (UChicago) — logistic/XGBoost vs DeepLOB on Bybit BTC/USDT LOB.
  Horizons 100ms–1s, one trading day, binary accuracy 0.52–0.73 depending on depth and
  filtering; simpler models matched the deep nets. No weights released, horizon far
  shorter than anything we trade.
- arXiv 1512.03492 (queue imbalance, logistic) — Nasdaq equities, not crypto.
- Hugging Face has no other public direction model of this shape trained on crypto; what
  is there is LLM fine-tunes and sentiment classifiers.
