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
            "Would price the selected Up/Down window against the Chainlink "
            "feed and take one confidence-sized entry per window. NOTHING IS "
            "WIRED TO IT: market_selection.LOOP_SUPPORTED is empty since the "
            "5m family was retired, so every selection is unwired and this "
            "switch gates a loop that cannot enter. Leave it off until a "
            "market is pointed at the loop."
        ),
    ),
    "daily_altcoin": Strategy(
        name="daily_altcoin",
        label="Daily altcoin scanner",
        description=(
            "Scores the 24h Up/Down market for each tracked altcoin every scan "
            "and opens one flat-size paper position on the strongest signal "
            "versus a realized-volatility model. Paper only — it has no live "
            "path."
        ),
    ),
    "maker": Strategy(
        name="maker",
        label="Maker — rest on the favourite",
        description=(
            "Rests ONE passive bid per crypto Up/Down market when the "
            "favourite sits inside the 0.55-0.92 band, never crosses the "
            "spread, and holds to resolution. One fixed clip per market, "
            "never a second bite. Paper only."
        ),
    ),
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
