"""FADE 1H MOMENTUM ON 15M card (panels/fade_1h.py) and its loader in execution_view.py.

Pure ``render(...)`` tests with dict fixtures shaped like the ledger's rows and the runner's
status (the inputs are a real ``Inputs.as_record()``), then tests against a real ledger: one
real runner pass read back through the loader, a loop that died, and the whole page, to check
where the card sits.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import pytest_asyncio

import config as _config
import db as _db
from polymarket_bot.fade_1h_momentum_15m import executor as ex
from polymarket_bot.fade_1h_momentum_15m import ledger
from polymarket_bot.fade_1h_momentum_15m import runner as rn
from polymarket_bot.fade_1h_momentum_15m.inputs import Problem
from polymarket_exec.ops.dashboard import execution_view as ev
from polymarket_exec.ops.dashboard.panels import fade_1h as panel
from tests.unit.test_fade1h_runner import (
    END,
    NOW,
    FakeHub,
    FakeVenue,
    make_inputs,
    save_window,
    slug_of,
)

ROOT = Path(__file__).resolve().parents[2]
SLUG = slug_of("btc")
STORY = ("BTC's 15m leg is up 0.15% with 10 min left; the last candles ran up so the snap-back "
         "takes 4 points off Up; the maths rests a scaled Up buy order, child orders at 53c "
         "and 50c.")
FACTORS = {"leg_pts": 8.0, "snapback_pts": -3.5, "momentum_pts": 2.5, "anchor_pts": 3.0,
           "stretch_M": 0.004, "p_model": 0.62, "p": 0.60, "market_up": 0.54,
           "growth_buy_up": 0.0041, "growth_buy_down": 0.0}
CHILD_ORDERS = [
    {"level": 0, "kind": "entry", "order_side": "BUY", "side": "Up", "price": 0.53,
     "shares": 7.5, "usd": 3.975, "depth_ahead": 100.0, "p_fill": 0.6, "q_fill": 0.62,
     "optimal_shares": 8.0},
    {"level": 3, "kind": "entry", "order_side": "BUY", "side": "Up", "price": 0.50,
     "shares": 5.0, "usd": 2.5, "depth_ahead": 150.0, "p_fill": 0.4, "q_fill": 0.58,
     "optimal_shares": 5.2},
]
SUMMARY = {"net_pnl_usd": 12.34, "cents_per_share": 2.5, "return_on_staked": 0.081,
           "max_drawdown_usd": 3.2, "settled_windows": 17, "settled_shares": 493.6,
           "staked_usd": 152.3, "sold_shares": 0.0, "sale_proceeds_usd": 0.0,
           "open_exposure_usd": 4.05, "open_windows": 1, "resting_usd": 2.5,
           "resting_sell_shares": 0.0, "resting_orders": 1, "windows_observed": 96}
BANNED = ("coin flip", "coin-flip", "coinflip", "50/50", "fifty-fifty", "toss-up", "gamble",
          "lottery")
COINED = ("lad" + "der", "ru" + "ng")  # words the operator's standard terms rule out
HELD_BACK = (f"{SLUG}: Down buys were held back because Up orders cancelled this pass may "
             "still have filled; the strategy never holds both sides.")


def inputs_record(asset: str = "btc", *, spot_feed: str = "chainlink", **over) -> dict:
    record = make_inputs(asset).as_record()
    record["settings"] = {"spot_feed": spot_feed, "bankroll_usd": 100.0}
    record.update(over)
    return record


def decision(asset: str = "btc", *, id: int = 10, action: str = rn.ORDERS,  # noqa: A002
             side: str | None = "Up", p: float | None = 0.60, p_model: float | None = 0.62,
             child_orders=None, hedge=None, factors=None, order=("buy", "Up"),
             reason=None, inputs=None, ts: float = NOW, sizing=None) -> dict:  # noqa: ANN001
    return {
        "id": id, "ts": ts, "asset": asset, "window_slug": slug_of(asset), "mode": "paper",
        "dials_version": 1,
        "inputs": inputs if inputs is not None else inputs_record(asset),
        "p_model": p_model, "p": p, "side": side, "kelly_f": 0.0655, "stake_usd": 6.55,
        "child_orders": CHILD_ORDERS if child_orders is None else child_orders,
        "hedge": hedge,
        "factors": None if factors is False else {
            "model": FACTORS if factors is None else factors,
            "sizing": sizing if sizing is not None else {"bankroll_usd": 100.0,
                                                         "joint_scale": 1.0, "fit_scale": 1.0},
            **({"order": {"action": order[0], "side": order[1]}} if order else {}),
            "explanation": STORY},
        "action": action, "reason": reason,
    }


def order(price: float = 0.53, *, filled: float = 0.0, kind: str = "entry", side: str = "Up",
          order_side: str = "BUY", shares: float = 7.5, asset: str = "btc", slug: str = SLUG,
          levels: list | None = None, depth: float = 100.0) -> dict:
    return {"id": int(price * 100), "asset": asset, "window_slug": slug, "kind": kind,
            "order_side": order_side, "side": side, "price": price, "shares": shares,
            "filled_shares": filled, "depth_ahead": depth,
            "levels_ahead_json": json.dumps(levels) if levels is not None else None,
            "state": "partial" if filled else "resting", "placed_ts": NOW - 60,
            "window_end": END}


def position(side: str = "Up", *, bought: float = 2.5, cost: float = 1.325,
             sold: float = 0.0, proceeds: float = 0.0, offered: float = 0.0,
             slug: str = SLUG) -> dict:
    return {"window_slug": slug, "asset": "btc", "window_end": END, "side": side,
            "shares": bought - sold, "bought_shares": bought, "cost_usd": cost,
            "sold_shares": sold, "proceeds_usd": proceeds, "offered_shares": offered,
            "avg_price": cost / bought if bought else None}


def status(**over) -> dict:
    base = {"state": "running", "last_pass_ts": NOW - 12, "passes": 31, "errors": [],
            "last_error": None, "last_error_ts": None,
            "executor": {"state": "paper", "requested_mode": "paper", "can_place": True,
                         "message": "Paper trading."},
            "bankroll": {"start_usd": 100.0, "free_usd": 91.5},
            "assets": {"btc": {"action": rn.ORDERS, "decision_id": 10, "kept": 1, "placed": 1,
                               "cancelled": 0}}}
    base.update(over)
    return base


def card(**over) -> str:
    kwargs = dict(summary=SUMMARY, decisions=[decision()], orders=[order()], positions=[],
                  status=status(), dials={"version": 1, "source": "fit_sep17_20",
                                          "n_windows": 0},
                  enabled=True, mode="paper", poll_s=60.0, now=NOW)
    kwargs.update(over)
    return panel.render(**kwargs)


def block(html: str, asset: str) -> str:
    """One coin's block of the card."""
    start = html.index(f"<b>{asset.upper()}</b>")
    end = html.find("<div class='fade-coin'>", start)
    return html[start:end if end != -1 else len(html)]


def head(html: str) -> str:
    return html.split("</summary>")[0]


# ---------------------------------------------------------------------------
# Profit first, then the state
# ---------------------------------------------------------------------------


def test_the_card_is_wide_refreshes_and_links_its_doc() -> None:
    html = card()
    assert panel.TITLE in html
    assert html.startswith("<details class='card wide fade-card fold' data-fold='fade-1h' open>")
    assert "data-static" not in html  # a live card is swapped on every refresh
    assert "href='/strategy-docs/fade_1h_momentum_15m'" in html


def test_profit_comes_first_and_there_is_no_win_rate() -> None:
    html = card()
    order_of = [html.index(x) for x in ("Net P&amp;L", "Return on staked", "Per share",
                                        "Max drawdown", "Settled windows", "Open exposure",
                                        "class='fade-state'", "class='fade-coin'")]
    assert order_of == sorted(order_of)
    for shown in ("+$12.34", "+8.1%", "+2.50c", "-$3.20", ">17<", "$4.05",
                  "1 window unsettled · 1 child order resting ($2.50 of buys)"):
        assert shown in html
    assert " won" not in html and "win rate" not in html.lower()

    sold = card(summary={**SUMMARY, "sold_shares": 12.0, "sale_proceeds_usd": 6.6,
                         "resting_sell_shares": 4.0, "resting_orders": 3})
    assert "12.00 shares sold before settling for $6.60" in sold
    assert "3 child orders resting ($2.50 of buys, 4.00 shares offered)" in sold


def test_an_empty_card_still_renders() -> None:
    html = panel.render(now=NOW)
    assert panel.TITLE in html
    assert "No pass has run yet." in html and "The loop has not run yet" in html
    assert html.count("no decision recorded yet") == 4
    assert "nothing settled yet" in html


def test_paper_is_labelled_and_live_says_not_built_and_not_authorised() -> None:
    assert "<span class='pill paper'>PAPER</span>" in head(card())

    live = card(status=status(executor={
        "state": ex.LIVE_STATE, "requested_mode": "live", "can_place": False,
        "message": "LIVE is selected, but this strategy has no live order path."}))
    assert "<span class='pill live'>LIVE · NOT BUILT / NOT AUTHORISED</span>" in live
    assert "LIVE is selected, but this strategy has no live order path." in live

    # Before the runner has reported, the global selection still tells the truth.
    early = panel.render(mode="live", now=NOW)
    assert "NOT BUILT / NOT AUTHORISED" in early and "not authorised for any market" in early
    assert "No new orders are placed" in early

    killed = card(status=status(executor={"state": ex.KILL_STATE, "can_place": False,
                                          "message": "The kill switch file is present."}))
    assert "KILL SWITCH" in killed and "The kill switch file is present." in killed


def test_the_switch_state_is_shown() -> None:
    assert "<span class='pill on'>SWITCH ON</span>" in head(card(enabled=True))
    off = card(enabled=False)
    assert "<span class='pill off'>SWITCH OFF</span>" in head(off)
    assert "Switched off on MY STRATEGIES: no new orders." in off


def test_last_pass_and_every_error_of_the_latest_pass() -> None:
    html = card(status=status(last_error="Checking fills failed: <b>boom</b>",
                              last_error_ts=NOW - 180))
    assert "Last pass 12s ago" in html and "pass 31" in html
    assert "Last error 3m ago: Checking fills failed: &lt;b&gt;boom&lt;/b&gt;" in html
    assert "the latest pass ran clean" in html
    assert "last pass 12s ago" in head(html)
    assert "No errors." in card()

    # A failure that repeats every pass is not hidden behind a later one.
    failing = card(status=status(
        last_error="BTC failed: y", last_error_ts=NOW - 12,
        errors=["Settling failed: x", "BTC failed: y", "Settling failed: x"]))
    assert "2 errors in the latest pass:" in failing
    assert "<div class='down fade-err'>Settling failed: x</div>" in failing
    assert "<div class='down fade-err'>BTC failed: y</div>" in failing
    assert "ran clean" not in failing

    many = card(status=status(errors=[f"Step {i} failed" for i in range(9)],
                              last_error="Step 8 failed"))
    assert "9 errors in the latest pass:" in many
    assert f"and {9 - panel.MAX_ERRORS} more" in many and "Step 8 failed" not in many


def test_a_loop_that_died_says_so_even_folded() -> None:
    died = card(status={"state": rn.STOPPED_ON_ERROR, "last_pass_ts": None,
                        "last_error": "The loop stopped: RuntimeError: no sockets",
                        "last_error_ts": NOW - 30,
                        "errors": ["The loop stopped: RuntimeError: no sockets"]})
    assert "<span class='pill down'>LOOP DIED</span>" in head(died)
    assert "The loop died on an error" in died and "until the app restarts" in died
    assert "An error 30s ago:" in died
    assert "The loop stopped: RuntimeError: no sockets" in died
    assert "No errors." not in died

    failed = card(status=status(state=rn.PASS_FAILED, errors=["A pass failed: x"],
                                last_error="A pass failed: x"))
    assert "<span class='pill down'>PASS FAILED</span>" in head(failed)
    assert "the next pass tries again" in failed and "An error:" in failed

    stopped = card(status=status(state=rn.STOPPED))
    assert "LOOP STOPPED" in head(stopped)
    assert "LOOP" not in head(card())


def test_a_late_pass_is_called_out() -> None:
    late = card(status=status(last_pass_ts=NOW - 600))
    assert "No pass for 10m, though one runs every 60s: the loop may be stuck." in late
    # A slower interval from Settings is not late yet.
    assert "may be stuck" not in card(status=status(last_pass_ts=NOW - 600), poll_s=300.0)
    # An earlier run's pass is old by nature; the not-started line explains it.
    assert "may be stuck" not in card(status=status(
        last_pass_ts=NOW - 600, from_earlier_run=True, state="not_started"))


def test_a_status_from_an_earlier_run_says_so() -> None:
    html = card(status=status(from_earlier_run=True, state="not_started"))
    assert "(from an earlier run of the app)" in html
    assert "The loop has not run yet in this process." in html


def test_bankroll_dials_and_windows_waiting_to_settle() -> None:
    html = card(status=status(settle_waiting={"resolution": 2, "tape": 1, "retry_later": 0,
                                              "no_market_id": 1, "backlog": 0},
                              learn_note="w_M moved from 0.60 to 0.61."))
    assert "bankroll $100.00 to start, $91.50 free to buy with" in html
    assert "dials v1: the starting fit (Sep 17-20 tape)" in html
    assert "2 ended windows waiting for the venue's result" in html
    assert "1 ended window waiting for the trade tape" in html
    assert "1 ended window with no market id recorded (cannot settle)" in html
    assert "Last learning step: w_M moved from 0.60 to 0.61." in html


# ---------------------------------------------------------------------------
# One coin: inputs and chances
# ---------------------------------------------------------------------------


def test_one_block_per_coin_in_the_runner_order() -> None:
    html = card(decisions=[decision(a, id=i) for i, a in enumerate(rn.ASSETS, 1)])
    assert html.count("<div class='fade-coin'>") == 4
    where = [html.index(f"<b>{a.upper()}</b>") for a in rn.ASSETS]
    assert where == sorted(where)


def test_the_inputs_are_in_plain_words() -> None:
    btc = block(card(), "btc")
    # make_inputs: Chainlink 100.25 against a price to beat of 100.1; 60 moves of 0.05%;
    # Binance 100.3 against an hour open of 100; the 1h book 58/60; ten minutes left.
    for label, shown in (("15m move", "+0.15%"), ("Jumpiness", "0.39% an hour"),
                         ("Stretch", "+0.40%"), ("1h move", "+0.30%"),
                         ("1h market", "Up 59c"), ("Trend", "+0.00%")):
        assert f"<span>{label}</span><b>{shown}" in btc, label
    assert ("Chainlink now $100.2500 against the price to beat $100.1000, the TWAP-60s print "
            "at the open") in btc
    assert "the snap-back pulls toward Down" in btc  # the candles ran up
    assert "10 min left" in btc and "10m 00s left" in btc and "quarter 2 of the hour" in btc
    assert f"href='https://polymarket.com/event/{SLUG}'" in btc


def test_the_move_is_measured_on_the_models_price_feed() -> None:
    binance = block(card(decisions=[decision(
        inputs=inputs_record(spot_feed="binance"), factors={**FACTORS, "leg_pct": 0.1998})]),
        "btc")
    assert "<span>15m move</span><b>+0.20%" in binance
    assert "Binance now $100.3000 against the price to beat" in binance

    # Without the model's own figure, the recorded move on the same feed.
    no_model = block(card(decisions=[decision(inputs=inputs_record(spot_feed="binance"))]), "btc")
    assert "<span>15m move</span><b>+0.20%" in no_model

    legacy = block(card(decisions=[decision(inputs=inputs_record(spot_feed="chainlink_twap60"))]),
                   "btc")
    assert "Chainlink now $100.2500" in legacy

    record = inputs_record()
    record["derived"] = {**record["derived"], "close_abar": 0.001}
    closing = block(card(decisions=[decision(inputs=record)]), "btc")
    assert "in the closing minute the 60-second average it settles on is +0.10% so far" in closing


def test_the_maths_chance_against_the_markets() -> None:
    btc = block(card(), "btc")
    assert "<span>Model</span><b>Up 62.0%" in btc
    assert "<span>Market</span><b>Up 54c" in btc  # the Up book's mid, 53/55
    assert "<span>Traded on</span><b>Up 60.0%" in btc
    assert "+6.0 pts against the market&#x27;s price" in btc


def test_the_waterfall_adds_up_to_the_chance_traded_on() -> None:
    btc = block(card(), "btc")
    moves = [float(v) for v in re.findall(r"class='fade-wf-v [a-z]+'>([+-]\d+\.\d)<", btc)]
    running = [float(v) for v in re.findall(r"class='fade-wf-c'>(\d+\.\d)%<", btc)]
    *parts, net = moves
    assert parts == [8.0, -3.5, 2.5, 3.0]
    assert sum(parts) == pytest.approx(net) == pytest.approx(100 * (0.60 - 0.5))
    assert running == [58.0, 54.5, 57.0, 60.0, 60.0]  # ends at the chance traded on
    for left, width in re.findall(r"style='left:([\d.]+)%;width:([\d.]+)%'", btc):
        assert 0.0 <= float(left) and float(left) + float(width) <= 100.05
    assert "The parts add up" not in btc

    off = block(card(decisions=[decision(p=0.70)]), "btc")
    assert "The parts add up to 60.0%, not the 70.0% traded on." in off


def test_the_story_is_in_a_traders_words_with_the_doc_link() -> None:
    btc = block(card(), "btc")
    story = btc.split("<div class='fade-story'>", 1)[1].split("</div>", 1)[0]
    assert story.startswith(STORY.replace("'", "&#x27;"))
    assert "href='/strategy-docs/fade_1h_momentum_15m'" in story


# ---------------------------------------------------------------------------
# One coin: the order
# ---------------------------------------------------------------------------


def test_the_chosen_side_and_each_child_order() -> None:
    btc = block(card(orders=[order(levels=[[0.54, 20.0], [0.53, 60.0]])]), "btc")
    assert "<span class='pill on'>BUY UP</span>" in btc
    assert "Orders · Buy Up" in btc
    assert ("Buy Up: a scaled passive limit order, 2 child orders at price levels at or under "
            "the best bid.") in btc
    # Resting: the depth still ahead, level by level, and the plan's fill and win chances.
    assert ("<tr><td>Buy Up at 53c</td><td>7.50</td>"
            "<td><span title='20.00 at 54c · 60.00 at 53c'>80</span></td>"
            "<td>60.0%</td><td>62.0%</td><td>resting</td></tr>") in btc
    # The plan's second child order is not in the book: it filled or was replaced since.
    assert ("<tr><td>Buy Up at 50c</td><td>5.00</td><td>150</td><td>40.0%</td><td>58.0%</td>"
            "<td>not resting</td></tr>") in btc
    assert "Expected growth each option adds to the account: buy Up +0.41% · buy Down " \
           "nothing." in btc
    assert "This pass: 1 kept in the queue, 1 placed, 0 cancelled." in btc
    assert ("The buys cost $6.55 if every child order fills: 6.6% of the $100.00 this coin "
            "sized from") in btc


def test_an_order_without_levels_keeps_its_whole_depth_at_its_price() -> None:
    btc = block(card(orders=[order(levels=None, depth=42.0)]), "btc")
    assert "<td><span title='42.00 at 53c'>42</span></td>" in btc
    cleared = block(card(orders=[order(levels=[])]), "btc")
    assert "<td>0</td>" in cleared  # the tape has traded through everything ahead


def test_joint_sizing_and_a_plan_that_is_not_placed() -> None:
    shrunk = block(card(decisions=[decision(sizing={"bankroll_usd": 100.0,
                                                    "joint_scale": 0.8})]), "btc")
    assert "sized down to 80% with the other coins buying in this pass" in shrunk

    held_back = block(card(decisions=[decision(action=rn.NOT_PLACED,
                                               reason="LIVE is selected.")], orders=[]),
                      "btc")
    assert held_back.count("<td>not placed</td>") == 2 and "NOT PLACED" in held_back
    assert "LIVE is selected." in held_back


def test_no_order_when_neither_side_adds_growth() -> None:
    reason = "Neither side adds expected growth at any price level in the band, so no order rests."
    btc = block(card(decisions=[decision(action=rn.NO_ORDER, side=None, child_orders=[],
                                         order=("none", None), reason=reason,
                                         factors={**FACTORS, "growth_buy_up": 0.0})],
                     orders=[]), "btc")
    assert "<span class='pill off'>NO ORDER</span>" in btc
    assert "Orders · No order" in btc
    assert btc.count("No order: neither side adds growth.") == 1
    assert reason not in btc  # the headline says it already
    assert "buy Up nothing · buy Down nothing" in btc
    assert "<table" not in btc and "The buys cost" not in btc

    # Below the venue's minimum is a different reason, and it is said.
    small = "The maths' order is below the venue's minimum of 5 shares a child order."
    btc = block(card(decisions=[decision(action=rn.NO_ORDER, child_orders=[], reason=small)],
                     orders=[]), "btc")
    assert small.replace("'", "&#x27;") in btc
    assert "Buy Up adds growth, but no child order is left to place after the sizing." in btc


def test_orders_held_back_by_the_one_side_rule_say_why() -> None:
    btc = block(card(decisions=[decision(side="Down", order=("buy", "Down"), child_orders=[
        {**CHILD_ORDERS[0], "side": "Down", "price": 0.45}])], orders=[],
        status=status(assets={"btc": {"action": rn.ORDERS, "decision_id": 10, "kept": 0,
                                      "placed": 0, "cancelled": 2,
                                      "held_back": [HELD_BACK]}})), "btc")
    assert "BUY DOWN" in btc and "<td>held back</td>" in btc
    assert "This pass: 0 kept in the queue, 0 placed, 2 cancelled." in btc
    assert f"Held back: {HELD_BACK}" in btc


def test_a_partial_fill_and_the_position_held() -> None:
    btc = block(card(orders=[order(filled=2.5)], positions=[
        position(),
        position("Down", bought=10.0, cost=4.0, slug=slug_of("btc", END - 1800)),
    ]), "btc")
    assert "<td>Buy Up at 53c</td><td>5.00</td>" in btc and "resting, 2.50 filled" in btc
    assert "Held in this window: 2.50 Up (bought 2.50 at avg 53c for $1.32)." in btc
    assert "Held in ended windows, awaiting the result: 10.00 shares ($4.00 net cost)." in btc


def test_a_resting_sell_reduces_the_position_and_never_buys_the_other_side() -> None:
    sale = {"level": 0, "kind": "hedge", "order_side": "SELL", "side": "Up", "price": 0.57,
            "shares": 4.0, "usd": 2.28, "depth_ahead": 25.0, "p_fill": 0.3, "q_fill": 0.35,
            "optimal_shares": 4.2}
    row = decision(order=("sell", "Up"), child_orders=[sale], p=0.3, p_model=0.25,
                   hedge={"held_side": "Up", "held_shares": 8.0, "sell": sale})
    row["stake_usd"], row["kelly_f"] = 0.0, 0.0
    btc = block(card(decisions=[row],
                     orders=[order(0.57, kind="hedge", order_side="SELL", shares=4.0,
                                   levels=[[0.56, 10.0], [0.57, 15.0]])],
                     positions=[position(bought=10.0, cost=5.4, sold=2.0, proceeds=1.14,
                                         offered=4.0)]), "btc")
    assert "<span class='pill on'>SELL UP</span>" in btc and "Orders · Sell Up" in btc
    assert ("Sell Up: reduce the 8.00 Up shares held with a resting sell at or over the best "
            "ask.") in btc
    assert ("<tr><td>Sell Up at 57c</td><td>4.00</td>"
            "<td><span title='10.00 at 56c · 15.00 at 57c'>25</span></td>"
            "<td>30.0%</td><td>35.0%</td><td>resting</td></tr>") in btc
    assert ("Held in this window: 8.00 Up (bought 10.00 at avg 54c for $5.40; sold 2.00 for "
            "$1.14; 4.00 offered for sale).") in btc
    assert ("Reducing it: a resting sell of 4.00 Up at 57c, sized by the maths and never more "
            "than the shares held; it fills 30.0% of the time, and Up still wins 35.0% if it "
            "does.") in btc
    assert "Buy Down" not in btc and "The buys cost" not in btc


def test_a_position_the_maths_keeps_says_why() -> None:
    note = "Keeping the shares pays more than selling them at any price level."
    btc = block(card(decisions=[decision(action=rn.NO_ORDER, side=None, child_orders=[],
                                         order=("none", None),
                                         hedge={"held_side": "Up", "held_shares": 10.0,
                                                "note": note})],
                     orders=[], positions=[position(bought=10.0, cost=5.3)]), "btc")
    assert ("No order: neither adding to the 10.00 Up shares held nor selling them adds "
            "growth.") in btc
    assert note in btc


def test_rows_written_before_the_rename_still_read() -> None:
    row = decision(action="bid", order=None)
    btc = block(card(decisions=[row],
                     status=status(assets={"btc": {"action": "bid", "decision_id": 10}})),
                "btc")
    assert "<span class='pill on'>BUY UP</span>" in btc and "Orders · Buy Up" in btc
    old = block(card(decisions=[decision(action="no_bid", side=None, child_orders=[],
                                         order=None)], orders=[],
                     status=status(assets={})), "btc")
    assert "NO ORDER" in old and "No order: neither side adds growth." in old


# ---------------------------------------------------------------------------
# One coin: what the runner did, and when there is no decision
# ---------------------------------------------------------------------------


def test_a_switched_off_coin_shows_what_the_runner_did_last() -> None:
    reason = "The strategy is switched off: no new orders. Fills and settlement keep running."
    btc = block(card(decisions=[decision(ts=NOW - 60)],
                     status=status(last_pass_ts=NOW, state="switched_off",
                                   assets={"btc": {"action": "switched_off",
                                                   "reason": reason}})), "btc")
    assert "SWITCHED OFF" in btc and "BUY UP" not in btc
    assert reason in btc and "decided 60s ago" in btc


def test_a_refused_plan_shows_the_decision_and_why() -> None:
    refusal = {"id": 11, "ts": NOW, "asset": "btc", "window_slug": SLUG, "action": "refused",
               "reason": "The orders were refused (would_cross): the buy is at the ask.",
               "inputs": {"status": "refused", "decision_id": 10}, "side": None,
               "child_orders": [], "hedge": None, "factors": None}
    btc = block(card(decisions=[refusal, decision()], orders=[],
                     status=status(assets={"btc": {"action": "refused", "decision_id": 10}})),
                "btc")
    assert "REFUSED" in btc and "BUY UP" not in btc
    assert "Refused: The orders were refused (would_cross): the buy is at the ask." in btc
    assert "<td>refused</td>" in btc
    assert "Jumpiness" in btc  # the refused decision's inputs are still shown


def test_a_decision_for_an_ended_window_is_not_shown_as_current() -> None:
    btc = block(card(now=END + 30, status=status(last_pass_ts=END + 20, assets={})), "btc")
    assert "no decision for the current window" in btc
    assert "Jumpiness" not in btc and "BUY UP" not in btc


def test_a_coin_without_inputs_says_why() -> None:
    problem = Problem(asset="eth", code="book_not_live", ts=NOW, window_slug=slug_of("eth"),
                      message="The 15m Up book is not live.",
                      also=(("price_stale", "The Chainlink price is 9 s old."),),
                      notes=("Could not save this window's row: disk full.",))
    eth = block(card(decisions=[decision("eth", action=rn.NO_INPUTS, side=None, p=None,
                                         p_model=None, child_orders=[], factors=False,
                                         reason=problem.message,
                                         inputs=problem.as_record())]), "eth")
    assert "NO INPUTS" in eth and "The 15m Up book is not live." in eth
    assert "Also: The Chainlink price is 9 s old." in eth
    assert "Note: Could not save this window&#x27;s row: disk full." in eth
    assert "book_not_live" not in eth  # plain words, not the internal codes

    # No window known at all (no market data): still the coin's latest word.
    no_hub = Problem(asset="sol", code="no_hub", ts=NOW, message="No market data hub.")
    row = decision("sol", action=rn.NO_INPUTS, side=None, p=None, p_model=None,
                   child_orders=[], factors=False, reason=no_hub.message,
                   inputs=no_hub.as_record())
    row["window_slug"] = None
    sol = block(card(decisions=[row]), "sol")
    assert "NO INPUTS" in sol and "No market data hub." in sol and "decided 0s ago" in sol


def test_notes_on_good_inputs_are_shown() -> None:
    record = inputs_record(notes=["The tick size was not given; 1c assumed."])
    btc = block(card(decisions=[decision(inputs=record)]), "btc")
    assert "Note: The tick size was not given; 1c assumed." in btc


def test_input_warnings_are_shown_as_warnings() -> None:
    # A failure that did not stop the coin (finding 11) is on its entry, in the warning tone.
    record = inputs_record(warnings=["Could not read this window's stored start reference."],
                           notes=["The tick size was not given; 1c assumed."])
    btc = block(card(decisions=[decision(inputs=record)]), "btc")
    assert ("<div class='fade-reason warn'>Warning: Could not read this window&#x27;s "
            "stored start reference.</div>") in btc
    assert "<div class='fade-reason'>Note: The tick size was not given; 1c assumed.</div>" in btc
    problem = Problem(asset="eth", code="book_not_live", ts=NOW, window_slug=slug_of("eth"),
                      message="The 15m Up book is not live.",
                      warnings=("Could not save this window's row: disk full.",))
    eth = block(card(decisions=[decision("eth", action=rn.NO_INPUTS, side=None, p=None,
                                         p_model=None, child_orders=[], factors=False,
                                         reason=problem.message,
                                         inputs=problem.as_record())]), "eth")
    assert "Warning: Could not save this window&#x27;s row: disk full." in eth


def test_a_window_the_model_cannot_price_still_shows_its_inputs() -> None:
    btc = block(card(decisions=[decision(action=rn.NO_MODEL, side=None, p=None, p_model=None,
                                         child_orders=[], factors={}, order=None,
                                         reason=rn.NO_MODEL_REASON)], orders=[]), "btc")
    assert "NO PRICE" in btc and rn.NO_MODEL_REASON in btc
    assert "No decision from the maths, so no orders." in btc
    assert "the model has not priced this window" in btc
    assert "fade-wf" not in btc  # no waterfall without the model's numbers


def test_a_read_failure_is_shown_not_hidden() -> None:
    html = panel.render(load_error="the ledger (OperationalError: no such table)", now=NOW)
    assert "Could not read this strategy's records: the ledger" in html


def test_every_string_is_escaped() -> None:
    evil = "<script>x()</script>"
    html = card(decisions=[decision(reason=evil, inputs=inputs_record(notes=[evil]))],
                status=status(last_error=evil, last_error_ts=NOW, errors=[evil],
                              executor={"state": ex.KILL_STATE, "message": evil},
                              assets={"btc": {"action": rn.ORDERS, "decision_id": 10,
                                              "held_back": [evil]}}))
    assert "<script>" not in html and "&lt;script&gt;" in html


def test_no_dialogs_no_coin_flip_language_and_standard_terms_only() -> None:
    sale = {**CHILD_ORDERS[0], "kind": "hedge", "order_side": "SELL", "price": 0.57}
    html = card(decisions=[decision(a, id=i) for i, a in enumerate(rn.ASSETS, 1)]
                + [decision(id=20, order=("sell", "Up"), child_orders=[sale],
                            hedge={"held_side": "Up", "held_shares": 8.0, "sell": sale})],
                positions=[position()])
    assert not re.search(r"\b(confirm|alert|prompt)\(", html)
    assert not any(word in html.lower() for word in BANNED)
    assert "Edge band" not in html and "AUTO-PAUSE" not in html.upper()
    sources = {
        "the card": html,
        "the panel": (ROOT / "polymarket_exec/ops/dashboard/panels/fade_1h.py").read_text(),
        "the stylesheet": (ROOT / "polymarket_exec/ops/dashboard/static/style.css").read_text(),
        "the loader": (ROOT / "polymarket_exec/ops/dashboard/execution_view.py").read_text(),
    }
    for where, text in sources.items():
        for word in COINED:
            assert word not in text.lower(), (where, word)
    assert "hedge" not in html.lower()  # a sale of shares held, in plain words


def test_the_card_knows_every_runner_action_state_and_coin() -> None:
    assert panel.ASSETS == rn.ASSETS and panel.STRATEGY == rn.STRATEGY
    assert (panel.ORDERS, panel.NO_ORDER) == (rn.ORDERS, rn.NO_ORDER)
    for action in (rn.NO_INPUTS, rn.COIN_OFF, rn.NO_MODEL, rn.MODEL_ERROR, rn.NO_ORDER,
                   rn.ORDERS, rn.NOT_PLACED, rn.REFUSED, "switched_off"):
        assert action in panel.ACTIONS
    for state in (rn.PASS_FAILED, rn.STOPPED, rn.STOPPED_ON_ERROR):
        assert state in panel.RUNNER_STATES and state in panel.LOOP_PILLS
    assert panel.DEFAULT_POLL_S == rn.DEFAULT_POLL_S
    assert set(panel.EXECUTOR_PILLS) == {ex.PAPER_STATE, ex.LIVE_STATE, ex.KILL_STATE,
                                         ex.UNKNOWN_MODE_STATE}


def test_picking_the_current_decision() -> None:
    older, newer = decision(id=3), decision(id=9, action=rn.NO_ORDER)
    refusal = {"id": 12, "asset": "btc", "action": "refused", "reason": "no",
               "inputs": {"status": "refused", "decision_id": 9}}
    picks = panel.current_decisions([older, refusal, newer, newer, decision("eth", id=4)])
    assert picks["btc"] == (newer, "no") and picks["eth"][1] is None
    assert panel.current_decisions([]) == {}


# ---------------------------------------------------------------------------
# The loader's status: this process first, an earlier run only for context
# ---------------------------------------------------------------------------


SAVED = {"state": "running", "last_pass_ts": NOW - 3600, "passes": 400, "errors": [],
         "last_error": None, "assets": {"btc": {"action": rn.ORDERS}}}


def test_before_the_first_pass_an_earlier_run_lends_its_last_pass() -> None:
    got = ev.fade_1h_status({"state": "not_started", "last_pass_ts": None,
                             "last_error": None}, SAVED)
    assert got["from_earlier_run"] is True and got["state"] == "not_started"
    assert got["last_pass_ts"] == NOW - 3600 and got["assets"] == SAVED["assets"]


def test_a_loop_that_died_before_its_first_pass_is_not_hidden() -> None:
    memory = {"state": rn.STOPPED_ON_ERROR, "last_pass_ts": None,
              "last_error": "The loop stopped: RuntimeError: no sockets",
              "last_error_ts": NOW, "errors": ["The loop stopped: RuntimeError: no sockets"]}
    got = ev.fade_1h_status(memory, SAVED)
    assert got["state"] == rn.STOPPED_ON_ERROR
    assert got["last_error"] == memory["last_error"] and got["errors"] == memory["errors"]
    assert got["last_pass_ts"] == NOW - 3600 and got["from_earlier_run"] is True


def test_this_processs_own_pass_wins_and_a_missing_copy_changes_nothing() -> None:
    memory = {"state": "running", "last_pass_ts": NOW, "last_error": None}
    assert ev.fade_1h_status(memory, SAVED) is memory
    fresh = {"state": "not_started", "last_pass_ts": None, "last_error": None}
    assert ev.fade_1h_status(fresh, None) is fresh
    assert ev.fade_1h_status(fresh, {"state": "running"}) is fresh


# ---------------------------------------------------------------------------
# Against a real ledger
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def fade_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "fade.db")
    monkeypatch.setattr(_config, "KILL_SWITCH_PATH", tmp_path / "KILL")
    await _db.init_db()
    await _db.set_config(ex.MODE_KEY, "paper")
    monkeypatch.setattr(rn, "_STATUS", {"state": "not_started"})
    return _db


@pytest.mark.asyncio
async def test_the_card_reads_a_real_pass(fade_db, monkeypatch: pytest.MonkeyPatch) -> None:
    from polymarket_bot.fade_1h_momentum_15m import inputs as _inputs

    await save_window("btc")
    others = {a: Problem(asset=a, code="no_hub", ts=NOW, message="No market data hub.")
              for a in ("eth", "sol", "xrp")}

    async def _gather(hub, client, now, *, memory=None):  # noqa: ANN001
        return {"btc": make_inputs("btc"), **others}

    monkeypatch.setattr(_inputs, "gather", _gather)
    runner = rn.Runner(FakeVenue(), hub_fn=FakeHub, clock=lambda: NOW)
    report = await runner.pass_once()  # the real model, with the starting dials
    assert report.errors == []

    data = await ev.fade_1h_data()
    assert data["load_error"] is None and data["status"]["last_pass_ts"] == NOW
    assert data["poll_s"] == rn.DEFAULT_POLL_S
    html = panel.render(**data, enabled=True, mode="paper", now=NOW + 5)

    btc = block(html, "btc")
    (row,) = [r for r in await ledger.recent_decisions(10) if r["asset"] == "btc"]
    assert row["action"] == rn.ORDERS and row["child_orders"]
    side = row["side"]
    assert f"<span class='pill on'>BUY {side.upper()}</span>" in btc
    assert f"Orders · Buy {side}" in btc and "fade-wf-total" in btc
    for child in row["child_orders"]:
        cents = f"{100 * child['price']:.0f}c"
        assert f"<td>Buy {side} at {cents}</td>" in btc
    assert "<td>resting</td>" in btc
    assert row["factors"]["explanation"].replace("'", "&#x27;") in btc
    assert f"Up {100 * row['p']:.1f}%" in btc
    assert "NO INPUTS" in block(html, "eth") and "No market data hub." in block(html, "eth")
    assert "Last pass 5s ago" in html and "No errors." in html
    for word in COINED:
        assert word not in html.lower()


@pytest.mark.asyncio
async def test_a_loop_that_died_reaches_the_card_through_the_loader(
        fade_db, monkeypatch: pytest.MonkeyPatch) -> None:
    await _db.set_config(rn.STATUS_KEY, json.dumps(SAVED))
    monkeypatch.setattr(rn, "_STATUS", {
        "state": rn.STOPPED_ON_ERROR, "last_pass_ts": None,
        "last_error": "The loop stopped: RuntimeError: no sockets", "last_error_ts": NOW,
        "errors": ["The loop stopped: RuntimeError: no sockets"]})
    data = await ev.fade_1h_data()
    assert data["status"]["state"] == rn.STOPPED_ON_ERROR
    html = panel.render(**data, enabled=True, mode="paper", now=NOW + 5)
    assert "LOOP DIED" in head(html)
    assert "The loop stopped: RuntimeError: no sockets" in html
    assert "(from an earlier run of the app)" in html and "No errors." not in html


@pytest.mark.asyncio
async def test_the_card_sits_under_my_strategies_and_survives_a_bad_read(
        fade_db, monkeypatch: pytest.MonkeyPatch) -> None:
    page = await ev.execution_view_html()
    where = [page.index(x) for x in ("MY STRATEGIES", panel.TITLE, "LIVE MARKET")]
    assert where == sorted(where)

    async def _broken() -> dict:
        raise RuntimeError("no such table: fade_orders")

    monkeypatch.setattr(ledger, "summary", _broken)
    page = await ev.execution_view_html()
    assert "Could not read this strategy's records: the ledger (RuntimeError: no such " \
           "table: fade_orders)" in page
    assert "MY STRATEGIES" in page and "LIVE MARKET" in page
