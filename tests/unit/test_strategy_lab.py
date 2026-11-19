"""Strategy Lab replay (polymarket_bot.strategy_lab): hand-built windows, no network."""
from __future__ import annotations

import pytest

from polymarket_bot import strategy_lab as lab


def _window(i: int, up: float, ticks: list[lab.Tick], block: int = 1) -> lab.Window:
    return lab.Window(("5m", 1_000_000 + 300 * i), up, ticks, block)


def _tick(rem, fair, ua, da, ub=None, db=None, size=500.0):
    return lab.Tick(rem, fair, ua, da, ub, db, size, size)


def test_settle_pays_one_entry_fee_and_settles_at_the_outcome() -> None:
    w = _window(1, 1.0, [_tick(200, 0.80, 0.74, 0.27), _tick(150, 0.85, 0.80, 0.21)])
    (t,) = lab.simulate([w], lab.Rules(), {1: None})
    fee = 5 * 0.07 * 0.74 * 0.26
    assert t.exit == "SETTLE" and t.entry == 0.74
    assert t.net == pytest.approx(5 * 0.26 - fee)


def test_scalp_sells_at_the_bid_and_pays_a_second_fee() -> None:
    w = _window(1, 0.0, [
        _tick(200, 0.80, 0.74, 0.27, 0.73, 0.26),
        _tick(180, 0.90, 0.85, 0.16, 0.84, 0.15),  # +10c at the bid: above the 10% target
    ])
    (t,) = lab.simulate([w], lab.Rules(exit_style="scalp"), {1: None})
    assert t.exit == "TARGET"
    assert t.net == pytest.approx(5 * (0.84 - 0.74) - 5 * 0.07 * (0.74 * 0.26 + 0.84 * 0.16))


def test_block_zero_only_trains_and_fills_are_capped_by_the_book() -> None:
    ticks = [_tick(200, 0.80, 0.74, 0.27, size=8.0)]
    trades = lab.simulate([_window(0, 1.0, ticks, block=0), _window(1, 1.0, ticks)],
                          lab.Rules(shares=50.0), {1: None})
    assert len(trades) == 1 and trades[0].shares == 8.0 and trades[0].capped


def test_learned_rules_need_a_walk_forward_fit() -> None:
    w = _window(1, 1.0, [_tick(200, 0.80, 0.74, 0.27)])
    assert lab.simulate([w], lab.Rules(learned=True), {1: None}) == []
    assert len(lab.simulate([w], lab.Rules(learned=True), {1: (1.0, 0.0)})) == 1


def test_score_flags_results_beyond_luck() -> None:
    def trade(net):
        return lab.Trade(1, "Up", 0.7, 5, net, net, "SETTLE", False)
    assert lab.score([trade(0.5)] * 40)["verdict"] == "ahead"
    assert lab.score([trade(-0.5)] * 40)["verdict"] == "behind"
    assert lab.score([trade(1.0), trade(-1.0)] * 20)["verdict"] == "luck"
    assert lab.score([trade(1.0)] * 5)["verdict"] == "too_early"
    assert lab.score([trade(1.0), trade(-3.0), trade(1.0)])["worst_drawdown_usd"] == -3.0



def test_compare_learning_scores_both_sides_on_the_same_windows(monkeypatch) -> None:
    # Up wins; the raw model (80%) buys Up at 74c, while a market-only fit
    # (a=0, b=1) sees no edge and stays out.
    ticks = [_tick(200, 0.80, 0.74, 0.27)]
    windows = [_window(i, 1.0, ticks, block=i * lab.BLOCKS // 10) for i in range(10)]
    monkeypatch.setattr(lab, "load_history", lambda db: windows)
    monkeypatch.setattr(lab, "_learned_fits", lambda ws: {k: (0.0, 1.0) for k in range(lab.BLOCKS)})
    out = lab.compare_learning("unused.db", lab.Rules())
    assert out["ok"] and out["windows"] == 8
    assert out["raw_n"] == 8 and out["learned_n"] == 0
    assert out["raw_usd"] == pytest.approx(8 * (5 * 0.26 - 5 * 0.07 * 0.74 * 0.26))
    monkeypatch.setattr(lab, "load_history", lambda db: windows[:3])
    assert lab.compare_learning("unused.db", lab.Rules())["ok"] is False


def test_several_trades_per_window_follow_the_setting() -> None:
    """Up to ``max_open`` entries per window, ``spacing_seconds`` apart."""
    from dataclasses import replace

    ticks = [
        lab.Tick(remaining=r, fair=0.70, up_ask=0.60, down_ask=0.42, up_bid=0.59,
                 down_bid=0.41, up_ask_size=100.0, down_ask_size=100.0)
        for r in range(290, 60, -10)
    ]
    window = lab.Window(key=("btc-updown-5m-0", 0), up=1.0, ticks=ticks, block=1)
    base = lab.Rules(edge_min=0.02, edge_max=0.5, price_min=0.5, price_max=0.95,
                     min_seconds_left=60, shares=5.0, min_confidence=0.5)
    one = lab.simulate([window], base, {})
    assert len(one) == 1
    three = lab.simulate([window], replace(base, max_open=3, spacing_seconds=30), {})
    assert len(three) == 3 and all(t.exit == "SETTLE" for t in three)
    assert sum(t.net for t in three) == pytest.approx(3 * one[0].net)
    assert lab.describe(replace(base, max_open=3, spacing_seconds=30)).endswith(
        "up to 3 at once, 30s apart")
    groups = {g for g, _label, _r in lab._variants(replace(base, max_open=3))}
    assert {"Trades open at once", "Wait between trades in a window"} <= groups
    assert "Wait between trades in a window" not in {g for g, _l, _r in lab._variants(base)}
    values = {
        "paper_entry_edge_min": 0.02, "paper_entry_edge_max": 0.5, "paper_min_entry_price": 0.5,
        "paper_max_entry_price": 0.95, "paper_entry_min_remaining_seconds": 60,
        "paper_max_open_positions": 3, "paper_entry_spacing_seconds": 30,
    }
    rules = lab.rules_from_settings(values, "settle")
    assert (rules.max_open, rules.spacing_seconds) == (3, 30)
    old = {k: v for k, v in values.items() if "open_positions" not in k and "spacing" not in k}
    assert (lab.rules_from_settings(old).max_open, lab.rules_from_settings(old).spacing_seconds) == (1, 0)


def test_scalp_with_several_holdings_exits_each_one() -> None:
    from dataclasses import replace

    rising = [
        lab.Tick(remaining=r, fair=0.70, up_ask=a, down_ask=0.42, up_bid=a - 0.01,
                 down_bid=0.41, up_ask_size=100.0, down_ask_size=100.0)
        for r, a in ((290, 0.60), (280, 0.60), (250, 0.60), (240, 0.75), (230, 0.75))
    ]
    window = lab.Window(key=("btc-updown-5m-0", 0), up=1.0, ticks=rising, block=1)
    rules = lab.Rules(exit_style="scalp", edge_min=0.02, edge_max=0.5, price_min=0.5,
                      price_max=0.95, min_seconds_left=60, shares=5.0, min_confidence=0.5,
                      max_open=2, spacing_seconds=30)
    trades = lab.simulate([window], rules, {})
    # Bought at 290 s and 250 s (30 s apart), both sold at the 74c bid for TARGET.
    assert [t.exit for t in trades] == ["TARGET", "TARGET"]
    assert all(t.entry == pytest.approx(0.60) for t in trades)
