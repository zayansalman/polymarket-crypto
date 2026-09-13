"""Unit tests for the tick-replay backtest tool (#144).

Covers the two load-bearing pieces: outcome reconstruction from the next
window's reference print (validated at 100% on ground truth), and the
first-signal-per-window replay with fee-true settlement.
"""

from __future__ import annotations

import pytest

from polymarket_bot import strategy
from polymarket_bot.shadow.fees import net_pnl_per_share
from tools.replay_race import SHARES, reconstruct_outcomes, replay


def _tick(slug: str, remaining: int, ref: float, **kw: object) -> dict:
    base = dict(
        created_at=str(kw.pop("created_at", "2026-06-12T00:00:00+00:00")),
        window_slug=slug,
        remaining_seconds=remaining,
        spot_price=50010.0,
        reference_price=ref,
        sigma_per_second=0.0003,
        fair_up_prob=0.61,
        up_best_bid=0.54,
        up_best_ask=0.55,
        down_best_ask=0.46,
        down_best_bid=0.44,
    )
    base.update(kw)
    return base


class TestReconstructOutcomes:
    def test_labels_from_next_window_reference(self) -> None:
        """outcome(N) = Up iff ref(N+1) >= ref(N); last window stays unlabeled."""
        ticks = {
            "btc-updown-5m-1000000000": [_tick("btc-updown-5m-1000000000", 250, 100.0)],
            "btc-updown-5m-1000000300": [_tick("btc-updown-5m-1000000300", 250, 101.0)],
            "btc-updown-5m-1000000600": [_tick("btc-updown-5m-1000000600", 250, 100.5)],
        }
        labels, agreement, checked = reconstruct_outcomes(ticks, known={})
        assert labels["btc-updown-5m-1000000000"] == "Up"  # 101 >= 100
        assert labels["btc-updown-5m-1000000300"] == "Down"  # 100.5 < 101
        assert "btc-updown-5m-1000000600" not in labels  # no next window
        assert checked == 0

    def test_ground_truth_wins_and_agreement_measured(self) -> None:
        ticks = {
            "btc-updown-5m-1000000000": [_tick("btc-updown-5m-1000000000", 250, 100.0)],
            "btc-updown-5m-1000000300": [_tick("btc-updown-5m-1000000300", 250, 101.0)],
        }
        # Known outcome DISAGREES with reconstruction: truth wins, agreement 0.
        labels, agreement, checked = reconstruct_outcomes(
            ticks, known={"btc-updown-5m-1000000000": "Down"}
        )
        assert labels["btc-updown-5m-1000000000"] == "Down"
        assert checked == 1 and agreement == 0.0

    def test_equal_reference_resolves_up(self) -> None:
        """The venue's >= tie rule: an unchanged print resolves Up."""
        ticks = {
            "btc-updown-5m-1000000000": [_tick("btc-updown-5m-1000000000", 250, 100.0)],
            "btc-updown-5m-1000000300": [_tick("btc-updown-5m-1000000300", 250, 100.0)],
        }
        labels, _, _ = reconstruct_outcomes(ticks, known={})
        assert labels["btc-updown-5m-1000000000"] == "Up"


class TestReplay:
    @pytest.fixture
    def params(self) -> strategy.StrategyParams:
        return strategy.StrategyParams(
            min_trade_usd=1.0,
            max_trade_usd=5.0,
            entry_edge_min=0.05,
            min_confidence=0.55,
            entry_min_remaining_seconds=60,
            max_entry_price=0.95,
            min_entry_price=0.05,
            entry_edge_max=1.0,
        )

    def test_first_signal_per_window_fee_true(self, params) -> None:
        """One window, two firing ticks -> ONE trade at the FIRST tick's ask,
        settled net of the taker fee."""
        from polymarket_bot.shadow.signals import pricing_fresh_v8

        slug = "btc-updown-5m-1000000000"
        ticks = {
            slug: [
                _tick(slug, 260, 100.0, up_best_ask=0.55, created_at="t1"),
                _tick(slug, 250, 100.0, up_best_ask=0.53, created_at="t2"),
            ]
        }
        trades = replay(
            ticks, {slug: "Up"}, {"pricing_fresh_v8": pricing_fresh_v8}, params
        )
        assert len(trades) == 1
        t = trades[0]
        assert t.side == "Up" and t.entry_price == pytest.approx(0.55)
        assert t.created_at == "t1"
        assert t.pnl == pytest.approx(SHARES * net_pnl_per_share(0.55, won=True))

    def test_unlabeled_window_produces_no_trades(self, params) -> None:
        from polymarket_bot.shadow.signals import pricing_fresh_v8

        slug = "btc-updown-5m-1000000000"
        ticks = {slug: [_tick(slug, 260, 100.0)]}
        assert replay(ticks, {}, {"pricing_fresh_v8": pricing_fresh_v8}, params) == []
