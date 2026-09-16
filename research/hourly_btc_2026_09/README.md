# Hourly BTC research, 2026-09-13 to 2026-09-15

Evidence behind the two hourly BTC strategies in `docs/strategies/hourly-btc-strategies.md`:

- **Kronos BTCUSDT 1h fine-tune (Hugging Face lc2004): next-hour Up chance vs Polymarket price**
- **Binance BTCUSDT 1h reversal after a spot taker-buy/sell push that perps didn't match, with the hour closing at its high or low**

The research ran in a temporary scratch folder that was wiped on 2026-09-16. Everything here was
rebuilt from the Claude Code session transcript by `tools/recover_from_transcript.py`
(Claude, 2026-09-16). `RECOVERY_LOG.md` lists the exact tool call each file came from.

## Layout

| Path | What it is |
|---|---|
| `recovered_verbatim/PREREG_*.md` | Test plans, each written before its test was run. |
| `recovered_verbatim/*.py` | The analysis scripts exactly as run (absolute scratch paths unchanged). |
| `session_commands/NNN_<UTC time>_<call id>.sh` + `.output.txt` | Every shell command of the research, in order, with the output it printed: data downloads, inline analyses and results. |
| `literature/literature_factcheck_2026-09-13.json` | Literature search and fact-check (Claude, 2026-09-13). |
| `literature/alphaxiv_sweep_2026-09-15_*.json` | alphaXiv sweep of 21 papers and its synthesis (Claude, 2026-09-15). |
| `plan_addendum_book_record_and_candle_audit_2026-09-15.md` | Plan for the book record and candle audit approved by Zayan (operator) on 2026-09-15. |
| `tools/recover_from_transcript.py` | The recovery script. |
| `scripts/` | Rerunnable copies (path changes only) and the 2026-09-16 re-download and app-code check. |
| `outputs_rerun_2026-09-16/` | Rerun outputs; compared in `REPRODUCTION.md`. |

The downloaded data files (CSV) were not recoverable, since they were never printed. The three
files the frozen rule was tested on were downloaded again on 2026-09-16 and came out identical in
row count and byte size; the walk-forward test and the app's own rule code reproduce the original
numbers exactly (`REPRODUCTION.md`). The other download commands are in `session_commands/`.

## Who decided what

Dates are the operator's local dates (Asia/Dhaka, UTC+6); the session log times are UTC.

| Decision | Source | Session time (UTC) |
|---|---|---|
| Use a Hugging Face model trained on crypto price history | Zayan (operator), 2026-09-13 | 2026-09-13 05:50 |
| Use the BTCUSDT 1h Kronos fine-tune for Polymarket's hourly BTC market | Zayan (operator), 2026-09-13 | 2026-09-13 05:57 |
| Bet Up/Down when the next-hour forecast is above/below the Polymarket price, hold to resolution | Zayan (operator), 2026-09-13 | 2026-09-13 13:07 |
| Fine-tune training code: github.com/Liucong-JunZi/Kronos-Btc-finetune | link given by Zayan (operator), 2026-09-13 | 2026-09-13 13:25 |
| 512 input candles, 50 paths, temperature 1, top_k 0, top_p 1, 0.05 edge threshold | Claude, 2026-09-13 | from 2026-09-13 13:42 (`bench.py`, `backtest.py`) |
| Look for oversold/overbought, momentum and RSI-type factors | Zayan (operator), 2026-09-14 | 2026-09-13 18:41 |
| Skip hours that lack an extra signal on top of the prediction | Zayan (operator), 2026-09-14 | 2026-09-13 18:52 |
| Use aggressive (taker) order flow as that extra signal | Proposed by Claude, 2026-09-14, citing Kitron & Wengrowicz, arXiv 2608.21888; test approved by Zayan (operator), 2026-09-14 | 2026-09-13 19:53 / 19:57 |
| Add taker-flow reversal to the strategy | Zayan (operator), 2026-09-14 | 2026-09-13 20:17 |
| Imbalance, 168-hour z-score, flow push definitions | Claude, 2026-09-14 (`PREREG_orderflow.md`) | 2026-09-13 19:58 |
| Perps must not confirm the push | Claude, 2026-09-14 (`PREREG_perp_selective.md`, "spot-only push") | 2026-09-13 20:35 |
| Hour must close at its high/low (close-location value, as in Marc Chaikin's Accumulation/Distribution line) | Claude, 2026-09-14 (`PREREG_minute.md`, rule R5) | 2026-09-13 20:38 |
| Thresholds 1.20 / 1.24 (80th percentiles of flow push on discovery data) and 0.80 | Claude, 2026-09-14 | 2026-09-13 20:38 onward |
| Drop the open-interest filter after it failed on ETH/SOL/XRP | Claude, 2026-09-14; final strategy reviewed with Zayan (operator), 2026-09-14 | 2026-09-13 22:29; 2026-09-14 03:15 |

## Evidence map

| Claim in the strategy doc | Where it comes from |
|---|---|
| Kronos fine-tune: 49–50% direction on 500 unseen hours, confidence not calibrated | `recovered_verbatim/backtest.py`, `analyze.py`; runs and printed results in `session_commands/020`–`030`, `034`, `035` |
| Discovery 57.7% n=515; held-out 57.3% [50.7, 63.6] n=220 | `PREREG_minute.md` R5 → `session_commands/046_…_Ko35aB.output.txt` (rule R5; same numbers as R7) |
| Walk-forward 57.3% [53.3, 61.1] n=620; by year 2024 59%, 2025 55%, 2026 58% | `PREREG_walkforward_oi.md` R1 → `session_commands/050_…_od5yeD.output.txt` |
| Walk-forward by direction: Down 56.8% n=303, Up 57.7% n=317; latest window Down 60.9% n=110, Up 55.3% n=103 | `session_commands/054_…_rduPZk.output.txt` |
| Open-interest-fell variant 60.5% n=306, then failed replication (pooled ETH/SOL/XRP 52.9% n=677) | `PREREG_walkforward_oi.md` R5 → `050`; `PREREG_replication_alts.md` → `053_…_smnyGj.output.txt` |
| Decay −2.42 points per year (95% CI −4.06 to −0.78) and the effect lasting about 2 hours | `session_commands/039_…_DCKHbf` (inline analysis). **Measured on the wider flow-push rule (spot flow push above its 80th percentile), not on the frozen three-condition rule.** |
| Spot-only push 55.1% / 54.4%; perps-and-spot both pushed 57.3% then 51.8% | `PREREG_perp_selective.md` → `043_…_aDT9UZ.output.txt` |
| Factor splits (funding, 08:00 UTC expiry, US open, weekend, ETH) | `PREREG_factors.md` → `041_…_qGDNtg.output.txt`; `PREREG_tierA_factors.md` → `047_…_QFYxWP.output.txt` |
| Time-of-day / weekday clustering flipped on unseen data | `PREREG_seasonality.md` → `036_…_wNUMjH.output.txt` |
| Last-15-minute flow did not help | `PREREG_minute.md` R2–R4, R6 → `046` |

## Data

| File the scripts read | Download command |
|---|---|
| `btc_1h_full.csv`, `btc_1h_oos.csv` (Binance spot BTCUSDT 1h) | `session_commands/032`, `033` |
| `btc_1h_flow.csv` (spot 1h with taker-buy volume) | `session_commands/037` |
| `eth_1h_flow.csv` | `session_commands/040` |
| `btc_perp_1h_flow.csv`, `btc_funding.csv` (Binance USD-M perp) | `session_commands/042` |
| `btc_15m_spot.csv` (the 1-minute download hung; 15-minute bars were used) | `session_commands/044` (hung), `045` |
| `macro_calendar.csv` | `session_commands/048` (built from a research agent's calendar; the step that wrote the unverified copy read a workflow journal and is not included) |
| `bybit_oi_1h.csv` (Bybit linear open interest) | `session_commands/049` |
| ETH/SOL/XRP spot and perp flow, Bybit OI | `recovered_verbatim/fetch_alts.py`, run in `session_commands/052` |

A rerun against freshly downloaded data should pin each script's end date to the last hour the
original run used (2026-09-13 19:00 UTC for most files) so the numbers stay comparable.
