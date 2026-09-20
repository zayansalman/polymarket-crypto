# Pre-registration: lc2004 Kronos BTCUSDT 1h fine-tune on 24-hour forecasts

Written by Claude on 2026-09-17, before any 24-hour forecast was run.

## Question

Zayan (operator), 2026-09-17: "this is trained on hourly data and we tested it on hourly but how
does it perform on 24 h predictions".

The earlier hourly test (2026-09-13, `research/hourly_btc_2026_09/recovered_verbatim/backtest.py`)
found 49.3% on 500 unseen hours for the next hour's direction.

## What is tested

| Item | Setting | Source |
|---|---|---|
| Model | `lc2004/kronos_base_model_BTCUSDT_1h_finetune` @ `eb51e682c8194a1ba7254cc4357c75819683fbaf` | Zayan (operator), 2026-09-13 |
| Tokenizer | `lc2004/kronos_tokenizer_base_BTCUSDT_1h_finetune` @ `b8f1c795b80231f5bdeb69dfe71fc1542d2111fc` | same |
| Model code | github.com/shiyu-coder/Kronos `model/` @ `67b630e` (MIT), as vendored in `third_party/kronos_67b630e` on branch `feature/tsinghua-kronos-btc-24h-daily-btc-market` | Kronos team (Tsinghua), arXiv 2508.02739 |
| Training data end | last training candle opens 2025-10-18 13:00 UTC | Claude's review of the fine-tune repo, 2026-09-13 |
| Input | the 512 closed Binance spot BTCUSDT 1h candles before the anchor; open, high, low, close, volume, quote volume; UTC timestamps | fine-tune training log (lookback 512, predict window 48) and the author's `btc_1h_prediction.py`, github.com/Liucong-JunZi/Kronos-Btc-finetune @ `2eef54e` |
| Sampling | T 1.0, top_k 0, top_p 0.9; 30 independent paths (`predict_batch` over 30 copies, `sample_count=1`); both modules in eval mode | top_p and T from the author's script; path count from the Kronos team's live BTCUSDT 24h demo; eval mode and independent paths from the 2026-09-13 hourly test |
| Anchors | 12:00 America/New_York on every date from 2025-10-18 to the last date whose next-day noon has passed (2026-09-15 at writing, 333 days) | the window of Polymarket's daily BTC Up/Down market |
| Horizon | K hourly steps to the next noon ET: 24, or 23/25 across a clock change | |
| Forecast | p_up = share of the 30 paths whose close at step K is above the last close before the anchor | same definition as the Kronos team's demo |

## Outcomes

- **Primary:** Polymarket's daily rule. Up if the Binance BTCUSDT 1-minute close at noon ET on D+1
  is above the one at noon ET on D. Exact ties settle 50-50 and are left out.
- **Secondary:** the model's own target, the hourly close at step K vs the last close.

## Reading the primary result

- The call is Up when p_up > 0.5 and Down when p_up < 0.5. Calls of exactly 0.5 are left out.
- Hit rate with a Wilson 95% range. Noon-to-noon windows don't overlap, so each day counts once.
  With about 333 days, the range is about ±5.4 points wide.
- The model shows direction skill only if the low end of the range is above 50%.
- A bet also has to cover the taker fee and the spread, so a usable edge needs the low end clearly
  above about 52-53%.

## Also reported (descriptive, no pass/fail)

- Brier score against always saying 50%, and AUC.
- Calibration table.
- Confident calls only: |p_up − 0.5| ≥ 0.2, 0.3 and 0.4.
- Baselines on the same days: always Up, follow the last 24 hours, bet against the last 24 hours.
- What p_up leans on: Spearman correlation with the last 24-hour move, the last hour's move and hourly RSI(14).
- Hit rate at each forecast hour 1-24 (all anchors).
- A same-day comparison with the Kronos team's published Kronos-mini 24h forecasts at noon ET
  (Tsinghua-Kronos BTC 24h, published up to 2026-07-03).

## Fixed in advance

- One run with the settings above. No reruns with other settings to pick a better number.
- Anchors run in a spread-out order, so partial results cover the whole period. Interim numbers are
  reported with their sample size. The answer uses all days.

## Known limits

- The model works on hourly candles. Its last close is the price at 12:00:00 ET, while the market
  uses the 12:00 1-minute close, 60 seconds later. The same gap applies at the end.
- With 30 paths, each p_up carries a sampling error of about ±9 points.
- top_p is 0.9 here and was 1.0 in the hourly test, because this test follows the author's own
  forecast script.
