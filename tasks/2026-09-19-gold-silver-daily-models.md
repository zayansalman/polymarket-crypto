# Gold and silver daily Up/Down — market check, model survey, paper notes (parked 2026-09-19)

Backlog issue: #254

Desk research only. **No code, no data, no backtest.** Nothing here has been measured against any
market. Parked at this point so it is recoverable later.

Three questions were asked: does Polymarket list daily gold/silver markets, what models exist on
Hugging Face for them, and what does the literature say is worth building.

## The markets (checked live 2026-09-14 via the Gamma API)

| Series | Slug | Recurrence | Settles on |
| --- | --- | --- | --- |
| Gold daily | `gold-daily-up-or-down` | daily | Pyth XAUUSD close vs the previous trading day's close |
| Silver daily | `silver-daily-up-or-down` | daily | Pyth XAGUSD close vs the previous trading day's close |

- Resolution text: "Up" if today's close is higher than the most recent prior trading day's close,
  "Down" if lower, 50-50 on an exact tie or if the metal does not trade. Closes are used exactly as
  published by **Pyth**, no rounding. End time 21:00 UTC; the market opens ~3 days ahead.
- History: the current XAUUSD/XAGUSD daily series run from **2026-04-09**. An earlier gold futures
  series (`gc-up-or-down-*`, GC) ran **2025-12-09 → 2026-03-25**.
- Size on the days sampled: biggest closed days ~$67k volume (gold), ~$45k (silver). The live
  2026-09-14 gold market had ~$19.5k liquidity and a **5c spread**; silver ~$1.1k and a 6c spread.
- Also listed: weekly and monthly "what price will it hit" markets for both metals.

## Hugging Face — what exists (~55 gold/silver repos, searched by name and by card text)

Only two are daily-close models with a locked out-of-sample test. Everything else is either a
different horizon, an intraday trading system, a news classifier, or an empty repo.

| Repo | What it does | Reported result |
| --- | --- | --- |
| `AurelPx/Aurum-1D` | Gold `GC=F`, next-day log return, **ExtraTrees** on 144 causal features | 970-day locked test: **53.9% hit rate**, net Sharpe 1.19 after 10bps/turnover, IC 0.148 |
| `AurelPx/Argent-1D` | Silver `SI=F`, next-day direction, **logistic regression** | 970-day locked test: **51.4% hit rate**, net Sharpe 1.32, max DD −44% |

Both were selected over 18 candidates with 5 expanding walk-forward folds and benchmarked against
Chronos-2 and TimesFM 2.5 on the same dates. Gold's White Reality Check p = 0.106 (does not clear);
silver's p = 0.042 (clears). For silver the foundation models had **lower squared error** but lost
money on the same days — forecast loss and signed trading performance disagree.

Everything else, by type:
- **Gradient boosting / trees:** `theonegareth/GoldPricePredictor` (next-day, ~55%, but Indonesian
  Antam retail gold, not spot), `Bhushanz16/xauusd-trading-predictor` (3-day, 53.3%, uses `SI=F` as
  an input), `anggakayy/xauusd-regime-switching-ensemble` (M5, 69.5%), `nerds-gaming/xauusd-ml-trader`
  (1h, walk-forward, 52.2% — code only, no weights), `AMFORGE/tabicl-gold-btc-finetuned` (TabICL,
  buy/sell/hold over 60 bars, 64.5%), the `JonusNattapong/romeo-v*` family (15m, 49–68%).
- **Deep sequence:** `JonusNattapong/xauusd-trading-v4-quantum-daily` (64% accuracy but ~9% recall —
  the target is a large up move, not plain direction), `transformer-classifier-gc1h` (GC=F 1h, 6h
  horizon, 52.2% win rate, Sharpe 0.03), `AcyLa/multi-asset-predictive-model` (LightGBM+LSTM; GLD
  1-day 47.8%, SLV 49.4%, both negative Sharpe), `anhbn/Chronos-T5-Fine-Tuning-XAUUSD` and
  `StockLlama/StockLlama-tuned-GOLD-USD-*` (no results published).
- **LLMs:** `nixnub/llama-3-8b-Instruct-bnb-4bit-xauusd` (most-downloaded at 144, no card),
  `sohail-kustagi/Qwen3-14B-xauusd-v1` (weights not uploaded), `TolgaAkat/bist-precious-metals-analyst`
  (Qwen2, no card). News-mood classifiers score headline labelling, not price: `mohanpanakam/goldBERT`
  95%, `DunnBC22/*-News_About_Gold` 91–92%, `misraanay/finbert-tone-gold-lora-final` 87%.
- **Reinforcement learning:** `JonusNattapong/AI-XAUUSD-Trading` (PPO/TD3/SAC, daily, 58.3% win rate,
  three verbatim copies under other accounts), `Reinforcement-Learning-for-Gold-Trading-Model` (PPO,
  15m, claims Sharpe 7.56).
- **Data worth keeping:** `ZombitX64/xauusd-gold-price-historical-data-2004-2025`, `fokan/xauusd-2009-2026`,
  `CarlosSilva1/xauusd-ticks`, `jason1966/aiwithcagri_20-years-of-gold-vs-silver-prices-ratio-daily-data`.
- ~10 repos are empty or gated, including the only gold+silver one (`akashraut/GOLD_SILVER_PRICE`).

## What the literature says (alphaXiv)

**Price the market before forecasting it.** A daily Up/Down contract is a digital option struck at
yesterday's close. Portnaya (arXiv 2606.19517) inverts listed option prices into the same payoff for
BTC threshold markets: Polymarket's Yes traded **6.3pp above** the option-implied value (pooled, n=287,
p < 1e-14), widest at low implied probability and long time-to-expiry, half-life ~4.2h, mean-reverting.
A delta-hedged proxy netted positive after costs but on 16 trades (p = 0.053).

**The market price is the baseline to beat, not 50%.** OpenMarket (arXiv 2607.26245) ran 43
microstructure features through a walk-forward logistic on Polymarket's BTC 15-minute binaries:
out-of-sample **AUC 0.8377 vs the Polymarket mid's 0.8405**, Brier 0.165 vs 0.163, and simulated
positive-EV trading netted **−0.116 payoff units per trade** at 1% fees + 0.5% slippage.

**Score direction against the base rate.** Cheung (arXiv 2607.12248) traced an "80% directional
accuracy" LoRA-TimesFM result to market drift: always-up scored 0.704, the model 0.626. Gold trends;
excess-over-base-rate is the honest metric, with McNemar / Diebold-Mariano on the same windows.

**Volatility is the input, and simple wins.** Brini (arXiv 2607.05291), 50 assets × 9 zero-shot TSFMs
vs 8 econometric specs: only **TTM** (<1M params) beat Log-HAR at every horizon, by 1.3–1.8%; an
equal-weight TTM + Log-HAR average entered the Model Confidence Set for 98–100% of assets. Note for
us: in VOLARE, **gold futures RV has ρ₁ ≈ 0** (no day-to-day carryover) with extreme spikes — the
hardest asset in the panel.

**Foundation models are a prior, not a signal.** Noguer i Alonso & Franklin (arXiv 2606.27100): TSFMs
won 8 of 10 task rankings on 20-day equity returns but beat the random walk under a one-sided DM test
in only 2 of 10 cases. Consistent with what Aurum/Argent found on gold and silver.

**Where the cheap side sits.** Cardozo & Rivero (arXiv 2609.12878), 588M Polymarket trades: buys under
10c lose 6.30% per dollar equal-weighted by child market (−19.35% pooled); buys at 90c+ earn +0.277%
(+0.83% pooled). Finance category: equal-weighted −2.62% / +0.085% (both CIs include zero), pooled
−72.40% / +1.826%.

**Costs and feed caveats.** Dubach (arXiv 2604.24366), 600-market tick panel: median quoted spread
~400bps of mid in the 0.4–0.6 decile, 1,300–1,800bps below 0.10; **trade direction inferred from the
public order-book feed matches on-chain truth only ~59%** of the time, so any "who is buying" signal
must come from on-chain `OrderFilled` events.

**News, if it is ever tried.** Paredes Amorin et al. (arXiv 2603.09085), aluminium, monthly: fine-tuned
Qwen3-8B headline sentiment lifted an LSTM from Sharpe 0.23 to 1.04 in high-volatility regimes.
Headlines reporting events that happened carried the signal (0.62); forward-looking commentary did not
(−0.01). Source mattered — Reuters 0.80 vs Dow Jones 0.18.

Weak but gold-specific: Kazemdehbashi (arXiv 2601.12706) — 2023 gold only, 75 test days, XGBoost
direction 58.7%, LSTM trend detection ~51%.

## What's left if this is picked up again

- Settle the data question first: Pyth XAUUSD/XAGUSD daily closes, point-in-time, are what settle
  these markets. `GC=F`/`SI=F` (what Aurum/Argent use) close at a different time on a different price.
- Decide the decision point. At the market's open it is a next-day direction problem (≈51–54% is what
  the honest models get). Intraday it is mostly "how far is spot from yesterday's close, with how much
  session left" — a digital-option calculation, not a forecast.
- Build the fair-value line before any model: Φ(ln(S/K)/(σ√τ)) with K = prior Pyth close, τ = session
  remaining, σ from Log-HAR on recent realized vol. Log it beside the Polymarket mid, score Brier and
  calibration against the mid, and let it run on paper.
- Spreads here are 5–6c, far wider than the BTC binaries in the papers above. Any measured edge has to
  clear that, and the limit-ladder rule applies the same way it does elsewhere.
