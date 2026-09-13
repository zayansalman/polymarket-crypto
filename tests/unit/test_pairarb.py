"""Unit tests for the two-sided pair quoter and its maker fill model (#182).

The fill model is the honesty-critical part: a maker simulation that is too
generous manufactures profit that does not exist. These tests pin the
pessimistic assumptions in place — back of queue, displayed size only, tape
attribution — so a future change that loosens them fails loudly.
"""

from __future__ import annotations

from polymarket_bot.pairarb.fills import hits_resting_bid, settle_window, simulate_fill
from polymarket_bot.pairarb.quoter import MIN_ORDER_SHARES, plan_quote, quote_price
from polymarket_bot.pairarb.types import BookSide, RestingOrder


def _order(price=0.50, size=10.0, depth_ahead=0.0, outcome="Up", posted_ts=1000):
    return RestingOrder(
        outcome=outcome,
        price=price,
        size=size,
        depth_ahead=depth_ahead,
        posted_ts=posted_ts,
    )


def _trade(outcome, price, size, ts=1000, side="BUY"):
    return {"outcome": outcome, "price": price, "size": size, "timestamp": ts, "side": side}


# --------------------------------------------------------------------------- #
# Tape attribution
# --------------------------------------------------------------------------- #


def test_complementary_buy_hits_our_bid():
    """A BUY of Down at 0.50 is a SELL of Up at 0.50 — it lifts an Up bid at 0.50."""
    assert hits_resting_bid(_trade("Down", 0.50, 10), "Up", 0.50)


def test_complementary_buy_too_cheap_does_not_reach_us():
    """A BUY of Down at 0.45 sells Up at 0.55, above our 0.50 bid — never reaches us."""
    assert not hits_resting_bid(_trade("Down", 0.45, 10), "Up", 0.50)


def test_buy_of_our_own_outcome_does_not_hit_our_bid():
    """A BUY of Up lifts the Up *ask*. It must never count as filling our Up bid."""
    assert not hits_resting_bid(_trade("Up", 0.51, 10), "Up", 0.50)


def test_explicit_sell_of_our_outcome_hits_us():
    assert hits_resting_bid(_trade("Up", 0.49, 10, side="SELL"), "Up", 0.50)


def test_seller_who_stopped_above_us_still_counts_as_through_volume():
    """A BUY of Down at 0.55 sells Up at 0.45 — through our 0.50 level."""
    assert hits_resting_bid(_trade("Down", 0.55, 10), "Up", 0.50)


def test_malformed_trade_is_ignored_not_crashed():
    assert not hits_resting_bid({"outcome": "Down", "price": "nope"}, "Up", 0.50)
    assert not hits_resting_bid({}, "Up", 0.50)


# --------------------------------------------------------------------------- #
# Queue mechanics — the pessimistic assumptions
# --------------------------------------------------------------------------- #


def test_queue_ahead_absorbs_volume_before_we_fill():
    """With 100 ahead of us, 60 shares of flow fills nothing of ours."""
    order = _order(size=10.0, depth_ahead=100.0)
    assert simulate_fill(order, [_trade("Down", 0.50, 60)]).filled == 0.0


def test_we_fill_only_the_excess_over_the_queue():
    """105 through, 100 ahead -> exactly 5 for us, not 105."""
    order = _order(size=10.0, depth_ahead=100.0)
    assert simulate_fill(order, [_trade("Down", 0.50, 105)]).filled == 5.0


def test_fill_is_capped_at_our_own_size():
    order = _order(size=10.0, depth_ahead=0.0)
    assert simulate_fill(order, [_trade("Down", 0.50, 999)]).filled == 10.0


def test_trades_before_we_posted_do_not_fill_us():
    """We were not in the book yet; prior volume must not count."""
    order = _order(size=10.0, depth_ahead=0.0, posted_ts=1000)
    assert simulate_fill(order, [_trade("Down", 0.50, 50, ts=999)]).filled == 0.0


def test_volume_accumulates_across_multiple_trades():
    order = _order(size=10.0, depth_ahead=20.0)
    trades = [_trade("Down", 0.50, 12), _trade("Down", 0.50, 13)]
    assert simulate_fill(order, trades).filled == 5.0


def test_empty_tape_leaves_order_untouched():
    order = _order(size=10.0, depth_ahead=0.0)
    assert simulate_fill(order, []) is order


# --------------------------------------------------------------------------- #
# Settlement — hedged pairs vs stranded legs
# --------------------------------------------------------------------------- #


def test_completed_pair_pnl_is_outcome_independent():
    """The whole point: a hedged pair pays the same whichever way it resolves."""
    up = settle_window("w", 10.0, 10.0, 0.50, 0.49, resolved_up=True)
    down = settle_window("w", 10.0, 10.0, 0.50, 0.49, resolved_up=False)
    assert up.pnl == down.pnl
    assert abs(up.pnl - 10.0 * 0.01) < 1e-9
    assert up.stranded == 0.0


def test_stranded_leg_settles_on_outcome_never_at_par():
    """A naked Up leg that loses must book its full cost as a loss."""
    out = settle_window("w", 10.0, 0.0, 0.50, 0.49, resolved_up=False)
    assert out.pairs == 0.0
    assert out.stranded == 10.0
    assert abs(out.pnl - (-10.0 * 0.50)) < 1e-9


def test_stranded_winning_leg_books_its_gain():
    out = settle_window("w", 10.0, 0.0, 0.50, 0.49, resolved_up=True)
    assert abs(out.pnl - (10.0 * 0.50)) < 1e-9


def test_partial_hedge_splits_into_pair_plus_stranded():
    """10 Up / 4 Down = 4 hedged pairs + 6 stranded Up, resolving Down."""
    out = settle_window("w", 10.0, 4.0, 0.50, 0.49, resolved_up=False)
    assert out.pairs == 4.0
    assert out.stranded == 6.0
    expected = 4.0 * 0.01 + 6.0 * (0.0 - 0.50)
    assert abs(out.pnl - expected) < 1e-9


def test_stranded_down_leg_wins_when_market_resolves_down():
    out = settle_window("w", 0.0, 10.0, 0.50, 0.49, resolved_up=False)
    assert abs(out.pnl - (10.0 * (1.0 - 0.49))) < 1e-9


# --------------------------------------------------------------------------- #
# Quote planning
# --------------------------------------------------------------------------- #


def _side(outcome, bid, depth=100.0, ask=None, ladder=None):
    bids = ladder if ladder is not None else ([(bid, depth)] if bid is not None else [])
    return BookSide(
        token_id=f"tok-{outcome}",
        outcome=outcome,
        bids=tuple(bids),
        best_ask=ask,
    )


def test_quotes_when_bid_sum_leaves_edge():
    """The live 2026-08-14 book: 0.500 + 0.490 = 0.990 -> 1c per pair."""
    plan = plan_quote("w", _side("Up", 0.50), _side("Down", 0.49), size=10.0)
    assert plan is not None
    assert abs(plan.edge_per_pair - 0.01) < 1e-9
    assert abs(plan.pair_cost - 0.99) < 1e-9


def test_stands_aside_when_bids_sum_to_par():
    """No edge at 0.50/0.50 — quoting here earns nothing and risks stranding."""
    assert plan_quote("w", _side("Up", 0.50), _side("Down", 0.50), size=10.0) is None


def test_stands_aside_when_edge_below_floor():
    plan = plan_quote("w", _side("Up", 0.50), _side("Down", 0.498), size=10.0)
    assert plan is None


def test_stands_aside_when_a_leg_has_no_bid():
    assert plan_quote("w", _side("Up", None), _side("Down", 0.49), size=10.0) is None
    assert plan_quote("w", _side("Up", 0.50), _side("Down", None), size=10.0) is None


def test_rejects_size_below_venue_share_minimum():
    """lessons.md #85: a sub-minimum order is unplaceable, not merely small."""
    assert plan_quote("w", _side("Up", 0.50), _side("Down", 0.49), size=1.0) is None
    assert plan_quote(
        "w", _side("Up", 0.50), _side("Down", 0.49), size=MIN_ORDER_SHARES
    ) is not None


def test_plan_carries_queue_depth_for_both_legs():
    plan = plan_quote(
        "w", _side("Up", 0.50, depth=313.0), _side("Down", 0.49, depth=224.0), size=10.0
    )
    assert plan is not None
    assert plan.up_depth_ahead == 313.0
    assert plan.down_depth_ahead == 224.0


# --------------------------------------------------------------------------- #
# VWAP — a re-quoted leg fills at several prices
# --------------------------------------------------------------------------- #


def test_vwap_weights_by_size_not_count():
    from polymarket_bot.pairarb.fills import vwap

    price, size = vwap([(0.40, 1.0), (0.50, 9.0)])
    assert size == 10.0
    assert abs(price - 0.49) < 1e-9


def test_vwap_of_no_executions_is_zero_not_a_crash():
    from polymarket_bot.pairarb.fills import vwap

    assert vwap([]) == (0.0, 0.0)
    assert vwap([(0.50, 0.0)]) == (0.0, 0.0)


def test_requoted_leg_settles_on_its_blended_cost():
    """Filled 5 @ 0.45 then 5 @ 0.55 -> blended 0.50 against a 0.49 hedge."""
    from polymarket_bot.pairarb.fills import vwap

    up_px, up_sz = vwap([(0.45, 5.0), (0.55, 5.0)])
    out = settle_window("w", up_sz, 10.0, up_px, 0.49, resolved_up=True)
    assert abs(up_px - 0.50) < 1e-9
    assert out.pairs == 10.0
    assert abs(out.pnl - 10.0 * (1.0 - 0.99)) < 1e-9


# --------------------------------------------------------------------------- #
# Resting BELOW the touch — the strategy the reference accounts actually run
# --------------------------------------------------------------------------- #


def test_depth_ahead_counts_every_level_at_or_above_our_price():
    """A seller sweeps the highest bids first, so all of them are ahead of us."""
    side = _side("Up", None, ladder=[(0.50, 10.0), (0.49, 20.0), (0.48, 40.0)])
    assert side.depth_ahead_of(0.50) == 10.0
    assert side.depth_ahead_of(0.49) == 30.0
    assert side.depth_ahead_of(0.48) == 70.0


def test_offset_rests_below_the_touch_and_widens_the_edge():
    """Touch sums to 0.99 (1c). Resting 5c below each leg makes it 11c."""
    up = _side("Up", 0.50)
    down = _side("Down", 0.49)
    at_touch = plan_quote("w", up, down, size=10.0, offset=0.0)
    deep = plan_quote("w", up, down, size=10.0, offset=0.05)
    assert at_touch is not None and deep is not None
    assert abs(at_touch.edge_per_pair - 0.01) < 1e-9
    assert abs(deep.edge_per_pair - 0.11) < 1e-9
    assert abs(deep.up_price - 0.45) < 1e-9
    assert abs(deep.down_price - 0.44) < 1e-9


def test_offset_quote_reports_the_deeper_queue():
    up = _side("Up", None, ladder=[(0.50, 10.0), (0.45, 25.0)])
    down = _side("Down", None, ladder=[(0.49, 8.0), (0.44, 12.0)])
    plan = plan_quote("w", up, down, size=10.0, offset=0.05)
    assert plan is not None
    assert plan.up_depth_ahead == 35.0
    assert plan.down_depth_ahead == 20.0


def test_never_rests_below_the_floor_price():
    """A 1c bid shows huge edge but fills only once the outcome is decided."""
    from polymarket_bot.pairarb.quoter import MIN_QUOTE_PRICE

    assert quote_price(_side("Up", 0.05), offset=0.04) is None
    assert quote_price(_side("Up", 0.10), offset=0.05) == 0.05
    assert MIN_QUOTE_PRICE > 0


# --------------------------------------------------------------------------- #
# Copy mirror — venue floor at small capital
# --------------------------------------------------------------------------- #


def _t(price=0.50, size=20.0, outcome="Up", ts=1000):
    return {
        "price": price, "size": size, "outcome": outcome, "timestamp": ts,
        "slug": "doge-updown-5m-1", "conditionId": "0xabc", "asset": "tok",
    }


def test_copy_is_priced_at_the_ask_we_cross_not_their_fill():
    from polymarket_bot.pairarb.mirror import price_the_copy

    f = price_the_copy(_t(price=0.40), [(0.52, 100.0)], max_shares=5.0)
    assert f is not None
    assert f.their_price == 0.40
    assert f.our_price == 0.52
    assert f.fee > 0  # a copy always crosses, so it always pays the taker fee
    assert f.slippage_per_share > 0.12


def test_copy_size_is_clamped_up_to_the_venue_floor():
    """Their 2-share clip cannot be matched — the floor forces 5."""
    from polymarket_bot.pairarb.mirror import price_the_copy

    f = price_the_copy(_t(size=2.0), [(0.50, 100.0)], max_shares=50.0)
    assert f is not None
    assert f.size == 5.0


def test_skip_below_min_declines_rather_than_oversizing():
    from polymarket_bot.pairarb.mirror import price_the_copy

    assert price_the_copy(_t(size=2.0), [(0.50, 100.0)], skip_below_min=True) is None
    assert price_the_copy(_t(size=20.0), [(0.50, 100.0)], skip_below_min=True) is not None


def test_declines_when_depth_cannot_cover_the_venue_minimum():
    """3 shares displayed cannot support a 5-share order — not a small fill."""
    from polymarket_bot.pairarb.mirror import price_the_copy

    assert price_the_copy(_t(), [(0.50, 3.0)], max_shares=5.0) is None


def test_copy_pnl_charges_the_fee_on_both_win_and_loss():
    from polymarket_bot.pairarb.mirror import price_the_copy

    f = price_the_copy(_t(), [(0.50, 100.0)], max_shares=5.0)
    assert f is not None
    won = f.pnl(resolved_up=True)
    lost = f.pnl(resolved_up=False)
    assert won == 5.0 * (1.0 - (f.our_price + f.fee))
    assert lost == 5.0 * (0.0 - (f.our_price + f.fee))
    assert won + abs(lost) == 5.0  # the pair of outcomes spans exactly $1/share
