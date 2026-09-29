"""Operator strategy switches: which strategies may open new positions.

Each strategy in this process is an independent experiment with its own
switch, so more than one can run at once and be watched side by side.

Everything registered here is visible in the dashboard — a strategy that
trades where nobody can see it does not belong in this process.

**Off means "open nothing new", never "abandon".** Every strategy reads its
switch at the top of its tick, ahead of entry — settlement, exits and
bookkeeping run regardless, so flipping a switch off can never strand a
position that is already open.

Storage is the generic ``config`` key/value table under ``runtime.strategy.*``,
the same table and convention ``runtime_knobs.py`` uses for the tuning knobs.
Absent an operator override, ``Strategy.default`` applies — so a fresh
checkout behaves exactly as it did before these switches existed.
"""

from __future__ import annotations

from dataclasses import dataclass

from ems.db import get_config, set_config  # type: ignore[import-untyped]


@dataclass(frozen=True)
class Strategy:
    name: str
    label: str
    description: str
    default: bool = True

    @property
    def key(self) -> str:
        return f"runtime.strategy.{self.name}.enabled"


STRATEGIES: dict[str, Strategy] = {
    "fade_1h_momentum_15m": Strategy(
        name="fade_1h_momentum_15m",
        label="Fade 1h Momentum on 15m",
        description=(
            "Rests scaled passive limit orders on BTC/ETH/SOL/XRP 15m Up/Down "
            "windows where the model says they pay: child orders at several "
            "price levels at or under the best bid, sized by joint Kelly across "
            "the four coins. A position that turns is cut with a resting sell of "
            "the shares held, never by buying the other side; nothing crosses "
            "the spread. Records every coin's inputs each minute. Paper only — "
            "it has no live path."
        ),
    ),
    "kelly_horse_race": Strategy(
        name="kelly_horse_race",
        label="Kelly horse-race",
        description=(
            "Once per BTC 15m Up/Down window: prices the chance of Up from the last hour's "
            "move and volatility, rolls a die weighted by it to pick the side, and rests one "
            "passive limit buy at that side's best bid with a random size between the "
            "venue's minimum order and the notional cap. The same order goes to paper and, "
            "when armed, live. Nothing crosses the spread."
        ),
    ),
}

"""Registry order is render order. Keys are permanent: the switch is stored at
``runtime.strategy.<name>.enabled``, so renaming a ``name`` silently resets an
operator's choice."""


def _strategy(name: str) -> Strategy:
    strategy = STRATEGIES.get(name)
    if strategy is None:
        raise ValueError(f"unknown strategy {name!r}")
    return strategy


async def enabled(name: str) -> bool:
    """True when ``name`` may open new positions."""
    strategy = _strategy(name)
    raw = await get_config(strategy.key)
    if raw is None or raw == "":
        return strategy.default
    return raw == "1"


async def set_enabled(name: str, value: bool) -> bool:
    """Persist ``name``'s switch; returns the stored state."""
    strategy = _strategy(name)
    value = bool(value)
    await set_config(strategy.key, "1" if value else "0")
    return value


async def enabled_map() -> dict[str, bool]:
    """Every strategy's switch, in registry order — what the card renders."""
    return {name: await enabled(name) for name in STRATEGIES}
