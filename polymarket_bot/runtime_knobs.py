"""Operator runtime knobs: single dashboard-editable source of truth (#206).

Every tunable strategy/risk/behavior constant that isn't a secret or an infra
setting lives here instead of ``.env`` — the operator sees the current value
and changes it from the dashboard, and the change applies on the next tick
with no restart. Absent an operator override, ``Knob.default`` applies, so a
freshly-cloned checkout behaves exactly like the old env defaults did.

Storage: the generic ``config`` key/value table (``db.get_config`` /
``set_config``), under ``runtime.*`` keys.

Two read paths, matching how each caller needs it:

* ``await get(name)`` — the async, always-current read. Fine anywhere already
  inside an ``async def`` (the dashboard's ``execution_view.py``, a strategy's
  own loop).
* ``cached(name)`` — a synchronous read of whatever the last ``refresh_cache()``
  call loaded, for plain ``def`` helpers that cannot ``await``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from db import get_config, set_config  # type: ignore[import-untyped]

KnobKind = Literal["float", "int", "bool", "enum"]


@dataclass(frozen=True)
class Knob:
    key: str
    default: Any
    kind: KnobKind
    label: str
    min_value: float | None = None
    max_value: float | None = None
    choices: tuple[str, ...] | None = None
    unit: str = ""
    group: str = ""


KNOBS: dict[str, Knob] = {
    # Paper only. Side, price and size come out of the maths; these are the
    # operator's preferences and caps around it, read fresh every pass. The
    # Kelly multiplier is the one preference inside the sizing (spec section 2).
    # Each order is a scaled passive limit order: one parent order split into
    # child orders resting at several price levels on the passive side of the
    # touch (buys at or under the best bid, sells at or over the best ask).
    "fade1h_poll_interval_seconds": Knob(
        "runtime.fade_1h.poll_interval_seconds", 60.0, "float",
        "Pass interval", 10.0, 600.0, unit="s", group="Fade 1h Momentum on 15m",
    ),
    "fade1h_bankroll_usd": Knob(
        "runtime.fade_1h.bankroll_usd", 100.0, "float",
        "Starting paper bankroll", 1.0, 1_000_000.0, unit="USD",
        group="Fade 1h Momentum on 15m",
    ),
    "fade1h_kelly_multiplier": Knob(
        "runtime.fade_1h.kelly_multiplier", 0.5, "float",
        "Kelly multiplier (1 = full Kelly)", 0.0, 1.0, group="Fade 1h Momentum on 15m",
    ),
    "fade1h_max_order_usd": Knob(
        "runtime.fade_1h.max_order_usd", 25.0, "float",
        "Largest single child order (buys)", 1.0, 100_000.0, unit="USD",
        group="Fade 1h Momentum on 15m",
    ),
    # 0 joins the best bid (a sell: the best ask); the maths sizes every level.
    "fade1h_levels_near_cents": Knob(
        "runtime.fade_1h.levels_near_cents", 0.0, "float",
        "Price range for child orders: nearest level below the best bid (sells: above "
        "the best ask), cents", 0.0, 50.0, unit="c",
        group="Fade 1h Momentum on 15m",
    ),
    "fade1h_levels_far_cents": Knob(
        "runtime.fade_1h.levels_far_cents", 15.0, "float",
        "Price range for child orders: deepest level below the best bid (sells: above "
        "the best ask), cents", 0.0, 50.0, unit="c",
        group="Fade 1h Momentum on 15m",
    ),
    "fade1h_reduce_positions": Knob(
        "runtime.fade_1h.reduce_positions", True, "bool",
        "Reduce a held position with a resting sell",
        group="Fade 1h Momentum on 15m",
    ),
    # The price now for the model. The 15m market settles on the Chainlink
    # TWAP-60s print, which is a 60 s average running about 30 s behind, so it
    # is only the settlement's reference, never the price now. Binance is there
    # to compare what the model makes of another feed.
    "fade1h_spot_feed": Knob(
        "runtime.fade_1h.spot_feed", "chainlink", "enum",
        "Price now for the model",
        choices=("chainlink", "binance"),
        group="Fade 1h Momentum on 15m",
    ),
    "fade1h_trade_btc": Knob(
        "runtime.fade_1h.trade_btc", True, "bool", "Trade BTC",
        group="Fade 1h Momentum on 15m",
    ),
    "fade1h_trade_eth": Knob(
        "runtime.fade_1h.trade_eth", True, "bool", "Trade ETH",
        group="Fade 1h Momentum on 15m",
    ),
    "fade1h_trade_sol": Knob(
        "runtime.fade_1h.trade_sol", True, "bool", "Trade SOL",
        group="Fade 1h Momentum on 15m",
    ),
    "fade1h_trade_xrp": Knob(
        "runtime.fade_1h.trade_xrp", True, "bool", "Trade XRP",
        group="Fade 1h Momentum on 15m",
    ),
}

# In-memory mirror of the last `refresh_cache()` read, for sync call sites.
# Seeded with defaults so a value is always available even before the first
# refresh (e.g. during import-time module evaluation in paper.py).
_cache: dict[str, Any] = {name: knob.default for name, knob in KNOBS.items()}


def _decode(raw: str, kind: KnobKind) -> Any:
    if kind == "float":
        return float(raw)
    if kind == "int":
        return int(float(raw))
    if kind == "bool":
        return raw == "1"
    return raw  # enum


def _encode(value: Any, kind: KnobKind) -> str:
    if kind == "bool":
        return "1" if value else "0"
    if kind == "enum":
        return str(value)
    return repr(value)


def _coerce(name: str, value: Any) -> Any:
    knob = KNOBS[name]
    if knob.kind == "float":
        value = float(value)
    elif knob.kind == "int":
        value = int(float(value))
    elif knob.kind == "bool":
        value = bool(value)
    elif knob.kind == "enum":
        value = str(value)
        if knob.choices and value not in knob.choices:
            raise ValueError(f"{name} must be one of {knob.choices}")
    if knob.min_value is not None and value < knob.min_value:
        raise ValueError(f"{name} must be >= {knob.min_value}")
    if knob.max_value is not None and value > knob.max_value:
        raise ValueError(f"{name} must be <= {knob.max_value}")
    return value


async def get(name: str) -> Any:
    """The effective value: an operator override if one is persisted, else the default."""
    override = await get_override(name)
    return override if override is not None else KNOBS[name].default


async def get_override(name: str) -> Any | None:
    """The raw operator override, or ``None`` when unset/blank/invalid.

    For knobs whose "no override" fallback is a caller-supplied value rather
    than the registry default (e.g. ``RiskGate``'s ``GateConfig``, which tests
    construct with their own values) — mirrors the precedence
    ``RiskGate.effective_max_trade_usd`` already uses for its two hand-rolled
    knobs: override wins, else the caller's own default.
    """
    knob = KNOBS[name]
    raw = await get_config(knob.key)
    if raw is None or raw.strip() == "":
        return None
    try:
        value = _decode(raw, knob.kind)
    except ValueError:
        return None
    # A stored choice the knob no longer offers (a value from an earlier build) is invalid:
    # the default applies, and the Settings page shows the same value the code reads.
    if knob.kind == "enum" and knob.choices and value not in knob.choices:
        return None
    return value


async def set(name: str, value: Any) -> Any:
    """Validate against the knob's bounds/choices, persist, and update the cache.

    Raises ``ValueError`` (message is dashboard-safe) on an out-of-range or
    wrong-shape value; nothing is written in that case.
    """
    value = _coerce(name, value)
    knob = KNOBS[name]
    await set_config(knob.key, _encode(value, knob.kind))
    _cache[name] = value
    return value


async def reset(name: str) -> None:
    """Clear the operator override so the knob falls back to its default."""
    knob = KNOBS[name]
    await set_config(knob.key, "")
    _cache[name] = knob.default


async def refresh_cache() -> None:
    """Re-read every knob from SQLite into the in-memory cache.

    Call once per tick (paper loop, live loop, daily-scanner loop) so
    ``cached()`` reads never go stale by more than one tick.
    """
    for name in KNOBS:
        _cache[name] = await get(name)


def cached(name: str) -> Any:
    """Synchronous read of the last ``refresh_cache()`` snapshot.

    Falls back to the knob's default until the first refresh has run (e.g.
    module import time), so this is always safe to call.
    """
    return _cache.get(name, KNOBS[name].default)
