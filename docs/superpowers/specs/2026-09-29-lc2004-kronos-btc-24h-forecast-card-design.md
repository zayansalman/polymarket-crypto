# lc2004-Kronos BTC 24h forecast card — design

| | |
|---|---|
| What the operator asked | Zayan (operator), 2026-09-29: "this doesnt need to trade for me actually just show its prediction in the regime overview or somewhere in the dashboard and ill go manaulla place a trade" |
| Design and build | Claude, 2026-09-29 |
| Research recipe | `research/kronos_lc2004_btcusdt_1h_finetune_24h_horizon/PREREG.md` (Claude, 2026-09-17), on the branch `research/kronos-lc2004-btcusdt-1h-finetune-24h-horizon` |
| Earlier design (trading version, not built on develop) | `docs/superpowers/specs/2026-09-22-lc2004-kronos-btc-24h-design.md` on the same branch |

The model's forecast for Polymarket's daily BTC Up/Down market, shown on the dashboard so the
operator can decide and trade by hand. **Display only.** Nothing in this work places, cancels or
simulates an order. It has no paper or live leg, no sizing, no risk gate leg and no strategy
switch. It is not a strategy: it is not in `ems/strategies.py`, `ems/inventory.py`, MY
STRATEGIES or `docs/strategies/`. The regime overview the operator mentioned is not on
develop (PR #283 went to main), so the forecast is its own card.

## The market

Polymarket's daily BTC Up/Down market: Gamma series 41, slug
`bitcoin-up-or-down-on-<month>-<day>-<year>` for the resolution date.

- **Window:** 12:00 ET on day D to 12:00 ET on D+1. That is 23 or 25 hours across a clock
  change; every instant is built from America/New_York noon.
- **Strike:** the close of the Binance BTCUSDT 1m candle that opens at 12:00 ET on D.
- **Settle:** the same candle at 12:00 ET on D+1.
- **Result:** Up if settle > strike, Down if settle < strike, an exact tie pays 0.50 to both.

## The model

- `lc2004/kronos_base_model_BTCUSDT_1h_finetune` at
  `eb51e682c8194a1ba7254cc4357c75819683fbaf` and
  `lc2004/kronos_tokenizer_base_BTCUSDT_1h_finetune` at
  `b8f1c795b80231f5bdeb69dfe71fc1542d2111fc`, read from the standard Hugging Face cache.
- The MIT Kronos model code is vendored unchanged at `third_party/kronos_67b630e`.
- It runs in an isolated worker process (`python -I -B`, a minimal environment, the Hugging
  Face hub offline), one at a time across every app process: the worker inherits a locked
  `worker.lock` and holds it until it exits. On a timeout or a cancel the worker is killed with
  its whole process group; if the app dies first (kill -9, its terminal closed), the worker
  notices within a second and kills its own group. The app never imports torch.

## The maths

Every hour, once the last 1h candle has closed and the strike's 1m candle has closed:

1. Read the 512 most recent closed Binance BTCUSDT 1h candles (open, high, low, close, volume,
   quote volume).
2. Sample 30 price paths: temperature 1.0, top_p 0.9, top_k 0, in chunks of 15, on 4 CPU
   threads. The seed is the last closed candle's hour number since 1970, so a rerun of the same
   hour draws the same paths.
3. Horizon K = (window end − last candle close) / 3600 hourly steps: 24 from the noon run,
   1 from the 11:00 ET run, 23 or 25 across a clock change.
4. k = the paths whose step-K close is strictly above the strike.
5. P(Up) = q_up = (k + 1) / 32 (Laplace's rule: a uniform prior on each path's chance, so
   0 of 30 reads 1/32 and 30 of 30 reads 31/32, never certainty). The raw share is k/30 and its
   sampling error sqrt(p(1 − p)/30).

That sampling error covers only the noise of drawing 30 paths, not any error in the model.

## What runs

`ems/lc2004_kronos_btc_24h/runner.py`, started in the dashboard lifespan after Kelly
horse-race. It waits 15 s on the stop event before its first pass (so an app started and
stopped at once never starts a forecast), then passes once a minute, waking a few seconds after
each hourly close. Each pass:

1. **Settings switch** `lc2004_forecast_enabled` ("Run the lc2004-Kronos BTC 24h forecast", on
   by default). Off: a running forecast is cancelled (its worker killed) and nothing else runs.
2. **Window and market:** the current noon-to-noon window; its market from Gamma, once per
   window, again a minute later while Gamma does not list it. A failed Gamma request is an
   error on the card, not "not listed yet".
3. **Strike:** the noon 1m close, once that minute has closed. A failed read after that is an
   error on the card, not "waiting for the noon candle".
4. **Books:** both outcomes' best bid and ask from the CLOB `/book`, every pass, for the card.
5. **Forecast:** when the weights are in place, the strike is known, no row is stored for
   (window, last closed 1h candle) and none is running, one background task runs
   `forecast.run_and_store` with this pass's book tops. A pass never waits for the model; the
   loop wakes when it ends. Retries are timed from the failure: Binance has not closed the
   hour yet, 20 s later; another failure before the model, a minute later; the model ran but
   its row could not be stored, not until the next hour (the card shows what the model said),
   so the model never runs twice for one hour.

Every step is guarded: a failure is logged and shown on the card, never raised out of the loop.
Each forecast is one row of `lc2004_forecasts` (`ems/db.py`), unique on (window, candle); a
failed run keeps its error text, so the card can say why there is no number.

## What the card shows

`ems/dashboard/panels/lc2004_kronos_btc_24h.py`, a pure `render(...) -> str`, after the Kelly
horse-race card. Title "lc2004-Kronos BTC 24h forecast", then "Display only — places no
orders."

- **Problems, in plain words, never hidden:** switched off in Settings; missing weights with
  the one-line fix; a failed run with its error (a missing library such as einops adds: set
  KRONOS_PYTHON to a Python that has torch and einops, then restart); waiting for the noon
  candle, for the market listing or for Binance's hourly candles; the last error.
- **Headline:** "Model: Up 78% (24 of 30 paths end above the strike)", from q_up, with Down,
  the raw share and the sampling error in small text. If the newest run failed, the headline
  stays on the newest run that worked and says so.
- **Market now:** Up and Down bid/ask to the market's tick (0.55, or 0.004 once the tick is
  0.001), and for each side the model's fair value minus the ask in cents ("model 0.78 vs ask
  0.55, +23c"). A measurement, not a rule; colour shows only the sign.
- **The window:** market link (`https://polymarket.com/event/<slug>`), strike, the last BTC
  close the model used, settle time and time left (ET), hours ahead (K).
- **Spread of the 30 paths:** 10th percentile, median and 90th percentile final close, each
  against the strike.
- **Forecast:** updated at (ET), how long the worker took, and "next forecast after HH:MM ET"
  or "running since HH:MM ET".
- **This window, hour by hour:** time (ET), hours left, paths above, P(Up), and the Up and
  Down asks at that time.

The data comes from `execution_view.lc2004_forecast_data()`: the window's newest forecast and
its history, the loop's status, the switch and a fresh weights check. A failed read becomes
`load_error` and is shown on the card.

## One-time setup

1. Fetch the weights once (about 425 MB, into the Hugging Face cache):
   `python3 tools/fetch_lc2004_kronos_weights.py`
2. Point the worker at a Python whose torch, pandas, einops, safetensors, huggingface_hub and
   tqdm import under `python -I` (which ignores the user site). On the operator's Mac einops is
   only in the user site, so set `KRONOS_PYTHON` in `.env` to an absolute path, for example a
   venv made with `python3 -m venv --system-site-packages ~/kronos-venv` and
   `~/kronos-venv/bin/pip install einops`. Restart the app after changing it.

## Known limits

- **One minute off at both ends.** The model's last input is the 1h candle closing at
  11:59:59 ET, and its step-K close is a 1h candle closing at 11:59:59 ET on D+1. The market's
  strike and settle are 1m closes at 12:00:59 ET. The forecast ignores that minute.
- **30 paths.** The sampling error is about ±9 points near 50% (sqrt(0.25/30)); the Laplace
  mean keeps 0/30 and 30/30 away from certainty but does not remove that noise.
- **Minutes on CPU.** A forecast takes minutes on the operator's Mac (estimated 2-4 minutes at
  4 threads, unmeasured; the limit is 15 minutes), so each hour's number arrives a few minutes
  after the hourly close. The card shows when it was made and the book at that time.
- **Source checkout only.** The vendored model code is read from `third_party/` in the source
  tree; a non-editable install would not include it.
