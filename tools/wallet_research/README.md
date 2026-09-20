# Wallet research

Offline measurement of Polymarket's crypto Up-or-Down markets. Nothing here
trades; it only reads public data.

    scan.py         pull every fill on every resolved Up-or-Down market in a window
    maker_label.py  label each stored fill maker or taker, one API call per market
    maker_edge.py   what each side actually earned, and which wallets earn it passively
    maker_band.py   calibration: does the fill price forecast the outcome?
    maker_holdout.py does a wallet's maker edge carry into the next month?
    taker_screen.py rank wallets on taker fills only (superseded — see below)
    holdout_test.py the test that falsified the taker screen
    rank.py         rank wallets by net-of-fee edge per share, FDR-corrected
    analyze.py      older ranking by total PnL — kept for comparison only
    validate.py     cross-check one wallet against Polymarket's own numbers

## What the measurements found

**The taker side loses, and the fee is why.** Gross taker edge is about
−0.05c/share against a fee of roughly 0.97c/share, so crossing the spread costs
about 1.0c/share held to resolution. `holdout_test.py` then killed the idea of
fixing that by picking better wallets: screened wallets were no likelier to
profit out of sample than anyone else.

**The maker side is where the arithmetic clears.** Makers pay no fee at all.
`scan.py` pulled trades with `takerOnly=false`, which returns BOTH sides of
every match, so the database already contained real resting orders that really
filled — no simulation and no fill-rate guess needed. `maker_label.py` re-pulls
each market with `takerOnly=true` and labels the difference as passive.

An earlier simulation said resting loses ~3.5c/share at every offset. That was
an artifact of the strategy it simulated — one stale one-sided quote left under
the market for the rest of the window — not a property of resting. Measured
directly, maker fills are roughly flat in aggregate and strongly positive on
favourites.

**The edge is entirely in the entry price.** Held to resolution, fee-free,
equal-weighted across markets:

| maker fill price | c/share | t |
|---|---|---|
| 0.25–0.35 | −7.6 | −20.1 |
| 0.35–0.45 | −6.9 | −18.5 |
| 0.45–0.55 | −1.1 | −3.8 |
| 0.55–0.65 | +6.1 | +14.5 |
| 0.65–0.75 | +6.3 | +14.8 |
| 0.75–0.85 | +4.7 | +12.2 |
| 0.85–0.92 | +2.4 | +7.0 |
| 0.92–0.97 | +0.9 | +3.2 |

The sign flips at the midpoint. Passive buyers of favourites win; passive
buyers of underdogs lose. This is the favourite-longshot bias, and
`maker_band.py` shows it as a calibration curve: realised win rates sit above
what the fill price implies across the whole favourite range.

## The sizing rule, which is not optional

Volume weighted, the 0.55–0.92 band **loses** 0.61c/share. Equal weighted across
markets it makes **+3.80c/share** (t=+13.1, 68% of markets positive, median
market +12.6c). The gap is entirely position size: the worst 1% of markets cost
$1.27M, a handful of enormous positions on favourites that lost.

| month | volume weighted | fixed clip | t | % markets positive |
|---|---|---|---|---|
| 2026-06 | −7.30 | **+1.95** | +2.1 | 65% |
| 2026-07 | +0.57 | **+4.09** | +8.6 | 68% |
| 2026-08 | −0.01 | **+3.87** | +7.8 | 67% |
| 2026-09 | +1.19 | **+4.18** | +6.3 | 70% |

June looked like a regime break and was a sizing artifact. **Quote a fixed clip
in every market; never scale with market volume or depth** — that is exactly the
sizing that turns +3.8c into −0.6c.

## What is still unproven

All of the above measures what filled orders earned. It does not measure whether
*our* order fills. Queue position is the whole unknown, and it cannot be
recovered from the trade tape. That is what `polymarket_bot/maker/` is for: it
records the depth resting ahead of each quote and refuses to fill until volume
has traded through it.

## Checks these tools apply, and why

* **Zero-sum residual.** Maker and taker are opposite sides of the same
  matches, so their gross dollars must cancel. `maker_edge.py` prints the
  residual; a large one means the labels are wrong and nothing else in the
  output should be believed. It currently runs at 0.1%.
* **Significance is clustered by market.** Every fill in one market settles on
  one outcome, so the market is the independent trial, not the share. An
  earlier version treated shares as trials and produced intervals so tight that
  every bucket looked significant.
* **Volume- and equal-weighted figures are both printed.** They disagree here:
  the edge is larger in small markets, which is a capacity warning.
* **Concentration.** A mean carried by a handful of markets is not a mean you
  can size, so the share of profit in the best 1/5/10% is reported, along with
  the trimmed mean.

## Why rank.py and not analyze.py

`analyze.py` ranks by profit, which reliably surfaces market makers — whose
profit is the spread a copier pays. `rank.py` ranks by net edge per share after
the taker fee, excludes wallets quoting both outcomes, applies
Benjamini-Hochberg FDR across every wallet tested, and requires the edge to
survive deleting each wallet's three best markets.

Both are superseded for the maker question: you cannot copy a passive fill,
because by the time you see it the trade has happened. What replaces copying is
quoting the same way — see `polymarket_bot/maker/`.

## Data

Written to `data/wallet_research/` (gitignored — the trade databases run to
gigabytes).

    wallets.db   7.2M fills across 7,010 resolved 1h and 24h markets
    maker.db     2.7M taker-side records, the maker/taker labels
    m15.db       10.8M fills on the 15-minute family
    macro.db     gold / silver / oil / SPY dailies
    strike.db    125k strike markets
