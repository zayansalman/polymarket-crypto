# Reproduction, 2026-09-16

Rerun by Claude on 2026-09-16 after the scratch folder was wiped, on freshly downloaded data
pinned to the original date range (`scripts/fetch_btcusdt_1h_spot_perp_klines_and_bybit_oi.py`).

## Data

| File | Rows then (2026-09-13) | Rows now | Size then | Size now |
|---|---|---|---|---|
| `btc_1h_flow.csv` | 25,627 (to 2026-09-13 18:00) | 25,627 | 2,983,415 bytes | 2,983,415 bytes |
| `btc_perp_1h_flow.csv` | 25,628 (to 2026-09-13 19:00) | 25,628 | 2,753,111 bytes | 2,753,111 bytes |
| `bybit_oi_1h.csv` | 25,631 (to 2026-09-13 22:00) | 25,631 | 1,127,727 bytes | 1,127,727 bytes |

"Then" comes from `session_commands/037`, `042`, `049` and a file listing printed on 2026-09-14.
SHA-256 of today's files: `outputs_rerun_2026-09-16/data_sha256.txt`.

## Results

| Number | Original | Rerun | Same? |
|---|---|---|---|
| Walk-forward rules R1–R8, every line (incl. R1 57.3% [53.3, 61.1] n=620 and R5 60.5% n=306) | `session_commands/050_…_od5yeD.output.txt` | `outputs_rerun_2026-09-16/walkforward_oi_out.txt` (`scripts/walkforward_oi.py`, path change only) | Yes, line for line |
| Held-out rule result: 220 bets, 57.27% | `PREREG_minute.md` R5 → `session_commands/046` | App code (`polymarket_bot/hourly/mean_reversion.py`): 220 bets, 126 wins, 0.5727 (`outputs_rerun_2026-09-16/check_app_rule_on_held_out_hours_out.txt`) | Yes |

Not rerun: the Kronos backtest (needs torch and hours of CPU on the operator's 8 GB Mac), and the
scripts that need the 15-minute, ETH/SOL/XRP and funding files; their original outputs are in
`session_commands/`.
