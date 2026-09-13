"""Shadow forward-tester runner.

Each tick, log what each candidate strategy *would* trade this window to
``model_shadow_positions`` (idempotent per window/model), so the candidates
accumulate an out-of-sample record alongside the live v0 strategy. Settlement is
independent (see ``paper._settle_due_shadows``) and PnL is booked NET of the
Polymarket 7% taker fee. No real orders are ever placed from here — this is a
pure paper comparison harness.

Candidates (all are logged; the dashboard model picker that let the loop trade
one was archived with the v0 strategy on 2026-09-13):
- ``pricing_v0``          — the live strategy, logged as the control baseline.
- ``cushion_favorite_v2`` — v0 + a cushion gate (spot clearly on the favoured
  side of the strike): the only model sign-positive in-sample AND out-of-sample
  in the 06-18→06-24 race.
- ``cushion_fresh_v7``    — v2 restricted to the first 60s of the window with
  edge claims capped at 0.065 (postmortem-motivated challenger, #142).
- ``pricing_fresh_v8``    — v0 in the first 60s only: the freshness gate
  alone, pre-registered from the tick-replay evidence (#144). Together the
  roster is a clean ablation: v0 / v2 (cushion) / v7 (all gates) / v8 (fresh).

Retired 2026-07-02 (#142; history stays in the ledger): ``late_convergence_v3``
(favorite-soak trap), ``down_skeptic_v4`` (IS→OOS rank flip), ``cushion_drift_v5``
(redundant with v2), ``down_skeptic_drift_v6`` (worst everywhere).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

import structlog

from polymarket_bot import strategy
from polymarket_bot.shadow import ledger, signals
from polymarket_bot.shadow.types import ShadowSignal, SnapshotView

if TYPE_CHECKING:
    # Import only for typing — paper.py imports this module, so a runtime import
    # here would be circular. Under TYPE_CHECKING there is no runtime import.
    from polymarket_bot.paper import PaperSnapshot

log = structlog.get_logger()

# Flat sizing — confidence-weighting was anti-predictive; flat is the robust
# choice. Shares match the bot's share-denominated default so notionals compare.
SHADOW_SHARES = 5.0


def build_view(snapshot: PaperSnapshot) -> SnapshotView:
    """Map a ``PaperSnapshot`` onto the minimal view the signals consume.

    ``market_up_price`` is the Up token MID (not the ask) so the favoured-side
    determination in late-convergence is unbiased; the executable asks are kept
    separately for entry pricing.
    """
    up_bid = snapshot.up_best_bid
    up_ask = snapshot.up_best_ask
    if up_bid is not None and up_ask is not None:
        market_up = (up_bid + up_ask) / 2.0
    elif snapshot.market_up_price is not None:
        market_up = float(snapshot.market_up_price)
    else:
        market_up = 0.5
    return SnapshotView(
        window_slug=snapshot.window_slug,
        remaining_seconds=snapshot.remaining_seconds,
        spot=snapshot.spot_price,
        reference=snapshot.reference_price,
        up_ask=up_ask,
        down_ask=snapshot.down_best_ask,
        market_up_price=market_up,
        fair_up=snapshot.fair_up_prob,
        sigma_per_second=snapshot.sigma_per_second,
        feed_source=snapshot.feed_source,
        quote_source=snapshot.quote_source,
        drift_per_second=snapshot.drift_per_second,
    )


def _v0_control(
    view: SnapshotView, params: strategy.StrategyParams
) -> ShadowSignal | None:
    """v0 logged as the baseline — signal_from_executable_edges, no extra gate."""
    up_ask, down_ask = view.up_ask, view.down_ask
    edge_up = (view.fair_up - up_ask) if up_ask is not None else None
    edge_down = ((1.0 - view.fair_up) - down_ask) if down_ask is not None else None
    side, confidence, _notional, reason = strategy.signal_from_executable_edges(
        edge_up, edge_down, view.remaining_seconds, up_ask, down_ask, params
    )
    if side is None:
        return None
    entry = up_ask if side == "Up" else down_ask
    if entry is None:
        return None
    edge = edge_up if side == "Up" else edge_down
    fair = view.fair_up if side == "Up" else (1.0 - view.fair_up)
    return ShadowSignal(
        side=side,
        entry_price=float(entry),
        fair_prob=float(fair),
        edge=float(edge or 0.0),
        confidence=float(confidence),
        reason=reason,
    )


# Ordered so the control is logged first. Each candidate is callable as
# fn(view, params); cushion variants carry their own defaulted thresholds.
# Roster surgery 2026-07-02 (#142, docs/archive/POSTMORTEM_2026-07.md): v3/v4/v5/v6
# retired on the frozen-race evidence (v3 favorite-soak trap, v4 IS→OOS rank
# flip, v5 redundant with v2, v6 worst everywhere). Historical shadow rows for
# retired models remain in the ledger; only new logging stops.
_MODELS: dict[
    str, Callable[[SnapshotView, strategy.StrategyParams], ShadowSignal | None]
] = {
    "pricing_v0": _v0_control,
    "cushion_favorite_v2": signals.cushion_favorite_v2,
    "cushion_fresh_v7": signals.cushion_fresh_v7,
    "pricing_fresh_v8": signals.pricing_fresh_v8,
    # Added #155 on the #149 replay evidence (OOS CI [+0.275, +0.887] vs v7's
    # [+0.080, +0.624]): v7 with the freshness gate tightened 60s→45s, the
    # 46–60s bucket having been fee-true negative for both fresh models.
    # Shadow-only, additive — the racing specs v0/v2/v7/v8 are untouched, so the
    # ablation stays intact; f45's own clock starts from the next loop restart.
    "cushion_fresh_v7_f45": signals.cushion_fresh_v7_f45,
}


# --- Roster registry -----------------------------------------------------------
DEFAULT_MODEL = "pricing_v0"
MODEL_IDS: list[str] = list(_MODELS.keys())

# Labels carry the model's version tag (vN) so they map 1:1
# back to the persisted `model_id` (e.g. "Down-Skeptic (v4)" -> down_skeptic_v4).
# The vN is a global experiment counter, not a per-family version: down_skeptic
# is v4 and its regime-drift child is v6 because cushion_drift (v5) was logged
# between them (#108).
MODEL_LABELS: dict[str, str] = {
    "pricing_v0": "Pricing · Settle (v0)",
    "cushion_favorite_v2": "Cushion Favorite (v2)",
    "cushion_fresh_v7": "Cushion · Fresh+Capped (v7)",
    "pricing_fresh_v8": "Pricing · Fresh (v8)",
    "cushion_fresh_v7_f45": "Cushion · Fresh≤45s+Capped (v7·f45)",
}
MODEL_DESCRIPTIONS: dict[str, str] = {
    "pricing_v0": "v0 baseline · edge 0.045–0.07 · favorites ≥0.50 · hold→resolution",
    "cushion_favorite_v2": "v0 + cushion: spot clearly on the favoured side of the strike",
    "cushion_fresh_v7": "v2 + first-60s windows only + edge claims capped at 0.065 (adverse-selection guard)",
    "pricing_fresh_v8": "v0 in the first 60s of the window only — the freshness gate alone (#144 replay evidence)",
    "cushion_fresh_v7_f45": "v7 with the freshness gate tightened to ≤45s (#149 replay: 46–60s bucket was fee-negative)",
}

# Candidate signal fns for the LIVE dispatch. v0 is intentionally absent — it
# uses the loop's native signal_from_executable_edges path.
CANDIDATE_SIGNALS: dict[
    str, Callable[[SnapshotView, strategy.StrategyParams], ShadowSignal | None]
] = {
    "cushion_favorite_v2": signals.cushion_favorite_v2,
    "cushion_fresh_v7": signals.cushion_fresh_v7,
    "pricing_fresh_v8": signals.pricing_fresh_v8,
    "cushion_fresh_v7_f45": signals.cushion_fresh_v7_f45,
}


def candidate_signal(
    model_id: str, view: SnapshotView, params: strategy.StrategyParams
) -> ShadowSignal | None:
    """Live-dispatch helper: the selected candidate's would-be trade, or None.

    Returns None for ``pricing_v0`` / unknown ids — the caller falls back to
    the native v0 path for those.
    """
    fn = CANDIDATE_SIGNALS.get(model_id)
    return fn(view, params) if fn else None


async def record_shadow(
    snapshot: PaperSnapshot, params: strategy.StrategyParams
) -> None:
    """Log each candidate's would-be entry for this tick's window (idempotent)."""
    if snapshot.feed_degraded:
        return
    if not snapshot.has_executable_quote:
        return
    view = build_view(snapshot)
    for model_id, fn in _MODELS.items():
        try:
            sig = fn(view, params)
        except Exception as exc:  # noqa: BLE001 — a candidate must never break the loop
            log.warning("shadow.signal_error", model_id=model_id, error=str(exc))
            continue
        if sig is None:
            continue
        await ledger.record_shadow_signal(
            created_at=snapshot.created_at,
            window_slug=view.window_slug,
            model_id=model_id,
            side=sig.side,
            entry_price=sig.entry_price,
            fair_prob=sig.fair_prob,
            edge=sig.edge,
            confidence=sig.confidence,
            reason=sig.reason,
            notional_usd=SHADOW_SHARES * sig.entry_price,
            shares=SHADOW_SHARES,
            quote_source=view.quote_source,
            feed_source=view.feed_source,
            # Market state at decision time → vol/basis regime axes (#122).
            spot_at_decision=view.spot,
            reference_at_decision=view.reference,
            sigma_per_second=view.sigma_per_second,
            drift_per_second=view.drift_per_second,
        )
