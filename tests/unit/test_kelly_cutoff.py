"""The Kelly runner's decision cutoff (``ems/kelly_horse_race/runner.py``) against the order
check both venues share (``ems/execution/resting.py:validate_request``).

Polymarket refuses a GTD order that expires less than 3 minutes ahead. A window the runner
may still decide must be one whose order the exchange would take, so a later window is
recorded as too late instead of being sent and refused.
"""

from __future__ import annotations

import pytest

from ems.execution import resting
from ems.execution.controls import PlacementRefused
from ems.kelly_horse_race import runner as rn

START = 1_789_935_300  # a 15m window boundary
END = START + 900
UP, DOWN = "UP-btc", "DN-btc"
READS_S = 10  # time a decision started just before the cutoff has for its reads


def request() -> resting.PlaceRequest:
    return resting.PlaceRequest(
        strategy=rn.STRATEGY, condition_id="0xcid", token_id=UP, outcome="Up", up_token=UP,
        down_token=DOWN, price=0.50, size=5.0, expires_ts=END, tick_size=0.01,
        queue_ahead=10.0, best_ask=0.52)


def test_an_order_sent_at_the_cutoff_is_one_the_exchange_takes() -> None:
    resting.validate_request(request(), END - rn.DECISION_CUTOFF_S)  # does not raise


def test_the_cutoff_leaves_the_decision_time_for_its_reads() -> None:
    """A decision started just before the cutoff still reads the price, the last hour and
    the book before its order goes; the order must not be refused as too late meanwhile."""
    sent = END - rn.DECISION_CUTOFF_S + READS_S
    resting.validate_request(request(), sent)  # does not raise
    with pytest.raises(PlacementRefused) as refused:
        resting.validate_request(
            request(), END - resting.GTD_MIN_AHEAD_S - resting.GTD_SEND_MARGIN_S + 1)
    assert refused.value.reason == "too_late"
