# Plan — Sub-dollar pair strategy (shadow only), 2026-08-14

## Origin

Operator asked whether Polymarket account `@mayormamdani`
(`0xf8af03f1e68ee7162db8983f0d6dd0dc869854c6`, $213 → $42,704 since 2026-06-09)
could be copy-traded. Measured on 5,072 of its trades over 9.3h on 2026-08-14:

| Fact | Value |
|---|---|
| Trade rate | 9.1/min across 476 distinct 5m windows |
| Side | 100% BUY, zero sells |
| Bought **both** Up and Down in same window | 377/476 = **79%** |
| Median combined cost of the two legs | **$0.989** (pair redeems at exactly $1.00) |
| Median clip | $7.10 |
| Assets | btc 28%, doge 28%, bnb 16%, eth 13%, sol 10%, xrp 4% |

**Not a copy-trade target.** No directional signal to mirror — 79% of the time both
legs are held, so a single leg is half a hedge, not a view. Its edge is *the fill*,
and the fill is what removes the mispricing. Operator decision: **run the strategy
natively, shadow mode, no execution.**

## The decisive finding: this is market making, not arbitrage

Live CLOB books pulled 2026-08-14 11:03 UTC, two consecutive active BTC 5m windows:

```
btc-updown-5m-1786705500   Up asks 0.530×298  |  Down asks 0.480×195   sum = 1.0100
btc-updown-5m-1786705800   Up asks 0.510×466  |  Down asks 0.500×294   sum = 1.0100
```

**Best-ask sum is exactly 1.0100 on both.** Crossing both legs costs 1.0449 after fees
for a $1.00 payout. **The taker arb does not exist** — the book is quoted at a
disciplined 1¢ over par, and the fee parabola adds 3.5¢ more at the money.

So the sub-$1 pairs can only be obtained from the **maker** side. Confirmed directly
from the target's fee record — implied fee rate `fee_per_share / (p·(1−p))` across
1,495 positions:

| Implied rate | Interpretation | Share |
|---|---|---|
| ≈ 0 | **maker fill, fee-free** | **40.3%** |
| 0.005–0.04 | mixed (position built from both) | 24.4% |
| 0.04–0.085 (p95 = 0.0666) | taker at ≈0.07 | 35.3% |

Clean bimodal split. Two consequences:

1. He posts **resting bids on both legs** below par and waits to get hit. He is not
   crossing a spread — he is *selling immediacy* to impatient takers on both sides.
2. p95 = 0.0666 ≈ 0.07 **independently validates `btc_bot/shadow/fees.py` against live
   venue data**, and confirms `lessons.md`'s "maker fills are fee-free" claim.

**This reframes the whole build.** Not "scan for sum(asks) < 1.0 and cross" — that
opportunity does not exist. It is "quote two-sided, get hit, stay hedged." Which is a
genuinely harder strategy:

- Fill rate depends on **queue position** behind orders we cannot see.
- **Adverse selection is the dominant cost.** Your Down bid gets hit precisely when
  price is falling — leaving you stranded short, not hedged. The hedge only completes
  when flow is two-sided.
- Inventory risk lives between the leg-1 fill and the leg-2 fill.

## Bug found in #181: discovery returns zombie windows

`tools/venue_recorder.py --discover` reports 5m families stamped `1766161800`
(**Dec 2025**), while live trading is on `1786705200` (Aug 2026), and every 5m family
shows zero liquidity/volume.

Cause: `discover()` pages `order=endDate&ascending=true` over `closed=false`. Stale
never-closed markets sort **first** and consume the pages, so the sweep never reaches
current windows before the API's ~2100 offset ceiling.

Fix: for the 5m family, the window slug is a **pure function of the clock** —
`{asset}-updown-5m-{floor(now/300)*300}`. Construct it and fetch by slug; use discovery
only to enumerate which *families* exist, never which windows. Filter `endDate >= now`
on all passes.

Blocking for this build: `rec_books` and `rec_trades` are currently empty (the recorder
has never been run — no `rec_*` tables exist in `data/`).

## Architecture

New package `btc_bot/pairarb/`, mirroring `btc_bot/shadow/`'s layout. Reuses
`btc_bot/shadow/fees.py` directly — **do not fork it** (`lessons.md` dual-fork trap).

| File | Role |
|---|---|
| `types.py` | `QuotePlan`, `RestingOrder`, `PairFill` — frozen contracts |
| `queue.py` | **Maker fill simulator** — the heart. See below |
| `quoter.py` | Two-sided quote placement: where to post both legs |
| `ledger.py` | Persistence → new `btc_pair_shadow` table |
| `runner.py` | Wiring: read `rec_books` + `rec_trades` → simulate → log → settle |

Separate table, not `btc_model_shadow_positions`: that ledger is
`UNIQUE(window_slug, model_id)` and single-leg directional.

### The maker fill simulator (`queue.py`)

This is where a maker backtest either stays honest or manufactures fake profit.

For a hypothetical resting bid of size `q` at price `p` posted at time `t`:

1. From `rec_books` at `t`, record `depth_ahead` = size already resting at `p` (we
   join the back of that queue — never assume priority we did not earn).
2. From `rec_trades` over `(t, t+Δ]`, accumulate volume that traded **at or through**
   `p` on that side.
3. We fill only once cumulative volume exceeds `depth_ahead`, and only for the
   remainder.
4. Cancel/replace resets `depth_ahead` to the then-current queue.

Conservative by construction: back-of-queue, displayed depth only, no self-impact
credit. If it still shows edge, the edge survives the pessimistic assumption.

### Then the pair logic on top

- Log **completed pairs** vs **stranded legs** separately, always.
- A stranded leg settles on **realized outcome**, never at par.
- Measure **adverse-selection rate**: P(leg fills | that leg ends up losing) vs
  P(leg fills | that leg wins). A fair quoter has these near-equal; a picked-off one
  does not. This is the single number that decides whether the strategy is real.

## Falsification bar (pre-registered)

- **Completed-pair PnL must be near-deterministic.** If noisy → the fill model is
  wrong, not the edge real.
- Daily decomposition + intraday max drawdown, never a bare total (`lessons.md`).
- Edge **per dollar deployed** — capacity binds at $7 median clips.
- If profitable **only** when stranded legs are marked at par, it is not real.
- If the adverse-selection gap is large, the maker edge is illusory regardless of
  what the pair arithmetic says.

## Honest limit of shadow mode

A taker strategy can be simulated honestly — the liquidity was displayed, we would have
crossed it. A **maker** strategy requires simulating *whether our order would have been
hit*, which depends on queue position behind unobservable orders and on counterfactual
flow. This is where maker backtests routinely produce fake profits.

The queue model above is the rigorous version, and it will bound the opportunity. But
final validation of true fill rates needs a small **live** pilot ($1–5 clips) — which is
the operator's call to arm, never an agent's (`AGENTS.md`).

## Scope

Operator approved amending `AGENTS.md` from "BTC 5-minute Up/Down only" to the 5-minute
Up/Down family across the venue's crypto assets (btc/eth/sol/xrp/doge/bnb). Timeframe
fence stays. Note the live census currently lists 5m families for **xrp/eth/sol/btc**
only — no doge/bnb 5m family was open at census time despite 44% of his flow being
doge+bnb, so family availability must be re-checked rather than assumed (C7).

## Tasks

- [ ] Open GitHub issue; branch `feature/<id>-pairarb-shadow` off `develop`
- [ ] Amend `AGENTS.md` scope fence + `docs/CODE_MAP.md` routing row
- [ ] **Fix `venue_recorder.discover()`** — clock-derived slugs for 5m, `endDate >= now`
      filter. Tests for the zombie-window regression
- [ ] Start recorder continuously; bank ≥24h of `rec_books` + `rec_trades` before
      trusting any fill number
- [ ] `types.py` — frozen contracts
- [ ] `queue.py` + tests: back-of-queue, partial fill, cancel/replace resets priority,
      volume-at-or-through, zero-depth, crossed book. TDD — tests first
- [ ] `quoter.py` + tests: two-sided placement, fee-aware bid pair `< 1.0`
- [ ] `db.py` migration dict (NOT the `SCHEMA` literal) → `btc_pair_shadow`
- [ ] `ledger.py` + tests
- [ ] `runner.py` + `tools/pairarb_shadow.py` CLI (`--once`, `--run`, `--assets`)
- [ ] Settlement: completed pairs at par, stranded legs on realized outcome
- [ ] `tools/pairarb_report.py` — daily decomposition, drawdown, edge/$ deployed,
      completed-vs-stranded, **adverse-selection gap**, contested rate
- [ ] Run shadow ≥1 week. **No execution.** Then operator decides on a live pilot
- [ ] `tools/gen_docs.py`; commit after each chunk (`lessons.md`)

## Review

_(to be filled in after implementation)_
