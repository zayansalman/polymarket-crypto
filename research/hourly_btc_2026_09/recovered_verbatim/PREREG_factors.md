# Pre-registered: context factors vs the order-flow reversal (hourly BTC)

Written 2026-09-14 before running. Discovery: before 2025-10-18 14:00 UTC. Validation: after.

## Base signal (unchanged)
- SIG: fz_{H-1} > discovery 80th percentile (flow-pushed move). Bet against H-1's direction.
- ALT: |z_{H-1}| > discovery 80th percentile. Bet against the flow sign.

## Factors
All are known at H's open. "Factor at H" means hour H's candle, the one being bet on.
1. Funding: H starts at 00/08/16 UTC, or H-1 did (the flow bar spans a funding print).
2. Options expiry: H starts at 08:00 UTC (daily Deribit expiry). Subset: last Friday of the month.
3. US equity open: H contains 09:30 America/New_York (13:00 UTC in summer, 14:00 UTC in winter). Also regular US session vs off-hours.
4. Weekend: Saturday or Sunday UTC.
5. Big / liquidation-like H-1: H-1 range (high-low)/open above the trailing 168h 95th percentile AND volume above the trailing 168h 90th percentile.
6. ETH confirmation, from ETH's own H-1 imbalance z (168h):
   - ETH_SAME: ETH fz above the discovery 80th percentile, same direction as BTC.
   - ETH_NOT: otherwise.
   - Also: ETH H-1 return sign vs BTC H up (lead-lag), over all hours.
7. Macro releases: added only if the verified calendar arrives. H contains a CPI / jobs report (08:30 ET) or an FOMC statement (14:00 ET).

## Metrics
For each factor split (factor on / off): SIG win rate, ALT win rate, all-hours P(up), mean |move|. Discovery and validation, 95% Wilson intervals.

## Reading rule
- A factor "helps" only if SIG's win rate is higher when the factor is on in BOTH periods, with the gap's direction the same and the validation n reported.
- A factor "warns" if SIG is lower when on in both.
- No FDR claims. With n in the hundreds, these are leads to log, not gates.
