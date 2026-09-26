# Findings — Cross-market signal & live-edge audit (negative result)

**Issue:** #97 · **Branch:** `feature/97-cross-market-signal` · **Date:** 2026-06-22 · **Status:** closed / no build

## TL;DR

The question was: *can data from other-timeframe Polymarket BTC markets (15m/1h) improve our
5m positions, or support a multi-book strategy?* After a deep, adversarially-verified
investigation the answer is **no — and the premise doesn't hold yet**: the 5m strategy has
**no statistically demonstrated live edge** to amplify, and every lever that might add one is
exhausted. This is a clean negative result. **Recommendation: build nothing here.**

## The investigation steps (each independently verified)

| Layer | Verdict | Deciding evidence |
|---|---|---|
| **Cross-market / drift as directional signal** | ❌ dead | Local drift (momentum *and* level-amplification) adds **nothing beyond the `log(spot/ref)` cushion** already in fair value; worsens Brier +0.08–0.11. On 709 *untraded* windows, raw corr(momentum, terminal)=+0.485 is the cushion in disguise — control for it and it collapses to −0.069 (wrong sign). Cross-market feeds would only proxy that drift, with added latency/basis risk. |
| **Model-math fix (recalibration / shrinkage)** | ❌ dead | Model is already near-calibrated: best global shrink improves Brier **0.3%**, sitting at the 94th pct of a perfectly-calibrated null. The "overconfidence" and "regime-instability" reads were **small-sample noise** (96% of calibrated-null sims reproduce the per-day shrink swing). |
| **The live edge itself** | ❌ unproven | n=193 live trades: **+$6.98 total, +$0.036/trade, bootstrap 95% CI [−$0.34, +$0.40]** (spans zero); win 52.3% < ~53–55% breakeven. The impressive paper PnL is a **scalp-fill artifact**: paper's +$370 `TARGET` layer assumes instant/full exit fills the live bot never places (9 EXIT orders ever). Strip it → paper −$265. Live runs `settle` mode deliberately (scalp was tested live and lost −$7.87/70min, `config.py:103-105`). |
| **Any sub-population edge (edge archaeology)** | ❌ null | 43 one-D slices + 35 two-way interactions; **nothing survives BH-FDR**; permutation family-wise p=0.79 for the best positive cell. Every positive signal **sign-flips across live/paper**, including the model's own highest-conviction bucket (edge≥6.5%: live +$0.69 / paper −$0.58). |
| **"Night-hour (02–04 UTC) loss" defensive gate** | ❌ mirage | 87% of it is **one date** (06-12, −$49.74, *paper*, pre-live); another night was +$11.45. Night is observationally **identical** to day (spread 0.010 vs 0.011, depth 394 vs 371, σ 3.5 vs 3.2e-5, 0% stale). It was ~36 correlated wrong-side bets in one directional move — **event risk, invisible ex-ante**, not a clock or microstructure regime. |

## Method & verification

- Honest population for PnL/calibration = **hold-to-settle** trades (`exit_reason='WINDOW_ROLL'`,
  realized PnL is real; no fill assumption), live and paper kept **separate** and required to **agree**
  (different periods) before any claim. FDR + permutation nulls for multiple testing.
- Every consequential claim was **independently re-derived by adversarial sub-agents** (fresh code,
  different RNG): all headline numbers reproduced; two of my interpretations were corrected (below).
- Snapshot: `data/btc_5m_binary_fair_value.db` as of 2026-06-22 03:25 UTC.

## Corrections the verification forced (recorded for honesty)

1. **"Live already avoids night hours (05–12 gate)" — false.** No hour-of-day gate exists in code;
   28 live trades ran 00–04 UTC.
2. **"Sub-0.50 entry bucket is the PnL leak" — paper-dominated.** In live alone (n=5) it did not lose;
   that gate is noise calibrated on paper.

## Risk-management thread (the June-12 −$49.74) — resolved, no action needed

The −$49.74 paper bleed predates the loss-halt: `RiskGate.loss_halt_breached()`
(`btc_5m_fv/execution/gate.py:227`) was added **2026-06-16** (#76), four days after. It trips when a
**rolling-UTC-day realized loss ≤ −$10**, per-mode. Because the bot holds **one position at a time**
(losses realized every ~5 min), the realized-only basis adds only ~1 trade of lag — a repeat would now
halt at ~$10–13, and `btc_bot/adaptive.py` already layers an adaptive complement. **Live is bounded.**
Optional low-value refinement (not recommended now): make the halt count open exposure / add a
consecutive-loss breaker — marginal, since one-position-at-a-time already caps open risk to ~$2.65.

## Implication for active work — `down_skeptic_drift_v6` (#100)

v6 is **not** the drift mechanism falsified here: it doesn't touch fair value; it uses `drift/σ` regime
to flex the **edge toll** (a selection gate that reduces to v4 at regime=0). This investigation gives v6
a **sharp risk to watch in its shadow validation**, not a veto:

- The `drift/σ` regime signal it keys on **did not predict terminal direction beyond the cushion** in the
  full-sample test (corr≈0, sign-flips across periods). So if v6's benefit is meant to come from *directional*
  alpha, it is at risk of being fit to the single June bearish episode that motivated it (the last 15 live
  trades sit in exactly the small-sample/one-regime data where no robust effect survived).
- **However**, v6 may still help for a *different, valid* reason: it reduces v4's structural **one-sided Up
  concentration**, which is what the 06-12 and recent-15 bleeds actually punished. That is an **exposure /
  risk-balancing** argument, not an alpha argument.
- **So-what for v6's shadow gate:** judge it on whether it cuts the **one-sided drawdown / concentration**
  in a bearish regime — not on a claimed directional edge, which this evidence says is unlikely to be real.
  Keep v4 as the byte-for-byte control and require an out-of-sample (post-June) bearish window before flipping.

## What NOT to do

No multi-book recorder, no 15m/1h connectors, no drift-in-fair-value term, no recalibration layer, no
night-hour gate. The strategy is at the *is-there-any-edge* stage, not the *amplify-the-edge* stage; the
only forward paths are (a) accumulate far more live data to detect/rule-out a sub-$0.40/trade edge, or
(b) source a structurally different signal **outside this dataset**.

## Power caveat

At the aggregate level this had real power — live rules out any uniform edge **≥ ~$0.40/trade** (and found
none). At the slice level it is underpowered (MDE $1.2–2.1/trade): a tiny hidden edge can't be excluded, but
at $3/trade sizing it would be economically negligible anyway.
