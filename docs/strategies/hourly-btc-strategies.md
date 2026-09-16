# Hourly BTC Strategies

Two strategies for Polymarket's hourly **Bitcoin Up or Down** market:

1. **Kronos BTCUSDT 1h fine-tune (Hugging Face lc2004): next-hour Up chance vs Polymarket price** — a candlestick foundation model forecasts the next hour and bets where its probability beats the market price.
2. **Binance BTCUSDT 1h reversal after a spot taker-buy/sell push that perps didn't match, with the hour closing at its high or low** — bets the last hour reverses when aggressive spot market orders pushed it to an extreme without the perpetual futures market confirming the move.

Written 2026-09-14; names set by Zayan (operator) on 2026-09-15. The pre-registrations, scripts, outputs and a 2026-09-16 reproduction are in `research/hourly_btc_2026_09/` (see its `README.md` and `REPRODUCTION.md`). Every rule, threshold, default and number below has a source in the **Sources** tables. This file is the operator-facing description of what each strategy does and what to expect.

---

## The market

| | |
|---|---|
| Series | `btc-up-or-down-hourly` |
| Slug | `bitcoin-up-or-down-<month>-<day>-<year>-<h><am\|pm>-et` (labelled by the ET start hour) |
| Resolves | **Up** if the Binance BTC/USDT 1-hour candle that starts at the title time closes at or above its open. Ties go to Up. |
| Candle timing | Gamma `eventStartTime` is the candle start in UTC; `endDate` is one hour later |
| November fall-back day | Two UTC hours get the same `1am-et` label, but a Gamma slug names only one market. On 2025-11-02 Polymarket listed neither 1am ET hour (read-only Gamma check, 2026-09-15). If the slug lookup misses, the bot takes the series market whose `eventStartTime` is the UTC hour. Hourly records are keyed by that UTC start, not the slug. (Claude, 2026-09-15, branch-review finding dst-fallback-slug-collision) |
| Listed | about 2 days ahead; the book is usually 1 cent wide |
| Fees | taker `0.07 × p × (1 − p)` per share (about 1.75¢ at 50¢); makers pay nothing |
| Order rules | tick 0.01, minimum 5 shares |

Break-even hit rate at a 50¢ price:

| Order style | Break-even |
|---|---|
| Paying the ask | about 52.25% (fee plus a 1-cent spread) |
| Resting order that fills at your price | 50% |

---

## Shared mechanics (both strategies)

- **One decision per hour.**
  - Decide at the start of UTC hour H, using only candles that closed through H-1. Binance always returns the still-forming candle, and it is always dropped.
  - Hour H's real open is read from the forming kline.
- **Hold to resolution.** Positions settle from the Binance 1-hour candle itself, not from Polymarket's resolution status. Gamma stops listing a resolved hourly market about 12 minutes after close. A position left open by an earlier run still settles while the bot runs BTC 5m, and an open 5m position still settles while it runs BTC 1h (Claude, 2026-09-15, branch-review finding other-timeframe-live-rows-never-settled). A live position only settles in a live run: a paper run on the other timeframe leaves it open (Claude, 2026-09-15, branch-review finding other-timeframe-live-rows-never-settled, review follow-up).
  - A live Stop tries to sell the current hour's position. Any shares that don't sell stay open and settle from the candle after the next live Start. That settlement counts only the shares still held, so shares already sold are never counted twice. If every share was sold, the position closes at the next live Start (Claude, 2026-09-15, branch-review finding reconcile-resets-sold-size-double-books).
- **Mode-agnostic.** A strategy never reads paper/live. The operator's global mode decides where the order goes, and paper and live run the identical decision.
- **One open position per strategy.** Each strategy has its own slot, so both can hold a position in the same hour. This replaces the old one-position-total rule (operator decision, 2026-09-14). Every other RiskGate check still applies to every entry: loss halt, caps, slippage guard and kill switch.
- **Order style is the operator's choice.** A dashboard setting picks one of:
  - **Pay the ask:** a marketable buy at the best ask, which fills now.
  - **Resting order:** a post-only buy at the best bid, cancelled at the entry deadline if unfilled.
- **Entry deadline.** A setting, 120 s after H:00 by default. If a strategy has no entry by then, the hour is recorded as `MISSED` and nothing is chased later.
- **At most one order attempt per hour per strategy** (Claude, 2026-09-15, branch-review finding hourly-ambiguous-post-error-retried).
  - A tick with no executable ask, or with the strategy's slot still held, just waits for the next tick, up to the deadline.
  - Otherwise the record is set to `SUBMITTING` before any order goes out, in paper and live.
  - A refusal before the order is sent (gate, no token id, venue minimum, kill switch) records `BLOCKED:<reason>`.
  - Any other live failure (an exception or timeout on the post, or a reply without success and an order id) may still have put an order on the book, and a re-post would be a second order. The hour ends as `UNCERTAIN:<status> <reason>`, nothing is re-posted, and the operator gets a notification to check the account's open orders and trades.
  - If a later tick still finds `SUBMITTING` (the attempt raised, or the process stopped mid-attempt), the hour ends as `UNCERTAIN:entry attempt did not finish`. Boot reconciliation still adopts any live fill the order journal recorded.
  - Exception: if the entry actually went through and only the `ENTERED` write was lost (a failed database write, or a restart after which boot reconciliation adopted the fill), the next tick records `ENTERED` with that position, even after the entry deadline. Only a position that is still open counts, in both modes; one already closed (sold at Stop, or closed by boot reconciliation) leaves the hour as `UNCERTAIN`. In live the strategy's position slot must also hold the entry. (Claude, 2026-09-15, branch-review finding crash-after-entry-marks-missed; open-only rule from a review by Claude session polymarket-crypto-95, 2026-09-16.)
  - That close-out also notifies the operator (`entry_attempt_unfinished`), in paper and live. If the process died right after a live post but before its journal write, the order can be on Polymarket with no journal entry, and boot reconciliation closes its ledger row as `RECONCILED_NO_LIVE_TRACE`. The hour is still never posted again; check the account's open orders and trades. (Claude, 2026-09-15, branch-review finding hourly-reentry-after-untraced-post)
  - A decision still open when its hour ends (the loop was stopped, crashed, or had failing ticks, or the strategy was switched off) is closed out on the next tick in a later hour: `ENTERED` if the strategy's position for that hour and mode exists and boot reconciliation did not close it as never placed or never filled; otherwise `UNCERTAIN:entry attempt did not finish` if an order attempt had started (with the same operator warning); otherwise `MISSED`. _(Claude, 2026-09-15, branch-review finding pending-row-never-finalized)_
- **Kill switch holds entries.** While the kill file exists, no strategy enters and the hour stays `PENDING`, the same way in paper and live. If the file is removed before the entry deadline, the entry goes ahead; if not, the hour is recorded as `MISSED`. (Claude, 2026-09-15, branch-review finding kill-switch-paper-blocked-live-pending)
- **Size.** The operator's share count from the order-size ticket, capped at the shares on the best ask, then raised to the 5-share venue minimum. Paper and live both size, gate and book that same amount, so a best ask holding only 3 shares still means a 5-share order. (Claude, 2026-09-15, branch-review finding thin-top-sizing-paper-vs-live)
- **Every hour is recorded for both strategies, bet or no bet.** The record holds:
  - the signal values and the decision;
  - the book at decision time;
  - what happened to the entry: `ENTERED`, `BLOCKED:<gate reason>`, `NO_SIGNAL`, `MISSED`, `UNFILLED`, `SUBMITTING` or `UNCERTAIN:<reason>` (the last two: Claude, 2026-09-15, branch-review finding hourly-ambiguous-post-error-retried);
  - the hour's outcome;
  - context factors: Kronos agreement, ETH's last hour, US stock-open hour, 08:00 UTC options expiry, scheduled US macro releases, funding, open interest, liquidations, Kraken flow, weekend.

  Factors are observations for learning. None of them blocks a bet.

  The record is kept per mode. If the operator switches between paper and live inside an hour, each run records its own decision, runs its own RiskGate check and writes its own action. A paper `BLOCKED` row does not stop the live run from trying, and a live entry is never filed under paper. (Claude, 2026-09-15, branch-review finding decision-row-shared-across-modes)

### Sources: shared mechanics

| What | Source | Evidence |
|---|---|---|
| Hourly BTC Up/Down market chosen for these strategies | Zayan (operator), 2026-09-13 | session of 2026-09-13 |
| Resolution rule (Binance BTCUSDT 1h candle, close ≥ open is Up) | Polymarket market rules for the hourly BTC series, read by Claude, 2026-09-14 | `polymarket_bot/hourly/market.py` docstring |
| Decide once at the open with closed candles only, hold to resolution, settle from the Binance candle, record every hour | Claude, 2026-09-14 | plan `docs/superpowers/plans/2026-09-14-hourly-engine-btcusdt-1h-spot-taker-push-reversal.md` |
| Entry deadline default 120 s | Claude, 2026-09-14 | `polymarket_bot/runtime_knobs.py` |
| One open position per strategy (replacing one position in total); both strategies may hold a position in the same hour | Zayan (operator), 2026-09-14 | AGENTS.md rule |
| Operator chooses the order style | Zayan (operator), 2026-09-14 | this file |
| No paper-only strategy; strategies run in the selected mode; BTC hourly authorized for live | Zayan (operator), 2026-09-14 | AGENTS.md |
| Strategy names, no vague naming, every rule sourced | Zayan (operator), 2026-09-15 | this file |
| Taker fee `0.07 × p × (1 − p)` | Polymarket fee schedule | `polymarket_bot/shadow/fees.py` |
| Break-even 52.25% when paying the ask (fee plus a 1-cent spread) | Claude, 2026-09-13, literature fact-check | `research/hourly_btc_2026_09/literature/literature_factcheck_2026-09-13.json` |
| Order-attempt marker, uncertain outcomes, restart linking, kill-switch parity, per-mode records, UTC-hour keys, ended-hour close-out, sizing parity, cross-timeframe settlement, sold-share restore | Claude, 2026-09-15 branch review (finding ids next to each rule above); open-only linking from Claude session polymarket-crypto-95, 2026-09-16 | tests under `tests/unit/test_hourly_*.py`, `test_live_executor.py` |

---

## Strategy 1 — Kronos BTCUSDT 1h fine-tune (Hugging Face lc2004): next-hour Up chance vs Polymarket price

### What it is
[Kronos](https://github.com/shiyu-coder/Kronos) (Shi et al., Tsinghua University, arXiv 2508.02739) is an open-source foundation model trained on candlestick data from 45 exchanges. This strategy uses a BTC-specific fine-tune published by the Hugging Face account lc2004, with training code at github.com/Liucong-JunZi/Kronos-Btc-finetune:

| Piece | Pinned revision |
|---|---|
| Model: `lc2004/kronos_base_model_BTCUSDT_1h_finetune` (102M parameters) | commit `eb51e682c8194a1ba7254cc4357c75819683fbaf` |
| Tokenizer: `lc2004/kronos_tokenizer_base_BTCUSDT_1h_finetune` | commit `b8f1c795b80231f5bdeb69dfe71fc1542d2111fc` |
| Model code: `shiyu-coder/Kronos` | commit `67b630e67f6a18c9e9be918d9b4337c960db1e9a` |

All three are MIT-licensed. The model was fine-tuned on Binance spot BTCUSDT hourly candles from 2023-10-19 to 2025-10-18.

### How it decides
1. **Input.** The last 512 closed hourly Binance spot BTCUSDT candles (open, high, low, close, volume, quote volume), with naive UTC timestamps.
2. **Forecast.** Sample **50 independent next-hour paths**:
   - model in eval mode, on CPU, in chunks of 25;
   - temperature 1.0, top_k 0, top_p 1.0;
   - seeded by the hour, so a rerun reproduces the same paths.
3. **Probability.** `P(up)` is the share of paths whose close is at or above the **real** open of hour H.
4. **Bet.**
   - Buy **Up** if `P(up) − Up price ≥ edge threshold`.
   - Buy **Down** if `(1 − P(up)) − Down price ≥ edge threshold`.
   - Otherwise skip.
   - Price is the ask when paying the ask, or the bid when resting.
   - The edge threshold is a dashboard setting, 0.05 by default.
5. **Timing.**
   - The model runs as an isolated once-an-hour subprocess, started as soon as hour H-1 has closed.
   - It gets a minimal environment, never the app's own (which holds the wallet key), and reads weights from the offline Hugging Face cache.
   - If its result isn't ready by the entry deadline, the hour is `MISSED`.

### What to expect (honest)
Tested on 500 random hours it never saw (2025-10-18 to 2026-09-11):
- It picked the right direction **49–50%** of the time.
- Its confidence didn't line up with outcomes. Whether it said 10% or 88% up, BTC closed up about half the time. Scored as probabilities, its forecasts did worse than always saying 50%.
- Its logic is reversal/RSI-like: its P(up) moves opposite to the last hours and to RSI.
- Its forecast spread does predict how **big** the next hour's move will be, slightly better than a trailing 24-hour average.

**The historical test gives no reason to expect a profit.** It runs forward so its calls are measured against real Polymarket prices and against the other hourly strategy. It is not running because a backtest showed an edge.

### Cost
About 10–20 s of CPU and about 1.7 GB peak memory per hour on the operator's 8 GB M2 MacBook Air. The dependencies (torch, einops, pandas, tqdm, safetensors) are an optional install. Without them the strategy records `unavailable` and never bets.


### Sources

| What | Source | Evidence |
|---|---|---|
| Use a Hugging Face model trained on crypto price history | Zayan (operator), 2026-09-13 | `research/hourly_btc_2026_09/README.md` |
| This fine-tune for the hourly BTC market | Zayan (operator), 2026-09-13 | same |
| Base model and code | Shi et al., arXiv 2508.02739; github.com/shiyu-coder/Kronos @ 67b630e | pinned table above |
| Fine-tuned weights and training data range | Hugging Face lc2004 (commits above); training code github.com/Liucong-JunZi/Kronos-Btc-finetune (link given by Zayan, 2026-09-13) | model cards |
| Bet when the forecast is above/below the Polymarket price, hold to resolution | Zayan (operator), 2026-09-13 | `research/hourly_btc_2026_09/README.md` |
| 512 candles, 50 paths, temperature 1, top_k 0, top_p 1, per-hour seed, 0.05 edge | Claude, 2026-09-13 | `recovered_verbatim/bench.py`, `backtest.py` |
| 49–50% on 500 unseen hours; confidence not calibrated | Claude, 2026-09-13 | `recovered_verbatim/backtest.py`, `analyze.py`; `session_commands/020`–`030` |
| Name | Zayan (operator), 2026-09-15 | — |

---

## Strategy 2 — Binance BTCUSDT 1h reversal after a spot taker-buy/sell push that perps didn't match, with the hour closing at its high or low

### The idea
Sometimes an hour's move is driven by an unusual burst of **aggressive market orders** (takers) buying or selling spot BTC. In that pattern:

- the perpetual futures market does **not** show the same burst;
- the hour closes at the very edge of its range.

The next hour tends to **revert**. The push looks like a one-sided rush that ran out of fuel, not new conviction across the market.

### Inputs (all known at the open of hour H)
Binance spot **BTCUSDT** and Binance USD-M perpetual **BTCUSDT** hourly klines: the closed hour H-1 plus the trailing 168 hours. From each kline:

- **Imbalance** = `2 × taker_buy_volume / volume − 1`. It runs from −1 (all aggressive selling) to +1 (all aggressive buying).
- **Flow z** = the imbalance z-scored against the trailing 168 hours, including H-1.
- **Flow push (fz)** = `flow z × sign(close − open)`. It is large and positive when aggressive orders pushed the hour in the direction it moved.
- **Close location (CLV)** = `(2·close − high − low) / (high − low)`. It is +1 when the hour closed at its high and −1 at its low.

### The rule (frozen)
Bet when **all** of these hold for hour H-1:

1. **Spot pushed hard:** spot fz > **1.20** (the top 20% of hours).
2. **Perps did not confirm:** perp fz ≤ **1.24**, meaning perps were not in their own top 20%.
3. **Closed at the extreme:** CLV > **0.80** after an up hour, or CLV < **−0.80** after a down hour.
4. H-1 was not flat.

**Bet against H-1:** buy **Down** after an up hour, **Up** after a down hour.

The thresholds are the 80th percentiles from the discovery period (before 2025-10-18). They are recorded on every decision and never retuned automatically.

**Recorded but not bet:** the wider flow-push rule (spot fz > 1.20 on its own, about 53–54%, around 4–5 hours a day). It runs alongside so the operator can compare.

### Evidence
Every test was pre-registered: rules chosen on 2023-10 to 2025-10, then checked on data they were never fitted to.

| Test | Win rate (95% range) | Bets |
|---|---|---|
| Discovery, 2023-10 to 2025-10 | 57.7% | 515 |
| Held-out validation, 2025-10-18 to 2026-09 | **57.3%** [50.7, 63.6] | 220 |
| Walk-forward (thresholds re-set monthly from past data only), 2024-04 to 2026-08 | **57.3%** [53.3, 61.1] | 620 |
| Walk-forward by year | 2024: 59% · 2025: 55% · 2026: 58% | |
| Every hour, plain "bet against last hour" | 52.3% [51.2, 53.4] | 7,924 (validation) |

- **Frequency:** about 2.8% of hours, roughly **one bet every 1.5 days** (about 20 a month).
- **Both directions work.** Walk-forward: bet Down 56.8% (n=303), bet Up 57.7% (n=317). In the latest 11 months, betting Down has been the stronger leg (60.9% vs 55.3%).
- **The reversion window is short.** On the wider flow-push rule (spot flow push above its 80th percentile, no other condition), the effect lasted about 2 hours and was gone by the third, so entries happen at the open only. This was not measured on the three-condition rule itself.

### What didn't help (kept as recorded context, not filters)
| Idea | Result |
|---|---|
| Open interest falling during the push | 60.5% on BTC, but **failed replication** on ETH/SOL/XRP (52.9% pooled) |
| Spot and perps both pushed | 57% then 52% (faded) |
| Time of day, weekday, clustering of hours | direction patterns flipped on unseen data |
| Funding rate, funding hours | flipped between periods |
| Last-15-minute flow | no improvement |
| ETH moving the same way | no lift for this rule |
| US stock-open hour, 08:00 UTC expiry | small, consistent effects; samples too small to act on |
| Scheduled macro-news hours | the move is about twice as big, but too few signals to judge direction |
| Kronos agreeing (tested on the wider flow rule) | only 85 overlapping hours: inconclusive |

### Risks
- **Small sample.** About 20 bets a month, so a 60% month and a 50% month are both normal noise.
- **Decay.** Measured on the wider flow-push rule, not on the three-condition rule: it lost about 2.4 points a year (95% range 0.8 to 4.1), from 60% in late 2023 to about 52% through 2025–26, then back up to 56% on 351 hours in 2026-H2. On the three-condition rule, walk-forward by year was 59%, 55%, 58%.
- **Priced in?** Untested whether Polymarket's price at the open already leans against the last hour. Paying 53¢ for a 57% bet is fine; paying 58¢ is not.
- **Fees.** Paying the ask needs about 52.25% just to break even.
- **The 60% target** is not reached historically. Progress toward it is tracked on the live record (hit rate and its 95% lower bound), with no automatic action.


### Sources

| What | Source | Evidence (under `research/hourly_btc_2026_09/`) |
|---|---|---|
| Skip hours that lack an extra signal on top of the prediction | Zayan (operator), 2026-09-14 | `README.md` |
| Look for oversold/overbought, momentum and RSI-type factors | Zayan (operator), 2026-09-14 | `README.md` |
| Aggressive (taker) order flow as that signal | Proposed by Claude, 2026-09-14, citing Kitron & Wengrowicz, arXiv 2608.21888; test approved and added to the strategy by Zayan (operator), 2026-09-14 | `literature/literature_factcheck_2026-09-13.json` |
| Imbalance, 168-hour z-score (sample stdev), flow push | Claude, 2026-09-14 | `recovered_verbatim/PREREG_orderflow.md`, `orderflow.py`; `session_commands/038` |
| Perps must not confirm (perp flow push ≤ 1.24) | Claude, 2026-09-14 | `PREREG_perp_selective.md`; `session_commands/043` ("spot-only push") |
| Close at the extreme (close location × direction > 0.80); close-location value as in Marc Chaikin's Accumulation/Distribution | Claude, 2026-09-14 | `PREREG_minute.md` rule R5; `session_commands/046` |
| Thresholds 1.20 and 1.24 (80th percentiles on 2023-10-12 to 2025-10-18 14:00 UTC); 0.80 | Claude, 2026-09-14 | `session_commands/046` (cutoffs line) |
| Plain "bet against last hour" 52.3% (7,924 validation hours) | Claude, 2026-09-14 | `session_commands/043` (all-hours reversal line) |
| Frequency 2.8% of hours; wider flow-push rule 53.5% on 19.4% of hours | Claude, 2026-09-14 | `session_commands/046` (share-of-hours column) |
| Discovery 57.7% (515); held-out 57.3% [50.7, 63.6] (220) | Claude, 2026-09-14 | `session_commands/046`; app code rerun 2026-09-16: `outputs_rerun_2026-09-16/check_app_rule_on_held_out_hours_out.txt` |
| Walk-forward 57.3% [53.3, 61.1] (620), by year 59/55/58% | Claude, 2026-09-14 | `PREREG_walkforward_oi.md` R1; `session_commands/050`; rerun `outputs_rerun_2026-09-16/walkforward_oi_out.txt` |
| By direction: Down 56.8% (303), Up 57.7% (317); latest window 60.9% / 55.3% | Claude, 2026-09-14 | `session_commands/054` |
| Decay 2.4 points a year and a 2-hour effect (wider flow-push rule) | Claude, 2026-09-14 | `session_commands/039` |
| Open-interest variant 60.5% (306), failed on ETH/SOL/XRP (52.9% pooled) | Claude, 2026-09-14 | `PREREG_walkforward_oi.md` R5, `PREREG_replication_alts.md`; `session_commands/050`, `053` |
| Factor results in "What didn't help" | Claude, 2026-09-14 | `PREREG_factors.md`, `PREREG_tierA_factors.md`, `PREREG_seasonality.md`, `PREREG_minute.md`; `session_commands/036`, `041`, `046`, `047` |
| Name | Zayan (operator), 2026-09-15 | — |

---

## How results are judged
The dashboard shows, per strategy and separately for paper and live:

- bets, win rate with its 95% range, average price paid, and P&L after fees;
- would-have-bet hours with their outcomes, plus `MISSED`, `BLOCKED` and `UNFILLED` counts;
- for the spot taker-push reversal strategy, the running win rate against the 60% line and the 52.25% break-even line.

Nothing switches a strategy off automatically. The operator reads the record and decides.
