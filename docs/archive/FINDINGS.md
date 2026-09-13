# Findings — what 30 days of venue-true measurement established

> **Historical record, June–July 2026 research phase — superseded by the
> 2026-08-04 reopen, kept for reference.** These findings are about the
> unfiltered BTC-only signal family tested through 2026-07-10; they are not a
> current verdict on the project or on later work (#182 pairarb/copytrade).
> See `tasks/todo.md` for current status.

Every number below is net of the Polymarket taker fee (`0.07·p·(1−p)`/share) and
reproducible from the shipped ledger (`tools/race_status.py`, `tools/replay_race.py`,
`tools/regime_attribution.py`).

## 1. There is no directional edge at retail latency

- **The unfiltered control (v0)** — the raw pricing-model signal with standard entry filters —
  finished at ≈ **$0 over 400+ settled shadow trades** (drifting slightly negative at
  close). Five-minute BTC direction is priced correctly to within the fee.
- **Live confirms paper.** Real-money lifetime: **−$19.35 across 351 fills**; June-era
  decomposition: gross **+$6.27** vs taker fees **−$23.51**. Realized win rate 55.1% vs
  a fee-adjusted breakeven of 55.7% at the average entry price — a coin flip paying a
  2.6%-of-turnover rake.
- **Fill fidelity was not the problem.** A 16-fill live probe matched the shadow book on
  every window with small *positive* entry slippage; the paper/shadow books are a faithful
  simulator at 5-share size. The loss is the rake, not the execution.

## 2. Every filtered "edge" died out-of-sample — including the best one

Seven gate variants were raced (freshness windows, spot-vs-strike cushions, claimed-edge
caps, and combinations). The pattern, three times in a row with three different leaders:

| Leader | Looked like | Ended at |
|---|---|---|
| v7 (fresh+cushion+cap) | +$0.25/trade at n=47 | ≈$0 by n=89; Up leg negative |
| v8 (fresh only) | +$0.21/trade at n=178 | +$0.09 by n=224; PnL was one leg (Up) and one day |
| **f45 (fresh≤45s)** | **replay OOS CI [+0.275, +0.887], n=198** | **post-freeze segment −$0.41/trade, WR 48.6% (n=37); live book −$3.79/n12, replay-consistent 12/12** |

The f45 collapse is the cleanest measurement in the project: its spec was frozen in
PR #152 on 07-08, so every tick recorded afterward is untouched by any selection decision
— and that segment is negative *for the entire fresh family* (v7 −$17.27/n50,
v8 −$6.95/n118). Whatever fresh-window inefficiency existed in June was decayed, harvested,
or an artifact of having mined June. All three readings command the same action.

## 3. Strategy-switching is systematically worse than holding

Simulated on the race's own daily PnL: "trade yesterday's leader" earned **+$6.10** and
"trade the cumulative leader" **+$3.36**, vs **+$16.60** for just holding one (itself
noise). The mechanism is visible in the trace: the switcher buys the day *after* the big
day and eats the give-back (−$15.99 after +$19.44; −$16.67 after +$20.56). The operator's
three real switches that month (to v6, v7-live, v8) all followed hot streaks and all ended
in halts. With per-trade σ ≈ $2.45 against candidate edges ≤ $0.35, any switching window
short enough to be responsive has ~zero statistical power — it follows noise by
construction.

## 4. Regimes hide nothing (tested twice, four axes)

A-priori bands (time-of-day, edge, volatility, basis), side-attributed cells, a two-sided
edge requirement (both the Up and Down bets inside a regime must be positive), one-vs-rest
permutation tests, and Benjamini–Hochberg FDR: **0/12 regime×model cells** and **0/75
slices** survive. Two "obvious" effects failed replication outright (a night-hours bleed,
p=0.51 on fresh data; a mid-vol sweet spot, permutation p=0.21–0.65). Regime *awareness*
(logging vol/basis at decision time) is built in; regime *switching* is falsified.

## 5. Who actually earns on 5-minute markets

The venue's own fee schedule explains the ecosystem: crypto takers pay the **highest** fee
(0.07), makers pay **zero** and receive **20% of taker fees as rebates** plus a
**>$5M/month liquidity-rewards pool**. The books are 1¢ tight and 250–350 shares deep,
24/7, because professionals are *paid* to stand there. Measured from the other side, their
adverse selection is brutal: our resting-quote backtest filled **98% of losers and 75% of
winners** — so naive maker-mode is not an escape (that's selection rent, not a fee
problem). 23% of our own taker entries incidentally rested and paid no fee (#137), which
only sharpens the accounting. The profitable actors — subsidized MMs, sub-100ms
last-second snipers, near-resolution scalpers — are **execution businesses. Nobody in this
market wins by predicting.**

## 6. Capacity was the ceiling all along

Even a fully-real +$0.35/trade edge at the book's depth supports ~$2–4/day at singleton
$3 positions — a spectacular *percentage* return on ~$100 employed, and simultaneously an
absolute prize too small for any professional to defend against. That asymmetry is exactly
why such pockets can exist for retail — and why the burden of proof must be carried by
selection-free data before a dollar moves. Here, it wasn't.

## 7. What transfers

The falsification machinery is market-agnostic: fee-true single-source accounting,
pre-registration with frozen thresholds, subset-structured ablations, FDR-gated slicing,
a self-validating replayer, kill criteria written before the data. The pre-registered
successor experiment — a forecasting-skill pilot on *slow*, fee-free/low-fee markets where
judgment rather than latency is the edge dimension — ships as `tools/forecast_journal.py`
(#162) with its success bar already fixed: positive Brier skill vs the market **and**
simulated PnL CI > 0 over ≥30 resolutions, or it doesn't get funded.
