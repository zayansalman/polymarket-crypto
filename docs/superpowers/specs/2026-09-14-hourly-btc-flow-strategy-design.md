# Hourly BTC Up/Down — order-flow reversal strategy, venue feeds, context factors, Kronos

Status: draft for operator review (2026-09-14). Nothing here is built yet.

## Context

The v0 BTC 5-minute strategy was archived on 2026-09-13 and the loop runs with no
strategy loaded. The operator wants a new strategy for Polymarket's **hourly
"Bitcoin Up or Down"** market that:

- enters once at the start of the hour and holds to resolution;
- is built once and runs on paper or live according to the operator's global
  mode selection (no paper-only or live-only logic);
- is fed by Binance spot, Binance perpetual futures, and Kraken (spot and
  futures) data;
- records context factors on every hour so the operator can learn which ones
  matter;
- logs the Kronos BTC 1h fine-tune as a second opinion;
- targets a **60% win rate**.

### Market facts (verified live 2026-09-13)

- Series `btc-up-or-down-hourly`. The slug is
  `bitcoin-up-or-down-<month>-<day>-<year>-<h><am|pm>-et`, labelled by the ET start
  hour. `polymarket_exec/connectors/updown_quote.py:window_slug` already builds it.
- It resolves Up if the Binance BTC/USDT 1H candle that starts at the title time
  closes at or above its open. Ties go to Up. Gamma `eventStartTime` is the candle
  start in UTC; `endDate` is one hour later.
- Markets list about 2 days ahead. Resolved markets need `&closed=true` to be
  found by slug.
- Taker fee is `0.07 × p × (1 − p)` per share (makers pay nothing). Tick size
  0.01, minimum order 5 shares. The book is usually 1 cent wide.
- Hour H's open is the first trade of hour H. It can differ from H-1's close by a
  cent. Binance klines always include the still-forming candle, which must be
  dropped from any input.

## Evidence

All tests were pre-registered before running. Data: Binance spot and USD-M
perpetual 1h klines with taker-buy volume, 2023-10 to 2026-09. Rules were found on
**discovery** (before 2025-10-18 14:00 UTC) and checked on **validation** (after).
Scripts and outputs live in the research scratchpad, not the repo.

Terms used below:

- **imbalance** = `2 × taker_buy_volume / volume − 1` for an hour.
- **flow z** = imbalance z-scored against the trailing 168 hours.
- **flow push (fz)** = flow z × sign(close − open).
- **CLV** = `(2·close − high − low) / (high − low)`, where the hour closed within
  its own range.

Win rates for "bet against hour H-1's direction", with 95% intervals:

| Rule | Discovery | Validation | Share of hours |
|---|---|---|---|
| Every hour | 52.6% | 52.3% [51.2, 53.4] | 100% |
| Flow push, spot fz top quintile (> 1.20) | 56.1% (n=3480) | 53.5% [51.0, 56.0] (n=1539) | 19% |
| Spot-only push: spot fz > 1.20 and perp fz ≤ 1.24 | 55.2% (n=2125) | 54.4% [51.3, 57.4] (n=1041) | 13% |
| **Tier A:** spot-only push and CLV beyond ±0.8 in the move's direction | **57.7% (n=515)** | **57.3% [50.7, 63.6] (n=220)** | 2.8% |
| Spot and perp both pushed | 57.3% | 51.8% (faded) | 6% |
| 10-feature logistic model (selective) | best in-sample 53.3% | 52.4% | 6% |

Other findings:

- **Decay.** The flow-push edge has lost about 2.4 points a year (60% in late
  2023, 52% through 2025–26, 56% so far in 2026-H2 on 351 hours). It lasts about
  2 hours after the signal and is gone by the third.
- **Up vs Down.** On unseen data, fading aggressive buying (bet Down) is the
  stronger leg: 54.4% vs 51.9% for ordinary up hours. Fading aggressive selling
  gives 52.8% vs 52.1%.
- **Factors that held in both periods** (small n, recorded not gated):
  - US stock-open hour: +3 points.
  - 08:00 UTC Deribit expiry hour: −2 to −6 points.
  - BTC and ETH moving the same way last hour makes plain reversal work (53%).
    When they diverge it is about 50%.
- **Factors that flipped between periods:** funding hour, US session, big
  liquidation-like hours, ETH also flow-pushed, crowded funding.
- **Move size** replicates well: US session hours move about 0.5% vs about 0.28%
  otherwise, and weekends about 0.2%.
- **Kronos BTC 1h fine-tune** on 500 unseen hours: direction hit rate 49–50%, and
  its logic is reversal/RSI-like (P(up) correlates −0.36 with RSI). Its forecast
  spread predicts move size a little beyond a 24h average (+0.03 ranked R²).
- **Literature.** Hourly BTC direction is at most about 52% out of sample in
  credible studies. OpenMarket (arXiv 2607.26245) found a 43-feature
  Binance/Polymarket model did not beat Polymarket's own price on 15-minute
  markets, and Polymarket quotes react to large Binance moves in a median 347 ms.

**On the 60% target.** No rule tested reached a validation point estimate of 60%
with a lower bound of 55% on at least 150 bets. The best candidate, Tier A, holds
about 57% in both periods on about 0.7 bets a day. Break-even at a 50¢ price is
about 52.25% as a taker (fee plus 1-cent spread) and 50% as a maker. The strategy
therefore records every hour so forward results show whether 57–60% holds. Nothing
auto-disables a tier.

### Update: open-interest filter (walk-forward, pre-registered 2026-09-14)

Thresholds were set each month using only earlier data; out-of-sample covers
2024-04 to 2026-08. Open interest is Bybit BTCUSDT linear hourly, free back to 2023-10.

| Rule | Out-of-sample | Latest 11 months | Per year |
|---|---|---|---|
| Tier A | 57.3% [53.3, 61.1] n=620 | 58.2% n=213 | 59 / 55 / 58% |
| **Tier A + open interest did not rise during H-1** | **60.5% [54.9, 65.8] n=306** | **61.4% [52.2, 69.8] n=114** | 59 / 61 / 61% (half-years 54–63%) |
| Tier A + open interest rose during H-1 | 54.1% n=314 | 54.5% n=99 | — |
| Strict Tier A (top-decile push, CLV ±0.9) | 62.9% [55.5, 69.7] n=175 | 57.5% n=73 | — |

Reading:

- Aggressive spot flow that does **not** add open interest (closing, covering,
  liquidations) reverts about 60% of the time. Flow that opens new positions reverts
  about 51–54%.
- Both bet sides hold: bet Down 58.9%, bet Up 61.8%. About 2.4 bets a week.
- **Not yet proven.** Eight rules were tested (this one passes FDR at q=0.10). The
  pre-set bar required a lower bound of at least 55%, and this rule's is 54.9%. The
  open-interest split does not show up on the broader flow-push signal. About 2–3
  more months of forward bets would settle the bound.
- Timing: the rule needs open interest at H:00. Bybit's hourly history publishes about
  1–2 minutes late, so live use should sample open interest itself at the hour
  boundary. The flow recorder already samples Binance open interest each hour; Bybit
  sampling would be a small addition.

### Update: replication on ETH, SOL and XRP failed (pre-registered 2026-09-14)

The BTC open-interest rule was frozen and run unchanged on each asset's own spot, perp
and Bybit OI data, over the same walk-forward months.

| Asset | Rule (OI did not rise) | Mirror (OI rose) |
|---|---|---|
| BTC (original) | 60.5% n=306 | 54.1% n=314 |
| ETH | 55.2% n=201 | 57.5% n=221 |
| SOL | 51.7% n=230 | 53.6% n=233 |
| XRP | 52.0% n=246 | 48.1% n=237 |
| ETH+SOL+XRP pooled | 52.9% n=677 (latest 11 months 47.1%) | 53.0% n=691 |

The open-interest split does not replicate; on ETH it points the other way. The BTC
60.5% is best read as one lucky slice of the eight rules tested, or at most a
BTC-only effect. It should be carried as an observed, recorded filter, not as the
strategy's expected win rate. Planning numbers stay as before:

- Tier A: about 57%.
- Flow push: about 53–54%.

## Goals

1. Trade the hourly BTC market through the one shared paper/live pipeline, with
   the decision made once per hour at the open and held to resolution.
2. Decision rule v1, frozen from discovery, with no fitting at runtime:
   - **Tier A** (spot-only push plus CLV extreme): bet against H-1.
   - **Tier B** (spot flow push, not Tier A): bet against H-1.
   - A runtime knob picks which tiers enter (`A` or `A+B`). Every hour's tier and
     would-have-entered flag is recorded either way.
3. Wire in the venue feeds and store one closed-hour flow row per venue:
   - Binance spot BTCUSDT and ETHUSDT;
   - Binance USD-M perp BTCUSDT, plus funding, mark/index and open interest;
   - Binance liquidations;
   - Kraken spot BTC/USD trades;
   - Kraken Futures PF_XBTUSD trades, plus funding and open interest.
4. Show every venue feed on the FEEDS card with status and delay.
5. Record one context row per hour, traded or not, with signal values, tier, the
   factors, the Kronos row, the Polymarket book at decision, the entry action and
   the Binance outcome.
6. Run Kronos as an isolated once-an-hour subprocess and store its result. It
   never blocks or gates the entry.

## Non-goals

- No change to `polymarket_exec/execution/gate.py` (RiskGate) or its tests.
- No auto-gating, auto-pause, or tier elimination. Filters are recorded, not
  enforced, beyond the operator-chosen tier knob.
- No Shabbat or holiday pause (operator decision).
- No Kraken historical backtest. Kraken's free API has only 720 hourly OHLC bars
  without taker split, and a full trade history would need about 50k requests.
  Kraken flow is collected going forward.
- No live authorization for BTC 1h in AGENTS.md. That line is the operator's.
- No change to the 5m path, other than guarding 5m-only code from 1h slugs.

## Architecture

### A. Venue flow feeds (always-on, observation only)

A new always-on task, `polymarket_exec/ops/flow_recorder.py`, is started in the
dashboard lifespan next to the feed monitor from PR #231. It records data whether
or not the bot is running:

- **Binance REST closed bars.** A few seconds after each hour boundary, fetch
  closed 1h klines for spot BTCUSDT, spot ETHUSDT and perp BTCUSDT (drop any row
  with `close_time ≥ now`). Upsert into `venue_flow_hourly`.
- **Binance perp state.** At each hour open, read `premiumIndex` (mark, index,
  last funding rate, next funding time) and `openInterest`, and insert into
  `venue_snapshot`.
- **Binance liquidations.** WS `!forceOrder@arr`, filtered to BTCUSDT and
  aggregated per hour: long-liquidated quantity (order side SELL) and
  short-liquidated quantity (BUY).
- **Kraken spot.** WS v2 `trade` channel for BTC/USD. `side` is the taker side.
  Aggregated per hour into volume, taker-buy volume and trade count. If the socket
  dropped during the hour, the row is written with `complete=0`.
- **Kraken Futures.** WS v1 `trade` feed for PF_XBTUSD, aggregated the same way.
  The REST `tickers` endpoint is read at each hour open for mark, index,
  `fundingRate` (absolute USD per contract per hour; relative rate =
  fundingRate / indexPrice) and `openInterest`.
- Verified message shapes are pinned in tests (captured 2026-09-13).

### B. Hourly strategy in the shared loop

- A per-timeframe market spec replaces the hard-wired 5-minute constants for the
  1h case. It covers window seconds, discovery via `updown_quote.window_slug`
  checked against Gamma `eventStartTime`, a Binance-kline settlement function, and
  an entry deadline.
- The market selection is pinned at Start, the same way mode is. `("btc","1h")`
  goes into `LOOP_SUPPORTED`, and into `STRATEGY_MARKETS` once #232 merges.
- A pure function `decide_hourly(inputs) -> HourlyDecision` is called from the
  `NO_STRATEGY_REASON` block in `polymarket_bot/paper.py:_build_snapshot`. It reads
  only closed Binance bars and never reads mode or the executor.
- The decision is computed once per window and cached in memory and in the
  context table. Ticks after the entry deadline record `MISSED_DEADLINE` instead
  of chasing.
- Settlement is dispatched on metadata stamped on the position (timeframe,
  `window_start_ts`), not by parsing the slug suffix. The 1h path settles from the
  Binance kline.
- The 5m shadow roster returns early when the pinned timeframe is not 5m.

### C. Context record

A new table, `hourly_strategy_context`, has one row per `(window_slug,
strategy_id)` written with `INSERT OR IGNORE` at decision time. It is updated when
Kronos lands and again at settlement for every hour, traded or not. Columns hold:

- signal inputs and values (imbalance, z, fz, CLV, perp fz, tier);
- factor observations (macro release, US session and open, ETF hours, funding
  hour and rate, Deribit expiry, ETH move and flow, Kraken flow, liquidations,
  weekend);
- the book at decision, entry action and position id, Kronos fields, and the
  outcome.

### D. Kronos worker

A one-shot subprocess each hour: `python -m polymarket_bot.kronos.worker`.

- **Input/output.** JSON on stdin (512 closed candles, the hour-H open, config);
  JSON on stdout.
- **Isolation.** Minimal explicit environment, never the app's environment (which
  holds the private key). Offline HF cache.
- **Model settings.**
  - Pinned revisions: model `eb51e682…`, tokenizer `b8f1c795…`, code `67b630e…`.
  - Load with `strict=True`, then `.eval()`. CPU only, 3 threads, niced.
  - Sampling: 50 paths in chunks of 25, T=1.0, top_k=0, top_p=1.0.
  - Seeded by the hour.
- **Timing.** The parent enforces a 180 s timeout and never runs two workers at
  once. The parent is the only database writer.
- **Packaging.** torch, einops, pandas, tqdm and safetensors go in an optional
  `[kronos]` extra. Everything is imported lazily. CI tests use a fake worker.

## Operator decisions needed (flagged, not assumed)

1. **Fee parity.** Paper settlement PnL ignores the taker fee while live nets it
   (`paper.py:1665-1672` vs `live.py:708-712`). Close the gap?
2. **Live boot reconcile.** `live.py:_window_resolved` cannot parse 1h slugs and
   would refuse live boot on a stale open 1h row. Change this live-path helper?
3. **Stop behaviour.** Stop force-closes a held position at the bid. Keep that
   for a hold-to-resolution hourly strategy?
4. **Sizing.** A strategy's notional is overwritten to `trade_shares × ask`, and
   with no runtime share count the $3 cap blocks asks above 0.60. The operator
   should set `trade_shares`.
5. **Tier knob default.** `A` only (about 0.7 bets/day, 57% so far) or `A+B`
   (about 4.7/day, about 53–54%)?
6. **Execution style.** Tier B is only above water as a maker (zero fee). Use
   resting post-only orders at the open, or take the ask?

## Evaluation

- The dashboard shows forward hit rate per tier with a 95% interval, the number of
  hours, the share entered, and hit rate against the price paid (edge vs the ask),
  for paper and live separately.
- The 60% target is tracked on the Tier A forward record: point estimate and lower
  bound. No automatic action is taken on it.
- Decay is tracked as the rolling 90-day tier hit rate, for display only.

## Sequencing and overlap

- **Feeds.** Build after, or on top of, PR #231 (`fix/feeds-live`: feed monitor
  and FEEDS card rewrite) and PR #232 (`STRATEGY_MARKETS`). Both touch files this
  work edits.
- **Plans:**
  1. `docs/superpowers/plans/2026-09-14-venue-flow-feeds.md` — part A.
  2. `docs/superpowers/plans/2026-09-14-hourly-btc-strategy.md` — parts B and C
     (flow and CLV decision, context table, settlement).
  3. A Kronos worker plan (part D) and a factor calendar plan (macro calendar from
     official BLS/Fed/BEA schedules). Written after plans 1–2 land.
