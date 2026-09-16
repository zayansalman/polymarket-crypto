# Tsinghua-Kronos BTC 24h

The strategy name was chosen by Zayan (operator) on 2026-09-16, and this strategy trades Polymarket's daily **Bitcoin Up or Down** market.

**What it does:**
1. At noon New York time, it runs the Kronos team's own BTCUSDT forecast: Kronos-mini, the same setup and code they published every hour.
2. It gets the chance that BTC is higher 24 hours later.
3. It buys Up or Down when that chance beats the market price by the edge threshold.

Written 2026-09-16. The evidence and scripts are in `research/kronos_mini_official_btcusdt_demo/` on branch `research/kronos-mini-official-btcusdt-24h-demo-recreation`.

---

## The market

| | |
|---|---|
| Slug | `bitcoin-up-or-down-on-<month>-<day>-<year>`, named by the day it **ends** |
| Window | 12:00 America/New_York on day D to 12:00 on day D+1. Gamma `eventStartTime` is the window start, `endDate` the window end. On daylight-saving change days the window is 23 or 25 hours. |
| Resolves | **Up** if the close of the Binance BTC/USDT 1-minute candle "D+1 12:00 ET" is higher than the close of the "D 12:00 ET" candle, **Down** if lower, **50-50** if exactly equal |
| Listed | About 1 day before the window starts. Verified 2026-09-16: `bitcoin-up-or-down-on-september-17-2026` was listed 2026-09-15 16:14 UTC for a window starting 2026-09-16 16:00 UTC. |
| Fees | Taker `0.07 × p × (1 − p)` per share. Makers pay nothing and get a 20% rebate of taker fees (Gamma `feeSchedule`, verified 2026-09-16). |
| Order rules | Tick 0.01, minimum 5 shares |

Sources: the Gamma market rows and descriptions for the September 16 and 17, 2026 markets, read 2026-09-16 by Claude; `polymarket_bot/daily/market.py`.

---

## How it decides

1. **When.** At the start of the window S (noon ET). It waits until Binance has closed the 1-hour candle that ends at S.
2. **Input.** The 383 closed Binance spot BTCUSDT 1-hour candles before S, with open, high, low, close, volume and quote volume ("amount"). Timestamps are naive UTC open times. This matches the Kronos team's demo, which fetches 384 candles and drops the still-forming one.
3. **Model.**
   - Kronos-mini (`NeoQuasar/Kronos-mini`, snapshot f4e68697d9d5aed55cef5c96aabc3376bcad9f81) with `NeoQuasar/Kronos-Tokenizer-2k` (snapshot 26966d0035065a0cae0ebad7af8ece35bc1fb51c). Both run in eval mode on CPU with max context 512.
   - The model code is the demo repo's own `model/` package at commit eba16695 (2025-09-16, the attention-dropout fix), vendored unchanged under `third_party/kronos_demo_eba16695/` (MIT).
4. **Forecast.**
   - It samples **30** paths of the next **24** hours, with temperature 1.0, top_p 0.95 and top_k 0. The sampler is seeded with the window start hour, so a rerun gives the same numbers.
   - `P(up)` is the share of paths whose close 24 hours ahead is above the close of the last input candle, which is the price at S.
   - The paths and settings are fixed to match the published record this strategy is scored on.
5. **Bet.**
   - Up edge = `P(up) − Up ask`. Down edge = `(1 − P(up)) − Down ask`.
   - Buy the side with the larger edge if that edge is at least the **edge threshold**, a dashboard setting with default 0.05. Otherwise skip.
6. **Entry deadline.** A dashboard setting, default 300 s after S. With no entry by then, the day is recorded as `MISSED`.
7. **Hold to resolution.**
   - Settlement reads the two Binance 1-minute closes named in the market rules. A tie pays 0.50 a share on either side.
   - The market's own closes are one minute after the forecast's reference: the candle *opening* at 12:00 ET closes at 12:01. The forecast compares hourly closes at 12:00:00.
8. **Runs in whichever mode the operator selects at Start**, paper or live. It has one open position of its own (Zayan, 2026-09-14: "there is nothing such as paper mode only from now on, we will run what we run when we select mode").
9. **Safety.**
   - The model runs in a separate process with a minimal environment. It never gets the app's environment, which holds the wallet key.
   - It reads weights from a local folder and never downloads at decision time.
   - If the model can't run (torch not installed, weights missing, timeout), the day is recorded as `UNAVAILABLE` and nothing is bought.

## Recorded every day, bet or no bet

- `P(up)`, the number of paths, and the sampling error `sqrt(P(1 − P)/30)`. That error is about 9 points near 50%, which is more than the default edge threshold, so part of what crosses the threshold is sampling noise.
- The first and last input candle and the last close. The model and tokenizer snapshots and the vendored code commit. The worker's run time and the seed.
- The book at decision time, both edges, and the threshold.
- The side the published demo record was scored on: Up if `P(up) > 0.5`, Down if below.
- What happened to the entry, the two settlement closes, and the outcome (Up, Down or 50-50).

## What to expect (honest)

The Kronos team published this forecast every hour from 2025-07-11 to 2026-07-04. Scored against Binance for the Kronos-mini period after the 2025-09-16 fix (research branch above, `outputs/published_forecasts_score.txt`):

| Scored on | Right | 95% range |
|---|---|---|
| All hours, 24 hours ahead | 51.4% | 48.1–54.7% (6,673 forecasts) |
| Noon-ET forecasts only, this market's window | 52.2% | 46.3–58.1% (276 days) |

- **Calibration.** Confidence did not match outcomes. Whether it said about 9% or about 88%, BTC was higher 45–51% of the time. So its probabilities scored worse than always saying 50%.
- **Break-even.** Paying the ask at a 50¢ price needs about 52.25%. **The published record gives no reason to expect a profit.** It runs so its calls are measured against real daily prices.

## Sources

| What | Source |
|---|---|
| Kronos model family | Shi et al., arXiv 2508.02739 (Tsinghua University); github.com/shiyu-coder/Kronos, repo pointed to by Zayan 2026-09-16 |
| The forecast recipe (Kronos-mini, 383 candles, 30 paths, T 1.0, top_p 0.95, 24h, upside definition) | github.com/shiyu-coder/Kronos-demo `update_predictions.py` at eba16695; found by Claude 2026-09-16 |
| Use this forecast as a strategy on the daily BTC market, name "Tsinghua-Kronos BTC 24h" | Zayan (operator), 2026-09-16 |
| Bet only when the forecast beats the market price | Zayan (operator), 2026-09-13, for the Kronos strategies |
| Edge threshold default 0.05, decide at noon ET, entry deadline default 300 s, seed = window start hour, `UNAVAILABLE` handling | Claude, 2026-09-16 |
| Market rules, fee schedule | Gamma market rows, 2026-09-16 |
| Scored record | `research/kronos_mini_official_btcusdt_demo/`, Claude 2026-09-16 |
| One open position per strategy; runs in the selected mode | Zayan (operator), 2026-09-14 |
