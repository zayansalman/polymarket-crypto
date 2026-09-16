# Pre-registered 2026-09-14 (before downloading alt data): replication of the frozen BTC rule on ETH, SOL, XRP

Rule frozen exactly as walkforward_oi.py R5 (no re-tuning): for each asset, spot fz > q80 (walk-forward, own history),
perp fz <= q80 (own perp), CLV beyond +-0.8 in H-1 move direction, and own Bybit linear OI log-change during H-1 z <= 0
(R5 used z < 0); bet against H-1. Same months 2024-04..2026-08, same tie rule.
Also report the mirror (OI rose) and plain Tier A for each asset.

Replication criterion (per asset and pooled across the three): OOS win rate >= 57% with 95% lower bound above that
asset's all-hours reversal rate, and OI-fell win rate > OI-rose win rate. Pooled result is the headline.
