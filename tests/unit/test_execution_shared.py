"""The shared execution layer (``ems/execution/``): what every strategy's orders go through.

The fill model and the venue reads are exercised end to end by the fade executor tests; these
pin the layer's own contract, so it keeps its coverage whichever strategies use it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from ems import config as _config
from ems import db as _db
from ems.execution import controls, queue, tape
from ems.fade_1h_momentum_15m import executor as fade_executor


@pytest_asyncio.fixture
async def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "layer.db")
    await _db.init_db()
    return _db


def test_fade_uses_the_shared_layer() -> None:
    """Fade re-exports the moved names, and they are the shared objects, not copies."""
    assert fade_executor.allocate_fills is queue.allocate_fills
    assert fade_executor.TapePrint is queue.TapePrint
    assert fade_executor.read_taker_tape is tape.read_taker_tape
    assert fade_executor.market_outcome is tape.market_outcome
    assert fade_executor.PlacementRefused is controls.PlacementRefused
    assert fade_executor.requested_mode is controls.requested_mode
    assert fade_executor.MODE_KEY == controls.MODE_KEY


@pytest.mark.parametrize("crossed, queue_ahead, size, expected", [
    (0.0, 10.0, 5.0, 0.0),     # nothing reached our price
    (8.0, 10.0, 5.0, 0.0),     # the queue ahead is not used up yet
    (12.0, 10.0, 5.0, 2.0),    # 2 shares got past the queue ahead
    (40.0, 10.0, 5.0, 5.0),    # capped at the order's size
    (5.0, 0.0, 5.0, 5.0),      # first in the queue
])
def test_lone_buy_fills_as_crossed_minus_queue_ahead(
    crossed: float, queue_ahead: float, size: float, expected: float
) -> None:
    """One BUY behind ``queue_ahead`` shares at its own price: filled = min(size,
    max(0, crossed - queue_ahead)), the rule the resting-order design states."""
    order = queue.QueuedOrder(order_id=1, side="Up", price=0.40, shares=size, flow_from=100,
                              flow_to=200, levels=((0.40, queue_ahead),))
    prints = [queue.TapePrint(ts=150, outcome="Up", side="SELL", size=crossed, price=0.40)]
    if crossed == 0.0:
        prints = []
    flow = queue.allocate_fills(prints, [order])[1]
    assert flow.filled == pytest.approx(expected)


def test_a_trade_above_our_price_moves_the_queue_but_never_fills() -> None:
    order = queue.QueuedOrder(order_id=1, side="Up", price=0.40, shares=5.0, flow_from=100,
                              flow_to=200, levels=((0.41, 3.0), (0.40, 2.0)))
    above = queue.TapePrint(ts=150, outcome="Up", side="SELL", size=10.0, price=0.41)
    flow = queue.allocate_fills([above], [order])[1]
    assert flow.filled == 0.0
    assert flow.levels == ((0.41, 0.0), (0.40, 2.0))


def test_a_taker_buying_the_other_outcome_sells_into_our_bids() -> None:
    """A taker buying Down at 0.60 is a sale into Up's bids at 0.40 (one shared book)."""
    order = queue.QueuedOrder(order_id=1, side="Up", price=0.40, shares=5.0, flow_from=100,
                              flow_to=200, levels=())
    buy_down = queue.TapePrint(ts=150, outcome="Down", side="BUY", size=3.0, price=0.60)
    assert queue.allocate_fills([buy_down], [order])[1].filled == pytest.approx(3.0)


def test_queue_ahead_counts_our_price_and_better() -> None:
    bids = [(0.42, 1.0), (0.40, 2.0), (0.39, 50.0)]
    assert queue.queue_ahead(bids, 0.40) == pytest.approx(3.0)


class _Order:
    def __init__(self, order_side: str, price: float) -> None:
        self.order_side, self.token_id, self.price = order_side, "tok", price


def test_check_passive_refuses_a_bid_that_meets_the_ask() -> None:
    with pytest.raises(controls.PlacementRefused) as refused:
        controls.check_passive(_Order("BUY", 0.45), {"tok": 0.45}, {})
    assert refused.value.reason == "would_cross"
    controls.check_passive(_Order("BUY", 0.44), {"tok": 0.45}, {})  # rests below: fine
    controls.check_passive(_Order("BUY", 0.44), {"tok": None}, {})  # no asks: nothing to cross


def test_check_passive_needs_the_ask() -> None:
    with pytest.raises(controls.PlacementRefused) as refused:
        controls.check_passive(_Order("BUY", 0.44), {}, {})
    assert refused.value.reason == "no_ask"


def test_kill_switch_reads_the_configured_path_at_call_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kill = tmp_path / "KILL"
    monkeypatch.setattr(_config, "KILL_SWITCH_PATH", kill)
    assert not controls.kill_switch_active()
    kill.touch()
    assert controls.kill_switch_active()
    assert controls.kill_switch_path() == kill


@pytest.mark.asyncio
async def test_requested_mode_defaults_to_the_env_mode(
    temp_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_config, "BOT_MODE", "paper")
    assert await controls.requested_mode() == "paper"
    await _db.set_config(controls.MODE_KEY, " LIVE ")
    assert await controls.requested_mode() == "live"
