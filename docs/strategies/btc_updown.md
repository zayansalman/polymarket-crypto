# BTC Up/Down loop

<!-- BEGIN GENERATED:strategy -->
| | |
|---|---|
| Name | BTC Up/Down loop |
| Key | `btc_updown` |
| Status | cannot trade |
| Switch | `btc_updown` on the MY STRATEGIES card |
| Code | `polymarket_bot/paper.py` |
| Code fingerprint | `4e17e45c8430` |
<!-- END GENERATED:strategy -->

## What it does

A loop that runs while the operator holds Start. Every 5 seconds it finds the current BTC 5-minute Up/Down market, reads the Chainlink settlement feed and both order books, and journals a log-normal fair value and the executable edge. It never enters: since the v0 strategy was archived on 2026-09-13, every tick sets no side and journals `skip: no strategy loaded`. It prices `btc-updown-5m-*` windows whatever market the header selector shows.

## How it was formed

- **2026-05-21, origin not recorded.** The repo's first commits are this loop (`eebee55`, `f6dd738`): a local BTC 5-minute Polymarket paper-trading demo, sized $1–$5 by confidence (`PRD.md` at `f6dd738`). The log-normal fair value, then on 90 Binance 1-second closes, the 5-second tick and the 0.045 edge floor are all in the first commit. The repo does not record who designed the model.
- **2026-05-24.** The pricing maths moved to `polymarket_bot/strategy.py`, and a backtest over the operator's own trade history set the loop's defaults: the 0.045 edge floor kept, the confidence floor cut from 0.62 to 0.50, and a 60-second late-entry cutoff (`fb68e44`; see the BTC 5m offline backtest doc).
- **2026-06-10 to 06-17.** Live execution behind a boot gate (`80f2b47`). Chainlink became the source for reference, spot and volatility, quotes came from the CLOB book, and paper fills paid the spread (`e8565b4`, #21, #22). Hold-to-resolution became the default after a scalp-style soak lost $7.87 over 135 trades in 70 minutes (`79754e1`, #28). A 0.07 edge cap and a 0.50 entry-price floor followed a 26-hour soak (n=225, −14.4% ROI) in which larger claimed edges lost more (`92019ef`, #29). Paper and live began sharing one risk gate (`a842304`, #64). Orders below the venue's 5-share minimum were raised to it (`b1a38aa`, #87).
- **2026-07-02.** Fee-true booking (`0abd2fd`, #133). Venue records for the live period 2026-06-15 to 06-24 showed 333 buys, +$6.27 before fees, −$23.51 in taker fees and −$17.24 net (`docs/archive/POSTMORTEM_2026-07.md`).
- **2026-07-10.** The loop was stopped after the post-freeze segment measured −$0.41 per trade over n=37 at a 48.6% win rate (`docs/archive/TIMELINE.md`). The project reopened on 2026-08-04 (header of the same file).
- **2026-08-29, Zayan (operator).** Stopped all 5-minute market work (`27f38fd`, #182).
- **2026-09-13, Zayan (operator).** Shut down the v0 strategy to make room for new ones. Its entry gates, model picker, auto-pause, calibration and parameter tuner were archived, and the loop kept running with no strategy loaded (`8ece0bd`, PR #226, `docs/archive/v0-strategy.md`; code kept at tag `archive/v0-strategy`).
- **2026-09-19.** The STRATEGIES card gave it the `btc_updown` switch (`7a6fc41`, #249). The shadow forward-tester that ran on each tick was removed (`deb3c22`, #252). PR #244, which removes the 5-minute family from the code, was opened the same day and is still open.
- **2026-09-21.** The 5-minute option left the header selector and `market_selection.LOOP_SUPPORTED` became empty (`2fc57ce`, PR #265). The loop's dashboard-reporting half was deleted (`21c468d`, PR #267). That commit notes the loop cannot enter because the snapshot hardcodes no side, not because `LOOP_SUPPORTED` is empty.

## How it works

Pressing Start runs `run_paper_loop()`, which calls `paper_tick_once()` every `paper_tick_seconds` until Stop. Each tick builds a snapshot, journals it to `paper_ticks`, closes anything due, then tries an entry.

**Market** (`_fetch_current_market`). With $t$ the Unix time, the window start is $t_0=t-(t\bmod 300)$. It asks Gamma `/markets`, then `/events`, for the slug `btc-updown-5m-{t0}`, trying $t_0$, then $t_0+300$, then $t_0-300$. Time left is $T=t_0+300-t$. The header's market selection is only handed to the market-data hub (`_marketdata_hub.want`); it does not change which market is priced. On 2026-09-21 the stored selection was btc / 15m.

**Inputs** (`_build_snapshot`):

- Order books: best bid, best ask and their sizes for the Up and Down tokens, from the hub's stream when fresh, else CLOB `/book` (`_fetch_clob_book`).
- $S$ is the latest Chainlink BTC/USD print from the `ws-live-data` WebSocket if it is under 15 seconds old, else a REST poll (`_rest_spot_fallback`).
- $K$ is the window's Chainlink open from the `crypto-price` REST API, cached per window (`_get_window_reference`).
- $\sigma$ is the standard deviation of 1-second Chainlink log returns when the series has at least 30 points. Otherwise it is taken from 90 Binance 1-second BTCUSDT closes, using their returns only, never their levels. Failing both, it is the floor 0.00002 (`_sigma_with_fallback`).

If spot or reference is missing, the tick is marked degraded: fair value is pinned at 0.5 and no edge is journaled.

**Fair value** (`strategy.fair_up_probability`). These markets resolve Up when the close is greater than or equal to the open, so the chance of an exact tie is added to Up:

$$z=\frac{\ln(S/K)}{\sigma\sqrt{\max(T,1)}},\qquad w=\frac{g/K}{\sigma\sqrt{\max(T,1)}}$$

$$p_{\text{up}}=\min\Big(0.995,\ \max\Big(0.005,\ \Phi(z)+\min\big(\varphi(z)\,w,\ 0.45\big)\Big)\Big)$$

Here $g=0.01$ is the Chainlink print step in dollars (`PRINT_GRANULARITY_USD`), and $\Phi$ and $\varphi$ are the standard normal CDF and density.

**Edges.** Against the executable asks, $e_{\text{up}}=p_{\text{up}}-a_{\text{up}}$ and $e_{\text{down}}=(1-p_{\text{up}})-a_{\text{down}}$, each only when that side's book has an ask and is not crossed. The larger one is journaled as `edge`.

**Decision.** None. `_build_snapshot` sets the side to `None`, confidence to 0 and notional to 0 on every tick, with the reason `NO_STRATEGY_REASON` or the degraded-feed reason. `_maybe_open_position` then returns at once. `docs/archive/v0-strategy.md` names this block as the slot a new strategy fills.

**What a new strategy would inherit.** All of it is still reached on every tick (`21c468d`):

- Entry (`_maybe_open_position`): the `btc_updown` switch; no entry while any position is open; in the default `settle` style, at most one entry per window; a fill at the side's best ask; shares equal to notional divided by the ask, raised to the venue minimum of 5 and capped at the top-of-book size; the shared `RiskGate` in paper and the `LiveExecutor` in live.
- Exit (`_close_due_positions`): in `settle` style the position is held. When the window rolls, `_close_rolled_position` settles it at 1.00 or 0.00 from the Chainlink `crypto-price` endpoint. The `scalp` style adds time, take-profit, stop-loss and edge-collapse exits.

## Parameters

| Parameter | Default | Where it came from |
|---|---|---|
| `paper_tick_seconds` (SETTINGS card) | 5 s | first commit, `eebee55` |
| `exit_style` (SETTINGS card) | `settle` | #28, `79754e1` |
| `CHAINLINK_STALE_SECONDS` | 15 s | #21, `e8565b4` |
| `PRINT_GRANULARITY_USD` | 0.01 | #21, `e8565b4`; the observed Chainlink print step |
| Chainlink points needed for $\sigma$ | 30 | `e8565b4` |
| Binance fallback for $\sigma$ | 90 one-second closes | first commit, `eebee55` |
| Venue share minimum | 5 | #87, `b1a38aa` |

## Evidence so far

- `paper_ticks` on 2026-09-21: 254 ticks from 2026-08-30 to 2026-09-20, every one on a `btc-updown-5m-*` window. 178 journaled `skip: no strategy loaded`. The table's earliest row is 2026-08-30.
- `paper_positions` on 2026-09-21: 0 rows. `polymarket_bot/inventory.py` records the same, 254 ticks and 0 positions.
- The v0 strategy's trading from June and July 2026 is recorded in `docs/archive/POSTMORTEM_2026-07.md` and `docs/archive/TIMELINE.md` (figures above), not in these tables.

## Known weaknesses

- It prices the retired 5-minute family, not the operator's selection. `LOOP_SUPPORTED` is empty, but `paper.py` never reads it.
- Pressing Start runs the full feed, tick and settlement machinery with nothing to decide (`21c468d`).
- In paper mode a settled position books shares times payout minus entry, with no taker fee (`_close_position`). Live books the executor's fee-net figure (#133). A paper result would overstate PnL by $0.07\,a(1-a)$ per share.
- The switch defaults to on (`Strategy.default` in `strategies.py`), while its own description says to leave it off.

## Sources

- Code: `polymarket_bot/paper.py` (`run_paper_loop`, `paper_tick_once`, `_build_snapshot`, `_fetch_current_market`, `_sigma_with_fallback`, `_maybe_open_position`, `_close_rolled_position`, `_close_position`, `NO_STRATEGY_REASON`); `polymarket_bot/strategy.py` (`fair_up_probability`); `polymarket_bot/market_selection.py`; `polymarket_bot/strategies.py`; `polymarket_bot/runtime_knobs.py`; `config.py`; `polymarket_bot/inventory.py` (record and verdict).
- Tables `paper_ticks`, `paper_positions` and `config` in `data/btc_5m_binary_fair_value.db`, read-only query on 2026-09-21.
- `PRD.md` at `f6dd738`; `docs/archive/v0-strategy.md`; `docs/archive/POSTMORTEM_2026-07.md`; `docs/archive/TIMELINE.md`; git tag `archive/v0-strategy`.
- Issues #21, #22, #28, #29, #64, #87, #133, #182. PRs #226, #244 (open), #249, #252, #265, #267.
- `eebee55` 2026-05-21 — Build crypto trading ops demo
- `f6dd738` 2026-05-21 — Strip demo to BTC 5m paper trading
- `fb68e44` 2026-05-24 — Add BTC strategy backtest optimizer
- `80f2b47` 2026-06-10 — Add live execution mode: Polymarket CLOB orders with hard risk limits
- `e8565b4` 2026-06-11 — Data integrity: CLOB executable quotes, Chainlink settlement feed, honest fills
- `79754e1` 2026-06-11 — Add settle-style strategy profile: one entry per window, hold to resolution
- `92019ef` 2026-06-13 — Add anti-adverse-selection entry filters: edge cap + favorites only
- `a842304` 2026-06-15 — Unified RiskGate: paper is now a faithful preview of live (refs #64)
- `b1a38aa` 2026-06-17 — feat(#87): auto-bump sub-minimum orders to the venue share minimum
- `0abd2fd` 2026-07-02 — fix(#133): book venue taker fees at entry/exit/settlement — fee-true live PnL
- `27f38fd` 2026-08-29 — docs: branch close-out — pivot off all 5-minute markets (#182)
- `8ece0bd` 2026-09-13 — feat: archive the v0 strategy — loop runs with no strategy loaded
- `7a6fc41` 2026-09-19 — feat(dashboard): STRATEGIES card under FEEDS with a switch per strategy
- `deb3c22` 2026-09-19 — chore: remove the shadow forward-tester
- `2fc57ce` 2026-09-21 — feat(dashboard): ORDER SIZE is a small closable box, and 15m not 5m
- `21c468d` 2026-09-21 — chore: delete the dead dashboard-reporting half of paper.py + two tools
- Endpoints in the code: `https://gamma-api.polymarket.com`, `https://clob.polymarket.com/book`, `https://polymarket.com/api/crypto/crypto-price`, `wss://ws-live-data.polymarket.com`, `https://data-api.binance.vision/api/v3/klines`.

## Changelog

- 2026-09-21 · `4e17e45c8430` · Doc created.
