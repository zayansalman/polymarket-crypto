"""Ladder arithmetic: rung prices and sizes for a resting limit entry."""

from __future__ import annotations

import pytest

from polymarket_exec.execution.ladder import (
    LadderSpec,
    LadderState,
    build_ladder,
)

TICK = 0.01
MIN_SIZE = 5.0


def test_default_ladder_rests_five_rungs_from_five_to_fifteen_cents_under():
    rungs = build_ladder(0.60, 100.0, tick=TICK, min_size=MIN_SIZE)

    # 7.5c and 12.5c offsets land between ticks and snap to the 1c grid.
    assert [r.price for r in rungs] == [0.55, 0.52, 0.50, 0.48, 0.45]


def test_every_rung_rests_strictly_below_the_reference():
    for reference in (0.20, 0.35, 0.50, 0.75, 0.95):
        rungs = build_ladder(reference, 200.0, tick=TICK, min_size=MIN_SIZE)
        assert rungs, f"expected a ladder at {reference}"
        assert all(r.price < reference for r in rungs)


def test_a_full_fill_never_spends_more_than_was_authorised():
    rungs = build_ladder(0.60, 100.0, tick=TICK, min_size=MIN_SIZE)

    assert sum(r.notional_usd for r in rungs) <= 100.0


def test_rungs_run_nearest_the_touch_first():
    rungs = build_ladder(0.60, 100.0, tick=TICK, min_size=MIN_SIZE)

    prices = [r.price for r in rungs]
    assert prices == sorted(prices, reverse=True)
    assert rungs[0].offset == pytest.approx(0.05)
    assert rungs[-1].offset == pytest.approx(0.15)


def test_a_price_too_low_to_sit_five_cents_under_yields_no_ladder():
    assert build_ladder(0.04, 100.0, tick=TICK, min_size=MIN_SIZE) == []


def test_a_thin_notional_drops_rungs_but_keeps_the_full_offset_span():
    rungs = build_ladder(0.60, 12.0, tick=TICK, min_size=MIN_SIZE)

    assert 0 < len(rungs) < 5
    assert rungs[0].offset == pytest.approx(0.05)
    assert all(r.size >= MIN_SIZE for r in rungs)


def test_a_notional_too_small_for_even_one_rung_yields_no_ladder():
    assert build_ladder(0.60, 0.50, tick=TICK, min_size=MIN_SIZE) == []


def test_rung_prices_land_on_the_venue_tick():
    rungs = build_ladder(0.6234, 100.0, spec=LadderSpec(rungs=3), tick=0.01, min_size=1.0)

    for rung in rungs:
        assert rung.price == pytest.approx(round(rung.price, 2))


def test_a_single_rung_ladder_rests_at_the_near_offset():
    rungs = build_ladder(0.60, 50.0, spec=LadderSpec(rungs=1), tick=TICK, min_size=MIN_SIZE)

    assert len(rungs) == 1
    assert rungs[0].price == pytest.approx(0.55)


def test_offsets_are_configurable():
    spec = LadderSpec(rungs=3, min_offset=0.02, max_offset=0.06)
    rungs = build_ladder(0.50, 100.0, spec=spec, tick=TICK, min_size=MIN_SIZE)

    assert [r.price for r in rungs] == [0.48, 0.46, 0.44]


def test_no_rung_ever_clears_the_venue_minimum_by_rounding_up():
    rungs = build_ladder(0.99, 30.0, tick=TICK, min_size=MIN_SIZE)

    assert all(r.size >= MIN_SIZE for r in rungs)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"rungs": 0},
        {"min_offset": 0.0},
        {"min_offset": 0.10, "max_offset": 0.05},
    ],
)
def test_an_impossible_ladder_shape_is_rejected(kwargs):
    with pytest.raises(ValueError):
        LadderSpec(**kwargs)


@pytest.mark.parametrize("bad", [0.0, -1.0])
def test_nonsense_inputs_yield_no_ladder(bad):
    assert build_ladder(bad, 100.0, tick=TICK, min_size=MIN_SIZE) == []
    assert build_ladder(0.60, bad, tick=TICK, min_size=MIN_SIZE) == []


def _state(reference=0.60, notional=100.0):
    return LadderState.from_rungs(
        build_ladder(reference, notional, tick=TICK, min_size=MIN_SIZE)
    )


def test_an_unplaced_ladder_holds_nothing():
    state = _state()

    assert state.committed_size == 0.0
    assert state.filled_size == 0.0
    assert state.any_resting is False
    assert state.average_fill_price is None


def test_an_unfilled_ladder_has_no_entry_price():
    state = _state()
    for i, rung in enumerate(state.rungs):
        rung.order_id = f"order-{i}"

    assert state.filled_size == 0.0
    assert state.average_fill_price is None
    assert state.any_resting is True


def test_the_entry_price_weights_only_what_actually_filled():
    state = _state()
    for i, rung in enumerate(state.rungs):
        rung.order_id = f"order-{i}"
    # Two rungs fill at 0.55 and 0.50, the rest stay resting.
    state.rungs[0].matched = state.rungs[0].rung.size
    state.rungs[2].matched = state.rungs[2].rung.size

    near, far = state.rungs[0], state.rungs[2]
    expected = (near.rung.price * near.matched + far.rung.price * far.matched) / (
        near.matched + far.matched
    )
    assert state.average_fill_price == pytest.approx(expected, abs=1e-6)
    assert state.filled_size == pytest.approx(near.matched + far.matched)


def test_a_partly_filled_rung_keeps_resting():
    state = _state()
    rung = state.rungs[0]
    rung.order_id = "order-0"
    rung.matched = rung.rung.size / 2

    assert rung.resting is True
    assert rung.unfilled == pytest.approx(rung.rung.size / 2)


def test_a_filled_rung_stops_resting():
    state = _state()
    rung = state.rungs[0]
    rung.order_id = "order-0"
    rung.matched = rung.rung.size

    assert rung.resting is False
    assert rung.unfilled == 0.0


def test_a_cancelled_rung_stops_resting_even_when_unfilled():
    state = _state()
    rung = state.rungs[0]
    rung.order_id = "order-0"
    rung.cancelled = True

    assert rung.resting is False
    assert state.resting_order_ids() == []


def test_only_placed_rungs_count_as_committed():
    state = _state()
    state.rungs[0].order_id = "order-0"

    assert state.committed_size == pytest.approx(state.rungs[0].rung.size)


def test_resting_order_ids_lists_what_still_needs_cancelling():
    state = _state()
    for i, rung in enumerate(state.rungs):
        rung.order_id = f"order-{i}"
    state.rungs[0].matched = state.rungs[0].rung.size

    assert state.resting_order_ids() == [f"order-{i}" for i in range(1, len(state.rungs))]
