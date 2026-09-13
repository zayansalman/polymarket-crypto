"""Layer 2 — operator-gated promotion of a proposed strategy param set (#206).

Reads ``$DATA_DIR/params_proposed.json`` (written by ``params_propose``) and,
when ``--confirm`` is passed, writes each value through
``polymarket_bot.runtime_knobs`` — the same store the dashboard's Settings
tab writes to. The live bot re-reads these every tick; no restart. Refuses
without ``--confirm`` so accidental invocation is impossible.

Run::

    python -m polymarket_bot.params_apply           # prints the proposal, does NOT apply
    python -m polymarket_bot.params_apply --confirm # applies; live bot picks it up next tick
"""

from __future__ import annotations

import argparse
import asyncio

from polymarket_bot import runtime_knobs as _knobs
from polymarket_bot.params import load_proposed

_KNOB_NAMES = (
    "paper_entry_edge_min",
    "paper_entry_edge_max",
    "paper_min_confidence",
    "paper_entry_min_remaining_seconds",
    "paper_max_entry_price",
    "paper_min_entry_price",
)
_FIELD_FOR_KNOB = {
    "paper_entry_edge_min": "entry_edge_min",
    "paper_entry_edge_max": "entry_edge_max",
    "paper_min_confidence": "min_confidence",
    "paper_entry_min_remaining_seconds": "min_remaining_seconds",
    "paper_max_entry_price": "max_entry_price",
    "paper_min_entry_price": "min_entry_price",
}


async def _current_values() -> dict[str, object]:
    return {name: await _knobs.get(name) for name in _KNOB_NAMES}


async def _apply(proposed) -> None:
    for name in _KNOB_NAMES:
        await _knobs.set(name, getattr(proposed, _FIELD_FOR_KNOB[name]))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--confirm",
        action="store_true",
        help="REQUIRED to actually promote proposed -> active",
    )
    args = ap.parse_args()

    proposed = load_proposed()
    if proposed is None:
        print("no proposal found at params_proposed.json — run params_propose first.")
        return 2

    active = asyncio.run(_current_values())
    print("=== currently active ===")
    print(
        f"  entry_edge_min={active['paper_entry_edge_min']:.3f} "
        f"min_confidence={active['paper_min_confidence']:.2f} "
        f"min_remaining_seconds={active['paper_entry_min_remaining_seconds']} "
        f"max_entry_price={active['paper_max_entry_price']:.2f}"
    )
    print()
    print("=== proposed (pending) ===")
    print(
        f"  entry_edge_min={proposed.entry_edge_min:.3f} "
        f"min_confidence={proposed.min_confidence:.2f} "
        f"min_remaining_seconds={proposed.min_remaining_seconds} "
        f"max_entry_price={proposed.max_entry_price:.2f} "
        f"proposed_at={proposed.proposed_at}"
    )
    m = proposed.backtest_meta or {}
    if m:
        print(
            f"  backtest: current pnl=${m.get('current_pnl', 0.0):+.2f} -> "
            f"proposed pnl=${m.get('recommended_pnl', 0.0):+.2f} "
            f"(trades {m.get('current_trades')} -> {m.get('recommended_trades')})"
        )

    if not args.confirm:
        print()
        print("Refusing to apply without --confirm. Re-run with --confirm to promote.")
        return 1

    asyncio.run(_apply(proposed))
    print()
    print("PROMOTED -> runtime_knobs (SQLite)")
    print("Live bot picks this up on its next tick — no restart.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
