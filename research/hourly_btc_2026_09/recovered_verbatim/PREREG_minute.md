# Pre-registered: last-minutes flow and candle shape before the hour open

Written 2026-09-14 before the 1-minute data finished downloading and before any result was seen.

## Data
Binance spot BTCUSDT 1m klines with taker buy volume (2023-10-12 to 2026-09-13), aggregated per hour. Hourly spot/perp flow as before.
- Discovery: before 2025-10-18 14:00 UTC. Validation: after.
- Target: hour H up (close > open).

## Features (all from hour H-1 or earlier)
- L15: the last 15 minutes of H-1 (minutes 45-59).
  - m15 = close_59 / open_45 - 1.
  - imb15 = 2 * taker_buy / volume - 1.
  - z15 = z of imb15 vs the trailing 168 hourly values of the same last-15m window.
  - fz15 = z15 * sign(m15).
- CLV: close location of H-1 = (2*close - high - low) / (high - low), in [-1, 1].
- SIG: hourly spot fz > discovery 80th percentile (existing flow signal).
- SPOT_ONLY: spot fz top quintile and perp fz not top quintile.

## Rules (bet direction stated)
- R1 close at extreme: CLV > +0.8 after an up hour -> bet Down; CLV < -0.8 after a down hour -> bet Up.
- R2 last-15m flow-pushed: fz15 top quintile -> bet against m15's direction.
- R3 SIG and the last 15m continued the hour's direction with fz15 > 0 -> bet against the hour.
- R4 SIG and the last 15m already moved against the hour -> bet against the hour.
- R5 SPOT_ONLY and CLV extreme in the move's direction -> bet against the hour.
- R6 R2 and SIG, same direction -> bet against.
- R7 R1 and SPOT_ONLY -> bet against.

Quintile cutoffs come from discovery.

## Evaluation
Win rate per rule, discovery and validation, Wilson 95%, share of hours.

## Bar
Same as before: validation point >= 60%, lower bound >= 55%, n >= 150.
A rule must also be at least as good in discovery; selection happens on discovery only. Best-of-7 selection optimism is noted.
