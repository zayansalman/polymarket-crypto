"""Operator runtime knobs: single dashboard-editable source of truth (#206).

Every tunable strategy/risk/behavior constant that isn't a secret or an infra
setting lives here instead of ``.env`` — the operator sees the current value
and changes it from the dashboard, and the change applies on the next tick
with no restart. Absent an operator override, ``Knob.default`` applies, so a
freshly-cloned checkout behaves exactly like the old env defaults did.

Storage: the existing generic ``config`` key/value table (``db.get_config`` /
``set_config``), under ``runtime.*`` keys — the same table and prefix
``polymarket_exec/execution/gate.py`` already uses for
``runtime.max_trade_usd`` / ``runtime.trade_shares``. Those two stay hand-rolled
in ``gate.py`` (they have unique dollar-vs-shares precedence logic); everything
else registers here.

Two read paths, matching how each caller needs it:

* ``await get(name)`` — the async, always-current read. Fine anywhere already
  inside an ``async def`` (the dashboard's ``execution_view.py``, the gate's
  ``refresh_*`` methods, the daily scanner's own loop).
* ``cached(name)`` — a synchronous read of whatever the last ``refresh_cache()``
  call loaded. Needed by ``polymarket_bot/paper.py``, which reads several of
  these knobs from plain ``def`` helpers (and one module-level constant at
  import time) that cannot ``await``. ``refresh_cache()`` is called once per
  tick, mirroring ``RiskGate.refresh_runtime_limits()``.
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
    # --- Copy trade (polymarket_bot/copytrade/) --------------------------
    # The target is an enum rather than free text: the candidate set is closed
    # and each entry carries the measurement that earned it a place
    # (copytrade/targets.py). A typo'd address would poll a wallet with no
    # measured edge, which is worse than no copying at all.
    "copy_follow_all": Knob(
        "runtime.copy.follow_all", True, "bool",
        "Follow every registered target", group="Copy trade",
    ),
    "copy_scale": Knob(
        "runtime.copy.scale", 1.0, "float",
        "Fraction of their size to mirror", 0.01, 5.0, group="Copy trade",
    ),
    # Their clips are often 1-5 shares, below the venue's 5-share floor. True
    # skips those fills; False rounds the copy UP to the floor, which means
    # taking a larger bet than they did. Skipping is the honest default.
    "copy_skip_below_min": Knob(
        "runtime.copy.skip_below_min", True, "bool",
        "Skip fills under the venue minimum", group="Copy trade",
    ),
    "copy_max_shares": Knob(
        "runtime.copy.max_shares", 50.0, "float",
        "Max shares per copy", 5.0, 5000.0, unit="sh", group="Copy trade",
    ),
    "copy_max_slippage_cents": Knob(
        "runtime.copy.max_slippage_cents", 3.0, "float",
        "Skip a copy above this slippage", 0.0, 50.0, unit="c", group="Copy trade",
    ),
    # One registered target, so this is a one-item list. Kept as an enum rather
    # than free text because a typo'd address polls a wallet with no measured
    # edge, which is worse than not copying at all.
    "copy_target_wallet": Knob(
        "runtime.copy.target_wallet", "0xd9013df863c1ba932780857b020dfdeacedf8e14",
        "enum", "Copy target wallet",
        choices=("0xd9013df863c1ba932780857b020dfdeacedf8e14",),
        group="Copy trade",
    ),
    "copy_poll_interval_seconds": Knob(
        "runtime.copy.poll_interval_seconds", 12.0, "float",
        "Target poll interval", 5.0, 600.0, unit="s", group="Copy trade",
    ),
    # A fill older than this cannot be followed at any latency, so it is never
    # worth a book lookup or a ledger row. Also a backstop against dedupe slips
    # re-surfacing history.
    "copy_max_fill_age_seconds": Knob(
        "runtime.copy.max_fill_age_seconds", 300.0, "float",
        "Ignore fills older than", 30.0, 3600.0, unit="s", group="Copy trade",
    ),
    "copy_observe_limit": Knob(
        "runtime.copy.observe_limit", 100, "int",
        "Fills fetched per poll", 10, 500, group="Copy trade",
    ),
    # --- Maker (polymarket_bot/maker/) ------------------------------------
    # Measured over 7,000 resolved crypto Up/Down markets: real resting orders
    # that filled between 0.55 and 0.92 returned +2 to +6.6c/share held to
    # resolution, fee-free, while fills under the midpoint lost 5-7c. The band
    # is a knob and not a constant because that result is the thing under test.
    # The maker's on/off switch moved to ``polymarket_bot.strategies`` so it
    # sits with the other strategies and obeys the same "off stops new entries
    # only" contract. A second switch here would be one more place for the two
    # to disagree.
    "maker_band_lo": Knob(
        "runtime.maker.band_lo", 0.55, "float",
        "Quote only at or above", 0.05, 0.95, group="Maker",
    ),
    "maker_band_hi": Knob(
        "runtime.maker.band_hi", 0.92, "float",
        "Quote only below", 0.10, 1.0, group="Maker",
    ),
    "maker_size": Knob(
        "runtime.maker.size", 25.0, "float",
        "Shares per quote", 5.0, 1000.0, unit="sh", group="Maker",
    ),
    # Improving a tick costs 1c of the measured edge and buys front of queue.
    # Joining keeps the cent and waits behind everything already resting. Both
    # are recorded with their queue depth so the ledger can settle the question.
    "maker_improve_tick": Knob(
        "runtime.maker.improve_tick", True, "bool",
        "Improve best bid by one tick", group="Maker",
    ),
    "maker_max_spread_cents": Knob(
        "runtime.maker.max_spread_cents", 6.0, "float",
        "Skip books wider than", 1.0, 50.0, unit="c", group="Maker",
    ),
    # A fill in the last seconds is a coin flip on a stale price, not the edge
    # that was measured.
    "maker_min_seconds_left": Knob(
        "runtime.maker.min_seconds_left", 120, "int",
        "Do not quote inside", 0, 3600, unit="s", group="Maker",
    ),
    "maker_poll_interval_seconds": Knob(
        "runtime.maker.poll_interval_seconds", 45.0, "float",
        "Quote/fill poll interval", 10.0, 600.0, unit="s", group="Maker",
    ),
    # --- Paper strategy (polymarket_bot/paper.py) -------------------------
    "paper_min_trade_usd": Knob(
        "runtime.paper.min_trade_usd", 1.0, "float", "Min trade size",
        0.0, 1000.0, unit="USD", group="Paper strategy",
    ),
    "paper_max_trade_usd": Knob(
        "runtime.paper.max_trade_usd", 5.0, "float", "Max trade size (paper)",
        0.0, 1000.0, unit="USD", group="Paper strategy",
    ),
    "paper_target_return": Knob(
        "runtime.paper.target_return", 0.10, "float",
        "Target return (take-profit)", 0.0, 5.0, group="Paper strategy",
    ),
    "paper_stop_return": Knob(
        "runtime.paper.stop_return", -0.08, "float",
        "Stop return (stop-loss)", -1.0, 0.0, group="Paper strategy",
    ),
    "paper_tick_seconds": Knob(
        "runtime.paper.tick_seconds", 5.0, "float", "Tick interval",
        1.0, 300.0, unit="s", group="Paper strategy",
    ),
    "paper_time_exit_seconds": Knob(
        "runtime.paper.time_exit_seconds", 45, "int",
        "Time-based exit (seconds remaining)", 0, 300, unit="s", group="Paper strategy",
    ),
    "exit_style": Knob(
        "runtime.paper.exit_style", "settle", "enum", "Exit style",
        choices=("settle", "scalp"), group="Paper strategy",
    ),
    # --- Live risk limits (polymarket_exec/execution/gate.py, live.py) -----
    "live_daily_loss_halt_usd": Knob(
        "runtime.live.daily_loss_halt_usd", 10.0, "float",
        "Daily loss halt", 0.0, 100000.0, unit="USD", group="Live risk limits",
    ),
    "live_max_entry_slippage": Knob(
        "runtime.live.max_entry_slippage", 0.02, "float",
        "Max entry slippage", 0.0, 1.0, group="Live risk limits",
    ),
    "live_bankroll_cap_usd": Knob(
        "runtime.live.bankroll_cap_usd", 0.0, "float",
        "Daily bankroll cap (0 = disabled)", 0.0, 1000000.0,
        unit="USD", group="Live risk limits",
    ),
    "live_exit_fill_timeout_seconds": Knob(
        "runtime.live.exit_fill_timeout_seconds", 10.0, "float",
        "Exit fill timeout", 0.0, 300.0, unit="s", group="Live risk limits",
    ),
    # --- Daily altcoin scanner (polymarket_bot/daily/scanner.py) -----------
    "daily_trade_usd": Knob(
        "runtime.daily.trade_usd", 10.0, "float", "Trade size",
        0.0, 1000.0, unit="USD", group="Daily scanner",
    ),
    "daily_scan_interval_seconds": Knob(
        "runtime.daily.scan_interval_seconds", 60.0, "float",
        "Scan interval", 5.0, 3600.0, unit="s", group="Daily scanner",
    ),
    "daily_entry_edge_min": Knob(
        "runtime.daily.entry_edge_min", 0.045, "float", "Min entry edge",
        0.0, 1.0, group="Daily scanner",
    ),
    "daily_vol_lookback_days": Knob(
        "runtime.daily.vol_lookback_days", 30, "int",
        "Volatility lookback", 1, 365, unit="d", group="Daily scanner",
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
        return _decode(raw, knob.kind)
    except ValueError:
        return None


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
