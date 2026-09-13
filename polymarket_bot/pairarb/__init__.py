"""Two-sided maker quoting on 5-minute Up/Down markets — shadow only (#182).

Reproduces the strategy run by the venue's most profitable 5m account
(``0xf8af03f1e68ee7162db8983f0d6dd0dc869854c6``): rest bids on **both** legs of
the same window so a completed pair costs less than the 1.00 it redeems for.

This is market making, not arbitrage. Measured on the live venue 2026-08-14 the
best-*ask* sum is 1.0100 — crossing both legs costs 1.0449 after fees for a 1.00
payout, so no taker opportunity exists. The best-*bid* sum is 0.990, and maker
fills are fee-free. The edge is entirely in being the resting order.

**No order is ever placed from this package.** It reads public books and the
public trade tape and simulates what resting orders would have done. Live
quoting would be a separate, operator-armed decision (``AGENTS.md``).
"""
