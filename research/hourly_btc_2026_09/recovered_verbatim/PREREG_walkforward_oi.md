# Pre-registered 2026-09-14 (written before running): walk-forward strict tiers + open-interest factor

Data: Binance spot/perp BTCUSDT 1h with taker volume; Bybit BTCUSDT linear OI 1h snapshots (timestamp = hour start), 2023-10..2026-09.
Target: bet against H-1 spot direction; win if H closes the other way (tie = not up).

Walk-forward: for each calendar month M from 2024-04 to 2026-08, all quantile thresholds are computed on hours BEFORE M only (expanding). Out-of-sample = union of all M.

Features (known at H's open unless marked):
- spot fz, perp fz (168h imbalance z x move sign), CLV of H-1.
- dOI_lag: z (168h) of log(OI[H-1]/OI[H-2]) (OI change during H-2; strictly available at H:00).
- dOI_now: z (168h) of log(OI[H]/OI[H-1]) (OI change during H-1; Bybit publishes ~1-2 min after H, so would need a delayed entry).

Rules (8, fixed):
R1 Tier A: spot fz > q80, perp fz <= q80(perp), CLV beyond +-0.8 in move direction
R2 Strict A90: spot fz > q90, perp fz <= q80, CLV beyond +-0.9
R3 Strict A95: spot fz > q95, perp fz <= q80, CLV beyond +-0.9
R4 Tier A & dOI_now > 0      R5 Tier A & dOI_now < 0
R6 Tier A & dOI_lag > 0      R7 Tier A & dOI_lag < 0
R8 Flow push (spot fz > q80) & dOI_now < 0 (move pushed while OI fell = forced closing)

Report per rule: OOS win rate, Wilson 95%, n; same for the validation window (>= 2025-10-18 14:00); per-year split.
Benjamini-Hochberg FDR (q=0.10) over the 8 OOS tests vs 52.3% (all-hours reversal rate).
Bar (60% goal): OOS point >= 60%, lower bound >= 55%, n >= 150, AND validation-window point >= 58% with the same direction. Otherwise not met.
