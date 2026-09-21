# Wallet-research programme

<!-- BEGIN GENERATED:strategy -->
| | |
|---|---|
| Name | Wallet-research programme |
| Key | `wallet_research` |
| Status | offline only |
| Switch | none — nothing to turn on |
| Code | `tools/wallet_research/` — 16 files |
| Code fingerprint | `d15893ee9d3b` |
<!-- END GENERATED:strategy -->

## What it does

Offline scripts that read public Polymarket data and trade nothing. They pull every fill on resolved crypto Up/Down markets and label each one maker or taker. Then they measure who earned what, held to resolution, after the taker fee. The results feed both running strategies: the favourite band the maker quotes, and the wallet the copier follows. They also produced the finding that ranking wallets by past edge does not predict their future results.

## How it was formed

- **The question behind it.** Zayan (operator), 2026-08-14: could the account [@mayormamdani](https://polymarket.com/profile/0xf8af03f1e68ee7162db8983f0d6dd0dc869854c6) be copy-traded? (#182, `tasks/lessons.md`). This programme grew out of that question. Who proposed screening the whole market universe in September is not recorded.
- **2026-09-20 — first screen** (`d83e5c3`). `scan.py`, `analyze.py`, `validate.py`, `rank.py` and `maker_vs_taker.py` landed. Ranking by profit surfaced market makers, whose profit is the spread a copier pays. All six of the highest-profit wallets failed verification. Ranking by net edge per share after the fee instead surfaced wallets that buy near $1, where the fee drops from 1.75c/share to 0.03c.
- **2026-09-20 — drift.** Once the copier went live, 7 of the 9 screened wallets had left Up/Down markets for sports, weather and strike markets (`68e2a38`).
- **2026-09-20 — taker-only screening.** `taker_screen.py` labels each fill maker or taker first, then ranks wallets on their taker fills only (`9d58a39`).
- **2026-09-20 — the screen falsified.** `holdout_test.py` found screened wallets were no more likely to profit in the following period than any other active wallet (`a59e384`).
- **2026-09-20 — the fee.** `filter_test.py` found taker entries have a real gross edge that is about five times smaller than the fee (`5d69d0c`).
- **2026-09-20 — the maker side, wrong then right.** `maker_test.py` simulated a bid resting under the market and reported about −3.5c/share (`0ef2527`). `1179f11` withdrew that the same day, because it measured a stale one-sided quote, not resting. `maker_label.py`, `maker_edge.py`, `maker_band.py` and `maker_holdout.py` then measured real maker fills directly, and `1f05cb2` wrote the sizing rule into the README.
- **2026-09-20 — strike markets.** `strike_scan.py` reported a +1.31c/share favourite bias (`2d449a9`). `d59b3b3` corrected it the same day: the sample was 8 days, not five months.
- **2026-09-20 — one copy target.** `recent_wallets.py` and `inspect_wallet.py` picked the single 15m wallet the copier now follows (`0c80179`).
- **2026-09-21.** Lint fixes only (`a405de3`). All of this merged in PR #264. At some point before 2026-09-21 the local databases were cut down (see Known weaknesses). Nothing in git records when or why.

## How it works

**1. Collect** — `scan.py`. It lists resolved markets series by series from Gamma, then pulls every trade on each one from the Data API with `takerOnly=false`, which returns both sides of every match. It writes `markets` and `trades` tables to SQLite. The `UNIVERSES` are:

- `crypto`: 1h BTC/ETH/XRP and 24h BTC/ETH/SOL/XRP/BNB/HYPE, into `wallets.db`.
- `crypto15m`: BTC/ETH/SOL/XRP 15m, into `m15.db`.
- `macro`: gold, silver, oil and SPY dailies, into `macro.db`.

**2. Label** — `maker_label.py`. It re-pulls each market with `takerOnly=true`, which returns only the aggressive side. A fill that is in the full feed but missing from the taker feed was a resting order. Labels are cached in `maker.db` and `maker15.db`.

**3. Score a fill** — `maker_edge.py`. Held to resolution, each fill's result is exact and adds up, so no position tracking is needed. Here $s$ is the size, $p$ the price and $w\in\{0,1\}$ whether that outcome won:

$$\text{BUY: } s\,(w-p),\qquad \text{SELL: } s\,(p-w),\qquad \text{taker fee: } 0.07\,s\,p\,(1-p)$$

Makers pay no fee. Makers and takers are opposite sides of the same trades, so their gross results should cancel. The script prints the gap, which was 0.1%.

**4. Calibrate by price** — `maker_band.py`. Each maker fill is turned into a buy: selling outcome $o$ at $p$ is the same as buying the other outcome at $1-p$. Fills are bucketed by price, and a fill's edge is $w-p$. The market is the unit of observation, because every fill in a market settles on one outcome. With $M$ markets and $e_m$ the share-weighted edge in market $m$:

$$e_m=\frac{\sum_{i\in m}s_i(w_i-p_i)}{\sum_{i\in m}s_i},\qquad \bar e=\frac1M\sum_m e_m,\qquad t=\frac{\bar e}{\operatorname{sd}(e_m)/\sqrt M}$$

It also prints the volume-weighted figure, a split by month, results with per-market size caps, and how much of the profit comes from the top few markets.

**5. Rank wallets.**

- `analyze.py` rebuilds each wallet's profit from its fills. It is kept only for comparison.
- `rank.py` ranks by net edge per share after the fee. To count, a wallet needs at least 30 markets and must hold at least 80% of what it buys to resolution. Its 10th-percentile time left to settlement at entry must be 2 minutes or more, and it must have traded in the last 14 days. Wallets that bought both outcomes in more than 25% of their markets are dropped as quoting, not predicting. The survivors are corrected with Benjamini–Hochberg at q=0.10, and each wallet's edge must survive removing its 3 best markets.
- `taker_screen.py` has two passes. Pass 1 is local: one-sided, holds to resolution, active in the last 7 days, stakes $2,000 a market or less, and profitable after the fee. Pass 2 samples up to 25 markets per survivor against the taker-only feed. A wallet is kept only if at least 60% of its notional is taker and its taker fills are profitable.
- `recent_wallets.py` looks at the last 3 days of 15m markets. It needs at least 20 markets, at least 500 shares, and a trade in the last 18 hours. A wallet is a candidate if more than 60% of its shares were taker fills and its taker result is positive.
- `inspect_wallet.py` checks one candidate by hand: day by day, the maker/taker split, whether it ever sells before settlement, its entry prices, and whether a few markets carry the result. `validate.py` checks a wallet's rebuilt profit against Polymarket's own numbers.

**6. Test the rules.**

- `holdout_test.py` screens wallets on one window and measures them on the next, against every other active wallet. It uses a two-sample t on the per-share edge.
- `maker_holdout.py` asks the same question of maker edge, month to month.
- `filter_test.py` tests the 3c slippage rule across every wallet's entries.
- `maker_test.py` is the simulation of a resting bid, now withdrawn.
- `strike_scan.py` measures calibration on crypto strike markets ("will X be above a given price on a date").

## Evidence so far

All of these were measured on 2026-09-20.

- **Maker against taker.** Over 7,010 resolved 1h and 24h markets (7.2M fills), taker fills made −0.05c/share before the fee, and the fee was about 0.97c. Net, crossing the spread cost about 1.0c/share. Maker fills were roughly flat overall (README, PR #264).
- **Maker fills by price.** Positive at every bucket from 0.55 to 0.92, from +2.4 to +6.3c/share. Negative below 0.55, down to −7.6c at 0.25–0.35 (t=−20.1). See the maker doc for the full table.
- **The 0.55–0.92 band.** Equal-weighted across markets, it made +3.80c/share (t=+13.1, 68% of markets positive). Volume-weighted, it lost 0.61c/share. The fixed-clip figure was positive in all 4 months from June to September, at t of +2.1, +8.6, +7.8 and +6.3 (README).
- **Taker entries after anyone's fill.** Buying at the tape price 20–60 seconds after any wallet's fill made +0.213c/share gross (t=+3.04, n=336,582). The fee was 1.127c, so net was −0.914c. The 3c slippage rule made no difference: entries it accepted made −1.128c and entries it declined −1.112c (t=−0.14, 690,608 entries) (`5d69d0c`).
- **Screen holdout.** Across 4 train/test splits, screened wallets were less likely to profit than other active wallets in 3 of them, and no t was above 1.3. Weighted by size the edge was positive (+1.6 to +8.4c/share), but that came from one or two outliers (`a59e384`).
- **Drift.** 7 of 9 screened wallets had left the markets they were screened on (`68e2a38`).
- **Strike markets.** April: 368 markets, +0.361c/share, t=+0.77. May: 632 markets, +1.397c/share, t=+4.59. Both positive, but significant in one month only (`d59b3b3`).
- **The copy target.** Over the last 3 days of 15m markets (996 markets, 822k fills), [t-d901](https://polymarket.com/profile/0xd9013df863c1ba932780857b020dfdeacedf8e14) made +13.95c/share on its taker fills over 195 markets, with no t computed (`0c80179`, `polymarket_bot/copytrade/targets.py`).
- **Not recorded.** No result from `maker_holdout.py` is written down anywhere in the repo.

## Known weaknesses

- **The local data no longer supports the headline results.** On 2026-09-21, `data/wallet_research/` was 726 MB:
  - `wallets.db` holds 392 markets (2026-08-22 to 2026-09-20), not 7,010.
  - `m15.db` holds 1,136 markets.
  - `maker.db` has labels for 392 markets.

  So the 7,010-market results cannot be re-run from this copy, and nothing records whether the full set exists anywhere else. The inventory line ("8.0 GB", "m15.db alone is 4.6 GB") describes the earlier state.
- **The band edges were chosen on the data that measures them.** The only stability check is the split by month. The maker's live ledger is the first out-of-sample test.
- **Fill rate cannot be seen here.** These tools see only orders that filled. Queue position and orders that never filled are not in the trade tape (README).
- **Stale claims in the code.** The `strike_scan.py` docstring still says makers pay about 3.5c in adverse selection, which `1179f11` withdrew. `maker_band.py` defaults to an upper bound of 0.95, so the published 0.92 figures need `--hi 0.92`. The README lists 10 of the 16 scripts.
- **The fee rate is fixed at 0.07 in each script.** If the venue changes its fee, every net figure changes with it.

## Sources

- `d83e5c3` 2026-09-20 — feat(copytrade): watch a target wallet's fills in the dashboard
- `68e2a38` 2026-09-20 — feat(copytrade): show target drift before P&L
- `9d58a39` 2026-09-20 — feat(copytrade): screen for TAKER wallets, and retarget on them
- `a59e384` 2026-09-20 — test(wallet-research): holdout-test the screen — it does not predict
- `5d69d0c` 2026-09-20 — test(wallet-research): the fee, not the edge, is what is missing
- `0ef2527` 2026-09-20 — test(wallet-research): the maker side loses too — both sides are taxed
- `2d449a9` 2026-09-20 — feat(wallet-research): scan strike markets — the first positive result
- `d59b3b3` 2026-09-20 — fix(wallet-research): the strike finding was eight days, not five months
- `1179f11` 2026-09-20 — feat(maker): measure the passive side properly, and quote it
- `1f05cb2` 2026-09-20 — docs(wallet-research): the sizing rule the full history forces
- `0c80179` 2026-09-20 — feat(copytrade): one 15m target, chosen on three days, every trade on the card
- `a405de3` 2026-09-21 — style(wallet-research): satisfy the lint CI runs
- PR #264 — carried every commit above; the maker/taker figures and the 15m labelling gap
- Issue #182 and `tasks/lessons.md` — the 2026-08-14 copy-trade question about [@mayormamdani](https://polymarket.com/profile/0xf8af03f1e68ee7162db8983f0d6dd0dc869854c6)
- `tools/wallet_research/README.md` — what the measurements found, the sizing rule, the checks, data sizes
- `tools/wallet_research/*.py` — the 16 scripts described above
- `polymarket_bot/copytrade/targets.py` — the target the screen chose
- `data/wallet_research/wallets.db`, `m15.db`, `maker.db` — sizes and row counts read on 2026-09-21

## Changelog

- 2026-09-21 · `d15893ee9d3b` · Doc created.
