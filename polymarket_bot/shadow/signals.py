"""Candidate strategies for the shadow forward-tester.

Each function is a **pure** decision: given an immutable
:class:`~polymarket_bot.shadow.types.SnapshotView` and the active
:class:`~polymarket_bot.strategy.StrategyParams`, it returns the would-be trade as
a :class:`~polymarket_bot.shadow.types.ShadowSignal`, or ``None`` for "no trade
this tick". No database, no clock, no I/O — the loop wiring (built
separately) owns persistence and settlement, so these functions can be
unit-tested with hand-built fixtures and reasoned about in isolation.

The candidates here layer an extra gate on top of the live v0 signal
(:func:`polymarket_bot.strategy.signal_from_executable_edges`), which is reused
verbatim rather than reimplemented:

* :func:`cushion_favorite_v2` — take v0's pick only when spot sits a minimum
  number of basis points on the favourable side of the reference print, so
  a hair-thin lead near a pinned price does not count as a real cushion.
* :func:`cushion_fresh_v7` — v2 restricted to the first 60s of the window
  with claimed edges capped at 0.065 (postmortem-motivated challenger, #142).
* :func:`cushion_fresh_v7_f45` — v7 with the freshness gate tightened to 45s
  (pre-registered in #149 from day-5 race observation that 46–60s bucket is
  negative in both fresh models; evaluated by the tick-replay harness only).
* :func:`cushion_fresh_v7_f45_spread` — v7[f45] with an additional bid-ask
  spread guard: entry only when the chosen side's spread ≤ 1 tick (≤ $0.01).
  Requires ``view.up_bid`` / ``view.down_bid`` populated (replay-harness only;
  returns None when bid is absent, so safe in all other paths).

Retired 2026-07-02 (#142; see docs/archive/POSTMORTEM_2026-07.md): late_convergence_v3,
down_skeptic_v4, cushion_drift_v5, down_skeptic_drift_v6.
"""
from __future__ import annotations

from polymarket_bot import strategy
from polymarket_bot.shadow.types import ShadowSignal, SnapshotView


def cushion_favorite_v2(
    view: SnapshotView,
    params: strategy.StrategyParams,
    cushion_min_bps: float = 1.5,
) -> ShadowSignal | None:
    """Reuse v0's pick, but require a minimum spot-vs-reference cushion.

    v0 (:func:`polymarket_bot.strategy.signal_from_executable_edges`) decides the
    side, confidence, and reason from the executable edges. This wrapper
    then demands that the chosen side has at least ``cushion_min_bps`` basis
    points of distance between spot and the reference print *in its favour*
    — Up needs ``spot`` above ``reference``, Down needs ``spot`` below it.
    A lead thinner than the cushion is treated as noise around a pinned
    price and skipped, even if the edge cleared v0's threshold.

    Args:
        view: Immutable per-tick market view.
        params: Active strategy parameters (v0 thresholds).
        cushion_min_bps: Minimum favourable spot-vs-reference gap, in bps.

    Returns:
        A :class:`ShadowSignal` when v0 wants the trade *and* the cushion
        clears the floor, otherwise ``None``.
    """
    edge_up = view.fair_up - view.up_ask if view.up_ask is not None else None
    edge_down = (1.0 - view.fair_up) - view.down_ask if view.down_ask is not None else None

    side, confidence, _notional, reason = strategy.signal_from_executable_edges(
        edge_up,
        edge_down,
        view.remaining_seconds,
        view.up_ask,
        view.down_ask,
        params,
    )
    if side is None:
        return None

    if side == "Up":
        entry_price = view.up_ask
        edge = edge_up
        fair_prob = view.fair_up
    else:
        entry_price = view.down_ask
        edge = edge_down
        fair_prob = 1.0 - view.fair_up

    # mypy/ruff: the chosen side's ask cannot be None — v0 only selects a
    # side whose ask was a usable candidate — but assert intent explicitly.
    if entry_price is None or edge is None:
        return None

    favourable_gap = (view.spot - view.reference) if side == "Up" else (view.reference - view.spot)
    cushion_bps = favourable_gap / view.spot * 1e4
    if cushion_bps < cushion_min_bps:
        return None

    return ShadowSignal(
        side=side,
        entry_price=entry_price,
        fair_prob=fair_prob,
        edge=edge,
        confidence=confidence,
        reason=f"cushion {cushion_bps:.1f}bps; {reason}",
    )


_WINDOW_SECONDS = 300  # 5-minute up/down markets


def cushion_fresh_v7(
    view: SnapshotView,
    params: strategy.StrategyParams,
    cushion_min_bps: float = 1.5,
    max_age_seconds: int = 60,
    edge_cap: float = 0.065,
) -> ShadowSignal | None:
    """``cushion_favorite_v2``, restricted to FRESH windows and CAPPED claims.

    Two one-parameter gates on top of v2, frozen a-priori from the 2026-07
    postmortem recon (docs/archive/POSTMORTEM_2026-07.md; the resumed shadow race is
    their out-of-sample test):

    * **Freshness** (``max_age_seconds``): all of the concept family's
      realized profit sat in the first 60s of the window (v0 0–60s entries:
      +$88.57 at 62.6% win; every later bucket negative). A window that has
      aged past the gate is treated as already efficient and skipped.
    * **Edge cap** (``edge_cap``): claimed edges above ~0.065 realized WORST
      (v2: +4.8¢/share net below the cap vs −1.2¢ above) — when the model
      disagrees most with the book, the book is usually right (adverse
      selection at the touch). An oversized claim is distrusted entirely,
      not traded harder.
    """
    base = cushion_favorite_v2(view, params, cushion_min_bps=cushion_min_bps)
    if base is None:
        return None
    window_age = _WINDOW_SECONDS - view.remaining_seconds
    if window_age > max_age_seconds:
        return None
    if base.edge > edge_cap:
        return None
    return ShadowSignal(
        side=base.side,
        entry_price=base.entry_price,
        fair_prob=base.fair_prob,
        edge=base.edge,
        confidence=base.confidence,
        reason=f"fresh {window_age:.0f}s; {base.reason}",
    )


def cushion_fresh_v7_f45(
    view: SnapshotView,
    params: strategy.StrategyParams,
    cushion_min_bps: float = 1.5,
    edge_cap: float = 0.065,
) -> ShadowSignal | None:
    """``cushion_fresh_v7`` with the freshness gate tightened to ≤ 45 seconds.

    Pre-registered for replay evaluation in issue #149: the day-5 race
    observation is that both fresh models (v7 and v8) lose money in their
    46–60s freshness bucket (v7: −$4.42 on n=21; v8: −$4.80 on n=34) while
    the 16–45s bucket carries the positive PnL. This variant tests whether
    removing the 46–60s tail improves fee-true mean/trade on the FULL tick
    history and the pre-race OOS segment.

    This is a replay-only evaluation gate. Do NOT add it to the live shadow
    runner until it clears the pre-registered decision rule in #149.
    """
    return cushion_fresh_v7(
        view, params, cushion_min_bps=cushion_min_bps,
        max_age_seconds=45, edge_cap=edge_cap,
    )


def cushion_fresh_v7_f45_spread(
    view: SnapshotView,
    params: strategy.StrategyParams,
    cushion_min_bps: float = 1.5,
    edge_cap: float = 0.065,
    max_spread: float = 0.01,
) -> ShadowSignal | None:
    """``cushion_fresh_v7_f45`` with an additional spread guard (≤ 1 tick).

    Pre-registered for replay evaluation in issue #149: every ≥ 2-tick
    (≥ $0.02) spread entry is negative across all four race models (pooled
    ≈ −$22 on 21 entries). This variant tests whether gateing on a tight
    spread (``up_ask − up_bid ≤ max_spread`` for Up, same for Down) removes
    that negative-EV tail without reducing fee-true mean/trade.

    Requires ``view.up_bid`` / ``view.down_bid`` to be non-None. When bid
    quotes are absent (all live, paper, and shadow paths where only the ask
    is carried), the function returns ``None`` — this is intentional: the
    gate only makes sense when the full book is available.

    This is a replay-only evaluation gate. Do NOT add it to the live shadow
    runner until it clears the pre-registered decision rule in #149.
    """
    base = cushion_fresh_v7_f45(
        view, params, cushion_min_bps=cushion_min_bps, edge_cap=edge_cap,
    )
    if base is None:
        return None

    if base.side == "Up":
        bid = view.up_bid
        ask = view.up_ask
    else:
        bid = view.down_bid
        ask = view.down_ask

    if bid is None or ask is None:
        return None

    # Round to 6dp to absorb floating-point error in ask−bid differences
    # (Polymarket book prices are at most 3dp, so round-trip through floats
    # can inflate a nominal 0.01 spread to 0.010000000000000009).
    spread = round(ask - bid, 6)
    if spread > max_spread:
        return None

    return ShadowSignal(
        side=base.side,
        entry_price=base.entry_price,
        fair_prob=base.fair_prob,
        edge=base.edge,
        confidence=base.confidence,
        reason=f"spread {spread:.2f}; {base.reason}",
    )


def pricing_fresh_v8(
    view: SnapshotView,
    params: strategy.StrategyParams,
    max_age_seconds: int = 60,
) -> ShadowSignal | None:
    """The v0 signal restricted to the first 60s of the window — freshness alone.

    The tick-replay backtest (#144, tools/replay_race.py) showed the
    freshness gate carries most of the v7 effect on its own: v0+fresh60
    posted the largest fee-true totals in BOTH independent halves of the
    tick history (pre-race +$106.84 on n=307; race era +$85.25 on n=267;
    BH-q<0.05 in each). v8 pre-registers the simplest member of the family,
    completing the ablation the race can now decide: v0 (no gate) /
    v2 (cushion) / v7 (fresh+cushion+cap) / v8 (fresh only).
    """
    window_age = _WINDOW_SECONDS - view.remaining_seconds
    if window_age > max_age_seconds:
        return None
    edge_up = view.fair_up - view.up_ask if view.up_ask is not None else None
    edge_down = (1.0 - view.fair_up) - view.down_ask if view.down_ask is not None else None
    side, confidence, _notional, reason = strategy.signal_from_executable_edges(
        edge_up,
        edge_down,
        view.remaining_seconds,
        view.up_ask,
        view.down_ask,
        params,
    )
    if side is None:
        return None
    entry_price = view.up_ask if side == "Up" else view.down_ask
    edge = edge_up if side == "Up" else edge_down
    fair_prob = view.fair_up if side == "Up" else 1.0 - view.fair_up
    if entry_price is None or edge is None:
        return None
    return ShadowSignal(
        side=side,
        entry_price=float(entry_price),
        fair_prob=float(fair_prob),
        edge=float(edge),
        confidence=float(confidence),
        reason=f"fresh {window_age:.0f}s; {reason}",
    )
