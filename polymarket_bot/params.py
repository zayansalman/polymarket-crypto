"""Strategy parameter proposals (Layer 2 — operator-gated auto-tune, #206).

``params_propose`` (CLI) runs the existing backtest grid and writes the
recommended params to ``$DATA_DIR/params_proposed.json`` — never live. The
dashboard's STRATEGY panel surfaces this proposal alongside the currently
active (dashboard-editable, ``polymarket_bot.runtime_knobs``) values with a
backtest delta, so the operator can compare before applying.

The ACTIVE side used to be a second file (``params_active.json``, promoted by
``params_apply --confirm``) parallel to the ``.env`` defaults. That's retired:
the live bot's actual entry thresholds now live in ``runtime_knobs`` (SQLite,
dashboard-editable) — the single source of truth for every tunable knob, not
a third one. ``params_apply`` still exists as the "promote this proposal"
action, but it now writes through ``runtime_knobs.set(...)`` instead of a
JSON file.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class ActiveParams:
    """Tunable strategy parameters. None means "use env default"."""

    entry_edge_min: float
    entry_edge_max: float
    min_confidence: float
    min_remaining_seconds: int
    max_entry_price: float
    min_entry_price: float
    source: str = "env"
    proposed_at: str = ""
    applied_at: str = ""
    backtest_meta: dict = field(default_factory=dict)


def _default_path(filename: str) -> Path:
    data_dir = os.environ.get("DATA_DIR", "./data")
    return Path(data_dir) / filename


PROPOSED_FILE = "params_proposed.json"


def _from_env() -> ActiveParams:
    """Reference point for a proposal's "current" comparison — the registered
    knob defaults, matching what a freshly-cloned checkout starts at."""
    from polymarket_bot import runtime_knobs as _knobs

    return ActiveParams(
        entry_edge_min=_knobs.KNOBS["paper_entry_edge_min"].default,
        entry_edge_max=_knobs.KNOBS["paper_entry_edge_max"].default,
        min_confidence=_knobs.KNOBS["paper_min_confidence"].default,
        min_remaining_seconds=_knobs.KNOBS["paper_entry_min_remaining_seconds"].default,
        max_entry_price=_knobs.KNOBS["paper_max_entry_price"].default,
        min_entry_price=_knobs.KNOBS["paper_min_entry_price"].default,
        source="default",
    )


async def load_active() -> ActiveParams:
    """The strategy's current live values, read from ``runtime_knobs`` (#206).

    For CLI tools (``params_propose``, ``params_apply``) that want to print or
    diff against what the bot is actually enforcing right now — the dashboard
    itself reads ``runtime_knobs`` directly rather than through this.
    """
    from polymarket_bot import runtime_knobs as _knobs

    return ActiveParams(
        entry_edge_min=await _knobs.get("paper_entry_edge_min"),
        entry_edge_max=await _knobs.get("paper_entry_edge_max"),
        min_confidence=await _knobs.get("paper_min_confidence"),
        min_remaining_seconds=await _knobs.get("paper_entry_min_remaining_seconds"),
        max_entry_price=await _knobs.get("paper_max_entry_price"),
        min_entry_price=await _knobs.get("paper_min_entry_price"),
        source="operator" if await _is_operator_set() else "default",
    )


async def _is_operator_set() -> bool:
    from polymarket_bot import runtime_knobs as _knobs

    names = (
        "paper_entry_edge_min", "paper_entry_edge_max", "paper_min_confidence",
        "paper_entry_min_remaining_seconds", "paper_max_entry_price", "paper_min_entry_price",
    )
    for name in names:
        if (await _knobs.get_override(name)) is not None:
            return True
    return False


def load_proposed() -> ActiveParams | None:
    """Return the pending operator-review proposal, or None if absent."""
    p = _default_path(PROPOSED_FILE)
    try:
        with open(p) as f:
            d = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    env = _from_env()
    return ActiveParams(
        entry_edge_min=float(d.get("entry_edge_min", env.entry_edge_min)),
        entry_edge_max=float(d.get("entry_edge_max", env.entry_edge_max)),
        min_confidence=float(d.get("min_confidence", env.min_confidence)),
        min_remaining_seconds=int(
            d.get("min_remaining_seconds", env.min_remaining_seconds)
        ),
        max_entry_price=float(d.get("max_entry_price", env.max_entry_price)),
        min_entry_price=float(d.get("min_entry_price", env.min_entry_price)),
        source="proposed",
        proposed_at=str(d.get("proposed_at", "")),
        backtest_meta=dict(d.get("backtest_meta") or {}),
    )


def save_proposed(params: ActiveParams) -> Path:
    return _atomic_write(_default_path(PROPOSED_FILE), asdict(params))


def _atomic_write(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
    os.replace(tmp, path)
    return path
