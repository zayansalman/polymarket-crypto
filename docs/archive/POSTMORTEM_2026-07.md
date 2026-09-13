# Postmortem — July 2026: why the project lost money, and what is true now

> **Historical record, June–July 2026 research phase — superseded by the
> 2026-08-04 reopen, kept for reference.** The forensic numbers below (bot-era
> venue-true PnL, fee accounting) are still accurate as a record of that
> period; the restart protocol they prescribe has been superseded by the
> reopen. See `tasks/todo.md` for current status.

**Status:** definitive as of the 2026-07-02 forensic pass (frozen ledger snapshot + Polymarket
Data API ground truth). Supersedes all earlier PnL figures. Issues: #132–#138.

## The verdict in three numbers (bot era, 2026-06-15 → 06-24, venue records)

| | USD |
|---|---:|
| Gross PnL before fees (333 buys, $904.86 turnover) | **+$6.27** |
| Taker fees paid (262 taker fills; 71 maker fills free) | **−$23.51** |
| **Net venue-true PnL** | **−$17.24** |

The signal was a coin flip (+0.7% of turnover gross); the taker fee (−2.6% of turnover) was
the entire loss. The bot's own books showed −$8.01 (journal) / −$3.10 (ledger) because
settlement booking ignored fees — fixed in #133, ledger reconciled to −$17.77 (#134;
lifetime BTC −$18.19 including the pre-bot April era).

## How it died operationally

06-24 20:19 UTC: operator flipped the live model to `down_skeptic_drift_v6` (final shadow
standing: worst of six, −$31.45). 20:20: daily loss-halt fired; operator cleared and
restarted. 20:25: one entry (position 1768). Overnight the bot stopped silently (#138).
06-25 05:50: boot reconciliation hit a CLOB-pruned order id, crashed
(`NoneType.get`), hard-refused three boots, and the bot stayed dead (#132 — fixed;
boot now heals resolved-window rows). Irony: position 1768 **won** — auto-redeemed
+$2.41 thirty seconds after the bot died; the ledger never learned until #134.

## Market microstructure (venue-verified, exact)

- Taker fee = `0.07 · p · (1−p)` per share, charged in **USDC on top of price×size**, only
  on the portion of an order that **crosses at placement**. Resting (maker) fills and
  redemptions are fee-free. Winners auto-redeem ~30–40 s after resolution.
- At the bot's typical entry (p ≈ 0.54): fee ≈ 1.74 ¢/share ⇒ breakeven win-rate
  = `p + 0.07·p·(1−p)` ≈ **55.7%**. Live realized 51.4% (n=325).
- The shadow ledger's fee model matches the venue exactly (verified on 1,740 rows) and is
  the correct taker basis; ~21% of real fills were accidentally maker (fee-free) — a small
  real cost lever (#137).

## What the strategy evidence says (shadow race, frozen at ~7 days)

- **No model is distinguishable from zero.** Best: `cushion_favorite_v2` +$42.33, n=249,
  +0.17/trade, 95% CI [−0.14, +0.48], t=1.10. Powering a verdict on it needs ~1,623 trades
  (~39 more days). `pricing_v0` (+0.08/trade) needs ~104 days.
- **In-sample ranking inverted out-of-sample** (cut 06-23): `down_skeptic_v4` IS#1 → OOS#5;
  v0 IS#2 → OOS#4. Only the cushion family stayed sign-positive both sides
  (v2: +0.22 IS / +0.08 OOS; v5: +0.24 / +0.03). Ranking-on-a-week = noise-chasing;
  the 06-24 flip to v6 was exactly that (v6: −0.22/trade, worst in every window it lived).
- **The night 02–04 UTC loss signal is dead**: on the full sample the a-priori replication
  test fails (permutation p = 0.51; pooled night expectancy −0.02 ≈ flat). No time-of-day
  gate is justified.
- **Selectivity is null**: 0/75 gate cells (edge/confidence/price/side per model) survive
  BH-FDR; raising the claimed-edge threshold is non-monotone in-sample and OOS-negative at
  every threshold. There is no gate that turns this signal family profitable.
- **The claimed edge is real but ~3× overstated**: Spearman(claimed edge, realized lift)
  ≈ +0.2 (p<0.01) in every model, but claims of ~5.8 prob-points realize ~1.5–3.5, and the
  *largest* claims realize worst (adverse selection at the touch).
- Live execution was **not** the problem: on 240 shared windows live matched shadow-v0 at
  identical prices once fees are booked on both sides. Live's extra loss vs shadow totals
  came from participation gaps (downtime, $30/day bankroll cap, singleton slot), not fills.

## Pre-registered restart protocol (the only evidence-sane path to "profitable")

1. **Do not resume live trading now.** There is no validated positive-edge configuration;
   live trading today pays ~2.6% of turnover in fees to gamble on a CI that spans zero.
2. Operator actions before any restart: reset the active model away from
   `down_skeptic_drift_v6` (dashboard → active model; `pricing_v0` as neutral baseline
   or `cushion_favorite_v2` as the candidate under test); press Start once so the #132 heal
   closes stale row 1768; re-run `tools/reconcile_live_ledger.py --apply` afterwards.
3. **Shadow-only for ≥6 weeks** (all six models keep logging; fee-true basis; no live
   orders). The race resumes from zero credibility — the 7-day sample is exhausted.
4. **Deployment bar (pre-registered, no peeking-based switches):** a model goes live only
   when its full-race 95% CI on net expectancy excludes zero **and** its OOS half is
   sign-consistent. Expected first read: ~5–6 weeks for v2 if its point estimate holds.
5. If no model clears the bar by ~8 weeks of shadow: the honest conclusion is that this
   signal family cannot beat this market's fee structure — retire live trading and keep
   the EMS as research infrastructure, or hunt structurally different edges (maker-side
   was already falsified, #130; drift/cross-market falsified, #97).

## Accounting bases (so nobody re-derives the wrong number again)

| Basis | Value | Meaning |
|---|---:|---|
| Venue cashflow (bot era) | −$17.24 | buys→sells+redeems, Data API, 06-15→06-24 |
| Ledger after #134 reconcile | −$17.77 | per-window matched, incl. manual punt window |
| Lifetime BTC (incl. April manual era) | −$18.19 | `btc_recon.real_btc_pnl_lifetime` |
| Pre-fix journal / ledger | −$8.01 / −$3.10 | fee-blind — do not quote |
