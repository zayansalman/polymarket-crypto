# arXiv takeaways for WTI daily Up/Down (read 2026-09-14)

Ranked by expected usefulness for the Polymarket market.

1. Intraday fair value vs Polymarket price — 2606.19517 (BTC threshold markets vs Binance/Deribit options)
   - Polymarket Yes sat 5.6–6.3 pts above option-implied fair value (n=287 hourly), gap half-life ~4h,
     largest at low probabilities and longer time to expiry. Delta-hedged proxy: 16 trades, net positive, p≈0.05.
   - Oil version: fair P(Up) = Φ( ln(S_now / S_ref) / (σ·√τ) ), S_ref = prior 5pm close, τ = time to 5pm,
     σ from OVX or a realized-vol forecast. Trade only when Polymarket price is outside fair ± fees/spread.
   - Caveat: one author, 3 BTC contracts from 2023; untested on oil.

2. Volatility input — 2607.05291 (9 TSFMs vs HAR, 50 assets incl. CL futures)
   - Only IBM TTM (<1M params) beats Log-HAR at every horizon, by ~1.5%. Equal-weight TTM + Log-HAR is best
     (in MCS for 98–100% of assets). Toto blew up on crude/gold. Chronos-Bolt, TimesFM 2.5, Moirai lose to Log-HAR.

3. Price tails — 2609.12878 (588M Polymarket trades)
   - Buys <10c lose (crypto −15%, politics −16% equal-weight); buys ≥90c earn +0.3–0.8%.
   - Finance category: depends on weighting (−72% pooled, not significant equal-weight). Check on our own oil markets.

4. LLMs vs market prices — 2604.04220 (TimeSeek, 10 frontier models, 150 Kalshi markets)
   - All 10 lose to the market in the Financial category (BSS −0.22 to −1.23).
   - Only competitive early in a market's life and on toss-ups (price near 50c); lose badly near close.
   - Implication: if an LLM is used, only at the open (~50c, right after the 5pm reference), defer to price later.

5. News sentiment — 2603.11408 (GPT-4o/Llama-3.2-3B 5-dimension sentiment + LightGBM, WTI)
   - WEEKLY only. AUC 0.57–0.65, accuracy 0.49–0.58 vs 53% up-weeks. Optuna tuned on the same CV folds (optimistic).
   - Useful features were intensity, uncertainty (level vs change opposite signs), polarity dispersion — not polarity.
   - Llama 3.2-3B noisy; adding it to GPT-4o did not help. Prompt is in their Appendix A.

6. Look-ahead in LLM backtests — 2605.24564 (FinCAD)
   - Memorized outcomes inflated in-sample returns by up to 67%. Anonymizing names/dates only partly helps.
   - Simplest guard: only test on dates after the model's training cutoff.

7. TSFM wins on monthly ag prices (2601.06371) are price-level forecasts; TSFMs zero-shot on daily returns
   underperform tree ensembles (Rahimikia et al., cited in 2607.05291). Don't expect daily direction skill.

Also seen, not read: 2607.14051 Hindcast (replaying prediction markets to evaluate LLM forecasters),
2601.13770 Look-Ahead-Bench, 2602.19520 calibration by domain, 2606.07811 real-time information processing.
