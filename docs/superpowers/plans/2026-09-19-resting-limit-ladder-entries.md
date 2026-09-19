# Resting limit ladder entries

**Source:** Zayan (operator), 2026-09-19 — "never use market order, always use limit
orders below the price usually 5-15 cents lower than current price and set a bunch of
them out there and let them come to you, when whales have capital flying around you'll
hit the natural dips when their bots are shifting capital. hint: don't trade the event,
trade the orderbook wall."

## What changes

Every strategy's entry stops paying the ask and instead rests a ladder of GTC limit
BUYs below the touch. Entry becomes a liquidity-provision decision about book
structure, not a bet placed at whatever price the book currently shows.

This is shared execution infrastructure: strategies keep deciding *side* and *size*,
and the ladder is how any entry reaches the venue, in both modes.

## Current behaviour being replaced

- `polymarket_exec/execution/live.py:823` `submit_entry` — one GTC BUY priced at
  `best_ask`, i.e. marketable, charged the taker fee on whatever crosses.
- `polymarket_bot/paper.py:1328` — paper fills instantly at the top-of-book price.
- `polymarket_exec/execution/live.py:24` — "Exits never rest": the GTC SELL is priced
  at `best_bid` and awaited, then cancelled.

## Design

### Ladder shape

`N` rungs spanning 5–15¢ below the reference price, size split across them. Defaults:
5 rungs at 5/7.5/10/12.5/15¢ below, equal size per rung. All three of rung count,
offset span and size weighting are settings, so the shape is tunable without a
code change.

Reference price is the touch on the side being bought. Rungs are rounded to the
venue tick and each rung must clear the venue minimum size — rungs that cannot are
dropped and their size redistributed to the surviving rungs.

Every rung is posted below the touch, so no rung can cross the spread. A rung that
would price at or above the touch after tick rounding is not placed.

### One logical entry, not N entries

`gate.py:470` blocks when `position_open or entry_order_resting` — max 1. That check
is **stateless**: the executor passes those booleans in. So the gate needs no change.
The ladder is one logical entry occupying the one slot; the executor aggregates rungs
into the single position it already tracks (`_entry_size`, `_entry_price`,
`_entry_matched_size`). `record_buy_notional` is called with the ladder's committed
notional, so the per-trade and bankroll caps apply to the ladder as a whole.

**gate.py is not modified.** Neither are its tests.

### Fills

Rungs fill independently and partially. The executor tracks per-rung state and derives
the position from the aggregate: filled shares summed, entry price as the
size-weighted average of actual fills. Unfilled rungs are cancelled when the entry
window closes or the position is flattened.

A ladder that fills no rungs is a no-trade: the provisional ledger row is removed, the
same way a blocked entry is handled today.

### Restart

Boot reconciliation currently cancels every resting CLOB order on the account
(`live.py:435`) and re-adopts one position from the journal. A live ladder is
made of resting orders, so that sweep would kill the operator's own ladder on
every restart. The ladder's rungs are journaled at placement, so reconciliation
re-adopts them instead of cancelling: rungs still live in the book stay, rungs
that filled while the app was down are folded into the position, and only orders
the journal cannot account for are cancelled.

### Fees

Rungs rest below the touch, so they are maker fills and pay no taker fee. The
existing fee maths already keys off `_placement_crossed_shares`, which is zero for
a rung that never crossed, so it stays correct — but the entry price must come
from real fill amounts rather than the limit price, since a rung's limit is
deliberately below where the trade would otherwise have happened.

### Paper parity

Hard requirement (one pipeline, mode is a switch). Paper cannot keep filling instantly
at the touch or it will report fills the live ladder would never have got.

Paper simulates the same rungs against the same book snapshots: a rung fills when a
later observed book prints at or below the rung price, capped by the size resting
there. Same ladder maths, same rung prices, same aggregation — only the fill oracle
differs (observed book instead of venue confirmations).

### Making adverse selection measurable

The operator has already measured resting bids on these books at about -3.8c of
adverse selection: they fill preferentially when the outcome is turning against
you. A ladder rests 5-15c under the touch rather than at it, which is a different
seat from the top-of-book maker that measurement killed — deep enough to be
aiming at capital-rotation air pockets rather than competing where informed flow
is. Whether that seat is any better is a question for the recorded data, not for
argument.

So the journal records, per rung fill: the offset that filled, the book at fill,
and the window's outcome. That makes adverse selection measurable *by offset
depth* against the -3.8c baseline, on the marketdata hub's recorded depth and
trade prints. No gating, no scoring, no machinery that decides whether the ladder
is working — just enough recorded to answer the question later.

## Out of scope for this chunk

Exits keep their current behaviour (`best_bid`, bounded wait, cancel). Making exits
rest is a separate safety decision: an exit that rests and never fills means holding
to resolution, which is deliberate today rather than accidental. Flagged, not changed.

## Tasks

1. Ladder maths as a pure, tested function: reference price + settings → rung prices
   and sizes, with tick rounding, venue minimum and redistribution.
2. Ladder settings, defaults, and dashboard surfacing.
3. `submit_entry` posts the ladder; per-rung state and aggregation in the executor;
   cancellation of unfilled rungs.
4. Paper rung simulation against observed books.
5. Restart reconciliation: re-adopt journaled rungs instead of cancelling them.
6. Journaling: every rung placement, fill and cancel, so a ladder is reconstructable.
7. Tests: ladder maths, partial fills, zero fills, cancellation, paper/live agreement
   on identical book sequences.
8. Docs: `docs/strategies/` note on what the ladder changes for every strategy.

## Base and merge order

Branched from `origin/develop` at `531920c`. PR #244 (`chore/remove-5m`) is open and
touches `live.py`'s window parsing and `market_selection.py`; it does not touch the
pricing path, so the conflict surface is small. Rebase on #244 once it lands.
