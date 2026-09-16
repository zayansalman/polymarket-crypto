# Pre-registered: perp flow, funding, and a selective model aimed at 60%

Written 2026-09-14 before running.

## Data
Binance spot BTCUSDT 1h, Binance USD-M perp BTCUSDT 1h (both with taker buy volume), ETHUSDT spot 1h, BTCUSDT funding history. All 2023-10 to 2026-09, UTC.
- Discovery: before 2025-10-18 14:00. Validation: after.

## Target
Reversal win at hour H: H closes opposite to H-1's spot direction. Flat H-1 is dropped. Tie at H counts as "not up".

## Features at H (only H-1 and earlier)
1. s_fz: spot imbalance z (168h) x sign(spot H-1 move)
2. p_fz: perp imbalance z (168h) x sign(perp H-1 move)
3. div: (p_z - s_z) x sign(spot H-1 move). Perp-vs-spot flow divergence, in the move's direction.
4. basis: z (168h) of (perp close / spot close - 1) at H-1, x sign(move)
5. fund: z (last 90 prints) of the latest settled funding rate at H's open, x sign(move). Positive = longs crowded while price rose.
6. eth_same: ETH H-1 direction == BTC H-1 direction (0/1)
7. us_open: H contains 09:30 New York (0/1)
8. exp08: H starts 08:00 UTC (0/1)
9. weekend (0/1)
10. size: H-1 |move| / trailing 24h mean |move|, log1p

## Univariate checks (discovery vs validation, Wilson 95%)
- Reversal win by quintile of p_fz.
- Confluence: s_fz and p_fz both top quintile.
- Spot-only push: s_fz top quintile and p_fz not.
- Perp-only push: p_fz top quintile and s_fz not.
- Crowding: fund top quintile.

## Selective model (fixed)
- Logistic regression, numpy, L2 = 1.0 on standardized features, the 10 features above, no interactions.
- Threshold: fit on the first 80% of discovery; on the last 20% of discovery pick the smallest t in {0.52..0.70 step 0.01} where hours with P(rev) >= t or P(rev) <= 1-t hit >= 60% with n >= 150. If none qualifies, use the t with the best hit rate subject to n >= 150, and say 60% was not reached in-sample.
- Bets: reversal if P >= t, continuation if P <= 1-t, skip otherwise.
- Scheme A, static: refit on all discovery, apply t to validation.
- Scheme B, rolling: refit at the start of each validation month on all prior hours (expanding), same t.
- Report validation hit rate, 95% CI, share of hours bet, per-half-year hit rate, and a luck check (probability that 50% or 52.3% true skill produces the observed hit rate or better at that n).

## Bar
The 60% goal counts as met only if the validation lower 95% bound is >= 55% AND the point estimate is >= 60% on n >= 150. Anything less is reported as not met, with the best honest number.
