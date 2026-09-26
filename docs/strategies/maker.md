# Maker — rest on the favourite

<!-- BEGIN GENERATED:strategy -->
| | |
|---|---|
| Name | Maker — rest on the favourite |
| Key | `maker` |
| Status | running now |
| Switch | `maker` on the MY STRATEGIES card |
| Code | `polymarket_bot/maker/` — 5 files |
| Code fingerprint | `cef40afe6bd9` |
<!-- END GENERATED:strategy -->

## At a glance

### Concept

Rest one passive bid on the favourite of each crypto Up/Down market and hold it to resolution. A resting order pays no fee, and on this venue the fee is what cancels the taker-side edge.

### Main assumption

Resting fills on the favourite between 0.55 and 0.92 earn about +3.8c a share at resolution, and a bid placed now fills the way those measured fills did. The live paper ledger is the first out-of-sample test of both halves.

### The maths

A filled quote at price $p$ for $n$ shares settles with no fee:

$$\text{pnl}=n\,\big(\mathbb{1}[\text{won}]-p\big)$$

It fills only once the taker flow that sold our outcome at or below our bid $b$ has cleared the queue $D$ already resting there. A taker buying the other outcome at $q$ is selling ours at $1-q$:

$$C=\sum_{\substack{\text{taker sells of ours}\\ p_i\le b}} s_i+\sum_{\substack{\text{taker buys of the other}\\ 1-p_i\le b}} s_i$$

$$\text{filled}=\min(Q,\ C-D)\quad\text{when } C>D$$

With about 48c of per-trade spread, seeing a 4c edge at two standard errors takes

$$n\approx\Big(\frac{2\times 48}{4}\Big)^2\approx 580\ \text{settled quotes.}$$

### How it works

Every 45 seconds, on the BTC/ETH/XRP hourly and BTC/ETH/SOL/XRP daily Up/Down markets:

1. **Settle** each filled quote once the CLOB marks its market closed with a winner.
2. **Check fills** against taker-only trades since the quote went in.
3. **Quote**, only while the `maker` switch is on. The favourite is the side with the higher mid. Skip it if its spread is over 6c. Bid one tick above the best bid, never at or through the ask, and only if that price is in $[0.55,\,0.92)$. 25 shares, one quote per market, ever.

### How it was derived

- **Taker ruled out, 2026-09-20.** Taker entries across the hourly tape had a real gross edge of +0.21c a share against a 1.13c fee: −0.91c net (`5d69d0c`). That left resting quotes as the one setup not yet tested. Proposed by Claude.
- **Measured on real resting fills.** Every fill missing from the `takerOnly=true` trade feed was labelled passive (`maker_label.py`). `maker_band.py` then measured what those fills earned, held to resolution, over 7,010 resolved 1h and 24h markets. Every favourite bucket from 0.55 to 0.92 was positive (+3.80c a share, t = +13.1) and every bucket below 0.55 lost. The band edges were read off that table.
- **One clip per market.** Weighted by volume the same band loses 0.61c a share, so the edge only shows at a fixed size. The loop quotes a fixed clip once per market and never takes a second bite (`19dad91`).

### References

- PR #264 — the band table, the 48c per-trade figure and the ~580-quote estimate
- `tools/wallet_research/maker_band.py`, `maker_label.py`, `filter_test.py` and `README.md` — deleted in #273, still in git history
- Commits `1179f11`, `19dad91`, `5d69d0c`
- Code: `polymarket_bot/maker/quoter.py`, `filler.py`, `runner.py`; the queue rule is pinned by `tests/unit/test_maker_fill_model.py`

## What it does

A paper-only loop. It looks at the open BTC, ETH and XRP hourly markets and the BTC, ETH, SOL and XRP daily Up/Down markets every 45 seconds. For each market it has never quoted, it rests one 25-share bid on the favourite, one tick above the best bid, if that price is between 0.55 and 0.92 and the spread is 6c or less. It never crosses the spread, so it pays no fee. A quote counts as filled only after taker volume has traded through the queue that was already resting ahead of it, and filled shares are held to resolution.

## How it was formed

- **2026-08-14 — an earlier resting-order tester.** Zayan (operator) asked whether the account [@mayormamdani](https://polymarket.com/profile/0xf8af03f1e68ee7162db8983f0d6dd0dc869854c6) could be copy-traded. The investigation (#182) found the account was largely a market maker: 40.3% of its positions paid no fee. It built `pairarb`, a shadow tester for two-sided resting bids on 5m markets, with a back-of-queue fill model (depth ahead, then volume through it). The tester never recorded a completed pair: on 2026-08-17 its database held 25 windows and 0 executions (`tasks/todo.md`). The code was deleted on 2026-09-21 (`e7eda6f`, #267). The maker's queue rule works the same way.
- **2026-09-20 — the taker side is ruled out.** `tools/wallet_research/filter_test.py` measured taker entries across the hourly tape. They had a real gross edge of +0.213c/share (t=+3.04, 336,582 entries), but the fee on them was 1.127c, so the net was −0.914c (`5d69d0c`). That commit names resting quotes as the only setup not yet ruled out, and asks for a live test of them. This is the first written proposal for this strategy. Proposed by: Claude, 2026-09-20 (`5d69d0c` came from a Claude session).
- **2026-09-20 — a wrong simulation, withdrawn the same day.** `maker_test.py` simulated one bid resting under the market for a whole window. It reported about −3.5c/share at every offset from 1c to 15c (`0ef2527`). That commit read the result as confirming Zayan's own earlier figure of about −3.8c/share for resting bids. Later that day `1179f11` withdrew it: the simulation measured an order that can only fill when the price moves away from it, which says nothing about resting in general. The −3.8c figure was also corrected. [Zayan's own account](https://polymarket.com/profile/0xc1daaec036a8a49e4a71cad2daa51dcb19bb00c5) turned out to be 567 taker fills out of 644 (88%), so −3.8c is a taker-side number, not a maker one.
- **2026-09-20 — measured directly, then built.** `scan.py` had pulled trades with `takerOnly=false`, which returns both sides of every match. `maker_label.py` re-pulled each market with `takerOnly=true` and labelled every fill missing from that feed as passive. `maker_band.py` then measured what those real resting fills earned, held to resolution, over 7,010 resolved 1h and 24h markets. Fills on the favourite at 0.55–0.92 were positive in every price bucket, and fills below 0.55 lost money (see Evidence so far). The same commit, `1179f11`, added `polymarket_bot/maker/`.
- **2026-09-20 — one clip per market.** The first loop quoted a market again as soon as its first quote filled, so busy markets collected more clips. `19dad91` blocks a market after any quote in any state. The reason: measured volume-weighted, the same band loses 0.61c/share. Both commits merged in PR #264 (`19c21fc`, 2026-09-21).
- **2026-09-21 — the switch.** Until then a settings knob gated the whole pass, settlement included. `7dae8ee` made `maker` a registered strategy in `polymarket_bot/strategies.py`. Turning it off now stops new quotes only. Fill checks and settlement keep running.

## How it works

`polymarket_bot/maker/runner.py:run_forever` runs as a task inside the dashboard app (`polymarket_exec/ops/dashboard/app.py`, lifespan), so it runs only while that process is up. Each pass is `runner.pass_once` and does three things in this order.

**1. Settle** — `filler.settle_due`. For every filled quote, it reads the CLOB market record `/markets/{condition_id}`. It uses the CLOB and not Gamma because Gamma drops short-dated markets after they end and leaves hourly ones at `closed: false`. Once the market is `closed` and one token carries `winner`, the quote settles with no fee:

$$\text{pnl}=n\,\big(\mathbb{1}[\text{won}]-p\big)$$

Here $n$ is the filled shares and $p$ is the quote price.

**2. Check fills** — `filler.check_fills`. For each resting quote, it pulls taker-only trades on that market since the quote was placed (Data API `/trades?takerOnly=true`, newest first, up to 5,500 rows). Taker-only matters: the full feed has both sides of each match, so it would count every trade twice. The two outcomes share one book, so a taker buying the other outcome at $q$ is a taker selling ours at $1-q$. `filler.crossed_volume` adds up everything that sold our outcome at or below our bid $b$:

$$C=\sum_{\substack{\text{taker sells of our outcome}\\ p_i\le b}} s_i\;+\sum_{\substack{\text{taker buys of the other outcome}\\ 1-p_i\le b}} s_i$$

Our share of that flow is $C-D$, where $D$ is `depth_ahead`: the queue that was already resting when we quoted. If $C-D>0$, the quote fills at $\min(Q,\,C-D)$, where $Q$ is the 25-share quote size. The state moves to `filled` and the size is final, even if more flow arrives later. Otherwise $C$ is written to the row (`ledger.note_crossed`), so "flow went past us" stays apart from "nothing came". The quote should be marked `expired` once its market ends, but see Known weaknesses.

**3. Quote** — only when the `maker` switch is on. For each open market with at least 120 seconds left that has never been quoted (`ledger.quoted_markets`), it fetches both books and calls `quoter.decide`:

- The favourite is the outcome with the higher mid. If either book is missing, the market is skipped.
- It skips the market if the favourite's spread (ask − bid) is over 6c.
- It sets price $=b+0.01$ when `maker_improve_tick` is on (the default), otherwise $b$. If that price would reach the ask, it falls back to $b$, so it never crosses.
- It requires $\text{price}\in[0.55,\,0.92)$.
- $D$ is the bid size at the quote price or better when the quote joins the best bid. It is 0 when the quote improves by a tick, because it is then alone at the front.

The quote is written to `maker_quotes` with the book it went into (bid, ask, mid, spread, $D$). Every market the loop looks at, quoted or not, gets a row in `maker_decisions` with its reason. The loop checks each open market on every pass, so a market is quoted on the first pass where all the conditions hold. That puts quotes near the bottom of the band.

**Reporting.** `ledger.summary` gives the fill rate over all quotes placed, and cents per share as $100\cdot\sum\text{pnl}/\sum n$ over settled quotes. `ledger.by_band` groups settled quotes by fill price. `ledger.queue_report` counts unfilled quotes that flow reached apart from those it never reached. The MAKER card shows all three.

## Parameters

Operator knobs in `polymarket_bot/runtime_knobs.py` (group "Maker"). On 2026-09-21 the live `config` table had no override for any of them, so these defaults are what runs.

| Knob | Default | Where it came from |
|---|---|---|
| `maker_band_lo` | 0.55 | `maker_band.py`: maker fills lose below 0.55 (−1.1c at 0.45–0.55) and gain above it (+6.1c at 0.55–0.65). |
| `maker_band_hi` | 0.92 (exclusive) | `maker_band.py`: the edge falls to +0.9c/share at 0.92–0.97. The runtime_knobs comment says the band is a knob because it is the thing being tested. |
| `maker_size` | 25 shares | Fixed clip, from the README's sizing rule: "Quote a fixed clip in every market". Why 25 in particular is not recorded. |
| `maker_improve_tick` | on | runtime_knobs comment: improving costs 1c of the edge and buys the front of the queue. Joining keeps the cent and waits in line. Both record their queue depth so the ledger can compare them. Why "on" is the default is not recorded. |
| `maker_max_spread_cents` | 6 | Not recorded. |
| `maker_min_seconds_left` | 120 s | runner.py comment: a fill in the last seconds is priced off a stale book, not the measured edge. Why 120 in particular is not recorded. |
| `maker_poll_interval_seconds` | 45 s | Not recorded. |
| `runner.SERIES` | 1h BTC/ETH/XRP; 24h BTC/ETH/SOL/XRP | The markets the band was measured on, except the BNB and HYPE dailies. `scan.py` covered those too, and why they are left out is not recorded. |

## Evidence so far

**The research (measured 2026-09-20).** `maker_band.py` over 7,010 resolved 1h and 24h crypto Up/Down markets. It uses real maker fills, held to resolution, fee-free, with every sell normalised to a buy of the other outcome. Each figure is equal-weighted across markets, with t clustered by market (`tools/wallet_research/README.md`, PR #264):

| Maker fill price | c/share | t |
|---|---|---|
| 0.25–0.35 | −7.6 | −20.1 |
| 0.35–0.45 | −6.9 | −18.5 |
| 0.45–0.55 | −1.1 | −3.8 |
| 0.55–0.65 | +6.1 | +14.5 |
| 0.65–0.75 | +6.3 | +14.8 |
| 0.75–0.85 | +4.7 | +12.2 |
| 0.85–0.92 | +2.4 | +7.0 |
| 0.92–0.97 | +0.9 | +3.2 |

Across the whole 0.55–0.92 band, the fixed-clip (equal-weighted) figure is +3.80c/share: t=+13.1, 68% of markets positive, median market +12.6c. The volume-weighted figure for the same band is −0.61c/share. The worst 1% of markets cost $1.27M, from a few very large positions on favourites that lost. Fixed-clip figures by month:

| Month | Volume-weighted c/share | Fixed-clip c/share | t | Markets positive |
|---|---|---|---|---|
| 2026-06 | −7.30 | +1.95 | +2.1 | 65% |
| 2026-07 | +0.57 | +4.09 | +8.6 | 68% |
| 2026-08 | −0.01 | +3.87 | +7.8 | 67% |
| 2026-09 | +1.19 | +4.18 | +6.3 | 70% |

This measures what filled orders earned. It does not measure whether our order would have filled.

**The live paper record (read from `maker_quotes` on 2026-09-21 at 07:12 UTC).** There were 34 quotes placed between 2026-09-20 04:40 and 2026-09-21 07:07 UTC, all for 25 shares. Twenty-one filled (62%), and 11 of those 21 were partial fills of 2.7 to 20 shares. Eighteen have settled: 10 won, for a total of −$40.79 on 305.5 shares ($203.44 staked). Three are filled and waiting for resolution. Thirteen are `resting`, but 11 of those are in markets that ended between 05:00 and 20:00 UTC on 2026-09-20. None have expired.

| Fill price | Settled | Won | P&L |
|---|---|---|---|
| 0.55–0.65 | 8 | 3 | −$29.38 |
| 0.65–0.75 | 5 | 2 | −$21.27 |
| 0.75–0.85 | 3 | 3 | +$6.90 |
| 0.85–0.92 | 2 | 2 | +$2.95 |

On the settled quotes, that is −13.35c per share. The inventory line (30 quotes, 16 settled, −$36.44) is an earlier read of the same table. The loop logged 21,260 skips, and 19,912 of them say "favourite bid 0.50 outside the band".

PR #264 puts the per-trade standard deviation at about 48c. To see a 4c edge at two standard errors needs $n\approx(2\times48/4)^2\approx580$ settled quotes. At 18, the live record cannot yet confirm or rule out the research figure.

## Known weaknesses

- **Quotes in ended markets never leave `resting`.** `filler.check_fills` only handles quotes whose token is in this pass's `quoter.live_markets` list. That list drops every market whose end has passed. So once a market ends, its quote is never checked again: it cannot fill, the `expired` branch is never reached, and it stays `resting` for good. On 2026-09-21, 11 quotes were in this state. The same gap means flow between the last pass and the market's end is never counted.
- **Partial fills make the clip uneven.** A quote fills once, at $\min(25,\,C-D)$, and is never topped up. Filled sizes so far run from 2.7 to 25 shares. `ledger.summary` weights cents per share by filled shares, but the research figure is an equal-weighted mean per market. The two are not the same statistic.
- **Quote timing differs from the research.** The research counted every maker fill in a price bucket, whenever it happened. The loop quotes on the first pass where the favourite's bid plus a tick reaches 0.55, so its fills bunch at the bottom of the band. Of 18 settled quotes, 8 are in 0.55–0.65, the bucket that is weakest in the live record so far.
- **The band edges came from the same data.** 0.55 and 0.92 were read off the table they are judged by. The per-month split is the only stability check. The live ledger is the first out-of-sample test.
- **The fill model is conservative in two ways.** It never credits cancellations in the queue ahead of us. It also reads at most 5,500 taker trades, so on a very busy market it misses the oldest flow, which is the flow closest to when the quote was placed.
- **Scope.** The band was measured on 1h and 24h markets only. When PR #264 was written, only 996 of 11,520 15m markets were labelled. The maker does not quote 15m markets.
- **Code comments disagree on the sample.** `quoter.py` says 2,600 markets and "+2 to +6.6c/share". `runtime_knobs.py` says 7,000. The README and PR #264 say 7,010, which is the figure used here.
- **The research can no longer be re-run from local data.** On 2026-09-21, `data/wallet_research/wallets.db` held 392 markets (2026-08-22 to 2026-09-20), not 7,010. The scripts that measured it were deleted in #273 and are still in git history.
- **No supervisor.** The loop lives inside the dashboard process, so when that process stops, quoting, fill checks and settlement all stop with it.

## Sources

- `1179f11` 2026-09-20 — feat(maker): measure the passive side properly, and quote it
- `19dad91` 2026-09-20 — fix(maker): one fixed clip per market, never a second bite
- `5d69d0c` 2026-09-20 — test(wallet-research): the fee, not the edge, is what is missing
- `0ef2527` 2026-09-20 — test(wallet-research): the maker side loses too — both sides are taxed
- `1f05cb2` 2026-09-20 — docs(wallet-research): the sizing rule the full history forces
- `490f013` 2026-09-20 — feat(copytrade): log every decision, and re-price fills as a real order would land (Zayan's 644 orders)
- `7dae8ee` 2026-09-21 — feat(dashboard): split the strategy card, and show the whole tree in it
- `e7eda6f` 2026-09-21 — chore: retire the RPC fast-feed path — copytrade watcher replaced it
- `19c21fc` 2026-09-21 — Merge pull request #264 from zayansalman/feat/maker
- PR #264 — Maker strategy: measure the passive side properly, and quote it (band table, the 48c per-trade figure and the ~580-trade estimate, 15m labelling gap)
- PR #267 — the dead-code deletion that removed `pairarb`
- Issue #182 — two-sided maker quoting on 5m Up/Down (shadow only); the account's 40.3% fee-free positions; the back-of-queue fill model
- `tasks/lessons.md` — the 2026-08-14 entry on the copy-trade question
- `tasks/todo.md` — the 2026-08-17 `pairarb_shadow.db` state
- `tools/wallet_research/README.md` — band table, sizing rule, monthly split, data sizes
- `tools/wallet_research/scan.py`, `maker_label.py`, `maker_edge.py`, `maker_band.py`, `maker_test.py`, `filter_test.py`
- `polymarket_bot/maker/quoter.py`, `filler.py`, `ledger.py`, `runner.py`
- `polymarket_bot/runtime_knobs.py` — maker knobs and their comments
- `polymarket_bot/strategies.py` — the `maker` switch
- `polymarket_bot/pairarb/mirror.py` — maker fills are fee-free
- `polymarket_exec/ops/dashboard/app.py` — where the loop is started
- `tests/unit/test_maker_fill_model.py` — pins the queue rule
- `data/btc_5m_binary_fair_value.db`, tables `maker_quotes`, `maker_decisions` and `config`, read-only on 2026-09-21 at 07:12 UTC
- `data/wallet_research/wallets.db`, row counts read on 2026-09-21
- Zayan's own account, [0xc1daaec036a8a49e4a71cad2daa51dcb19bb00c5](https://polymarket.com/profile/0xc1daaec036a8a49e4a71cad2daa51dcb19bb00c5), which is the source of the 567-of-644 taker split

## Changelog

- 2026-09-26 · `cef40afe6bd9` · Wording only: the book reader's local helper that sorts price levels is named for price levels (standard terms). No behaviour change.
- 2026-09-21 · `1b7011906a67` · Added an At a glance summary (concept, main assumption, maths, how it works, how it was derived, references) for the dashboard's STRATEGY card.
- 2026-09-21 · `1b7011906a67` · Docstring-only change in ledger.py from the copy-trade removal (#272); behaviour unchanged.
- 2026-09-21 · `ed4856e27df9` · Doc created.
