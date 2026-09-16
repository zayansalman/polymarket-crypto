# Pre-registered: seasonality and clustering of hourly BTC win rates

Written 2026-09-14 before running.

## Data
Binance spot BTCUSDT 1h, 2023-10-19 14:00 to 2026-09-13 13:00 UTC (25,440 hours).
- Discovery: before 2025-10-18 14:00 UTC (17,490 hours). All patterns are found here.
- Validation: from 2025-10-18 14:00 UTC (7,920 hours). Only used to check patterns found in discovery.

## Per-hour outcomes
- up: close > open.
- Reversal win: the bet against the previous hour's direction wins.
- Move size: |close - open| / open.
- Volume share.

## Slices
Hour of day (UTC), hour of day (New York time, since Polymarket labels hours in ET and US sessions follow DST), day of week, weekend vs weekday, hour x weekday (168 cells), Binance funding hours (00/08/16 UTC).

## Tests
1. Each slice cell, discovery only: two-sided binomial test of its up rate vs the discovery base rate, and of its reversal win rate vs the discovery reversal base rate. Benjamini-Hochberg FDR at q = 0.10 within each slice family.
2. Stability: Spearman correlation across cells between discovery and validation, for up rate, reversal win rate, and move size. Move size is the positive control: known volatility seasonality should replicate.
3. Clustering: k-means (scipy kmeans2, 50 inits) and Ward hierarchical clustering on standardized discovery-period profiles. Profiles are the 24 UTC hours and the 168 hour x weekday cells, using up rate, reversal win rate, mean move size, volume share, and mean return. k = 2..5, chosen by silhouette. Report cluster membership and each cluster's win rates in discovery vs validation.
4. Honest skip-hours test: using discovery only, pick the hours (top quartile of cells) where (a) betting the discovery-favored side and (b) the reversal bet did best. Apply that exact rule to validation and compare with betting every hour. 95% Wilson intervals throughout.

## Bar
A seasonal edge counts only if it passes FDR in discovery AND holds the same direction in validation with a validation interval that excludes the all-hours rate. Anything else is reported as not established.
