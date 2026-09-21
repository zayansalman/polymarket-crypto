# Daily altcoin scanner

<!-- BEGIN GENERATED:strategy -->
| | |
|---|---|
| Name | Daily altcoin scanner |
| Key | `daily_altcoin` |
| Status | running now |
| Switch | `daily_altcoin` on the MY STRATEGIES card |
| Code | `polymarket_bot/daily/` — 6 files |
| Code fingerprint | `14f1bd0c38c4` |
<!-- END GENERATED:strategy -->

## At a glance

### Concept

Price each altcoin's daily Up/Down market as a log-normal bet on spot, and buy the side the market underprices the most. The thesis is Zayan's: daily markets on thinner altcoins are priced less efficiently than BTC. Issue #185 records it as a hypothesis to test, not a finding.

### Main assumption

For the rest of the day the coin's log price moves like a random walk with no drift, and its volatility equals the last 30 days of daily returns. Then spot, yesterday's noon-ET close and the time left are enough to price the day. Gamma's best ask is taken as fillable for the full trade size.

### The maths

With daily log returns $r_i=\ln(c_i/c_{i-1})$ over the last 30 closes, the volatility per second is

$$\sigma=\frac{\max\big(\operatorname{stdev}(r),\ 0.00002\big)}{\sqrt{86400}}$$

The chance of Up, from spot $S$, the reference close $K$ and $T$ seconds left, clipped to $[0.005,\,0.995]$:

$$p_{\text{up}}=\Phi\Big(\frac{\ln(S/K)}{\sigma\sqrt{\max(T,1)}}\Big)$$

The edge on each side against its ask:

$$e_{\text{up}}=p_{\text{up}}-a_{\text{up}},\qquad e_{\text{down}}=(1-p_{\text{up}})-a_{\text{down}}$$

It enters when the larger edge is at least 0.045, more than an hour is left, and the ask is between 0.05 and 0.95. With the taker fee $f=0.07\,a(1-a)$, PnL per share is $1-a-f$ on a win and $-a-f$ on a loss.

### How it works

Every 60 seconds, for DOGE, SOL, XRP, BNB and ETH:

1. Settle any position past its noon-ET resolution from the Binance 1-minute close. Up wins above the reference, and an exact tie pays 0.5.
2. Find the day's market by slug on Gamma and read its best bid and ask.
3. Price both sides and keep the one with the larger edge.
4. Of the assets that pass, enter only the one with the largest edge: a flat $10 paper position, at most one per asset per day, held to resolution.

Paper only: there is no live order path.

### How it was derived

- **2026-08-29, Zayan.** Stopped all 5-minute work after the multi-coin copy-trade shadow stayed negative (#182).
- **2026-08-30, Zayan.** Chose the mechanic: daily Up/Down on thinner altcoins, $10 paper positions, one position on the single strongest signal, always running (issue #185).
- **2026-08-30, Claude.** Built it (`5c91228`) by reusing the BTC 5-minute log-normal price with two changes: volatility from daily closes instead of one-second closes, and no tie term, because this family splits an exact tie 50-50. The 0.045 edge floor and the 30-day look-back carried over with no reason recorded. Issue #185 asked for momentum as an input; drift is computed but not used.
- **2026-09-17 to 19.** Fixes for the noon-ET market lookup (#239), late settlement (#245) and the reference close on DST days (#246).

### References

- Issue #185 and `tasks/2026-08-30-local-strategy-rebrand.md` — the thesis and the operator's decisions
- Commit `5c91228` — the build; #239, #245, #246 — the fixes
- Code: `polymarket_bot/daily/signal.py`, `scanner.py`, `market.py`; `polymarket_bot/strategy.py`; `polymarket_bot/fees.py`

## What it does

Every 60 seconds it prices the Polymarket daily Up/Down market for DOGE, SOL, XRP, BNB and ETH from Binance spot, the previous noon-ET close and 30 days of realised volatility. When a side's ask sits at least 0.045 below that price, it records one flat $10 position on the asset with the largest gap and holds it to the noon-ET resolution. It has no live order path: positions exist only in the `daily_shadow_positions` table. As of 2026-09-21 it has settled 18 positions for −$67.08.

## How it was formed

- **2026-08-29, Zayan (operator).** Stopped all 5-minute market work after the multi-coin copy-trade shadow stayed negative (doge ledger −$307.68 over 1,980 fills). The next chapter was to be daily, hourly or longer markets (`27f38fd`, #182).
- **2026-08-30, Zayan (operator).** Chose the mechanic. Daily Up/Down markets on thinner altcoins instead of BTC, with DOGE as his example of a market that is "not efficient". $10 paper positions. One strategy that scans every asset and trades the strongest signal, one position on that single signal rather than one per qualifying asset, and a loop that runs whenever the dashboard is up, with no Start/Stop (`tasks/2026-08-30-local-strategy-rebrand.md`, decisions 3–5 and tick 2; issue #185). Issue #185 records the thesis as a hypothesis to test, not a finding.
- **2026-08-30, Claude.** A Claude Sonnet 5 session built it in one commit (`5c91228`) after Zayan reviewed the plan. It reused the BTC 5-minute log-normal price and executable-edge rule from `polymarket_bot/strategy.py` with two changes: volatility from daily closes instead of 90 seconds of 1-second klines, and no tie term, because this family resolves an exact tie 50-50. Positions settle from Binance directly, because a resolved daily market stops being returned by the Gamma slug lookup. Issue #185 had asked for realised volatility and momentum as inputs, not a carry-forward of the 5-minute cushion-model maths. The build reused the 5-minute log-normal formula with daily volatility, and computes drift without using it. The first entry was ETH Down at 0.25 with a claimed edge of +0.143. It lost.
- **2026-09-13.** Trade size, edge floor, scan interval and look-back moved from `.env` to the dashboard SETTINGS card (`49b092f`).
- **2026-09-17 to 09-19, three fixes.**
  - #239 (`230d1b0`): discovery built the slug from the UTC date, so from noon ET to UTC midnight it asked for the market that had just resolved. The scanner was blind for those hours every day, and the ledger had no entry opened after 14:00Z.
  - #245 (`e499c7d`, then `99a9cdf`): settlement only ran when discovery found markets, so position 257 (DOGE, due 2026-09-15 16:00Z) stayed open a day late. Settlement now runs first on every tick, one row at a time, and logs any row it cannot price.
  - #246 (`be47831`): the reference close was read at `endDate` minus 86,400 seconds, an hour wrong on the two DST switch days. It is now noon ET on the previous calendar day. The next affected markets resolve 2026-11-01 and 2027-03-14.
- **2026-09-19.** The STRATEGIES card gave it an on/off switch that stops new entries only (`7a6fc41`, #249). The fee maths moved to `polymarket_bot/fees.py` when the shadow forward-tester was removed (`deb3c22`, #252).

## How it works

The dashboard starts `scanner.run_forever()` at boot (`polymarket_exec/ops/dashboard/app.py`). Every `daily_scan_interval_seconds` it calls `scan_once()`, which settles due positions first and then looks for one entry.

**Market discovery** (`market.discover_daily_markets`). For each asset in `config.DAILY_ASSETS` it builds the slug `{name}-up-or-down-on-{month}-{day}-{year}` with `updown_quote.window_slug(asset, "1d", now)`: today's market before noon ET, tomorrow's from noon on. It fetches that slug from Gamma `/markets`. If the slug misses, it falls back to a `tools.venue_recorder.discover()` sweep for that asset.

**Inputs per asset** (`market.build_market_view`, `scanner._scored_view`):

- $S$ is Binance spot from `/api/v3/ticker/price`.
- $K$ is the reference: the Binance 1-minute close at noon ET on the day before resolution (`fetch_close_at` at `updown_quote.daily_reference_instant(endDate)`).
- $T$ is the seconds from now to the market's `endDate`, which is the noon-ET resolution.
- $c_1,\dots,c_n$ are the last $n=30$ Binance daily closes (`fetch_daily_closes`, knob `daily_vol_lookback_days`). An asset with fewer than 5 closes is skipped.
- From the Gamma market row: `bestBid` and `bestAsk` for Up, `liquidity` in dollars, and `orderMinSize` (5 shares if absent).

**Volatility** (`signal.daily_sigma_and_drift_per_second`). With daily log returns $r_i=\ln(c_i/c_{i-1})$, the per-second volatility is

$$\sigma_{\text{day}}=\max\big(\operatorname{stdev}(r),\ 0.00002\big),\qquad \sigma=\frac{\sigma_{\text{day}}}{\sqrt{86400}}$$

The mean return is also scaled to a per-second drift $\mu=\bar r/86400$ and stored on the row. It does not enter the price.

**Fair value** (`signal.fair_up_probability`). The plain log-normal probability that the resolution close ends above the reference:

$$p_{\text{up}}=\min\Big(0.995,\ \max\Big(0.005,\ \Phi\Big(\frac{\ln(S/K)}{\sigma\sqrt{\max(T,1)}}\Big)\Big)\Big)$$

where $\Phi$ is the standard normal CDF. There is no drift term and no tie term. The BTC 5-minute version, `strategy.fair_up_probability`, adds tie mass to Up because those markets credit an exact tie to Up. This family resolves a tie 50-50, and pricing Down as $1-p_{\text{up}}$ already splits it evenly (docstring of `daily/signal.py`).

**Edges** (`signal.score`). The asks come from the Gamma row. Up's ask $a_{\text{up}}$ is `bestAsk`; Down's ask is $a_{\text{down}}=1-b_{\text{up}}$, where $b_{\text{up}}$ is `bestBid`.

$$e_{\text{up}}=p_{\text{up}}-a_{\text{up}},\qquad e_{\text{down}}=(1-p_{\text{up}})-a_{\text{down}}$$

The edge is measured before fees.

**Decision rule** (`strategy.signal_from_executable_edges`, with the parameters built in `scanner._params`). Take the side with the larger edge, then skip the asset if any of these hold:

- $T\le 3600$, so less than an hour to resolution;
- the edge is below 0.045 (knob `daily_entry_edge_min`);
- the ask is below 0.05 or above 0.95.

The upper edge cap `entry_edge_max` stays at its default of 1.0, so it never fires, and the confidence floor is 0. A confidence of $\min(0.99,\ 0.5+2.8\,e)$ is recorded but does not change the size.

**One entry per tick** (`signal.rank`, `ledger.record_signal`). Of the assets that pass, it takes the one with the largest edge and inserts it with `INSERT OR IGNORE` keyed on the window slug. So each asset holds at most one position per day. If the top asset already holds a position for its window, the insert does nothing and no other asset is entered on that tick.

**Size** (`scanner._shares_for`). With $u$ the trade size (knob `daily_trade_usd`), $a$ the ask, $m$ the order minimum and $L$ the Gamma liquidity figure, the share count is

$$n=\min\Big(\max\big(u/a,\ m\big),\ \max\big(L/a,\ m\big)\Big)$$

and the notional is $n\,a$. That is the full trade size unless the book reports less liquidity than the trade size.

**Settlement** (`scanner._settle_due`, `ledger.settle`). It runs on every tick whatever the switch says. Once the clock passes `resolves_at`, it reads the Binance 1-minute close $X$ at that instant. Up wins if $X>K$, Down wins if $X<K$, and $X=K$ is a tie. With the taker fee $f=0.07\,a(1-a)$ per share (`fees.taker_fee_per_share`), PnL per share is $1-a-f$ on a win, $-a-f$ on a loss and $0.5-a-f$ on a tie. It never asks Polymarket for the outcome.

**Switch.** `daily_altcoin` on the STRATEGIES card gates `_enter_best` only (`polymarket_bot/strategies.py`).

## Parameters

| Parameter | Default | Where it came from |
|---|---|---|
| `daily_trade_usd` (SETTINGS card) | $10 | Zayan, 2026-08-30 (`tasks/2026-08-30-local-strategy-rebrand.md`, decision 5) |
| `daily_scan_interval_seconds` (SETTINGS card) | 60 s | `5c91228`; the scanner docstring says a 24h market needs no 5-second tick |
| `daily_entry_edge_min` (SETTINGS card) | 0.045 | `5c91228`, no reason recorded; the same number as the BTC loop's `PAPER_ENTRY_EDGE_MIN` |
| `daily_vol_lookback_days` (SETTINGS card) | 30 | `5c91228`, no reason recorded |
| `DAILY_ENTRY_MIN_REMAINING_SECONDS` (env only) | 3600 s | `5c91228`, no reason recorded |
| `DAILY_ASSETS` (env only) | doge, sol, xrp, bnb, eth | Zayan, 2026-08-30 (issue #185) |
| Ask band | 0.05 to 0.95 | `StrategyParams` defaults in `strategy.py`, inherited from the BTC loop |
| `entry_edge_max` | 1.0, so off | `StrategyParams` default |
| Fee rate | 0.07 | `polymarket_bot/fees.py`; #182 measured the venue's implied rate at p95 0.0666 |

No `runtime.daily.*` overrides were stored in the `config` table on 2026-09-21, so these defaults are what runs.

## Evidence so far

Read from `daily_shadow_positions` on 2026-09-21: 20 rows, 18 settled and 2 open.

- 18 settled: 5 won, 13 lost, no ties. Net −$67.08 on $180.00 staked.
- Mean −$3.73 per position, standard deviation $11.93, t = −1.33.
- The model's average probability for the side it bought was 0.432. The average ask paid was 0.319. The realised win rate was 0.278 (5 of 18). At the average ask the break-even win rate after the fee is 0.334 (`fees.breakeven_winrate`).
- Brier score on these 18: 0.155 for the model, 0.149 for the ask read as a probability.
- Claimed edges averaged 0.113. The largest was 0.374, on DOGE Down for 2026-09-15, which won.
- 16 of 18 entries bought the side priced under 0.50: 4 won, −$57.34. By side, 11 Up (3 won, −$36.67) and 7 Down (2 won, −$30.41).
- By asset: XRP 0 of 4 won (−$42.24), ETH 1 of 4 (−$13.32), DOGE 1 of 4 (−$9.62), BNB 2 of 4 (−$5.39), SOL 1 of 2 (+$3.49).
- The first entry was 2026-08-30 and the second 2026-09-13.
- `polymarket_bot/inventory.py` records 19 positions, 18 settled for −$67.08 and 1 open. The query above also found ETH Up at 0.88, opened at 07:05Z on 2026-09-21.

## Known weaknesses

- Prices come from Gamma's `bestBid` and `bestAsk` fields, not the CLOB order book the BTC loop was moved to in #22. A fill is assumed at that ask with no depth check beyond Gamma's `liquidity` figure.
- Drift is computed and stored but does not enter the price, though #185 named momentum as a minimum input.
- `rank()` does not drop assets that already hold this window, so a held top asset blocks every other asset for that tick.
- The upper edge cap is off: any edge above 0.045 is taken at full size, however large.
- Outcomes are computed from Binance, not read from Polymarket. A disagreement with the venue's own resolution would go unnoticed.
- There is no live order path (`scanner.py` docstring), so the paper/live mode selector does not reach this strategy.
- `DAILY_ENTRY_MIN_REMAINING_SECONDS` and `DAILY_ASSETS` are env-only. The SETTINGS card cannot change them.

## Sources

- Code: `polymarket_bot/daily/` (`scanner.py`, `market.py`, `signal.py`, `ledger.py`, `types.py`); `polymarket_bot/strategy.py` (`signal_from_executable_edges`, `sigma_per_second`, `StrategyParams`); `polymarket_bot/fees.py`; `polymarket_exec/connectors/updown_quote.py` (`window_slug`, `daily_reference_instant`); `polymarket_bot/runtime_knobs.py`; `config.py` (`DAILY_*`); `polymarket_bot/strategies.py`; `polymarket_bot/inventory.py` (record line); `polymarket_exec/ops/dashboard/app.py` (lifespan start).
- Ledger: `daily_shadow_positions` and `config` in `data/btc_5m_binary_fair_value.db` (schema in `db.py`), read-only query on 2026-09-21.
- `tasks/2026-08-30-local-strategy-rebrand.md`: the operator's decisions and the build log.
- Issue #185 (open): thesis, required inputs and resolution process. Issue #182 (closed 2026-08-30): the implied fee rate.
- PRs #239, #245, #246, #249, #252.
- `27f38fd` 2026-08-29 — docs: branch close-out — pivot off all 5-minute markets (#182)
- `5c91228` 2026-08-30 — feat(#185): daily altcoin Up/Down shadow scanner
- `49b092f` 2026-09-13 — feat: dashboard SETTINGS card, MetaMask-only live setup, drop guardrails panel
- `230d1b0` 2026-09-17 — fix(daily): look up the market resolving at the next noon ET, not the UTC date
- `be47831` 2026-09-19 — fix(daily): reference price is noon ET the day before, not endDate - 86400
- `e499c7d` 2026-09-19 — fix(daily): settle every tick, not only when discovery finds markets
- `99a9cdf` 2026-09-19 — fix(daily): isolate settlement per row and stop its silent skips
- `7a6fc41` 2026-09-19 — feat(dashboard): STRATEGIES card under FEEDS with a switch per strategy
- `deb3c22` 2026-09-19 — chore: remove the shadow forward-tester
- Endpoints in the code: `https://gamma-api.polymarket.com/markets`; `https://data-api.binance.vision/api/v3/klines` and `/api/v3/ticker/price`.

## Changelog

- 2026-09-21 · `14f1bd0c38c4` · Added an At a glance summary (concept, main assumption, maths, how it works, how it was derived, references) for the dashboard's STRATEGY card.
- 2026-09-21 · `14f1bd0c38c4` · Doc created.
