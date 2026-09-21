# BTC 5m offline backtest / replay

<!-- BEGIN GENERATED:strategy -->
| | |
|---|---|
| Name | BTC 5m offline backtest / replay |
| Key | `btc5m_backtest` |
| Status | offline only |
| Switch | none — nothing to turn on |
| Code | `polymarket_bot/backtest.py` |
| Code fingerprint | `57e70e269abb` |
<!-- END GENERATED:strategy -->

## What it does

An offline check of the archived BTC 5-minute pricing model against the operator's own past trades. It reads an exported Polymarket trade-history CSV, rebuilds each BTC Up/Down buy with Binance 1-second prices, and grid-searches entry filters by hold-to-resolution PnL. It is not a replay of recorded markets; that harness was deleted on 2026-09-21. The CSV it reads is not on disk, so today it has nothing to run on.

## How it was formed

- **2026-05-24, origin not recorded.** Added with the shared pricing module to tune the BTC paper loop (`fb68e44`). Its run kept the 0.045 edge floor, cut the confidence floor from 0.62 to 0.50 and added a 60-second late-entry cutoff, and those became the loop's defaults (`docs/BACKTESTING.md`; the `config.py` change in `fb68e44`). `docs/BACKTESTING.md` noted from the start that it only sees trades the operator made.
- **2026-05-24.** A separate full-market replay harness and market-data recorder were added in an agent commit (`277a84a`). `docs/BACKTESTING.md` records that nothing live ever used it.
- **2026-06-10.** Binance fetches got retries so that one timeout could not kill a 2,688-combination grid run (`9698bb7`, `CHANGELOG.md`).
- **2026-06-11.** The settle-style change cites a "+31% April backtest" of edge-filtered entries held to settlement (`79754e1`). The repo does not say whether that figure came from this module, or its n.
- **2026-06-16 to 06-23.** Issue #83 set a 71% backtest win rate (n=256) against 50.0% in live paper (n=354). Its closing comment traced the gap to take-profit exits booked by the replay harness (`btc_5m_fv/backtest/harness.py`), not to this module.
- **2026-09-13.** The strategy it tests was archived (PR #226). The loop no longer loads it.
- **2026-09-21.** The full-market harness, its metrics and the storage replay engine were deleted as unreachable (`6bde070`, PR #267). This file is what remains.

## How it works

`tools/backtest_btc_strategy.py` runs `build_report()` and saves it. The dashboard's backtest card only displays `data/backtests/latest.json` when that file exists (`_backtest_html` in `polymarket_exec/ops/dashboard/app.py`).

1. **Input** (`build_opportunities`). Rows of `HISTORY_CSV_PATH` (default `data/polymarket_history.csv`) with action `buy`, a market name containing "Bitcoin Up or Down", and token `Up` or `Down`. `parse_market_window` reads the window's start and end times from the market name. The price paid $a$ is `usdcAmount` divided by `tokenAmount`.
2. **Prices** (`BinanceWindowCache`). BTCUSDT 1-second closes from 120 seconds before the window to 2 seconds after it, cached under `data/backtests/binance_1s/`. $K$ is the close at the window start, $S$ the close at the trade time and $X$ the close at the window end. $\sigma$ is the standard deviation of log returns over the 90 seconds before the trade.
3. **Score.** $p_{\text{up}}$ comes from `strategy.fair_up_probability`, the same tie-inclusive log-normal the BTC loop used, here with the default 0.01 print step. The edge is $e=p_{\text{side}}-a$ and the confidence is $c=\min(0.99,\ 0.5+2.8\,|e|)$. The outcome is Up when $X\ge K$.
4. **Filter** (`_accepts`). A buy is kept when $e$ is at least the edge floor, $c$ is at least the confidence floor, the time left is above the cutoff, and the price paid is between 0.05 and the max entry price.
5. **Size** (`evaluate_params`). Notional runs from $1 to $5 by confidence (`strategy.notional_from_confidence`).
6. **PnL** per kept trade is $(\text{notional}/a)\,(w-a)$, with $w=1$ if the bought side won and 0 if not. No fee is charged.
7. **Grid** (`optimize_params`). Edge floor in {0, 0.02, 0.04, 0.045, 0.06, 0.08, 0.10, 0.12}, confidence floor in {0.50, 0.55, 0.60, 0.62, 0.65, 0.70, 0.75}, cutoff in {60, 90, 120, 150, 180, 210} seconds, and max entry price in {0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.90, 0.95}: 2,688 combinations. A combination needs at least $\max(8,\min(25,\lfloor 0.08N\rfloor))$ trades, where $N$ is the number of buys read. Each is scored $\text{PnL}-0.5\,\text{DD}+0.01\,n$, with DD the maximum drawdown and $n$ the trade count.
8. **Output** (`build_report`, `save_report`). The baseline (every buy as made), the loop's `PAPER_*` defaults, the recommended filter, the top 10 and the edge distribution, written to `data/backtests/latest.json`. `_select_recommended` picks the best-scoring combination with an edge floor of at least 0.045 and a cutoff of at least 60 seconds.

Not covered here: `tools/offline_replay.py` (#56) replays the same pricing on the Hugging Face dataset `aliplayer1/polymarket-crypto-updown`. It is a separate tool outside this family's tracked code.

## Evidence so far

- No result is on disk. `data/` is gitignored. On 2026-09-21 the main checkout had neither `data/polymarket_history.csv`, the path `.env` points to, nor `data/backtests/latest.json`. With no CSV, `build_opportunities` returns an empty list.
- The only recorded outcome of a run is the parameter choice in `docs/BACKTESTING.md` (2026-05-24). No n, PnL or ROI from this module is recorded in the repo.

## Known weaknesses

- It only sees trades the operator made, so it cannot measure skipped markets or fill quality (`docs/BACKTESTING.md`).
- Reference, spot and settlement all come from Binance. The live loop settled on Chainlink, measured about $50.7 below Binance (`config.py`, #21).
- PnL ignores the taker fee of $0.07\,a(1-a)$ per share.
- Filters are chosen and reported on the same trades, with no hold-out.
- The strategy it tests is archived, and the 5-minute market family was closed on 2026-08-29 (#182).

## Sources

- Code: `polymarket_bot/backtest.py`; `tools/backtest_btc_strategy.py`; `polymarket_bot/strategy.py` (`fair_up_probability`, `confidence_from_edge`, `notional_from_confidence`); `config.py` (`HISTORY_CSV_PATH`, `PAPER_*`); `polymarket_exec/ops/dashboard/app.py` (`_backtest_html`); `polymarket_bot/inventory.py`.
- `docs/BACKTESTING.md`; `CHANGELOG.md` (the retry entry and the settle-style entry).
- Issue #83 and its closing comment; issue #56; issue #182. PRs #226 and #267.
- Local check on 2026-09-21: the main checkout's `data/` folder and the `HISTORY_CSV_PATH` line in `.env`.
- `fb68e44` 2026-05-24 — Add BTC strategy backtest optimizer
- `277a84a` 2026-05-24 — feat(storage,backtest): market data recorder, replay, full-market backtest
- `9698bb7` 2026-06-10 — Fix runtime blockers: entrypoint import, Binance endpoint, backtest retries
- `79754e1` 2026-06-11 — Add settle-style strategy profile: one entry per window, hold to resolution
- `8ece0bd` 2026-09-13 — feat: archive the v0 strategy — loop runs with no strategy loaded
- `6bde070` 2026-09-21 — chore: delete the unreachable v0 architecture cluster
- Endpoint in the code: `https://data-api.binance.vision/api/v3/klines`.

## Changelog

- 2026-09-21 · `57e70e269abb` · Doc created.
