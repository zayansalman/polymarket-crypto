"""Shadow candidates that trade on the bot's learned chances (paper only).

Two patterns from the 2026-11-16..18 replay of the bot's own snapshots,
pre-registered here so they are judged ONLY on windows after they started
logging (fixed specs; never tuned afterwards):

* :func:`learned_late_v1` — enter only with 60–180 s left. The raw model's
  error score beat the market's in the last two minutes and lost to it
  earlier, and the bot's replayed entries made money late and nothing early.
* :func:`learned_mid_v1` — buy only 60¢–90¢ shares. Replayed entries at
  50–60¢ and above 90¢ lost after fees; 60–90¢ made money.

Both use the learned chance from :mod:`polymarket_bot.learning` and require
the edge to beat the taker fee. Post-hoc patterns from ~2 days of data are
hypotheses, not findings: most such slices die out of sample
(docs/archive/FINDINGS.md §2). By 2026-11-19 the raw-model versions of both
had already turned behind-beyond-luck in the Strategy Lab replay.

Added 2026-11-19 from the Strategy Lab's 576-rule grid (every top pick
dropped the 7¢ edge cap; picked on the older half, positive on the newer half
but within luck), again fixed-spec and judged only on later windows:

* :func:`raw_wide_v1` — raw model, fee check, edges of 3¢+ with no cap.
* :func:`learned_wide_v1` — learned chances, edges of 3¢+ with no cap.

Since 2026-11-19 the learned candidates use the latest fit even when the main
bot has not adopted it (``require_active=False``), so learning keeps being
tested forward instead of going silent whenever its holdout check fails.
"""
from __future__ import annotations

from dataclasses import replace

from polymarket_bot import learning, strategy
from polymarket_bot.shadow.fees import DEFAULT_TAKER_FEE_RATE
from polymarket_bot.shadow.types import ShadowSignal, SnapshotView

LATE_MAX_REMAINING_SECONDS = 180
MID_MIN_PRICE, MID_MAX_PRICE = 0.60, 0.90
WIDE_EDGE_MIN, WIDE_EDGE_MAX = 0.03, 1.0


def _learned_signal(
    view: SnapshotView, params: strategy.StrategyParams, tag: str
) -> ShadowSignal | None:
    up_ask, down_ask = view.up_ask, view.down_ask
    learned_up = learning.adjust(view.fair_up, up_ask, down_ask, require_active=False)
    if learned_up is None:
        return None
    return _signal_at(view, params, tag, learned_up)


def _signal_at(
    view: SnapshotView, params: strategy.StrategyParams, tag: str, up_chance: float
) -> ShadowSignal | None:
    up_ask, down_ask = view.up_ask, view.down_ask
    edge_up = (up_chance - up_ask) if up_ask is not None else None
    edge_down = ((1.0 - up_chance) - down_ask) if down_ask is not None else None
    side, confidence, _notional, reason = strategy.signal_from_executable_edges(
        edge_up, edge_down, view.remaining_seconds, up_ask, down_ask, params
    )
    if side is None:
        return None
    entry = up_ask if side == "Up" else down_ask
    if entry is None:
        return None
    edge = edge_up if side == "Up" else edge_down
    fair = up_chance if side == "Up" else 1.0 - up_chance
    return ShadowSignal(
        side=side,
        entry_price=float(entry),
        fair_prob=float(fair),
        edge=float(edge or 0.0),
        confidence=float(confidence),
        reason=f"{tag}: {reason}",
    )


def learned_late_v1(
    view: SnapshotView, params: strategy.StrategyParams
) -> ShadowSignal | None:
    """Learned chances, fee-aware, entries only with 60–180 s left."""
    if view.remaining_seconds > LATE_MAX_REMAINING_SECONDS:
        return None
    rules = replace(params, fee_rate=DEFAULT_TAKER_FEE_RATE)
    return _learned_signal(view, rules, "learned late")


def learned_mid_v1(
    view: SnapshotView, params: strategy.StrategyParams
) -> ShadowSignal | None:
    """Learned chances, fee-aware, only 60¢–90¢ shares."""
    rules = replace(
        params,
        fee_rate=DEFAULT_TAKER_FEE_RATE,
        min_entry_price=MID_MIN_PRICE,
        max_entry_price=MID_MAX_PRICE,
    )
    return _learned_signal(view, rules, "learned mid-price")


def raw_wide_v1(
    view: SnapshotView, params: strategy.StrategyParams
) -> ShadowSignal | None:
    """Raw model, fee-aware, edges of 3¢+ with no upper cap."""
    rules = replace(params, fee_rate=DEFAULT_TAKER_FEE_RATE,
                    entry_edge_min=WIDE_EDGE_MIN, entry_edge_max=WIDE_EDGE_MAX)
    return _signal_at(view, rules, "raw wide", view.fair_up)


def learned_wide_v1(
    view: SnapshotView, params: strategy.StrategyParams
) -> ShadowSignal | None:
    """Learned chances, no fee check, edges of 3¢+ with no upper cap."""
    rules = replace(params, fee_rate=0.0, entry_edge_min=WIDE_EDGE_MIN,
                    entry_edge_max=WIDE_EDGE_MAX)
    return _learned_signal(view, rules, "learned wide")
