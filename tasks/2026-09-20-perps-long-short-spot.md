# Polymarket perps + binary + Kraken spot — venue capture, parked before any strategy work (2026-09-20)

Backlog issue: #263

**Status: parked on purpose.** The account became perp-enabled on 2026-09-20 and the venue was
checked for API access. Nothing about a strategy has been tested. Everything below is either a
documented venue rule or a single snapshot taken at 04:07 UTC on 2026-09-20 — no backtest, no
paper trades, no edge claim.

## What is confirmed

Perps are a separate API from the CLOB, and fully programmatic.

- Base `https://api.perpetuals.polymarket.com`, websocket `wss://ws.perpetuals.polymarket.com/v1/ws`.
- **Market data needs no auth** — `/v1/info/instruments`, `/tickers`, `/book`, `/klines`,
  `/trades`, `/funding`, `/mark-history`. Verified live.
- **Auth is a proxy key, not per-order wallet signing.** Sign an EIP-712 `CreateProxy` message
  with the Polymarket account key, `POST /v1/account/proxy`, store the returned secret, then send
  `polymarket-proxy` / `polymarket-secret` headers on every call.
- **Orders** `POST /v1/trade/orders` (`iid`, `buy`, `p`, `qty`, `tif`, `po`, `ro`, `c`), with
  GTC/IOC/FOK, post-only, reduce-only and TP/SL brackets. Leverage via `PATCH /v1/trade/leverage`,
  positions via `GET /v1/account/portfolio`, plus a dead-man switch at `PATCH /v1/trade/auto-cancel`.
- **Collateral is a separate pot.** Perps run on pUSD inside a perps sub-account; CLOB USDC does
  not cover it. Deposit via the SDK or the relayer, minimum 10.
- **Order placement is geo-blocked for the US and Canada**, enforced server-side.

## The three numbers that matter for anything we build here

1. **Fees are 4.00 bps taker / 1.25 bps maker on notional** at the $0 volume tier, falling to
   2.50/0.00 at $500M. Charged as `abs(price * qty) * rate`.
   For contrast, the binary CLOB taker fee measured during the wallet-research work is ~1.1c per
   share, which on a 50c contract is roughly 226 bps. The perp leg is about two orders of
   magnitude cheaper to enter.
2. **Books are tight and deep.** Snapshot, top-of-book spread and notional resting within 10 bps
   of mid:

   | symbol | spread (bps) | bid $ @10bps | ask $ @10bps |
   | --- | --- | --- | --- |
   | BTC-USD | 0.12 | 2,587,419 | 2,082,665 |
   | GOLD-USD | 0.23 | 2,593,585 | 2,626,989 |
   | ETH-USD | 0.39 | 1,115,622 | 1,059,027 |
   | SOL-USD | 0.92 | 481,834 | 537,809 |
   | SP500-USD | 0.92 | 3,099,937 | 2,467,006 |
   | WTIOIL-USD | 1.44 | 806,589 | 788,431 |
   | SILVER-USD | 2.56 | 1,583,562 | 1,597,065 |
   | NAS100-USD | 2.71 | 3,098,478 | 3,049,381 |
   | DOGE-USD | 3.15 | 204,667 | 144,736 |
   | NVDA-USD | 4.08 | 1,241,971 | 1,771,706 |
   | TSLA-USD | 5.23 | 1,607,834 | 1,696,887 |

   One snapshot in one minute. It says nothing about the book at a market open, at a news print,
   or at 03:00 UTC on a Sunday.
3. **Funding is hourly with a fixed interest leg.** `F_8h = scale * (mean_premium +
   clamp(0.0001 - mean_premium, ±0.0005))`, `FR_hour = clamp(F_8h / 8, ±0.04)`, scale 1.0 for
   crypto and 0.5 for everything else. A perp trading exactly at index therefore still pays
   **0.00125%/hr (10.95%/yr) for crypto** and **5.47%/yr for non-crypto**, longs to shorts. Paid
   hourly, peer to peer, no protocol cut.

## The hard constraint to act on first

**`/v1/info/funding` only serves about 24 hours of history.** Paging backwards stopped at
2026-09-19T05:00 UTC for every one of the 89 instruments. Funding cannot be backfilled — if we
ever want to study carry, a recorder has to start capturing it before the study, not during it.
Same likely applies to `/klines` and `/mark-history`; not checked.

The one day that does exist, annualised, ranges from **+80.7% (XMR-USD)** to **−89.4%
(UNITREE-USD)**. That is 24 points per instrument. It is a sample size of nothing and is recorded
only to show the dispersion is wide enough to be worth measuring properly.

## Instrument universe

89 instruments: 40 crypto, 39 equities, 6 indices, 4 commodities. Max leverage 50x on
BTC/ETH/SP500/NAS100, 20x on SOL/XRP/gold/silver/oil, 10x or less elsewhere. Min notional $10 on
all of them. Funding interval 1h on all of them.

The overlap with books this project already trades is real: BTC, ETH, SOL, XRP, DOGE, GOLD,
SILVER, WTIOIL all have both a perp and a Polymarket binary.

**Kraken spot coverage is unresolved.** The capture matched 34 of 89 legs, but the matcher is
naive — it missed BTC because Kraken lists it as XBT, and it cannot distinguish "Kraken has no
pair" from "the symbol is spelled differently". The equity and commodity legs genuinely have no
crypto-exchange spot. Redo this against Kraken's `wsname` properly before relying on it.

## Strategy families to test later — named by their exact calculation, none of them tried

No claim that any of these has an edge. They are the shapes the three venues make possible.

1. **Short perp + long Kraken spot, held while the trailing 1h funding rate exceeds the
   zero-premium floor.** Collects `FR_hour` each hour, pays 4 bps taker on each perp leg plus
   Kraken's spot fee plus the perp/spot tracking error. Needs the funding recorder above before
   it can be evaluated at all.
2. **Binary Up/Down position delta-hedged with the matching perp.** Size the perp leg so the
   combined P&L is flat to the underlying, leaving the binary's mispricing versus the perp's cost
   of carry. Only meaningful where both books exist for the same asset and the same window.
3. **Perp mark minus index, entered when the deviation exceeds the round-trip cost.** `mark` and
   `index` both come straight off `/v1/info/tickers`. Cost floor is 8 bps round trip at the $0 tier.
4. **Binary strike replication.** A Polymarket "BTC above X at time T" binary and a perp position
   are two prices on the same event. Measure whether the binary's implied probability is ever far
   enough from the perp-implied distribution to cover both fee schedules.

Every one of these is a two-or-three-legged trade, which means execution risk we have never taken
on: the legs are on different venues with different latencies and different collateral pots. That
is the part most likely to eat the edge, and the reason none of this should be sized before it has
been paper-traded through one pipeline.

## Reproducing the capture

    python3 tools/perps_research/survey_perps.py --funding-all --out data/perps_research/survey.json

`data/` is gitignored, so the JSON is not in the repo — the script regenerates it. It is read-only
and touches only public endpoints.
