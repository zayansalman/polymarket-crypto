# Raw agent findings — hourly BTC taker-volume rule survey (2026-09-14 run, parked 2026-09-19)

Verbatim return value of the only agent that completed in the 2026-09-14 research run (the
gap-filler). The other six sweeps stalled and the synthesis hit the monthly spend limit. Its
scripts lived in a session scratchpad that has since been cleared, so none of this is
reproducible from saved code. Summary and caveats:
tasks/2026-09-19-hourly-btc-flow-entry-cost-and-model-survey.md

```text
I worked through all five gaps and ran four measurements myself. Scripts and outputs are in /private/tmp/claude-501/-Users-zayankhan-projects-polymarket-crypto/79b8bfd4-8e2b-495c-af6c-87675d9d24f0/scratchpad/pm. The input klines were the Binance hourly spot/perp files with taker volume that an earlier session had already saved in .../6f03044c-.../scratchpad/kronos-test. I downloaded no datasets or weights and edited no repo files.

(1) BIGGEST RISK: the Polymarket price is already partly leaning against the last hour.
- Coverage: every Tier A firing (fixed 1.20/1.24 thresholds, ties go Up) from 2025-06-08 to 2026-09-12 that has a market. That is 334 of 340 firings. Sources: Gamma series 10114, CLOB /prices-history (fidelity=1) and data-api /trades (taker fills, 0 to 120 s after H:00).
- Resolution check: my Binance-derived outcome matched Polymarket's resolution on 334/334.
- Win rate on these hours: 55.7% [50.3, 60.9].
- The side the rule bets on was already priced above 50c:
  - pre-open mid: mean 0.526, median 0.530;
  - first print after the open: mean 0.531, median 0.535 (59% of firings at 0.53 or more, 19% at 0.55 or more);
  - taker VWAP in the first 120 s: 0.534.
- Control, 199 random non-firing hours: the against-last-hour side sat at 0.517 pre-open and 0.521 at the open. Non-Tier-A flow-push hours sat at 0.525 / 0.528. So the market already prices a graded reversal, and prices Tier A about 1c more than an ordinary hour.
- Paying the ask (mid + 0.5c plus the 0.07·p(1−p) fee) needs 55.3% to break even. Measured value per share:
  - at the first post-open price: +0.1c (se 2.8c);
  - at taker VWAP: +1.5c (se 2.7c);
  - at the 57% planning win rate this would be about +1.7c.
- Resting bids at 49–52c filled within 120 s in 48–78% of firings, but the fills are selected against you:
  - win rate when filled: 46.6–54.0%;
  - win rate when not filled: 61.6–65.3%;
  - value per signal: −1.2c to +1.6c (a sure-fill 50c bid lost 1.2c per signal).
- prices-history is the midpoint: Up+Down summed to 1.000 on 97% of 8,297 minute pairs, and 72% of prices sit on a half-cent.
- Exploratory, not pre-registered: firings where the pre-open lean was already 0.53 or more won 59.9% [51.5, 67.7] (n=137) at 0.525–0.545, versus 50.0% when it was 0.505 or less (n=84). 2025-H2 showed little split (55.4 vs 52.8); 2026 showed a large one (62.5 vs 50.0).
- The app's own decision-time hourly book cannot be cross-checked yet. No local database (main plus three worktrees) holds any hourly Polymarket rows, and venue_flow_hourly and venue_snapshot are empty.

(2) quant-model-library.
- It is a European-equities teaching repo on Python 3.11 with numpy<2. 56 modules import src/data.py, which commit 5d55aed deleted, so most method/signal/portfolio scripts cannot run as-is. The execution/tca modules need an equities tape. microstructure/, signal_generation/ and stat_arb/ are empty.
- Pre-registered regime-context test on 511 discovery and 220 held-out firings:
  - EWMA vol ratio: failed.
  - 2-state HMM: flipped between periods.
  - BOCPD run length (the bayesian_changepoint.py recursion on a trailing 168h window, 5 ms per firing): passed the pre-set bar. 'Stable 24h or more' won 59.6% (disc) / 59.3% (held-out), versus 'regime change in the last 24h' at 54.2% / 54.1%. Confidence intervals overlap and 3 readings were tested.

(3) Qlib (microsoft/qlib, about 48.5k stars, pushed 2026-09-02, MIT; pyqlib 0.9.7 from 2025-08-15). Poor fit:
- wheels only up to cp312 (the app runs on 3.14);
- equity daily/1-minute calendars;
- online serving is documented as next-trading-day only;
- needs mlflow/redis/pymongo.
The only transferable idea is rolling retrain, and the operator's roughly 20-line monthly walk-forward already does that.

(4) ML. Pre-registered walk-forward from 2024-04 to 2026-09 on 625 Tier A firings:
- Constant base rate: Brier 0.2460.
- L2 logistic on past firings: 0.2543.
- Logistic trained on all flow-push hours: 0.2473.
- LightGBM depth 2: 0.2476.
- In every model the top-probability tercile won less than the bottom one (e.g. 55.3% vs 59.8%).
Result: no lift. TabPFN, Chronos-Bolt, TimesFM 2.5 and Moirai 2.0 cards were opened and are dead ends here. Mechanism literature: arXiv 2608.21888 finds crypto reversal is concentrated after taker-flow-driven moves, grows with flow, fades from 15m to 1h and is gone by 4h, and BTC's yearly skill drifts down. This matches the operator's findings.

(5) Other books.
- Polymarket runs hourly ETH/SOL/XRP series (since 2025-06-09) and DOGE/BNB/HYPE (since 2026-03-04), all resolving on the Binance 1h candle.
- Frozen Tier A did NOT replicate held-out: ETH 50.7% (n=138), SOL 51.1% (141), XRP 49.2% (132), pooled 50.4% [45.6, 55.2]. ETH discovery had been 59.9%.
- The wider flow-push rule did replicate: 53.2% / 54.9% / 53.3%, versus all-hours reversal of 52.4% / 51.5% / 51.2%.
- Those alt books already lean 0.52–0.53 at the open, and median market volume is only $5–12k.
- Non-crypto books, measured anomalies:
  - arXiv 2609.12878, 588M trades: favorites at 90c or more earn +0.64% (Crypto) / +1.04% (Politics) pre-fee; Sports shows none.
  - arXiv 2602.19520: Polymarket politics prices are underconfident (calibration slope 1.31).

Overall: nothing in the libraries or models lifts Taker Snapback. The valuable output is the measured entry cost. The edge is thin after cost: roughly break-even to +1.5c per share as a taker. Maker fills are adversely selected.

=== CANDIDATES 9

## Measured Polymarket entry cost at H:00 for Tier A firings (CLOB prices-history + data-api taker fills) | https://clob.polymarket.com/prices-history ; https://data-api.polymarket.com/trades ; /private/tmp/claude-501/-Users-zayankhan-projects-polymarket-crypto/79b8bfd4-8e2b-495c-af6c-87675d9d24f0/scratchpad/pm/analyze_pm.py (data: pm_firings.jsonl, pm_firings_summary.csv, control: pm_control.jsonl) | data-source | taker-snapback | effort S | high
  what: Public Polymarket endpoints, keyed per market through Gamma series 10114:
- prices-history gives the per-minute midpoint;
- data-api /trades gives taker fills with side, outcome, price and size.
I used them to reconstruct the price of the contrarian side at the open of every Tier A firing from 2025-06 to 2026-09.
  how: (a) Recorded context: store three extra fields per hour, from the book the app already snapshots at decision time. Add a running 'hit rate minus average price paid' line next to the 52.25% line.
- contrarian-side pre-open mid;
- ask at H:00;
- lean vs 50c.
(b) Parallel recorded variants run side by side:
- 'taker at ask' vs 'resting bid at 52c' vs 'resting bid at 50c', each with fill flag, fill price and outcome. The measured adverse selection on resting bids (filled 47.8% win vs unfilled 65.3% at 50c) needs live confirmation.
- 'Tier A when pre-open lean >= 0.53' as an exploratory recorded-only split.
(c) Operator-choosable entry-price cap. The historical numbers say an ask near 0.55 or above is about break-even at a 57% hit rate.
  fit: Pre-open mid and best ask are both on the book before or at H:00. It is one book read the app already does, microseconds of compute, and nothing heavy.
  evidence: Opened and run this session. 334 firings (6 early-June-2025 hours had no market); Polymarket resolution matched the Binance-derived outcome 334/334.
- Win rate: 55.7% [50.3, 60.9].
- Contrarian mid: pre-open mean 0.5257 (median 0.530); first post-open print 0.5305 (median 0.535, 59% at 0.53 or more, 19% at 0.55 or more); taker VWAP 0–120 s 0.534.
- Control, 199 random non-firing hours: 0.517 pre / 0.521 open. Flow-push non-Tier-A: 0.525 / 0.528.
- Value per share after fee: +0.13c at (first print + 0.5c), +0.88c at (pre-open + 0.5c), +1.45c at taker VWAP; all se about 2.7c.
- Resting bids, 'sure' fill (trade-through) within 120 s:
  - 49c: 48% filled, filled win 46.6%;
  - 50c: 55% filled, filled win 47.8% vs 65.3% unfilled, −1.2c per signal;
  - 51c: 65% filled;
  - 52c: 72% filled, filled win 52.5%, +0.36c per signal.
  'Maybe' fills (at the level) were up to +1.6c per signal at 52c.
- By bet side: Up 57.5% at mid0 0.533; Down 54.0% at 0.529.
- By period: 2025-06..12 53.9% (n=154, fees off); 2026 57.2% (n=180, feesEnabled on 131).
- Pre-open lean bucket (exploratory): 0.525–0.545 won 59.9% n=137; 0.505 or less won 50.0% n=84.
- prices-history is a midpoint: Up+Down=1.000 on 97.3% of 8,297 minute pairs, 71.7% of prices on half-cents.
  caveats: - The rule's win rate over this window (55.7%) is below the 57% planning number; the CI still includes both.
- Before 2026 markets had no taker fee and a 0.001 tick; today's fee was applied to all hours to show current cost.
- 'Ask' is estimated as mid+0.5c or taker VWAP, not the true best ask. Real bid/ask history would need the aliplayer1 BBO subset or live recording.
- Resting-bid fill logic ignores queue position and size ('sure' = a trade through the level, 'maybe' = a trade at the level).
- The lean-bucket split was not pre-registered.
- The app's own decision-time hourly book cannot be cross-checked yet: no rows exist in any local database.

## BOCPD run length since the last volatility regime change (quant-model-library) as recorded context | /Users/zayankhan/projects/quant-model-library/src/methods/regime_detection/bayesian_changepoint.py (bocpd recursion); test: /private/tmp/claude-501/-Users-zayankhan-projects-polymarket-crypto/79b8bfd4-8e2b-495c-af6c-87675d9d24f0/scratchpad/pm/regime_ctx.py + PREREG_regime_context.md | quant-model-library | taker-snapback | effort S | low
  what: Adams–MacKay online Bayesian changepoint detection with a Normal-Gamma prior and Student-t predictive. It returns the most probable 'run length', meaning how long the current regime has lasted.
  how: Recorded context only:
- at H:00, run the recursion on the trailing 168 h of |hourly log return|;
- use a prior from the first 120 h and hazard 1/168;
- store the run length and a 'regime changed within 24h' flag on every Tier A hour;
- show the running hit rate in each bucket.
It could help explain the year-to-year spread (59/55/58%) without gating anything.
  fit: Uses only closed hours through H-1. Median 5.1 ms, max 10.5 ms per firing, including an HMM fit I ran alongside. It needs about 40 lines of numpy; math.lgamma can replace scipy. The app would not import the library: its modules have a broken `data` import and pin Python 3.11/numpy<2.
  evidence: Pre-registered before running, fixed Tier A.
- Stable 24h or more: discovery 59.6% [54.3, 64.8] n=332; held-out 59.3% [50.8, 67.2] n=135.
- Recent change under 24h: discovery 54.2% [46.9, 61.3] n=179; held-out 54.1% [43.6, 64.3] n=85.
- It meets the pre-set bar (same bucket better by 4 points or more in both periods).
- The other two readings failed: the EWMA vol-ratio terciles were not monotone (high/low/mid 62.9/56.1/54.1 in discovery vs 57.0/52.2/62.2 held-out), and HMM high-vol flipped (59.0 vs 56.6 discovery, 56.5 vs 58.0 held-out).
  caveats: - 3 context readings were tested, so one pass is weak evidence.
- Confidence intervals overlap heavily.
- The library's bocpd allocates an (n+1)×(n+1) matrix: fine on 168 h, about 5 GB if run on full history.
- The run length depends on prior and hazard choices (fixed a priori here).

## Wider flow-push reversal on ETH/SOL/XRP hourly Up/Down books (parallel recorded variant; Tier A itself failed replication) | /private/tmp/claude-501/-Users-zayankhan-projects-polymarket-crypto/79b8bfd4-8e2b-495c-af6c-87675d9d24f0/scratchpad/pm/alts.py and alt_lean.py (Gamma series 10117 ETH, 10122 Solana, 10123 XRP) | paper-method | other-crypto-updown-books | effort M | low
  what: The same frozen flow math, run on each coin's own Binance spot and perp hourly klines. It covers Polymarket's hourly ETH, Solana and XRP Up/Down series, which resolve on the Binance ETH/SOL/XRP-USDT 1h candle with ties going Up.
  how: Mainly a robustness warning for BTC: the CLV plus spot-only refinement did not carry to the other coins held-out.
As a shadow book, record for ETH/SOL/XRP:
- the spot fz > 1.20 flow-push signal;
- the contrarian pre-open mid, ask and outcome;
- side-by-side taker vs resting-bid variants.
This roughly triples the flow-push observations and tests whether the reversal pays on thinner books.
  fit: Same inputs and compute as BTC (closed klines plus a 168 h rolling z-score). The app already fetches spot ETHUSDT but would need perp and SOL/XRP klines; each is one REST call at H:00.
  evidence: Fixed 1.20/1.24 thresholds, ties go Up; discovery before 2025-10-18 14:00.
- Tier A: ETH 59.9% (n=409) → 50.7% [42.5, 58.9] n=138; SOL 54.9% → 51.1% [42.9, 59.2] n=141; XRP 52.8% → 49.2% [40.9, 57.7] n=132.
- Pooled held-out Tier A: 50.4% [45.6, 55.2] n=411. Walk-forward pooled: 53.1% [50.5, 55.8] n=1383; ETH walk-forward 2026: 48.6%.
- BTC held-out Tier A for reference: 57.3% n=220. Only 43–59 of each coin's firings share an hour with a BTC firing.
- Flow push held-out: ETH 53.2% [50.6, 55.7] n=1439; SOL 54.9% [52.3, 57.4] n=1443; XRP 53.3% [50.5, 56.0] n=1286; BTC 53.5%. All-hours reversal: 52.4 / 51.5 / 51.2.
- Market lean on 268 randomly sampled held-out flow-push hours (resolution matched 100%): contrarian mid pre-open 0.521 / 0.516 / 0.519, at open 0.534 / 0.525 / 0.521. Median market volume $12.0k / $5.0k / $5.3k.
- Series exist since 2025-06-09. DOGE/BNB/HYPE hourly series started 2026-03-04 and are very thin (24h volume $2.4–11.5k).
  caveats: - At the measured open prices, taker cost is about break-even or negative: e.g. ETH needs about 55.6% vs 53.2% measured. Resting orders are fee-free but likely adversely selected, as on BTC.
- The 268 sampled hours happened to win only 45.3% [39.3, 51.4] against pool rates of 53–55%. That sampling bad luck makes their value estimate unusable; the price lean is the reliable part.
- The open-interest split already failed on these coins.

## Short-horizon crypto reversal mechanism and decay paper, plus its forced-liquidation test | arXiv 2608.21888 ; https://github.com/nadav2/short-horizon-reversion | paper-method | taker-snapback | effort S | medium
  what: A matched, out-of-sample study (Kitron & Wengrowicz, Aug 2026) of 183 Binance pairs vs 187 US stocks. It finds pervasive sign reversal in crypto that concentrates after aggressive taker-flow-driven moves. The authors read it as compensated liquidity provision and propose a forced-liquidation-flow test as the sharpest check.
  how: (a) Mechanism and decay context for the operator's notes. Reversal grows with taker imbalance, is flat against book depth consumed, funding, basis and volatility, fades 15m → 1h → 4h (matching the ~2h snapback), and shows mild downward drift in BTC's per-year skill.
(b) Recorded context the app can already produce, since it records Binance liquidations: log each Tier A hour's liquidation share of H-1 taker volume. Under the paper's liquidity-provision reading, liquidation-driven pushes should revert at least as much. If they revert less, that points to informed flow, one plausible cause of the decay.
(c) A pointer for other books: the effect is strongest at 15m.
  fit: The liquidation share of H-1 is aggregated before H:00 from the existing recorder, so compute is trivial. The paper itself needs no runtime.
  evidence: Opened via alphaXiv PDF queries.
- 15m out-of-sample AUC: BTC 0.533, ETH 0.538, XRP 0.536.
- Focal crypto mean AUC by horizon: 0.536 (15m) / 0.521 (1h) / 0.496 (4h).
- Flow-driven minus flow-opposed flip rate: +0.021 [0.015, 0.026].
- Depth-consumption contrast: −0.002 [−0.007, +0.004].
- Sign flip rate rises 50.2 → 53.0% across move-size deciles.
- Gross edge about 1.3 bp vs a 5 bp round trip on spot.
- The authors disclose that they run a trading system on a variant of the model.
- GitHub API: repo exists, MIT, pushed 2026-08-22, 0 stars.
  caveats: - The mechanism evidence is a conditioning, not identification.
- The study is 15-minute-centric and prices edge against spot trading costs, not Polymarket binary pricing.
- The imbalance lags added nothing beyond sign lags in their logit.
- The liquidation test on Tier A would have very small n (about 20 firings a month).

## aliplayer1/polymarket-crypto-updown best bid/ask and on-chain fills (for true ask, not mid, at H:00) | aliplayer1/polymarket-crypto-updown (HF dataset) | huggingface-dataset | all | effort M | medium
  what: An MIT-licensed dataset of Polymarket crypto Up/Down markets: BTC/ETH/SOL/BNB/XRP/DOGE/HYPE on 5m/15m/1h/4h. Subsets:
- markets;
- prices (CLOB price history, i.e. mid);
- ticks (on-chain OrderFilled plus WebSocket fills with Binance spot attached);
- spot_prices;
- orderbook (per-token best bid/ask with sizes).
  how: Replaces my mid+0.5c ask estimate with the real best ask and bid depth at H:00:00–H:02:00 for past firings. That tightens the entry-cost numbers and the resting-bid fill test. Repo code already loads this dataset (tools/offline_replay.py, HF_REPO = aliplayer1/polymarket-crypto-updown), so a 1-hour offline check can reuse that loader. The same subsets support 15m/4h/alt-coin book studies.
  fit: Offline, precomputed research only; nothing runs at decision time. The BTC 1-hour orderbook part-0.parquet alone is 637 MB, heavy for an 8 GB laptop unless read lazily with polars filtered by market_id and time.
  evidence: Opened the README and file tree this session.
- BTC 1-hour folders: orderbook 66 files (part-0 637.5 MB plus WebSocket shards named with ts 1777225446644 ≈ 2026-04-26); ticks 60 files (part-0 122 MB); prices 1 file (3.4 MB).
- HF overview says last updated 26 Apr 2026.
- The linked pipeline repo aliplayer1/polymarket-data-pipeline returns GitHub API 404 (re-checked).
- How far back the BBO history goes is unknown without reading part-0.
  caveats: - Start date of the orderbook history unverified.
- Stale for May–Sep 2026, where the live API or app recording is needed.
- The full orderbook config is about 23.6 GB per the repo's offline_replay docstring.
- Not downloaded here (read-only constraint).

## Favorite-longshot bias on Polymarket by category (shadow maker book for 90c-plus favorites in Politics/Crypto) | arXiv 2609.12878 | paper-method | event-books | effort M | medium
  what: A measurement (Cardozo & Rivero-Wildemauwe, Sep 2026) on 588M Polymarket trades, 2022-11 to 2026-03, using vgregoire/polymarket-users v1.3. It covers returns by price tail, category, buyer action and fee flag.
  how: Candidate new shadow book, recorded only:
- post-only bids on 90c-plus favorites in Politics and Crypto event markets that resolve soon;
- record the fill, the price and the realized return against the paper's category averages;
- run a mirror 'longshot seller' record for tokens under 10c.
This is a slow, non-latency book that fits a CPU laptop.
  fit: Needs only Gamma market scans and book reads, with no model. It is not time-critical (minutes to days).
  evidence: Opened via PDF queries.
- Longshots under 10c: −6.30% equal-child, −19.35% pooled.
- Favorites 90c or more: +0.277% equal-child, +0.83% pooled.
- Crypto: longshots −14.84% [−17.81, −11.87]; favorites +0.643% [0.589, 0.697].
- Politics: longshots −16.34%; favorites +1.044% [0.904, 1.184] (pooled +1.419%).
- Sports: longshots +2.43%, favorites −0.230% (no bias).
- Buyer action: favorites bought via posted offers +0.641% (equal-child) vs accepted offers −0.460%.
- Grouping by parent event flips longshots to +4.09%.
  caveats: - Returns are pre-fee. Taker fee at p=0.9 is 0.36c/share for Politics (feeRate 0.04) and 0.63c for Crypto (0.07), which eats most of a +0.6–1.0% edge, so this is maker-only.
- Capital lock-up and resolution-dispute risk.
- The result depends on aggregation.
- Data ends 2026-03-29, before CLOB V2.

## Domain-specific calibration slopes (Polymarket politics underconfident) for event-book recalibration | arXiv 2602.19520 | paper-method | event-books | effort S | low
  what: A logistic recalibration study (Le, Feb 2026) on 292M Kalshi and Polymarket trades. It estimates calibration slope by domain, time-to-resolution and trade size.
  how: A recorded probability for event books: p* = logit^-1(b·logit(p)), with b taken from the domain/horizon cell (Polymarket politics about 1.3). Record the implied edge and outcome next to any shadow event-book entry (pairs with the favorite-longshot candidate). It is a parallel variant, not a gate.
  fit: A closed-form transform of the live price, so compute is trivial.
  evidence: Opened via PDF queries.
- Polymarket mean slopes over the reliable 3h+ horizons: Politics 1.313, Sports 1.082, Crypto 1.049. Kalshi: 1.637 / 1.150 / 1.114.
- Kalshi politics by horizon: 1.32 (3–6h) to 1.83 (2d–1mo). Weather is overconfident short-horizon (0.69–0.97, Kalshi).
- The large-trade effect does not replicate on Polymarket (+0.11 [−0.15, 0.39]).
- Polymarket timestamps carry about 3 h of noise, so 0–3h bins are unusable.
  caveats: - Sample ends 2025-12-31.
- Polymarket domains assigned by title regex (42.5% labelled 'Other').
- A slope estimated on trades is not a tradable edge after fees and spread.
- Long-horizon favorite premium may partly be rational capital-lock discounting (not measured here).

## Polymarket Users trade-level dataset (for pre-testing event-book shadow strategies) | vgregoire/polymarket-users (HF dataset) | huggingface-dataset | event-books | effort L | medium
  what: A dataset (Akey, Grégoire, Harvie & Martineau) of all reconciled end-user Polymarket trades, 2022-11-11 to 2026-03-29. Configs include:
- markets, events;
- trades, with maker/taker;
- daily PnL with and without fees, and category PnL;
- ohlcv_1d, ohlcv_1h, ohlcv_5m.
  how: Offline backtests of the favorite-longshot and recalibration ideas by category, horizon, maker vs taker and fee regime, before running them as shadow books. It is also the source of the 2609.12878 numbers, so results can be replicated rather than trusted.
  fit: Offline only. Large; scan lazily with polars on 8 GB. TimeSeventeen/Polymarket-v2 (CC-BY-4.0, V2 OrderFilled logs, updated 2026-09-14) covers the period after April 2026.
  evidence: Opened the README/config list this session. CC-BY-4.0, 35.2K downloads, updated 6 Jul 2026, span 2022-11-11 to 2026-03-29. Also opened the TimeSeventeen/Polymarket-v2 card (OrderFilled plus daily_aligned layers, daily updates) and the od2961/polymarket-full-market-dataset card (1,788,703 markets, daily OHLC candles, 44.9 GB JSON, snapshot 2026-07-12; daily fidelity only).
  caveats: - Pre-V2 only (ends 2026-03-29); join with TimeSeventeen for later.
- Size is heavy for the laptop.
- Category labels come from the dataset.

## 15-minute / 5-minute crypto book order-book datasets (test whether short books already price the reversal) | trentmkelly/polymarket_crypto_derivatives (HF dataset) | huggingface-dataset | other-crypto-updown-books | effort M | low
  what: About 100 ms decision snapshots of Polymarket BTC/ETH/SOL/XRP 5m and 15m Up/Down books, 2026-02-21 to 2026-03-24. kachoio/polymarket-5-minute-crypto-up-down-markets adds per-second top-of-book for about 89,000 5m markets (BTC 2026-03-24 to 2026-05-18, CC0).
  how: Repeat this session's entry-cost test on the 15m books, where arXiv 2608.21888 finds reversal strongest (AUC about 0.536). The question: at window open, how far does the contrarian side already lean after a flow-driven 15m candle? Only if the lean is smaller than the flow-conditioned flip rate would a shadow 15m variant be worth recording.
  fit: Offline research. A live 15m variant would decide at the window open from closed 15m klines (cheap).
  evidence: Opened HF overviews this session.
- trentmkelly: CC-BY-SA-4.0, 30.0K downloads, updated 12 May 2026, 2026-02-21 15:45 to 2026-03-24 20:05 UTC.
- kachoio: CC0-1.0, 26,858,579 per-second observations.
- Prior null result, from the arXiv 2607.26245 PDF: on BTC 15m books, a 43-feature Binance flow model scored AUC 0.8377 / Brier 0.165 vs the Polymarket mid's 0.8405 / 0.163 out-of-sample, and quotes responded to 5 bp or larger Binance moves in a median 347 ms.
  caveats: - One month (trentmkelly) and about two months (kachoio) of data.
- OpenMarket's null result is a strong prior that the 15m mid already embeds Binance flow.
- The operator's 5m two-sided maker book already closed.

=== DEAD ENDS
- Qlib (microsoft/qlib; PyPI pyqlib 0.9.7) : Opened the GitHub API (48,539 stars, pushed 2026-09-02, MIT, latest release v0.9.7 2025-08-15), PyPI JSON, README, and the data and online-serving docs. Poor fit:
- Wheels exist only for cp38–cp312 (macOS universal2); the app runs Python 3.14, so it would need a source build with libomp.
- Hard dependencies include mlflow, redis, pymongo, cvxpy and gym.
- Data model: equity instruments with trading calendars (dump_bin day/1min/60min); nothing on 24/7 crypto calendars.
- Online serving is documented as 'daily prediction updates for the next trading day only'.
- Alpha158 and cross-sectional LightGBM pipelines target stock universes, not one binary hourly bet with about 20 firings a month.
The only reusable idea, RollingGen-style monthly retrain, is already covered by the operator's roughly 20-line walk-forward. A prior session pre-registered an Alpha158-on-hourly-BTC test (PREREG_qlib_test.md in the 6f03044c scratchpad), but I found no results file for it.
- Calibrated logistic regression / LightGBM probability per Tier A firing : Pre-registered walk-forward test run this session (ml_prob.py): 625 firings, 2024-04..2026-09, 9 features.
- Constant base rate M0: Brier 0.2460.
- Logistic on past firings: 0.2543.
- Logistic trained on all flow-push hours: 0.2473.
- LightGBM depth 2: 0.2476.
- Top-probability tercile won less than the bottom tercile in every model (M2 55.3% vs 59.8%; M3 in 2025-05..2026-09 49.2% vs 57.9%).
No lift, consistent with the earlier 10-feature selective logistic (validation 52.4% static, 50.3% rolling).
- TabPFN (Prior-Labs/TabPFN-v2-clf, Prior-Labs/tabpfn_2_5; PyPI tabpfn 8.5.0) : Not run: installing packages is out of scope here. Opened both model cards and PyPI.
- v2 checkpoints are ungated, about 13–29 MB each, under the Prior Labs License (Apache 2.0 + attribution); the card lists 16 GB+ RAM.
- The current default weights, TabPFN-2.5, are gated under a non-commercial licence that explicitly forbids using outputs 'for internal commercial decision-making'. That conflicts with live trading, so a v2 checkpoint would have to be pinned.
- Given that simpler learners on the same features showed no out-of-sample lift and inverted terciles, expected value is low. At most it would be a recorded probability, not worth a torch dependency now.
- Zero-shot time-series foundation models: amazon/chronos-bolt-small, google/timesfm-2.5-200m-pytorch, Salesforce/moirai-2.0-R-small : Opened the model cards and PyPI entries.
- chronos-bolt: 9–205M parameters, Apache-2.0; the card points to amazon/chronos-2 as the newer model; chronos-forecasting 2.3.2.
- TimesFM 2.5: 200M, Apache-2.0; timesfm 3.0.2.
- Moirai 2.0: CC-BY-NC-4.0, research only; uni2ts pins numpy~1.26 and pulls jax.
All forecast price or return paths. arXiv 2608.21888 shows the hourly crypto effect is a sign/flow phenomenon with near-zero lag-1 return autocorrelation, and the candle-native BTC fine-tune already scored 49–50% direction. The repo already has a dead Chronos stub. No reason to expect lift; CPU inference would fit but adds nothing.
- quant-model-library regime readings other than BOCPD (hmm_regime.py / time_series/hmm.py, EWMA vol ratio, gmm_regime.py, markov_switching.py, wasserstein_kmeans_regime.py) : Pre-registered test run this session with hmmlearn from the library's own venv.
- 2-state HMM high-vol probability: flipped (discovery 59.0 vs 56.6, held-out 56.5 vs 58.0).
- EWMA vol-ratio terciles: not monotone and did not hold (held-out 'mid' tercile best at 62.2%).
- GMM, Markov-switching and Wasserstein k-means are daily-SPY demos of the same idea; not run, and there is no reason to expect a different answer.
- All import the deleted src/data.py (removed in commit 5d55aed; 56 modules affected), so none run as-is.
- quant-model-library volatility forecasters (garch.py, asymmetric_garch.py, ewma.py, har_rv.py, stochastic_volatility.py) : They forecast variance, not direction; inputs are daily SPY/BTC-USD from the deleted data loader. Move size is already recorded and Kronos spread already predicts it. HAR-RV as written uses squared daily returns. No path to a better Tier A probability or entry price; at most a move-size field the app effectively has.
- quant-model-library execution/order_book_imbalance.py and execution/kyle_lambda.py : Both analyse Feb-2013 Xetra/Chi-X/BATS level-1 books and trade tapes through store.py (DAX_VENUES, load_book/load_trades, EQUITIES_DATA_ROOT). They measure 1–60 s mid moves and per-venue bp per EUR 1m. Nothing transfers to hourly klines. A Polymarket book-imbalance field would mostly restate a mid that already leans (OpenMarket imbalance_60s AUC 0.586, diagnostic only).
- quant-model-library signals (mean_reversion.py, ornstein_uhlenbeck.py, kalman_filter.py), ml/boosting.py, deep_learning/lstm_gru.py and tft.py, portfolio/kelly_criterion.py; empty src/microstructure, src/signal_generation, src/stat_arb : - mean_reversion, OU and Kalman are KO/PEP pairs-spread z-score tools; the rule already z-scores imbalance.
- boosting, LSTM/GRU and TFT (pytorch-forecasting) are next-day SPY return demos; the small-data ML test here showed no lift.
- kelly_criterion does walk-forward Gaussian Kelly on continuous daily returns, whereas binary-contract Kelly is a one-liner; at the measured roughly 1–2c/share edge the fraction is tiny.
- The three folders are empty.
The README itself frames the repo as European equity market structure and TCA.
- Frozen Tier A rule on ETH/SOL/XRP hourly books : Replication failed on held-out data with thresholds frozen: ETH 50.7% [42.5, 58.9] n=138, SOL 51.1% n=141, XRP 49.2% n=132, pooled 50.4% [45.6, 55.2] n=411, despite ETH discovery at 59.9%. Only the wider flow-push rule replicates (listed as a low-confidence candidate).
- Resting post-only bid at 49–50c as default entry : Measured on 334 BTC firings: fills are adversely selected.
- 50c trade-through fill within 120 s: 55% of firings, win when filled 47.8% vs 65.3% when not filled, −1.2c per signal.
- 49c: filled win 46.6%, −1.2c per signal.
Fills happen when BTC continues in H-1's direction. Worth recording as a variant, not assuming as a free maker edge.
- Cross-check against the app's own decision-time Polymarket hourly book : Not possible yet. data/btc_5m_binary_fair_value.db (main) has only 5m paper_ticks. In the venue-flow-feeds worktree database, the venue_flow_hourly and venue_snapshot tables exist but are empty. The feeds-live and market-execution databases have no hourly rows. The hourly context table in the spec is not built.
- OpenMarket dataset/paper (gregyoung14/openmarket-btc-polymarket, arXiv 2607.26245) for Taker Snapback : Opened: BTC 15-minute books only, event data on 54 days 2026-02-12..2026-05-15, collection ended. Useful only as supporting evidence that Polymarket's mid already embeds Binance flow: walk-forward model AUC 0.8377 vs mid 0.8405, quote response median 347 ms. It offers no hourly-book data for the H:00 entry-cost question.
- aliplayer1/polymarket-data-pipeline GitHub repo : Re-checked the GitHub API this session: 404 Not Found, so the dataset's collection code and back-coverage cannot be audited.
```
