"""Resting limit ladder: turn one entry decision into several resting BUY rungs.

Source: Zayan (operator), 2026-09-19 — never a market order and never a marketable
limit; rest BUYs 5-15 cents below the current price, several rungs laddered, and let
price come down to them. Entry is a claim on the order book's structure, not on the
event outcome.

This module is pure arithmetic: reference price plus a ladder shape in, rung prices
and sizes out. It has no venue, no I/O and no state, so both the live executor and
the paper simulation build identical rungs from it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

DEFAULT_RUNGS = 5
DEFAULT_MIN_OFFSET = 0.05
DEFAULT_MAX_OFFSET = 0.15


@dataclass(frozen=True)
class LadderRung:
    """One resting BUY: *size* shares bid at *price*, *offset* below the reference."""

    price: float
    size: float
    offset: float

    @property
    def notional_usd(self) -> float:
        return round(self.price * self.size, 6)


@dataclass(frozen=True)
class LadderSpec:
    """The ladder's shape, independent of any particular price or size."""

    rungs: int = DEFAULT_RUNGS
    min_offset: float = DEFAULT_MIN_OFFSET
    max_offset: float = DEFAULT_MAX_OFFSET

    def __post_init__(self) -> None:
        if self.rungs < 1:
            raise ValueError(f"rungs must be at least 1, got {self.rungs}")
        if self.min_offset <= 0:
            raise ValueError(f"min_offset must be positive, got {self.min_offset}")
        if self.max_offset < self.min_offset:
            raise ValueError(
                f"max_offset {self.max_offset} is below min_offset {self.min_offset}"
            )

    def offsets(self, count: int) -> list[float]:
        """*count* offsets spanning [min_offset, max_offset], nearest rung first."""
        if count == 1:
            return [self.min_offset]
        step = (self.max_offset - self.min_offset) / (count - 1)
        return [self.min_offset + step * i for i in range(count)]


def _round_price_to_tick(price: float, tick: float) -> float:
    decimals = max(0, -int(math.floor(math.log10(tick))))
    return round(round(price / tick) * tick, decimals)


def _round_size_down(size: float) -> float:
    return math.floor(size * 100) / 100


def build_ladder(
    reference_price: float,
    notional_usd: float,
    *,
    spec: LadderSpec | None = None,
    tick: float,
    min_size: float,
) -> list[LadderRung]:
    """Rungs for one entry, nearest the reference first.

    *notional_usd* is the ladder's total commitment, split evenly across the rungs;
    sizes round down so a fully filled ladder never spends more than was authorised.

    Returns fewer rungs than requested when the notional cannot put *min_size* on
    each one, re-spacing the survivors across the full offset range rather than
    dropping the rungs nearest the touch. Returns an empty list when the ladder is
    impossible — a reference price too low to sit 5 cents under, or a notional too
    small for a single rung. An empty ladder is a no-trade, not an error.
    """
    spec = spec or LadderSpec()
    if reference_price <= 0 or notional_usd <= 0 or tick <= 0 or min_size <= 0:
        return []

    for count in range(spec.rungs, 0, -1):
        rungs = _try_build(
            reference_price, notional_usd, spec, count, tick=tick, min_size=min_size
        )
        if rungs:
            return rungs
    return []


def _try_build(
    reference_price: float,
    notional_usd: float,
    spec: LadderSpec,
    count: int,
    *,
    tick: float,
    min_size: float,
) -> list[LadderRung]:
    """A *count*-rung ladder, or [] when any rung would be unplaceable."""
    per_rung_notional = notional_usd / count
    rungs: list[LadderRung] = []
    for offset in spec.offsets(count):
        price = _round_price_to_tick(reference_price - offset, tick)
        # A rung must rest strictly below the reference, or it is the marketable
        # order this whole module exists to avoid.
        if price >= reference_price or price < tick:
            return []
        size = _round_size_down(per_rung_notional / price)
        if size < min_size:
            return []
        rungs.append(LadderRung(price=price, size=size, offset=round(offset, 6)))
    return rungs
