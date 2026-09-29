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
from tests.unit.venue_fakes import FakeVenue, maker, trade


@pytest_asyncio.fixture
async def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "layer.db")
    await _db.init_db()
    return _db


def test_fade_uses_the_shared_layer() -> None:
    """Fade re-exports the moved names, and they are the shared objects, not copies."""
    assert fade_executor.allocate_fills is queue.allocate_fills
    assert fade_executor.TapePrint is queue.TapePrint
    assert fade_executor.read_fill_tape is tape.read_fill_tape
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


@pytest.mark.parametrize("outcome, side, size, price, bid_side, bid", [
    # Real records: market buys priced notional / size, with the size rounded up to 1e-6 of
    # a share, so they land a hair under the price level they traded at.
    ("Down", "BUY", 81.538462, 0.5199999971, "Up", 0.48),
    ("Up", "BUY", 83.333334, 0.2399999981, "Down", 0.76),
    ("Down", "BUY", 11.177778, 0.8999999821, "Up", 0.10),
    # $1 buys at 0.90 and 0.99: the same rounding, 7e-7 and 1e-6 under the level.
    ("Up", "BUY", 1.111112, 1.0 / 1.111112, "Down", 0.10),
    ("Up", "BUY", 1.010102, 1.0 / 1.010102, "Down", 0.01),
    # A sale a hair over the level it traded at.
    ("Up", "SELL", 5.0, 0.4000000029, "Up", 0.40),
])
def test_a_taker_price_a_hair_off_its_level_trades_at_that_level(
    outcome: str, side: str, size: float, price: float, bid_side: str, bid: float
) -> None:
    order = queue.QueuedOrder(order_id=1, side=bid_side, price=bid, shares=100.0,
                              flow_from=100, flow_to=200, levels=())
    record = queue.TapePrint(ts=150, outcome=outcome, side=side, size=size, price=price)
    flow = queue.allocate_fills([record], [order])[1]
    assert (flow.filled, flow.fill_ts) == (pytest.approx(size), 150)


def test_a_taker_price_a_hair_off_a_level_above_us_uses_that_level_up() -> None:
    """A market buy of Up at 0.2399999981 sells into Down's bids at 0.76: it uses up the 10
    shown at 0.76, and the 2 left never reach our bid at 0.75."""
    order = queue.QueuedOrder(order_id=1, side="Down", price=0.75, shares=5.0, flow_from=100,
                              flow_to=200, levels=((0.76, 10.0), (0.75, 3.0)))
    record = queue.TapePrint(ts=150, outcome="Up", side="BUY", size=12.0, price=0.2399999981)
    flow = queue.allocate_fills([record], [order])[1]
    assert (flow.filled, flow.levels) == (0.0, ((0.76, 0.0), (0.75, 3.0)))


@pytest.mark.parametrize("outcome, side, price", [
    ("Up", "SELL", 0.40001),    # a sweep whose average is 1e-5 over our level
    ("Up", "SELL", 0.4000274),  # the closest real sweep average seen, 2.74e-5 over
    ("Down", "BUY", 0.59999),   # the first, mirrored
])
def test_a_sweep_averaging_just_over_our_level_does_not_reach_it(
    outcome: str, side: str, price: float
) -> None:
    order = queue.QueuedOrder(order_id=1, side="Up", price=0.40, shares=5.0, flow_from=100,
                              flow_to=200, levels=())
    record = queue.TapePrint(ts=150, outcome=outcome, side=side, size=10.0, price=price)
    assert queue.allocate_fills([record], [order])[1].filled == 0.0


# ---------------------------------------------------------------------------
# A trade below our price: our level was empty, so a real order there has filled
# ---------------------------------------------------------------------------


def test_a_trade_below_our_bid_fills_it_whatever_the_queue_ahead() -> None:
    """Real case (btc-updown-15m-1790505000): a Down bid at 0.70 behind 250 shown, then a
    taker buys Up at 0.31, a sale into Down's bids at 0.69. By price priority nothing was left
    at 0.70 by then, so a real order there has filled, whether the 250 traded or were pulled."""
    order = queue.QueuedOrder(order_id=1, side="Down", price=0.70, shares=5.0, flow_from=100,
                              flow_to=200, levels=((0.70, 250.0),))
    through = queue.TapePrint(ts=137, outcome="Up", side="BUY", size=8.0, price=0.31)
    flow = queue.allocate_fills([through], [order])[1]
    assert (flow.filled, flow.fill_ts, flow.done_ts) == (5.0, 137, 137)
    assert flow.levels == ((0.70, 0.0),)


def test_a_trade_below_our_bids_fills_them_only_as_far_as_its_size() -> None:
    """Our higher bid takes its share first; the lower one gets what is left of the trade,
    and the depth shown ahead of each (above it and at it) is gone."""
    high = queue.QueuedOrder(order_id=1, side="Up", price=0.45, shares=20.0, flow_from=100,
                             flow_to=200, levels=((0.45, 10.0),))
    low = queue.QueuedOrder(order_id=2, side="Up", price=0.40, shares=20.0, flow_from=100,
                            flow_to=200, levels=((0.42, 500.0), (0.40, 1000.0)))
    through = queue.TapePrint(ts=150, outcome="Up", side="SELL", size=30.0, price=0.39)
    flows = queue.allocate_fills([through], [low, high])
    assert (flows[1].filled, flows[2].filled) == (20.0, 10.0)
    assert flows[2].levels == ((0.42, 0.0), (0.40, 0.0))


def test_after_a_trade_below_our_bid_the_next_one_at_our_price_is_ours() -> None:
    order = queue.QueuedOrder(order_id=1, side="Up", price=0.40, shares=10.0, flow_from=100,
                              flow_to=200, levels=((0.40, 300.0),))
    prints = [queue.TapePrint(ts=150, outcome="Up", side="SELL", size=4.0, price=0.39),
              queue.TapePrint(ts=160, outcome="Up", side="SELL", size=6.0, price=0.40)]
    flow = queue.allocate_fills(prints, [order])[1]
    assert (flow.filled, flow.fill_ts, flow.done_ts) == (10.0, 150, 160)


def test_a_sale_fills_when_a_taker_buys_our_token_above_its_price() -> None:
    """A sale of Up at 0.60 behind 500 shown is a Down bid at 0.40 (the mirror): a taker
    buying Up at 0.61 is a sale into Down's bids at 0.39, below it."""
    side, price, levels = queue.book_terms("SELL", "Up", 0.60, ((0.60, 500.0),))
    order = queue.QueuedOrder(order_id=1, side=side, price=price, shares=5.0, flow_from=100,
                              flow_to=200, levels=levels)
    lift = queue.TapePrint(ts=150, outcome="Up", side="BUY", size=3.0, price=0.61)
    flow = queue.allocate_fills([lift], [order])[1]
    assert (flow.filled, flow.fill_ts) == (3.0, 150)
    assert queue.own_terms("SELL", flow.levels) == ((0.60, 0.0),)


def test_a_sweep_read_at_its_average_below_our_bid_empties_the_queue_but_fills_nothing() -> None:
    """A real sweep (0xd04ceee83192): 192 sold into Down's bids at 0.71, then 133.35 at 0.70,
    against a Down bid at 0.71 for 300 behind 1,000. Level by level, 133.35 of ours fill. Read
    at its average only (0.705901337), it traded below us, so our level was empty, but how much
    of it traded there is unknown: none of it is ours. The next trade at 0.71 is."""
    order = queue.QueuedOrder(order_id=1, side="Down", price=0.71, shares=300.0, flow_from=100,
                              flow_to=200, levels=((0.71, 1000.0),))
    by_levels = [queue.TapePrint(ts=150, outcome="Down", side="SELL", size=192.0, price=0.71),
                 queue.TapePrint(ts=150, outcome="Down", side="SELL", size=133.35, price=0.70)]
    assert queue.allocate_fills(by_levels, [order])[1].filled == pytest.approx(133.35)
    averaged = queue.TapePrint(ts=150, outcome="Down", side="SELL", size=325.35,
                               price=0.705901337, averaged=True)
    flow = queue.allocate_fills([averaged], [order])[1]
    assert (flow.filled, flow.crossed, flow.levels) == (0.0, 0.0, ((0.71, 0.0),))
    after = queue.TapePrint(ts=160, outcome="Down", side="SELL", size=10.0, price=0.71)
    assert queue.allocate_fills([averaged, after], [order])[1].filled == 10.0


# ---------------------------------------------------------------------------
# A trade at a price: by price priority, nothing was bid above it
# ---------------------------------------------------------------------------


def test_a_sweep_through_a_child_bid_under_the_touch_fills_it() -> None:
    """The same sweep against a Down bid at 0.70 one tick under the touch, 500 shown at 0.71
    and 50 at 0.70. When its 133.35 traded at 0.70 nothing was bid at 0.71 any more, so the 308
    still shown there had been pulled: the 133.35 works through the 50 at 0.70, then fills us."""
    order = queue.QueuedOrder(order_id=1, side="Down", price=0.70, shares=5.0, flow_from=100,
                              flow_to=200, levels=((0.71, 500.0), (0.70, 50.0)))
    sweep = [queue.TapePrint(ts=150, outcome="Down", side="SELL", size=192.0, price=0.71),
             queue.TapePrint(ts=150, outcome="Down", side="SELL", size=133.35, price=0.70)]
    flow = queue.allocate_fills(sweep, [order])[1]
    assert (flow.filled, flow.levels) == (5.0, ((0.71, 0.0), (0.70, 0.0)))


def test_a_trade_at_our_bid_means_the_depth_shown_above_it_was_gone() -> None:
    """Real opening book (eth-updown-15m-1790695800): an Up bid at 0.51 behind 52 at 0.52 and
    50 at 0.51, and the first trade is 18 sold into the bids at 0.51. By price priority the 52
    at 0.52 had been pulled, so only the 50 at our own price were ahead of us."""
    order = queue.QueuedOrder(order_id=1, side="Up", price=0.51, shares=40.0, flow_from=100,
                              flow_to=200, levels=((0.52, 52.0), (0.51, 50.0)))
    prints = [queue.TapePrint(ts=105, outcome="Up", side="SELL", size=18.0, price=0.51),
              queue.TapePrint(ts=120, outcome="Up", side="SELL", size=50.0, price=0.51)]
    flow = queue.allocate_fills(prints, [order])[1]
    # 18, then 32 of the next 50, use up the 50 at 0.51; the other 18 are ours.
    assert (flow.filled, flow.fill_ts, flow.crossed) == (18.0, 120, 68.0)
    assert flow.levels == ((0.52, 0.0), (0.51, 0.0))


def test_a_trade_above_our_bid_clears_only_the_levels_above_its_own_price() -> None:
    order = queue.QueuedOrder(order_id=1, side="Up", price=0.40, shares=5.0, flow_from=100,
                              flow_to=200,
                              levels=((0.43, 500.0), (0.42, 300.0), (0.41, 200.0), (0.40, 50.0)))
    at_42 = queue.TapePrint(ts=150, outcome="Up", side="SELL", size=100.0, price=0.42)
    flow = queue.allocate_fills([at_42], [order])[1]
    assert (flow.filled, flow.levels) == (
        0.0, ((0.43, 0.0), (0.42, 200.0), (0.41, 200.0), (0.40, 50.0)))
    # Then 60 sold at our 0.40: everything above us was gone, the 50 at 0.40 go first.
    at_40 = queue.TapePrint(ts=160, outcome="Up", side="SELL", size=60.0, price=0.40)
    flow = queue.allocate_fills([at_42, at_40], [order])[1]
    assert (flow.filled, flow.levels) == (
        5.0, ((0.43, 0.0), (0.42, 0.0), (0.41, 0.0), (0.40, 0.0)))


# ---------------------------------------------------------------------------
# The fill tape: every trade level by level, from the maker records
# ---------------------------------------------------------------------------

CID, UP, DN = "0xcid", "UP", "DN"


def _trade(ts: int, outcome: str, side: str, size: float, price: float, legs=()) -> dict:
    return trade(ts, outcome, side, size, price, cid=CID, up=UP, down=DN, legs=legs)


async def _fill_tape(venue: FakeVenue, since: int = 0) -> tape.TapeRead:
    return await tape.read_fill_tape(venue, CID, since=since, up_token=UP, down_token=DN)


@pytest.mark.asyncio
async def test_a_sweep_is_read_level_by_level_from_its_maker_records() -> None:
    """Real sweep (btc-updown-15m-1790505000, 0xd04ceee83192): a taker sold 325.35 Down at an
    average of 0.705901337, 192 to bids at 0.71 and 133.35 to bids at 0.70. Read as one record
    at its average it never reached a bid at 0.70; read level by level, it did."""
    venue = FakeVenue()
    venue.add(CID, _trade(1522, "Down", "SELL", 325.35, 0.705901337,
                          legs=[("Down", "BUY", 192.0, 0.71), ("Down", "BUY", 133.35, 0.70)]),
              _trade(1530, "Up", "SELL", 1.0, 0.99))
    read = await _fill_tape(venue, since=1500)
    assert [(p.ts, p.hits(), p.size) for p in read.prints] == [
        (1522, ("Down", 0.71), 192.0), (1522, ("Down", 0.70), 133.35),
        (1530, ("Up", 0.99), 1.0)]
    assert (read.newest_ts, read.skipped, read.averaged) == (1530, 0, 0)
    # A Down bid at 0.70 behind 100 shown gets 5 of the 133.35 that traded at its level.
    order = queue.QueuedOrder(order_id=1, side="Down", price=0.70, shares=5.0, flow_from=1500,
                              flow_to=1530, levels=((0.70, 100.0),))
    assert queue.allocate_fills(read.prints, [order])[1].filled == 5.0


@pytest.mark.asyncio
async def test_maker_records_are_sales_into_one_outcomes_bids() -> None:
    """Real trade (0x069ac16d7456): a taker bought 83.333334 Up at 0.2399999981 from two Down
    bids at 0.76 (5 and 60, minted) and an Up ask at 0.2399999913 (18.333334). All three are
    sales into Down's bids at 0.76: a maker BUY of Down at 0.76 as a taker SELL of Down, a
    maker SELL of Up at 0.24 as a taker BUY of Up."""
    venue = FakeVenue()
    venue.add(CID, _trade(10, "Up", "BUY", 83.333334, 0.2399999981,
                          legs=[("Down", "BUY", 5.0, 0.76), ("Down", "BUY", 60.0, 0.76),
                                ("Up", "SELL", 18.333334, 0.2399999913)]),
              _trade(20, "Up", "SELL", 1.0, 0.99))
    read = await _fill_tape(venue)
    legs = [p for p in read.prints if p.ts == 10]
    assert {p.hits() for p in legs} == {("Down", 0.76)}
    assert sorted((p.outcome, p.side) for p in legs) == [
        ("Down", "SELL"), ("Down", "SELL"), ("Up", "BUY")]
    assert sum(p.size for p in legs) == pytest.approx(83.333334)


@pytest.mark.asyncio
async def test_a_trade_counts_once_as_its_levels_or_else_at_its_average() -> None:
    """Never a taker record and its maker records both. A trade whose maker records were not
    all read stays one print at its average price, and is counted as such."""
    venue = FakeVenue()
    venue.add(CID,
              _trade(10, "Up", "SELL", 30.0, 0.405,
                     legs=[("Up", "BUY", 15.0, 0.41), ("Up", "BUY", 15.0, 0.40)]),
              _trade(11, "Up", "SELL", 30.0, 0.405, legs=[("Up", "BUY", 15.0, 0.41)]),
              _trade(12, "Up", "SELL", 1.0, 0.99))
    read = await _fill_tape(venue)
    assert [(p.ts, p.hits(), p.size, p.averaged) for p in read.prints] == [
        (10, ("Up", 0.41), 15.0, False), (10, ("Up", 0.40), 15.0, False),
        (11, ("Up", 0.405), 30.0, True), (12, ("Up", 0.99), 1.0, False)]
    assert read.averaged == 1


def _sweep(ts: int = 10) -> dict:
    """10 Up sold at an average of 0.405: 5 to a bid at 0.41, 5 to a bid at 0.40."""
    return _trade(ts, "Up", "SELL", 10.0, 0.405,
                  legs=[("Up", "BUY", 5.0, 0.41), ("Up", "BUY", 5.0, 0.40)])


def _a_leg_for_another_token(rec: dict) -> None:
    rec["_legs"].append(maker(rec, "Up", "BUY", 3.0, 0.40, asset="OTHER"))


def _a_leg_a_second_later(rec: dict) -> None:
    rec["_legs"][1]["timestamp"] += 1


def _a_leg_into_the_other_outcomes_bids(rec: dict) -> None:
    rec["_legs"][1] = maker(rec, "Up", "SELL", 5.0, 0.60, asset=UP)


@pytest.mark.asyncio
@pytest.mark.parametrize("spoil", [
    _a_leg_for_another_token, _a_leg_a_second_later, _a_leg_into_the_other_outcomes_bids])
async def test_a_trade_whose_maker_records_do_not_fit_it_stays_at_its_average(spoil) -> None:
    """None of these has been seen on the venue. A maker record that cannot be read, one in
    another second, or one selling into the other outcome's bids means the records cannot be
    trusted to show the trade's levels, even when the rest add up to its size."""
    venue = FakeVenue()
    sweep = _sweep()
    spoil(sweep)
    venue.add(CID, sweep, _trade(20, "Up", "SELL", 1.0, 0.99))
    read = await _fill_tape(venue)
    assert [(p.ts, p.hits(), p.size, p.averaged) for p in read.prints] == [
        (10, ("Up", 0.405), 10.0, True), (20, ("Up", 0.99), 1.0, False)]
    assert read.averaged == 1


@pytest.mark.asyncio
async def test_two_taker_records_in_one_transaction_stay_at_their_averages() -> None:
    """Not seen on the venue: a transaction with two taker records cannot say which maker
    record met which, even when the ones listed add up to one taker's size."""
    venue = FakeVenue()
    first = _sweep()
    second = {**_trade(10, "Up", "SELL", 4.0, 0.40),
              "transactionHash": first["transactionHash"], "_legs": []}
    venue.add(CID, first, second, _trade(20, "Up", "SELL", 1.0, 0.99))
    read = await _fill_tape(venue)
    assert [(p.hits(), p.size, p.averaged) for p in read.prints if p.ts == 10] == [
        (("Up", 0.405), 10.0, True), (("Up", 0.40), 4.0, True)]
    assert read.averaged == 2


@pytest.mark.asyncio
async def test_one_makers_identical_orders_in_one_trade_all_count() -> None:
    """Seen live (btc-updown-15m-1790692200): one wallet's three 5-share bids at 0.52 filled by
    one taker, three identical records, here across the boundary of two pages."""
    venue = FakeVenue()
    same = [("Up", "BUY", 5.0, 0.52, "0xsame")] * 3
    venue.add(CID, _trade(10, "Up", "SELL", 15.0, 0.52, legs=same))
    # 499 newer trades, two records each in the combined list, put its maker records at
    # positions 999-1001: the first page ends at 999, the second starts at 950.
    venue.add(CID, *(_trade(20 + i, "Up", "SELL", 1.0, 0.99) for i in range(499)))
    read = await _fill_tape(venue)
    ours = [p for p in read.prints if p.ts == 10]
    assert [(p.hits(), p.size) for p in ours] == [(("Up", 0.52), 5.0)] * 3
    assert read.averaged == 0


@pytest.mark.asyncio
async def test_a_quiet_stretch_reads_only_the_taker_list() -> None:
    venue = FakeVenue()
    venue.add(CID, _trade(10, "Up", "SELL", 1.0, 0.99))
    read = await _fill_tape(venue, since=50)
    assert (read.prints, read.newest_ts) == ((), 10)
    assert [p["takerOnly"] for p in venue.tape_calls(CID)] == ["true"]


@pytest.mark.asyncio
async def test_the_read_is_complete_only_as_far_as_both_lists_reach() -> None:
    """The two lists are separate copies and can be at different points in time. A trade
    the combined copy has not reached yet is past the reply's newest record: it waits for the
    next read rather than be taken at its average."""
    venue = FakeVenue()
    venue.add(CID, _trade(10, "Up", "SELL", 5.0, 0.40), _trade(20, "Up", "SELL", 5.0, 0.40),
              _trade(30, "Up", "SELL", 5.0, 0.40))
    real = venue._page

    def older_combined_copy(cid: str, offset: int, limit: int, taker_only: bool = True):
        page = real(cid, offset, limit, taker_only)
        return page if taker_only else [r for r in page if r["timestamp"] < 30]

    venue._page = older_combined_copy  # type: ignore[method-assign]
    read = await _fill_tape(venue)
    assert (read.newest_ts, read.averaged) == (20, 0)
    # Past the reply's newest record it is not counted, but it is still marked as a whole
    # trade at its average, so it can never fill an order beyond what its levels would.
    assert [p.averaged for p in read.prints if p.ts == 30] == [True]
    # Once both copies have it, it is read at its level.
    venue._page = real  # type: ignore[method-assign]
    assert (await _fill_tape(venue)).newest_ts == 30


@pytest.mark.asyncio
async def test_a_stretch_too_deep_for_the_combined_list_falls_back_to_averages() -> None:
    """The combined list holds about 2.8 records per trade and pages back only about 10,500.
    Read from the start of a busy window (after a restart, say), its oldest trades come at
    their average price, the rest level by level, every trade once, and nothing is refused."""
    venue = FakeVenue()
    # 5,000 two-level sweeps over one window: 15,000 records in the combined list.
    venue.add(CID, *(_trade(1_790_503_200 + i * 900 // 5000, "Up", "SELL", 2.0, 0.405,
                            legs=[("Up", "BUY", 1.0, 0.41), ("Up", "BUY", 1.0, 0.40)])
                     for i in range(5000)))
    read = await _fill_tape(venue, since=1_790_503_200)
    assert sum(p.size for p in read.prints) == pytest.approx(10_000.0)
    averaged = [p for p in read.prints if p.hits()[1] == 0.405]
    levels = [p for p in read.prints if p.hits()[1] != 0.405]
    assert 1000 < len(averaged) == read.averaged < 2000
    assert len(levels) == 2 * (5000 - len(averaged))
    assert max(p.ts for p in averaged) <= min(p.ts for p in levels)
    assert max(int(p["offset"]) for p in venue.tape_calls(CID)) <= 10_000


def _busy_tape(venue: FakeVenue, records: int) -> None:
    """``records`` taker records spread over one 15m window from 1_790_503_200."""
    venue.add("0xcid", *(trade(1_790_503_200 + i * 900 // records, "Up", "SELL", 1.0, 0.40,
                               cid="0xcid", up="UP", down="DN") for i in range(records)))


@pytest.mark.asyncio
async def test_the_busiest_window_seen_reads_back_from_its_start() -> None:
    """7,017 taker records, the most seen in one BTC 15m window, are all read from the
    window's start, within the venue's paging limits (1,000 a page, offsets up to 10,000)."""
    venue = FakeVenue()
    _busy_tape(venue, 7017)
    read = await tape.read_taker_tape(venue, "0xcid", since=1_790_503_200,
                                      up_token="UP", down_token="DN")
    assert len(read.prints) == 7017
    pages = venue.tape_calls("0xcid")
    assert max(int(p["limit"]) for p in pages) <= 1000
    assert max(int(p["offset"]) for p in pages) <= 10_000


@pytest.mark.asyncio
async def test_a_tape_deeper_than_the_venue_pages_is_refused_not_cut_short() -> None:
    venue = FakeVenue()
    _busy_tape(venue, 12_000)
    with pytest.raises(tape.TapeUnavailable, match="too long to read back"):
        await tape.read_taker_tape(venue, "0xcid", since=1_790_503_200,
                                   up_token="UP", down_token="DN")
    assert max(int(p["offset"]) for p in venue.tape_calls("0xcid")) <= 10_000


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
