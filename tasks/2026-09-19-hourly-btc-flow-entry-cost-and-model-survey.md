# Hourly BTC taker-volume rule — entry cost at the open, and a model survey (parked 2026-09-19)

Backlog issue: #262

The question asked (2026-09-14): is there anything in `quant-model-library`, in Qlib, or in any
machine-learning model / neural net / Hugging Face repo that lifts the hourly BTC rule — and is
there anything for the other Polymarket books?

**Short answer: no model lifted it. The useful finding is the measured entry price.** The market
already leans against the last hour at the open, and after the taker fee the rule is worth roughly
break-even to +1.5c per share.

## The rule these numbers refer to

Binance BTCUSDT spot and USD-M perp hourly klines, decided at the open of UTC hour H from closed
hour H-1 and the trailing 168h:

- `imbalance = 2 * taker_buy_volume / volume - 1`, z-scored over 168h → `flow z`
- `flow push (fz) = flow z * sign(close - open)`
- `CLV = (2*close - high - low) / (high - low)`
- Bet against H-1 when **spot fz > 1.20**, **perp fz ≤ 1.24**, and **CLV > 0.80** after an up hour
  or **CLV < −0.80** after a down hour.

Called "Tier A" in the scripts, and written up as strategy 2 in
`docs/strategies/hourly-btc-strategies.md` on `feature/hourly-btc-strategies`.
The wider rule referred to below is **spot fz > 1.20 alone**.

## How much to trust this file

Read this as a lead, not as a settled result:

- A 15-agent research run was launched on 2026-09-14. All six source sweeps stalled and the
  synthesis hit the monthly spend limit. **One** agent — the gap-filler — completed, and it ran the
  measurements below itself.
- Its scripts and outputs lived in a session scratchpad under `/private/tmp` that has since been
  cleared. **Nothing here is reproducible from saved code.** The numbers were reported by that agent
  and are not independently re-run.
- The full raw report is kept beside this file at
  `tasks/notes/2026-09-19-hourly-btc-flow-raw-findings.md`.

Anything acted on should be re-measured first.

## 1. What the market charges at H:00 (the finding worth keeping)

Coverage: 334 of 340 rule firings from 2025-06-08 to 2026-09-12 (6 early hours had no market).
Sources: Gamma series 10114, CLOB `/prices-history` (fidelity=1, which is the **midpoint** —
Up+Down summed to 1.000 on 97.3% of 8,297 minute pairs), and data-api `/trades` for taker fills in
the first 120 s. Binance-derived outcomes matched Polymarket's resolution 334/334.

- **Win rate over this window: 55.7% [50.3, 60.9].** Below the 57% planning number; the interval
  covers both.
- **The side the rule buys is already above 50c before the hour starts:**

  | | contrarian-side price |
  | --- | --- |
  | pre-open mid | mean 0.526, median 0.530 |
  | first print after the open | mean 0.531, median 0.535 (59% ≥ 0.53, 19% ≥ 0.55) |
  | taker VWAP, first 120 s | 0.534 |

- **Control:** 199 random non-firing hours sat at 0.517 pre-open / 0.521 at the open; non-Tier-A
  flow-push hours at 0.525 / 0.528. So the market prices a graded reversal on every hour, and prices
  a firing about 1c above an ordinary hour.
- **Break-even paying the ask** (mid + 0.5c, plus the `0.07·p(1−p)` fee) is **55.3%**. Measured value
  per share: **+0.1c** at the first post-open price, **+0.9c** at pre-open + 0.5c, **+1.5c** at taker
  VWAP — all with a standard error near 2.7c. At a 57% hit rate it would be about +1.7c.
- **Resting bids are adversely selected.** At 50c: filled within 120 s on 55% of firings, win rate
  **47.8% when filled vs 65.3% when not filled**, −1.2c per signal. At 49c: 48% filled, 46.6% win.
  At 52c: 72% filled, 52.5% win, +0.4c per signal. Fills arrive when BTC keeps going in H-1's
  direction. A free maker edge is not there.
- By side: Up bets 57.5% at mid 0.533; Down bets 54.0% at 0.529. By period: 2025-06..12 53.9%
  (n=154, no fees then); 2026 57.2% (n=180).
- **Exploratory, not pre-registered:** firings where the pre-open lean was already ≥0.53 won
  **59.9% [51.5, 67.7]** (n=137), versus 50.0% when the lean was ≤0.505 (n=84). Strong in 2026
  (62.5 vs 50.0), weak in 2025-H2 (55.4 vs 52.8).
- The app's own decision-time hourly book could not be cross-checked: no local database holds hourly
  Polymarket rows, and `venue_flow_hourly` / `venue_snapshot` are empty.

Caveats: "ask" is estimated as mid + 0.5c or taker VWAP, not the true best ask; fill logic ignores
queue position; pre-2026 hours had no taker fee and a 0.001 tick, with today's fee applied to all.

## 2. quant-model-library — one thing, and it is weak

The repo is a European-equities teaching library on Python 3.11 / numpy<2. **56 modules import
`src/data.py`, which commit `5d55aed` deleted**, so most of it does not run as-is;
`microstructure/`, `signal_generation/` and `stat_arb/` are empty.

Pre-registered regime-context test on 511 discovery + 220 held-out firings:

| Reading | Result |
| --- | --- |
| Bayesian online changepoint (`bayesian_changepoint.py`) run length on 168h of abs log returns | **passed the pre-set bar**: "stable ≥24h" won 59.6% (discovery) / 59.3% (held-out) vs 54.2% / 54.1% after a recent change. ~5 ms per firing. |
| 2-state HMM high-vol probability | flipped between periods |
| EWMA vol ratio terciles | not monotone, did not hold |

Three readings were tested and the intervals overlap heavily, so the changepoint result is one weak
pass. It would be a recorded context field (~40 lines of numpy), never a gate. Everything else —
`order_book_imbalance.py`, `kyle_lambda.py` (Xetra/Chi-X level-1 tapes), the OU/Kalman pair-spread
signals, the SPY boosting/LSTM/TFT demos, the Gaussian Kelly module — does not transfer.

## 3. Qlib — no

`microsoft/qlib` is alive (≈48.5k stars, pushed 2026-09-02, MIT; `pyqlib` 0.9.7). Fit is poor:
wheels stop at cp312 and the app runs 3.14; hard deps on mlflow/redis/pymongo/cvxpy/gym; equity
daily/1-minute calendars with no 24/7 crypto calendar; online serving is documented as next-trading-
day only; Alpha158 + cross-sectional LightGBM targets stock universes, not one binary bet ~20 times
a month. The only transferable idea is rolling retrain, which the existing ~20-line monthly
walk-forward already does.

## 4. Machine learning and Hugging Face — no lift

Pre-registered walk-forward, 2024-04 to 2026-09, 625 firings, 9 features (Brier, lower is better):

| Model | Brier |
| --- | --- |
| Constant base rate | **0.2460** |
| L2 logistic on past firings | 0.2543 |
| Logistic on all flow-push hours | 0.2473 |
| LightGBM depth 2 | 0.2476 |

In every model the top-probability tercile won **less** than the bottom one (e.g. 55.3% vs 59.8%).
Nothing beat the base rate.

- **TabPFN** — cards opened, not run. v2 checkpoints are ungated (Prior Labs License, Apache-2.0 +
  attribution) but the card asks for 16 GB+ RAM; the current default **TabPFN-2.5 weights are gated
  under a non-commercial licence that forbids internal commercial decision-making**, so a v2
  checkpoint would have to be pinned. Low expected value given the above.
- **Zero-shot forecasters** (`amazon/chronos-bolt-small`, `google/timesfm-2.5-200m-pytorch`,
  `Salesforce/moirai-2.0-R-small` — the last is CC-BY-NC, research only) all forecast price paths.
  The effect here is a sign/flow phenomenon; a candle-native BTC fine-tune already scores 49–50% on
  direction. No reason to expect lift.
- **Mechanism paper: arXiv 2608.21888** (183 Binance pairs vs 187 US stocks, matched, out of
  sample). Crypto reversal concentrates after taker-flow-driven moves, grows with flow imbalance, is
  flat against depth consumed / funding / basis / volatility, and fades 15m → 1h → 4h (AUC 0.536 /
  0.521 / 0.496). BTC's yearly skill drifts down. This matches what was already measured here,
  including the ~2h snapback window and the decay. Code: `github.com/nadav2/short-horizon-reversion`
  (MIT). The authors disclose they trade a variant.
  - Free recorded context it suggests: the liquidation share of H-1's taker volume, which the flow
    recorder already captures.

## 5. Other books

**ETH / SOL / XRP hourly** (Polymarket series 10117 / 10122 / 10123, live since 2025-06-09, all
resolving on the Binance 1h candle):

- **The frozen rule did not replicate held-out:** ETH 50.7% (n=138), SOL 51.1% (141), XRP 49.2%
  (132), pooled **50.4% [45.6, 55.2]** — despite ETH discovery at 59.9%. That is a robustness
  warning for BTC, where the same CLV + spot-only refinement is what is being traded.
- The **wider flow-push rule did** replicate: ETH 53.2%, SOL 54.9%, XRP 53.3%, against all-hours
  reversal of 52.4 / 51.5 / 51.2.
- Those books already lean 0.52–0.53 at the open and median volume is only $5–12k, so taker entry is
  around break-even (ETH needs ~55.6% vs 53.2% measured). DOGE/BNB/HYPE hourly series exist since
  2026-03-04 and are thinner still ($2.4–11.5k daily).

**Event books** (measured by others, not here):

- arXiv 2609.12878, 588M Polymarket trades: favourites ≥90c earn **+0.64%** (Crypto) / **+1.04%**
  (Politics) pre-fee; longshots ≤10c lose 6–19%; **Sports shows no bias**. Favourites bought via
  posted offers +0.64% vs accepted offers −0.46% — i.e. maker-only, since the taker fee at p=0.9
  eats most of it.
- arXiv 2602.19520, 292M Kalshi+Polymarket trades: Polymarket politics prices are **underconfident**
  (calibration slope 1.31; Sports 1.08, Crypto 1.05). Sample ends 2025-12-31.

## 6. Datasets worth remembering

| Dataset | What it holds | Why |
| --- | --- | --- |
| `aliplayer1/polymarket-crypto-updown` (MIT) | BTC/ETH/SOL/BNB/XRP/DOGE/HYPE on 5m/15m/1h/4h: markets, mid prices, on-chain + WS fills, **best bid/ask**, spot | The only route to a *true* ask at H:00 instead of mid+0.5c. BTC 1h orderbook part-0 alone is 637 MB; card last updated 2026-04-26; its pipeline repo 404s. `tools/offline_replay.py` already loads this repo. |
| `vgregoire/polymarket-users` (CC-BY-4.0) | All reconciled end-user trades 2022-11 → 2026-03-29, with maker/taker, PnL, 1d/1h/5m OHLCV | Source behind arXiv 2609.12878; lets the favourite-longshot claim be replicated rather than trusted |
| `TimeSeventeen/Polymarket-v2` (CC-BY-4.0) | V2 `OrderFilled` logs, updated daily | Covers the period after April 2026 |
| `trentmkelly/polymarket_crypto_derivatives`, `kachoio/polymarket-5-minute-crypto-up-down-markets` | 15m/5m book snapshots (~100 ms and per-second) | Would test the same entry-cost question at 15m, where the reversal is strongest |

Prior null result to respect: on BTC **15m** books, a 43-feature Binance flow model scored AUC
0.8377 vs the Polymarket mid's 0.8405 out of sample, and quotes reacted to 5bp Binance moves in a
median 347 ms (arXiv 2607.26245, `gregyoung14/openmarket-btc-polymarket`). The short books already
embed Binance flow.

## If this is picked up again

In rough order of value:

1. Re-run the entry-cost measurement from scratch and keep the script in the repo this time — it is
   the only result that changes what to pay.
2. Record, on every firing the app sees: contrarian-side pre-open mid, ask at H:00, and lean vs 50c,
   next to the outcome. That turns the historical claim into a live one.
3. Record taker-at-ask vs resting-at-52c vs resting-at-50c side by side, with fill flags, to confirm
   or kill the adverse-selection number live.
4. The ≥0.53 pre-open-lean split and the changepoint run-length field, both recorded only.
5. Leave Qlib, TabPFN and the zero-shot forecasters alone unless something else changes.
