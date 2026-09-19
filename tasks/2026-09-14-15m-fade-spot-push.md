# 15-minute Up/Down — fade the spot-only push (parked)

Work done 2026-09-14, parked 2026-09-19. **Nothing was measured.** The data was built and the
market plumbing was checked; the backtest itself never ran (two research agents died on the
monthly spend limit, and the scratchpad holding the built data has since been cleared).

The question: the hourly rule — bet against the last hour when aggressive spot buying or selling
was in its top 20% for the week, perps did not show the same push, and the hour closed at the edge
of its range — does it do anything on Polymarket's **15-minute** books, including the thin ones
(DOGE, BNB, HYPE)?

The hourly version of this question on the thin hourly books was answered separately (see the
hourly thin-book test). This file covers only the 15-minute books.

## What the 15m market actually is (checked live 2026-09-14)

| | |
|---|---|
| Series | `<asset>-up-or-down-15m`, slug per window `<asset>-updown-15m-<start_unix>` |
| Assets | btc, eth, sol, xrp, doge, bnb, hype |
| Resolves | **Chainlink**, not Binance. `<asset>-usd` stream, end price vs start price, through at least 2026-07-01. By 2026-08-10 the wording is the `<asset>-usd-twap-60s-streams` TWAP stream. Ties go Up. |
| Tick / min size | 0.01 / 5 shares |
| Fees | taker only. None in 2025-11; `rate 0.25, exponent 2` in 2026-01..03; `rate 0.07, exponent 1` from 2026-04. `rebateRate 0.2` to makers. |
| History | BTC 15m markets resolved as far back as 2025-11-15; first prints in the trade API from about 2025-10-10. DOGE/BNB/HYPE 15m start about 2026-03-24. |

Book depth and quoted spread, one random mid-window snapshot (2026-09-14 03:35 UTC):

| Asset | Liquidity | Spread |
|---|---|---|
| BTC | $13.6k | 1c |
| ETH | $2.7k | 1c |
| SOL | $1.4k | 1c |
| XRP | $0.8k | 3c |
| HYPE | $0.37k | 6c |
| BNB | $0.33k | 2c |
| DOGE | $0.68k | **10c** |

Two things that matter before any result is believed:

1. **The settlement reference is not Binance.** The hourly rule settles on a Binance candle; these
   settle on a Chainlink stream, and the rule behind that stream changed during the sample. Any
   long-history backtest on Binance bars is a proxy, and the proxy has to be checked against the
   real resolutions per era.
2. **The switch to a TWAP reference changes the bet.** An endpoint bet and an
   average-over-the-window bet are different questions for a mean-reversion signal, which is
   front-loaded. The exact switch timestamp per asset was never pinned down.

## Pre-registration (written before any 15m results were pulled)

Assets BTC, ETH, SOL, XRP, DOGE, BNB. HYPE only if a spot venue with a taker side is used (see below).

Per bar, spot and perp separately, same timestamps:

- `imbalance = 2 * taker_buy_base / volume - 1`
- `z` = imbalance z-scored on a trailing window ending at and including the bar (15m bars: 672 bars
  = one week; 1h bars: 168 bars)
- `fz` (flow push) = `z * sign(close - open)`
- `CLV = (2*close - high - low) / (high - low)`
- Decision at the open of window W uses bar W-1 only; skip if W-1 closed flat. Bet **against** W-1's
  direction, hold to resolution.

Rules, frozen, no runtime tuning:

- **N1** native 15m, walk-forward: spot `fz` > the 80th percentile of spot `fz` over all bars in
  calendar months before W's month; perp `fz` <= the same percentile for perps; CLV > 0.8 after an
  up bar, < -0.8 after a down bar.
- **N2** native 15m, literal "top 20% of the past week": the same, with both percentiles taken over
  the 672 bars before W-1.
- **N3** native 15m, fixed thresholds carried over unchanged from the BTC hourly spec: spot fz > 1.20,
  perp fz <= 1.24, same CLV rule.
- **H1** hourly signal on the 15m book: the hourly rule evaluated at hour H's open, bet against hour
  H-1 on the 15m market starting at H:00.
- Context, not rules: H1 on the H:15, H:30 and H:45 markets and on the Binance 1h candle (how the
  effect spreads across the hour); every window's plain "bet against the last bar"; the spot
  condition alone.

Outcomes: `O_PM` the actual Polymarket resolution wherever the market exists; `O_END` Binance spot
15m close >= open; `O_TWAP` a 1m/1s Binance proxy for the TWAP era, chosen only by agreement with
`O_PM` on a rule-check sample drawn independently of signals and fixed before signal results.

Windows: walk-forward months 2024-04..2026-09 on proxy outcomes; the Polymarket-live window per
asset on real resolutions, split at the rule switch. Per year, and bet-Up vs bet-Down.

Entry price: fade-side cost obtainable in [start, start+60s]; EV per share for a taker =
hit - price - fee(price, era); the maker view labelled as an assumption. Controls, seed 20260914:
random non-signal windows stratified by asset and prior-bar direction, plus non-signal windows whose
prior-bar move is in the same size quintile — to see whether the book already leans toward the
reversal on signal windows.

Reporting: win rate with Wilson 95% interval and n, no pass/fail labels.

## What got built (then lost with the scratchpad)

Binance spot 15m, perp 15m and spot 1m for btc, eth, sol, xrp, doge, bnb — 2023-10-01 to
2026-09-14 03:30 UTC, from data.binance.vision monthly/daily bulk plus a REST tail, checksum
verified, microsecond timestamps normalised. Full grid, no gaps or duplicates; 1m rolled up to 15m
exactly, and 15m rolled up to REST 1h exactly for spot on all 25,899 hours per asset.

Rebuilding costs a couple of hours of fetching; these are the traps found:

- **Binance bulk perp 15m files contain bad bars.** BTC 2023-11-10 15:15-16:15 and btc/eth/sol
  2024-10-28 20:00-20:45 are flat zero-volume placeholders while REST shows real trading. Smaller
  volume / taker-buy differences on 2024-03-27, 2024-03-28, 2025-01-14, 2025-01-29 (about 16-21
  bars per asset).
- **REST disagrees with itself** in 2023-11-10 15:00-17:00 and 2024-10-28 20:00-22:00 — REST 15m
  sums do not match REST 1h. Perp imbalance in those windows is unreliable whichever source is used.
- DOGE spot 1m has 45 zero-volume bars; imbalance is undefined there.
- Spot bulk files use microsecond timestamps from 2025-01 onward, milliseconds before.

**HYPE:** Binance has no HYPE spot pair (only the unrelated HYPER\*), so Binance cannot give HYPE
spot flow; its USD-M perp exists from 2025-05-30. Per-trade taker side back to 2026-03 does exist
publicly at Bybit (`public.bybit.com/spot/HYPEUSDT/`, columns include `side`) and OKX
(`static.okx.com/cdn/okex/traderecords/trades/daily/...`, same). Hyperliquid's own public candle
snapshot returns only the last ~5,000 bars and no taker split. Whether Bybit's and OKX's `side`
means the taker side was not verified in their docs.

## Entry prices: the source that works

- **Use** `https://data-api.polymarket.com/trades?market=<condition_id>&start=&end=` (inclusive unix
  seconds; `takerOnly` defaults true). It reaches back to the first 15m markets. Query by time
  window — `offset` caps at 10,000 — and note that `startTs`/`endTs`/`after`/`before` are silently
  ignored.
- Rows give the taker's side on that token and a size-weighted fill price that can sit off-tick
  (0.4325). A buy of Up at p also appears as a sell of Down at 1-p; count both.
- CLOB `prices-history` works on closed markets at `fidelity=1` (about one point a minute, not
  aligned to the window start) but behaves like a mirrored midpoint — it printed 0.50/0.50 on a
  DOGE window with no trades at all, so it is not a tradeable price.
- **No usable public historical bid/ask** for closed 15m markets: the Goldsky orderbook subgraph is
  deprecated, and the Hugging Face book datasets checked were either a 4-week window
  (2026-03-30..04-25) or stale/fake. Commercial archives were not tried.
- Liquidity reality on the thin side: in the 12 test markets, BTC/ETH/SOL had 23-365 taker prints in
  the first 60 seconds; DOGE had 0-9, and three of four DOGE markets had **no taker prints at all
  before the window started**. Fetch cost about 2.9 s per market at 4 requests/second.

## If picked up again

1. Pin the Chainlink rule switch per asset (binary search the slugs on description/resolutionSource)
   and the fee-era boundaries; verify the per-share fee formula for `rate`/`exponent` from
   Polymarket's own docs or client source.
2. Build the full 15m market index per asset (slug, condition id, token ids, resolution) and check
   which Binance proxy reproduces real resolutions in each era, before looking at any signal.
3. Only then run the pre-registered variants above, and price the signal windows against the
   controls — on DOGE the honest question may be whether a fill exists at all, not what the hit rate is.
