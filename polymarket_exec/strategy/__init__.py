"""Signal generation module — pricing model, sizing, and signal composition."""

from .pricing_model import fair_up_probability, sigma_per_second
from .sizing import confidence_from_edge, notional_from_confidence
from .signal import signal_from_edge

__all__ = [
    "confidence_from_edge",
    "fair_up_probability",
    "notional_from_confidence",
    "sigma_per_second",
    "signal_from_edge",
]
