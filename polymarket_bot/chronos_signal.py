"""Layer 3 — Chronos time-series ensemble (stub).

This module defines the integration shape for adding a Hugging Face Chronos
time-series foundation model as a *second* probability source, ensembled with
the calibrated Black-Scholes baseline. It does NOT add a Hugging Face
dependency, does NOT download any model, and does NOT change live behavior.

See ``docs/CHRONOS_INTEGRATION.md`` for the full design, activation gate
(OOS replay-archive validation + operator sign-off + initial weight cap), and
file layout for the eventual real implementation.

Current state: ``predict()`` returns ``None`` (signal unavailable).
``apply_ensemble()`` is therefore identity. The live bot is unaffected.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


CHRONOS_ACTIVE_FILE = "chronos_active.json"


def _activation_path() -> Path:
    data_dir = os.environ.get("DATA_DIR", "./data")
    return Path(data_dir) / CHRONOS_ACTIVE_FILE


@dataclass(frozen=True)
class ChronosActivation:
    """Operator activation marker. Absence on disk = OFF.

    weight_cap caps Chronos's ensemble share until N closed-trade Brier
    samples have accumulated (see CHRONOS_INTEGRATION.md "Initial weight cap").
    """

    activated_at: str
    weight_cap: float
    samples_required: int
    model_id: str  # e.g. "amazon/chronos-bolt-small"


def load_activation() -> ChronosActivation | None:
    """Return the operator activation marker, or None if Chronos is OFF."""
    try:
        with open(_activation_path()) as f:
            d = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    try:
        return ChronosActivation(
            activated_at=str(d["activated_at"]),
            weight_cap=float(d["weight_cap"]),
            samples_required=int(d["samples_required"]),
            model_id=str(d["model_id"]),
        )
    except (KeyError, ValueError, TypeError):
        return None


def is_active() -> bool:
    return load_activation() is not None


def predict(window_closes: list[float], reference_price: float) -> float | None:
    """Return Chronos P(window close ≥ reference), or None if unavailable.

    Stub: always returns None. The real implementation (next PR, gated by
    OOS validation) will lazily load the model from Hugging Face Hub,
    sample-forecast the next ~5 minutes, and integrate the predicted density
    above ``reference_price``.

    Parameters:
        window_closes: 1-second BTC closes, oldest first, ≥ ~3600 points
            for the foundation model to have meaningful context.
        reference_price: window opening print (settlement reference).
    """
    del window_closes, reference_price  # unused in stub
    return None


def apply_ensemble(
    fair_up_cal: float,
    fair_up_chronos: float | None,
    *,
    weight_cal: float = 1.0,
    weight_chronos: float = 0.0,
) -> float:
    """Ensemble the calibrated baseline with the Chronos probability.

    When ``fair_up_chronos`` is None or weights collapse to zero, returns
    ``fair_up_cal`` unchanged — identity. Weights come from a separate Brier-
    tracking module (also not yet built); the live path computes them per
    window roll and passes them in.
    """
    if fair_up_chronos is None:
        return fair_up_cal
    total = weight_cal + weight_chronos
    if total <= 0:
        return fair_up_cal
    blended = (weight_cal * fair_up_cal + weight_chronos * fair_up_chronos) / total
    return float(max(0.0, min(1.0, blended)))
