"""Operator strategy switches: which strategies may open new positions.

Several strategies run side by side in this process. Each one is an
independent experiment, so each gets its own switch instead of a single
"which strategy is active" selector: the point of the paper lab is watching
more than one run at once.

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

from db import get_config, set_config  # type: ignore[import-untyped]


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
    "btc_updown": Strategy(
        name="btc_updown",
        label="BTC Up/Down loop",
        description=(
            "Prices the selected Up/Down window against the Chainlink feed and "
            "takes one confidence-sized entry per window. Start/Stop runs the "
            "loop; this switch decides whether it may enter."
        ),
    ),
    "daily_altcoin": Strategy(
        name="daily_altcoin",
        label="Daily altcoin scanner",
        description=(
            "Scores the 24h Up/Down market for each tracked altcoin every scan "
            "and opens one flat-size paper position on the strongest signal. "
            "Paper only — it has no live path."
        ),
    ),
}


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
