# BTC 5m win streaks and the ride-the-stack ladder

**Status: parked / backlog.** Measured, written up, nothing built. Reopen only if
a compounding-stake or streak-conditional idea comes back.

Tool: `tools/ladder_streaks.py` (read-only, `--chart` for the survival curve).

## Question

"Double your stake through 17 consecutive 5-minute BTC up/down wins and you're a
millionaire in 90 minutes." Worth measuring once, because the same arithmetic
governs any compounding-stake scheme on these markets.

## Data

Every complete 5-minute bucket in the Binance 1m archive, **Jul 2024 – Jul 2026,
219,168 windows**, settled Up when `close >= open` (matches
`polymarket_bot/backtest.py`; ties credit Up). Book depth and spread come from the
72 recorded `paper_ticks` snapshots — the only real BTC 5m book we have.

There is no paper-trade history to run this on: `paper_ticks` holds 76 rows, 75 of
them skips, and `paper_positions` is empty. That is itself worth remembering.

## What came out

**1. The ladder never finishes.** Betting Up every window gives 109,538 ladders.
Half die on the first bet. 64 reach 10 wins. The record is 15 — twice, 2024-10-28
and 2025-08-10 — which is $10 → $328k, and both lost rung 16. Nothing reached 17.
Chasing it for two years costs $1,095,380 in restakes.

**2. Depth binds long before probability does.** Median recorded top-of-book ask is
185 shares. By rung 4 (an $80 bet) only 52.8% of snapshots could fill at the ask;
by rung 8 ($1,280) it is 2.8%; past rung 11 nothing fills. The ladder is not
improbable so much as unexecutable — you run out of book around bet 4.

**3. Stopping early does not create edge.** Cash out at N wins and restart:
at a frictionless 50c the net is ~0 at N=1–2 and drifts negative with N; at the
measured 50.5c ask every N loses. A fair game stays fair wherever you stop. The
only systematic drag is the spread, and it is charged to the **payout** (1.98x per
rung instead of 2x), never to the win rate — an earlier note of mine got that
backwards.

**4. Streaks are shorter than independence predicts.** This is the one real
finding. Stop-at-8 landed 313 hits where independent flips predict ~429 (5.5σ
low). Directly: `P(next window continues the run)` is 0.506 at k=0 but 0.462 at
k=4 (n=6,660, ~6σ below 0.5) and 0.444 at k=9. Lag-1 autocorrelation of the
up/down indicator is **−0.0121**. Symmetric across always-Up and always-Down, so
it is mean reversion, not directional drift.

## Caveats before anyone acts on #4

- Binance futures `close >= open` on 5m buckets, **not** Chainlink settlement
  prints. The settlement feed is the thing that actually pays, and it is not this.
- No cost model beyond a flat 50.5c. A −1.2% autocorrelation is far inside a 1c
  spread — this is a property of the series, not a tradeable edge, and nothing
  here tested it as one.
- k≥7 cells are thin (687 down to 64). The monotone shape is suggestive; the
  individual deep cells are not powered.

## If it is ever reopened

The question that would matter is whether the same reversion survives on Chainlink
prints and after crossing the spread. That is a venue-recorder question, not an
archive question, and it needs the recorded book — see #181.
