# Chainlink-vs-Binance lead-lag study

<!-- BEGIN GENERATED:strategy -->
| | |
|---|---|
| Name | Chainlink-vs-Binance lead-lag study |
| Key | `chainlink_lead_lag` |
| Status | offline only |
| Switch | none — nothing to turn on |
| Code | `tools/chainlink_lead_lag.py` |
| Code fingerprint | `086bb5abe0f6` |
<!-- END GENERATED:strategy -->

## What it does

An offline study of how Chainlink's BTC/USD price trails Binance's BTC/USDT. It measures the price gap between them, how long Chainlink takes to follow a sharp Binance move, and how often the direction of Chainlink's next change matches Binance's recent move. It is not a trading rule. It was run when it was built on 2026-06-15 and the results were written into the commit and issue #57, but no output file exists now.

## How it was formed

- **Origin not recorded.** Issue #57 was opened on 2026-06-15 as one of six research issues (#55 to #60) filed within two minutes. It was scoped as groundwork for #55, a shadow feed that would predict Chainlink's next print from exchange feeds, and planned to pull Chainlink's on-chain `AnswerUpdated` events from Polygon.
- **2026-06-15.** `127a3f7` built the tool on a Hugging Face dataset instead, because it already held about 2.2 M Chainlink and 2.2 M Binance prints at roughly one per second. Merged in `a1891af`; #57 closed the same day.
- **What it fed.** The result was used to keep the `entry_edge_max = 0.07` cap, the original BTC 5-minute strategy's rule that rejected signals whose edge looked too large to be real. It also backed #55 and led to #65, a regime-aware cap. Both were closed unbuilt when the project was archived on 2026-07-11.
- **Since.** The strategy that applied the cap was archived on 2026-09-13 (`8ece0bd`) and the BTC 5-minute family was removed on 2026-09-19 (`23e8d33`). The cap now survives only as `PAPER_ENTRY_EDGE_MAX` in `config.py` and in the offline replay.

## How it works

- **Input.** `load_paired_btc_spots()` downloads `data/spot_prices/part-0.parquet` from the dataset, keeps symbol `btc/usd` from source `chainlink` and `btcusdt` from `binance`, takes the last price per source per second, and inner-joins on the second. Seconds where either feed is missing are dropped. With $C_t$ and $B_t$ the two prices in row $t$, `run()` computes:
- **`static_gap_bps`:** $g_t=10^4\,|C_t-B_t|/B_t$ in every row; p50, p90, p99 and mean.
- **`reaction_lag`:** a trigger is any row where Binance moved at least 10 bps over the previous 5 rows. From there it walks forward up to 60 rows for the first row where Chainlink has moved at least half as far in the same direction. It reports the p50, p90 and mean lag in seconds, and the share of triggers never matched.
- **`next_print_predictability`:** for every row where Chainlink changed, it compares the sign of $C_t-C_{t-1}$ with the sign of $B_t-B_{t-10}$, skipping rows where Binance did not move, and reports the share that agree.
- **`regime_breakdown`:** the rolling 300-row standard deviation of Binance returns, split at its median into calm and volatile; gap p50 and p90 in each.
- **Output.** JSON at `data/lead_lag/latest.json` plus a printed summary. Flags: `--move-threshold-bps`, `--move-window-s`, `--lookback-s`, `--max-seconds`, `--output`.

## Evidence so far

One run, on 2.16 M paired seconds (about 25 days), 2026-06-15 (`127a3f7`, #57):

- Gap: p50 2.5 bps, p90 4.6, p99 9.2. Calm p90 4.1, volatile p90 5.9.
- Reaction lag: p50 9 s, p90 42 s; 44% of triggers were never matched.
- Sign agreement: 62.7% over 1.67 M compared changes, where 50% would mean Binance tells us nothing.

## Known weaknesses

- The sign test is not a clean lead. The Binance window $B_t-B_{t-10}$ ends in the same second as the Chainlink change $C_t-C_{t-1}$, so the two moves overlap.
- The 5-, 10-, 60- and 300-row windows count rows, not seconds. After the inner join, rows are not always one second apart.
- The dataset's Chainlink series is about one print per second, not the on-chain `AnswerUpdated` history #57 asked for. The #57 comment says that pull is still needed for exact heartbeat timing.
- The docstring's "rejects ~44% of live signals" describes a June bot that no longer exists.

## Sources

- `tools/chainlink_lead_lag.py`; `polymarket_bot/inventory.py` (record and verdict lines).
- Dataset: [aliplayer1/polymarket-crypto-updown](https://huggingface.co/datasets/aliplayer1/polymarket-crypto-updown), `spot_prices` config (cited in the code as `HF_REPO`).
- Issue #57 — scope, and the 2026-06-15 comments with the results and the switch from the Polygon pull to the dataset.
- Issue #55 — the shadow feed this was groundwork for; closed at archive 2026-07-11.
- Issue #65 — regime-aware `entry_edge_max`, filed from these results; closed at archive 2026-07-11.
- `127a3f7` 2026-06-15 — Chainlink-vs-Binance BTC lead-lag analysis (refs #57)
- `a1891af` 2026-06-15 — Merge feature/57-chainlink-lead-lag into develop (refs #57)
- `8ece0bd` 2026-09-13 — feat: archive the v0 strategy — loop runs with no strategy loaded
- `23e8d33` 2026-09-19 — chore: remove the BTC 5-minute market family
- `polymarket_bot/strategy.py` (`StrategyParams.entry_edge_max`); `config.py` (`PAPER_ENTRY_EDGE_MAX`); `tools/offline_replay.py`.

## Changelog

- 2026-09-21 · `086bb5abe0f6` · Doc created.
