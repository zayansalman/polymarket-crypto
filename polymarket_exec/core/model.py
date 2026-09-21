"""The domain model the live path uses.

There was no canonical model before this: the repo had eight incompatible
"position" representations, five "fill", seven "quote", three classes named
``MarketWindow`` and two named ``PaperSnapshot`` — none of it shared, most of
it degrading to ``dict[str, Any]`` the moment it crossed a function boundary
(the dead-code cleanup of 2026-09-21 deleted a NINTH, unused one:
``polymarket_exec/core/types.py``, which nothing live had ever called).

This is deliberately small — four frozen dataclasses plus the ``Side`` enum
already used as raw strings ("Up"/"Down") throughout ``polymarket_bot``. It
does not replace every dict-shaped row overnight; see issue #266. It exists so
that new code — the repositories in the next step of that issue, and any
strategy written after this lands — has one vocabulary to converge on instead
of inventing a tenth.

Field names carry their own units (``price_usd``, ``size_shares``,
``notional_usd``) on purpose: a bare ``price`` or ``size`` is exactly the kind
of ambiguity that let ``BookTop``/``TopOfBook``/``MarketQuote``/``Book`` drift
into five different shapes for the same idea.

This module imports nothing but the stdlib. Per the layering issue #266 asks
for eventually, ``core`` depends on nothing else in the tree.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Side(str, Enum):
    """Which outcome token. Values match the "Up"/"Down" strings already
    written into every ledger row and every strategy's decision — this enum
    does not introduce a new convention, it types the existing one."""

    UP = "Up"
    DOWN = "Down"


@dataclass(frozen=True)
class Market:
    """One Up/Down window: its identity and its two outcome tokens.

    ``slug`` is the thing every part of the app already keys on
    (``paper_positions.window_slug``, every ledger's ``window_slug`` column,
    every settlement lookup). ``condition_id`` and the two token ids are what
    the CLOB and settlement connectors need instead.
    """

    slug: str
    condition_id: str
    up_token_id: str
    down_token_id: str
    question: str = ""
    start_ts: int | None = None
    end_ts: int | None = None


@dataclass(frozen=True)
class Quote:
    """Top of book for one outcome token.

    Supersedes, in shape though not yet in call sites, ``polymarket_bot.
    paper.BookTop``, ``polymarket_exec.marketdata.order_book.TopOfBook``,
    ``polymarket_exec.marketdata.hub.MarketQuote``, ``polymarket_bot.maker.
    quoter.Book``, and ``polymarket_bot.pairarb.types.BookSide`` (the last of
    those was deleted in the RPC-path cleanup, 2026-09-21) — five
    independent reimplementations of "best bid, best ask, and their sizes."
    """

    best_bid: float | None = None
    best_ask: float | None = None
    bid_size: float | None = None
    ask_size: float | None = None

    @property
    def crossed(self) -> bool:
        """A bid above the ask — a malformed or momentarily inconsistent book."""
        return (
            self.best_bid is not None
            and self.best_ask is not None
            and self.best_bid > self.best_ask
        )

    @property
    def buyable(self) -> bool:
        """An entry BUY needs an ask, on a book that is not crossed."""
        return self.best_ask is not None and not self.crossed


@dataclass(frozen=True)
class Fill:
    """One executed trade, ours or observed from someone else's wallet.

    Deliberately venue-agnostic: the live executor's own fills, a copy-trade
    target's public fill, and a maker's resting-order fill all reduce to
    "this many shares of this side, at this price, for this fee" once
    they're through the venue-specific parsing. What differs — how the fill
    was detected, whether it crossed the spread — belongs in the strategy
    that produced it, not in the fill itself.
    """

    market_slug: str
    side: Side
    price_usd: float
    size_shares: float
    fee_usd: float = 0.0
    filled_at: str | None = None  # ISO 8601, matching every other timestamp column

    @property
    def notional_usd(self) -> float:
        return self.price_usd * self.size_shares

    @property
    def cost_usd(self) -> float:
        """All-in cost including the fee — what actually left the account."""
        return self.notional_usd + self.fee_usd


@dataclass(frozen=True)
class Position:
    """One strategy's position in one market: entry, and, once closed, exit.

    Every ledger in the repo (``polymarket_bot/{copytrade,maker,daily}/
    ledger.py``, and the ``paper_positions`` table itself) stores exactly
    this shape today, each with its own column names and its own
    hand-written SQL. This is the type a repository (issue #266, step 2)
    reads a row into and writes a row from, so that shape stops being
    reinvented per strategy.
    """

    market_slug: str
    side: Side
    entry_price_usd: float
    size_shares: float
    opened_at: str
    state: str = "open"  # "open" | "closed"
    exit_price_usd: float | None = None
    closed_at: str | None = None
    realized_pnl_usd: float | None = None

    @property
    def notional_usd(self) -> float:
        return self.entry_price_usd * self.size_shares

    @property
    def is_open(self) -> bool:
        return self.state == "open"
