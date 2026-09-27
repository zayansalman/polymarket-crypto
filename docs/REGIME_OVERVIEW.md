# Market Regime Overview

What kind of market is this right now, and which strategy family's mechanics
currently work in it? That is the whole question `polymarket_bot/regime/`
answers — for the operator today (the MARKET REGIME card), and, later, as the
persisted input a strategy router is backtested against.

It is **advisory and shadow-only**. Nothing on the trading path reads it, no
order is placed from it, and the loop's entry decision is untouched.

## What it is not

The project already tested regime *switching* and lost
([docs/archive/FINDINGS.md](archive/FINDINGS.md) §3–4): "trade yesterday's
leader" earned less than holding, and 0/12 regime×model cells and 0/75 slices
survived FDR-corrected permutation tests. Two "obvious" effects (a night-hours
bleed, a mid-vol sweet spot) failed replication outright. So this module never
ranks families by expected profit, never labels one "favoured", and never
reads the session or move axes in a rule. It reports **feasibility** — whether
a family's mechanics work on today's measured numbers — with those numbers
attached. Regime *awareness* was already built in (vol/basis logged at
decision time); this is that awareness made whole and visible.

## Where it runs

`polymarket_bot/regime/monitor.py:run_forever` is started by the dashboard's
lifespan (`polymarket_exec/ops/dashboard/app.py`) alongside the daily altcoin
scanner: always-on, independent of the BTC loop's ▶ Start / Stop, so the
regime is readable *before* choosing what to start. It follows the operator's
selected asset and timeframe (the header market selector). Knobs on the
SETTINGS card, group **Regime monitor**: `Monitor enabled` and `Scan interval`
(default 60 s).

Per scan it reads (all existing endpoints; nothing new on the network surface):

| Source | What | Cadence |
|---|---|---|
| Binance spot `/api/v3/klines` (`BINANCE_API_BASE`) | 60 × 1m bars (last hour), 288 × 5m (24h), 168 × 1h (7 days) — completed bars only | 1m every scan; 5m/1h cached 5 min |
| Polymarket Gamma `/markets?slug=` | current window's liquidity + outcome token ids; the last 6 *completed* windows' volume (medians) | every scan; completed windows cached |
| Polymarket CLOB `/book` | top-of-book for both outcome tokens, only when the loop is not journaling the selected market | every scan, in the quotable phase |
| `paper_ticks` (own DB) | the loop's last 12 ticks for the selected market (~60 s), in-phase only | every scan |

Every scan journals one `regime_snapshots` row even when inputs are missing;
the row says what was missing.

## Features (all `None` when not computable, never zero)

Volatility is **per-second-equivalent** everywhere (bar sigma ÷ √bar-seconds);
× 5616 (√ seconds-per-year) gives the annualized figure the card shows.

| Feature | Definition | Why this one |
|---|---|---|
| `vol_1h_gk`, `vol_24h_gk` | Garman–Klass on 1m / 5m OHLC: `σ² = ½ ln(H/L)² − (2 ln 2 − 1) ln(C/O)²` per bar, mean, √, ÷ √bar-s | ~7× the efficiency of close-to-close on the same bars; robust to one outlier print; built from trade prices (no quote bounce) |
| `vol_1h_cc`, `vol_24h_cc` | close-to-close stdev of log returns ÷ √bar-s | cross-check; the loop's `sigma_per_second` family, without its 2e-5 floor |
| `vol_1s` | the loop's own `sigma_per_second` from the newest in-phase tick; `None` on the floor or `vol=floor` | a different instrument (Chainlink oracle prints, autocorrelated); banded on its **own** legacy cutoffs, never mixed with the bar estimates |
| `vol_variance_ratio` | `vol_1h_gk² / vol_1s²` | diagnostic of the gap between the two instruments (uncalibrated as yet) |
| `vol_ratio_seasonal` | `vol_1h_gk` ÷ same-UTC-hour 7-day median of hourly GK vol (blended by minute-of-hour) | crypto vol and volume have a strong intraday cycle; against a flat 24h mean the ratio is a clock, not a regime |
| `volume_1h_usd`, `volume_ratio_seasonal`, `trades_ratio_seasonal` | last-hour quote (USDT) volume / trade count ÷ same-hour 7-day medians | same reason; trade count is robust to one block print |
| `volume_ratio_24h`, `vol_ratio_1h_24h` | unadjusted 1h vs 24h | kept as labelled secondaries only |
| `rv_bv_ratio_1h`, `max_return_z_1h` | realized ÷ bipower variance; max |1m return| ÷ robust scale | jump diagnostics: "vol is high because of one print" vs "continuously high" — the Gaussian fair value cares |
| `return_1h`, `move_z_1h` (and 24h) | log return; `return ÷ (σ·√seconds)` | **the one** trend-like statistic. A drift t-stat and a Kaufman efficiency ratio on a fixed window are the same z-score, and none of them measures persistence (not detectable on one klines call). Says how far price travelled, nothing more |
| `overround` | mean(up ask + down ask) − 1, in-phase reads only | the taker's round-trip cost before fees; a 1-tick book both sides ≈ 0.01 |
| `maker_capture` | 1 − mean(up bid + down bid) | what a two-sided resting quote at the touch earns per completed pair |
| `executable_depth_usd` | mean of the thinner side's ask size × price | caps size |
| `venue_liquidity_usd`, `venue_volume_per_window_median_usd` | current window's Gamma liquidity; median volume of the last 6 *completed* windows | the current window's own volume is a partial sum and is deliberately not a feature |
| `taker_buy_ratio_1h`, `range_position_24h`, `move_z_24h` | descriptive only | no band or rule reads them; a router must not either |

**Quotable phase.** Book reads count only with 60–270 s remaining in a 5m
window. Outside it the book widens and skews for reasons tied to the window
clock, not the market; a snapshot then records `book_out_of_phase` instead of
a fabricated cost.

## Bands (a-priori, versioned — `THRESHOLDS_VERSION`)

Cutoffs are round numbers chosen for their units, declared up front and never
fitted; the values behind every version are journaled once in
`regime_threshold_versions`, so history can be re-banded reproducibly.

| Axis | Bands | Cutoffs |
|---|---|---|
| `volatility` (bar GK) | low / mid / high | annualized < 30% / < 55% / ≥ 55% |
| `vol_1s` (loop) | low / mid / high | 3e-5 / 6e-5 per s — the exact `tools/regime_attribution.py:vol_band` cutoffs, pinned by test |
| `vol_trend` | contracting / stable / expanding | seasonal ratio ≤ 0.67 / ≥ 1.5 |
| `volume` | quiet / normal / active | seasonal ratio < 0.6 / > 1.5 |
| `move` | big_down / flat / big_up | \|z\| ≥ 2 — **display only** |
| `jumps` | continuous / jump | RV/BV > 1.5 or max-return z > 4 |
| `book` | cheap / normal / expensive / stale / unknown | overround ≤ 0.02 / ≥ 0.04; stale > 60 s |
| `session`, `weekday` | night / day / evening; weekday / weekend | 08:00 / 16:00 UTC (the attribution tool's bands) — **display only** |

## Strategy feasibility

Three stable ids (`STRATEGY_FAMILIES`): `btc_5m_taker`, `pairarb_maker`,
`daily_altcoin`. Each gets `feasible` / `degraded` / `blocked`, a `metrics`
dict of the measured numbers, and `reasons` as **rule ids** from
`classify.RULES` (prefix `m.` = measured on this snapshot, `p.` = context,
not evidence). The card resolves ids to prose; the row stays small and a
backtest can count firings.

| Family | Feasible when | Degraded when | Blocked when |
|---|---|---|---|
| `btc_5m_taker` | half-overround + taker fee (0.07·p·(1−p)) ≤ the loop's edge gate (`PAPER_ENTRY_EDGE_MIN`), depth ≥ venue minimum, loop sigma usable | cost above the gate; depth thin; book unknown/stale; jump in the last hour; loop sigma on the floor | no volatility estimate from any source |
| `pairarb_maker` | maker capture at the touch > 0 | capture ≤ 0; book unknown/stale; jump | no volatility estimate |
| `daily_altcoin` | 24h vol present and consistent with its seasonal baseline, **and** the snapshot asset is one the scanner trades (`DAILY_ASSETS`) | vol expanding (its 30-day sigma is stale); jump; snapshot asset is a proxy | no 24h bars |

`recommendation` is a display line ("feasible: …", "stand down: …"); a router
computes its own from the fits.

## Data quality and the router contract

`quality` is a list of `{code, detail}` with an enumerated code set
(`classify.QUALITY_CODES`); `grade` rolls it up: `full` (no flags), `partial`,
`none` (no market data). `usable_for_router` is `grade == "full"`.

Join keys on every row: `created_ts` (epoch s), `window_slug` (the clock-derived
Up/Down window for 5m/15m/1h; `NULL` for the daily family, which joins on the
latest snapshot with `created_ts ≤` the trade's entry time), `run_id` /
`scan_seq` (monitor boot + monotonic counter, so gaps and restarts are
detectable).

A router built on this must, before any fit ever becomes a switch:

1. **Transition only on `grade == "full"` snapshots**, on the persisted
   continuous metrics (not the band labels), with dual enter/exit thresholds
   (e.g. enter "high vol" at 55% annualized, exit at 45%).
2. **Dwell in settled trades, not minutes.** FINDINGS §3: per-trade σ ≈ $2.45
   against candidate edges ≤ $0.35 gives `n ≈ (2·2.45/0.35)² ≈ 200` settled
   trades per (family, band) cell for a 2σ read. A switch window short enough
   to feel responsive has no power by construction.
3. **Carry an evidence gate** that reuses `tools/regime_attribution.py`
   (side-attributed cells, two-sided edge, one-vs-rest permutation,
   Benjamini–Hochberg): a family may be "favoured" in a regime only when that
   instrument reports an FDR-surviving candidate — and every would-be switch
   is written down with its hold counterfactual so §3's switch-vs-hold test can
   be re-run on the regime ledger first.

None of that exists yet, and nothing here pretends to be it.

## Known limits / next chunks

- **Asset coverage.** One row per scan, for the *selected* asset. The daily
  scanner trades doge/sol/xrp/bnb/eth; on any other selection its fit is
  labelled a proxy. Scanning every `SPOT_SYMBOL` asset per interval (one row
  each) would make the history independent of UI state.
- **`vol_variance_ratio` is uncalibrated.** The loop's 1s sigma and the 1m
  Garman–Klass estimate differ by an instrument-specific factor; a one-off
  join of `paper_ticks.sigma_per_second` to same-minute Binance bars would
  freeze a per-asset median into a thresholds version.
- **No `tools/regime_backtest.py` yet** — the read-only instrument that joins
  `regime_snapshots` to each family's ledger and re-bands under a chosen
  thresholds version is what makes "backtestable later" true.
- **15m / 1h families** are slug-derivable but were not observed on the venue
  at the last probe; the venue block reports `venue_market_absent` for them.
- Binance futures (funding, open interest) are not assumed reachable and are
  not used.
