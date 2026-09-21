"""Tests for price_the_copy — what following a target wallet's trade would cost."""

from __future__ import annotations

from polymarket_bot.pairarb.mirror import price_the_copy


def _t(price=0.40, size=10.0, outcome="Up", ts=1786705500):
    return {
        "price": price, "size": size, "outcome": outcome, "timestamp": ts,
        "slug": "doge-updown-5m-1", "conditionId": "0xabc", "asset": "tok",
    }


def test_copy_is_priced_at_the_ask_we_cross_not_their_fill():
    f = price_the_copy(_t(price=0.40), [(0.52, 100.0)], max_shares=5.0)
    assert f is not None
    assert f.their_price == 0.40
    assert f.our_price == 0.52
    assert f.fee > 0  # a copy always crosses, so it always pays the taker fee
    assert f.slippage_per_share > 0.12


def test_copy_size_is_clamped_up_to_the_venue_floor():
    """Their 2-share clip cannot be matched — the floor forces 5."""
    f = price_the_copy(_t(size=2.0), [(0.50, 100.0)], max_shares=50.0)
    assert f is not None
    assert f.size == 5.0


def test_skip_below_min_declines_rather_than_oversizing():
    assert price_the_copy(_t(size=2.0), [(0.50, 100.0)], skip_below_min=True) is None
    assert price_the_copy(_t(size=20.0), [(0.50, 100.0)], skip_below_min=True) is not None


def test_declines_when_depth_cannot_cover_the_venue_minimum():
    """3 shares displayed cannot support a 5-share order — not a small fill."""
    assert price_the_copy(_t(), [(0.50, 3.0)], max_shares=5.0) is None


def test_copy_pnl_charges_the_fee_on_both_win_and_loss():
    f = price_the_copy(_t(), [(0.50, 100.0)], max_shares=5.0)
    assert f is not None
    won = f.pnl(resolved_up=True)
    lost = f.pnl(resolved_up=False)
    assert won == 5.0 * (1.0 - (f.our_price + f.fee))
    assert lost == 5.0 * (0.0 - (f.our_price + f.fee))
