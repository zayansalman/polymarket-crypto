"""FADE 1H MOMENTUM ON 15M card (panels/fade_1h.py) and its loader in execution_view.py.

Pure ``render(...)`` tests with dict fixtures shaped like the ledger's rows (the inputs are a
real ``Inputs.as_record()``), then two tests against a real ledger: one real runner pass read
back through the loader, and the whole page, to check where the card sits.
"""

from __future__ import annotations

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

SLUG = slug_of("btc")
STORY = ("BTC's 15m leg is up 0.10% with 10 min left; the last candles ran up so the snap-back "
         "takes 4 points off Up; the maths rests Up bids at 54c and 50c.")
FACTORS = {"leg_pts": 8.0, "snapback_pts": -3.5, "momentum_pts": 2.5, "anchor_pts": 3.0,
           "stretch_M": 0.004, "p_model": 0.62, "p": 0.60, "market_up": 0.54}
LADDER = [
    {"rung": 0, "kind": "entry", "side": "Up", "price": 0.54, "shares": 7.5,
     "stake_usd": 4.05, "depth_ahead": 100.0, "p_fill": 0.6, "q_fill": 0.62},
    {"rung": 1, "kind": "entry", "side": "Up", "price": 0.50, "shares": 5.0,
     "stake_usd": 2.5, "depth_ahead": 40.0, "p_fill": 0.4, "q_fill": 0.58},
]
SUMMARY = {"net_pnl_usd": 12.34, "cents_per_share": 2.5, "return_on_staked": 0.081,
           "max_drawdown_usd": 3.2, "settled_windows": 17, "settled_shares": 493.6,
           "staked_usd": 152.3, "open_exposure_usd": 4.05, "open_windows": 1,
           "resting_usd": 2.5, "resting_orders": 1, "windows_observed": 96}
BANNED = ("coin flip", "coin-flip", "coinflip", "50/50", "fifty-fifty", "toss-up", "gamble",
          "lottery")


def decision(asset: str = "btc", *, id: int = 10, action: str = "bid", side: str | None = "Up",
             p: float | None = 0.60, p_model: float | None = 0.62, ladder=None, hedge=None,
             factors=None, reason=None, inputs=None, ts: float = NOW) -> dict:  # noqa: A002
    return {
        "id": id, "ts": ts, "asset": asset, "window_slug": slug_of(asset), "mode": "paper",
        "dials_version": 1,
        "inputs": inputs if inputs is not None else make_inputs(asset).as_record(),
        "p_model": p_model, "p": p, "side": side, "kelly_f": 0.042, "stake_usd": 6.55,
        "ladder": LADDER if ladder is None else ladder, "hedge": hedge,
        "factors": None if factors is False else {
            "model": FACTORS if factors is None else factors,
            "sizing": {"bankroll_usd": 100.0}, "explanation": STORY},
        "action": action, "reason": reason,
    }


def order(price: float = 0.54, *, filled: float = 0.0, kind: str = "entry", side: str = "Up",
          shares: float = 7.5, asset: str = "btc", slug: str = SLUG) -> dict:
    return {"id": int(price * 100), "asset": asset, "window_slug": slug, "kind": kind,
            "side": side, "price": price, "shares": shares, "filled_shares": filled,
            "depth_ahead": 100.0, "state": "partial" if filled else "resting",
            "window_end": END}


def status(**over) -> dict:
    base = {"state": "running", "last_pass_ts": NOW - 12, "passes": 31, "errors": [],
            "last_error": None, "last_error_ts": None,
            "executor": {"state": "paper", "requested_mode": "paper", "can_place": True,
                         "message": "Paper trading."},
            "bankroll": {"start_usd": 100.0, "free_usd": 91.5},
            "assets": {"btc": {"action": "bid", "decision_id": 10, "kept": 1, "placed": 1,
                               "cancelled": 0}}}
    base.update(over)
    return base


def card(**over) -> str:
    kwargs = dict(summary=SUMMARY, decisions=[decision()], orders=[order()], positions=[],
                  status=status(), dials={"version": 1, "source": "fit_sep17_20",
                                          "n_windows": 0},
                  enabled=True, mode="paper", now=NOW)
    kwargs.update(over)
    return panel.render(**kwargs)


def block(html: str, asset: str) -> str:
    """One coin's block of the card."""
    start = html.index(f"<b>{asset.upper()}</b>")
    end = html.find("<div class='fade-coin'>", start)
    return html[start:end if end != -1 else len(html)]


# ---------------------------------------------------------------------------
# The card
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
                                        "class='fade-coin'")]
    assert order_of == sorted(order_of)
    for shown in ("+$12.34", "+8.1%", "+2.50c", "-$3.20", ">17<", "$4.05"):
        assert shown in html
    assert " won" not in html and "win rate" not in html.lower()


def test_an_empty_card_still_renders() -> None:
    html = panel.render(now=NOW)
    assert panel.TITLE in html
    assert "No pass has run yet." in html
    assert html.count("no decision recorded yet") == 4
    assert "nothing settled yet" in html


def test_one_block_per_coin_in_the_runner_order() -> None:
    html = card(decisions=[decision(a, id=i) for i, a in enumerate(rn.ASSETS, 1)])
    assert html.count("<div class='fade-coin'>") == 4
    where = [html.index(f"<b>{a.upper()}</b>") for a in rn.ASSETS]
    assert where == sorted(where)


def test_the_inputs_are_in_plain_words() -> None:
    btc = block(card(), "btc")
    # make_inputs: TWAP-60s 100.2 against a price to beat of 100.1; 60 moves of 0.05%;
    # Binance 100.3 against an hour open of 100; the 1h book 58/60.
    for label, shown in (("15m leg", "+0.10%"), ("Jumpiness", "0.39% an hour"),
                         ("Stretch", "+0.40%"), ("1h leg", "+0.30%"), ("1h market", "Up 59c"),
                         ("Trend", "+0.00%")):
        assert f"<span>{label}</span><b>{shown}" in btc, label
    assert "price to beat $100.1000" in btc
    assert "pulls back toward Down" in btc  # the candles ran up
    assert "10 min left" in btc and "10m 00s left" in btc


def test_the_chances_and_the_side() -> None:
    btc = block(card(), "btc")
    assert "<span>Model</span><b>Up 62.0%" in btc
    assert "<span>Market</span><b>Up 54c" in btc  # the Up book's mid, 53/55
    assert "<span>Traded on</span><b>Up 60.0%" in btc
    assert "Bids · Up" in btc and "BIDDING" in btc
    assert STORY.replace("'", "&#x27;") in btc


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


def test_each_rung_shows_its_price_shares_and_fill_chance() -> None:
    btc = block(card(), "btc")
    assert ("<tr><td>Up 54c</td><td>7.50</td><td>60.0%</td><td>62.0%</td><td>100</td>"
            "<td>resting</td></tr>") in btc
    # The plan's second rung is not in the book: it filled or was replaced since.
    assert "<tr><td>Up 50c</td><td>5.00</td><td>40.0%</td>" in btc
    assert "no longer resting" in btc
    assert "This pass: 1 kept in the queue, 1 placed, 0 cancelled." in btc
    assert "Ladder $6.55, 4.2% of the $100.00 free bankroll" in btc

    held_back = block(card(decisions=[decision(action="not_placed", reason="LIVE is selected.")],
                           orders=[]), "btc")
    assert held_back.count("not placed") == 2 and "NOT PLACED" in held_back


def test_a_partial_fill_and_the_position_held() -> None:
    btc = block(card(orders=[order(filled=2.5)], positions=[
        {"window_slug": SLUG, "asset": "btc", "side": "Up", "shares": 2.5, "cost_usd": 1.35,
         "hedge_shares": 0.0, "avg_price": 0.54},
        {"window_slug": slug_of("btc", NOW - 3600), "asset": "btc", "side": "Down",
         "shares": 10.0, "cost_usd": 4.0, "hedge_shares": 0.0, "avg_price": 0.4},
    ]), "btc")
    assert "<td>Up 54c</td><td>5.00</td>" in btc and "partial, 2.50 filled" in btc
    assert "Filled in this window: 2.50 Up at avg 54c ($1.35)." in btc
    assert "Filled in earlier windows, awaiting the result: 10.00 shares ($4.00)." in btc


def test_a_hedge_is_a_resting_bid_on_the_other_side() -> None:
    hedge = {"held": "Up", "held_shares": 10.0, "paired_shares": 2.0, "side": "Down",
             "price": 0.41, "p_held_given_fill": 0.55, "optimal_shares": 8.2, "shares": 8.2,
             "bid": {"rung": 0, "kind": "hedge", "side": "Down", "price": 0.41, "shares": 8.2,
                     "depth_ahead": 30.0}}
    btc = block(card(decisions=[decision(hedge=hedge)],
                     orders=[order(), order(0.41, kind="hedge", side="Down", shares=8.2)]),
                "btc")
    assert "Hedge: holding 10.00 Up unpaired (2.00 paired)" in btc
    assert "rest 8.20 Down at 41c; Up still wins 55.0% if it fills" in btc
    assert "<tr><td>hedge Down 41c</td><td>8.20</td>" in btc

    none = {"held": "Up", "held_shares": 3.0, "paired_shares": 0.0,
            "note": "The model gave no hedge quote for the Up position."}
    btc = block(card(decisions=[decision(hedge=none)]), "btc")
    assert "The model gave no hedge quote for the Up position." in btc


def test_paper_is_labelled_and_live_says_not_built_and_not_authorised() -> None:
    assert "<span class='pill paper'>PAPER</span>" in card()

    live = card(status=status(executor={
        "state": ex.LIVE_STATE, "requested_mode": "live", "can_place": False,
        "message": "LIVE is selected, but this strategy has no live order path."}))
    assert "<span class='pill live'>LIVE · NOT BUILT / NOT AUTHORISED</span>" in live
    assert "LIVE is selected, but this strategy has no live order path." in live

    # Before the runner has reported, the global selection still tells the truth.
    early = panel.render(mode="live", now=NOW)
    assert "NOT BUILT / NOT AUTHORISED" in early and "not authorised for any market" in early

    killed = card(status=status(executor={"state": ex.KILL_STATE, "can_place": False,
                                          "message": "The kill switch file is present."}))
    assert "KILL SWITCH" in killed and "The kill switch file is present." in killed


def test_the_switch_state_is_shown() -> None:
    assert "<span class='pill on'>SWITCH ON</span>" in card(enabled=True)
    off = card(enabled=False)
    assert "<span class='pill off'>SWITCH OFF</span>" in off
    assert "Switched off on MY STRATEGIES: no new bids." in off


def test_a_switched_off_coin_shows_what_the_runner_did_last() -> None:
    reason = "The strategy is switched off: no new bids. Fills and settlement keep running."
    btc = block(card(decisions=[decision(ts=NOW - 60)],
                     status=status(last_pass_ts=NOW, state="switched_off",
                                   assets={"btc": {"action": "switched_off",
                                                   "reason": reason}})), "btc")
    assert "SWITCHED OFF" in btc and "BIDDING" not in btc
    assert reason in btc and "decided 60s ago" in btc


def test_last_pass_and_last_error() -> None:
    html = card(status=status(last_error="Checking fills failed: <b>boom</b>",
                              last_error_ts=NOW - 180))
    assert "Last pass 12s ago" in html and "pass 31" in html
    assert "Last error 3m ago: Checking fills failed: &lt;b&gt;boom&lt;/b&gt;" in html
    assert "the latest pass ran clean" in html
    assert "last pass 12s ago" in html.split("</summary>")[0]  # in the header too

    now_failing = card(status=status(last_error="Settling failed: x", last_error_ts=NOW - 12,
                                     errors=["Checking fills failed: y", "Settling failed: x"]))
    assert "(+1 more in the latest pass)" in now_failing
    assert "ran clean" not in now_failing
    assert "No errors." in card()


def test_a_status_from_an_earlier_run_says_so() -> None:
    assert "(from an earlier run of the app)" in card(
        status=status(from_earlier_run=True))


def test_a_refused_plan_shows_the_decision_and_why() -> None:
    refusal = {"id": 11, "ts": NOW, "asset": "btc", "window_slug": SLUG, "action": "refused",
               "reason": "The bids were refused (would_cross): the bid is at the ask.",
               "inputs": {"status": "refused", "decision_id": 10}, "side": None,
               "ladder": [], "hedge": None, "factors": None}
    btc = block(card(decisions=[refusal, decision()],
                     status=status(assets={"btc": {"action": "refused", "decision_id": 10}})),
                "btc")
    assert "REFUSED" in btc and "BIDDING" not in btc
    assert "Refused: The bids were refused (would_cross): the bid is at the ask." in btc
    assert "Jumpiness" in btc  # the refused decision's inputs are still shown


def test_a_decision_for_an_ended_window_is_not_shown_as_current() -> None:
    btc = block(card(now=END + 30, status=status(last_pass_ts=END + 20, assets={})), "btc")
    assert "no decision for the current window" in btc
    assert "Jumpiness" not in btc and "BIDDING" not in btc


def test_a_coin_without_inputs_says_why() -> None:
    problem = Problem(asset="eth", code="book_not_live", ts=NOW, window_slug=slug_of("eth"),
                      message="The 15m Up book is not live.",
                      also=(("price_stale", "The TWAP-60s print is 9 s old."),))
    eth = block(card(decisions=[decision("eth", action="no_inputs", side=None, p=None,
                                         p_model=None, ladder=[], factors=False,
                                         reason=problem.message,
                                         inputs=problem.as_record())]), "eth")
    assert "NO INPUTS" in eth and "The 15m Up book is not live." in eth
    assert "Also: The TWAP-60s print is 9 s old." in eth
    assert "book_not_live" not in eth  # plain words, not the internal codes

    # No window known at all (no market data): still the coin's latest word.
    no_hub = Problem(asset="sol", code="no_hub", ts=NOW, message="No market data hub.")
    row = decision("sol", action="no_inputs", side=None, p=None, p_model=None, ladder=[],
                   factors=False, reason=no_hub.message, inputs=no_hub.as_record())
    row["window_slug"] = None
    sol = block(card(decisions=[row]), "sol")
    assert "NO INPUTS" in sol and "No market data hub." in sol and "decided 0s ago" in sol


def test_a_window_the_model_cannot_price_still_shows_its_inputs() -> None:
    btc = block(card(decisions=[decision(action="no_model", side=None, p=None, p_model=None,
                                         ladder=[], factors={},
                                         reason=rn.NO_MODEL_REASON)], orders=[]), "btc")
    assert "NO PRICE" in btc and rn.NO_MODEL_REASON in btc
    assert "No decision, so no bids." in btc
    assert "the model has not priced this window" in btc
    assert "fade-wf" not in btc  # no waterfall without the model's numbers


def test_a_read_failure_is_shown_not_hidden() -> None:
    html = panel.render(load_error="the ledger (OperationalError: no such table)", now=NOW)
    assert "Could not read this strategy's records: the ledger" in html


def test_every_string_is_escaped() -> None:
    evil = "<script>x()</script>"
    html = card(decisions=[decision(reason=evil)],
                status=status(last_error=evil, last_error_ts=NOW,
                              executor={"state": ex.KILL_STATE, "message": evil}))
    assert "<script>" not in html and "&lt;script&gt;" in html


def test_no_dialogs_and_no_coin_flip_language() -> None:
    html = card(decisions=[decision(a, id=i) for i, a in enumerate(rn.ASSETS, 1)])
    assert not re.search(r"\b(confirm|alert|prompt)\(", html)
    assert not any(word in html.lower() for word in BANNED)
    assert "Edge band" not in html and "AUTO-PAUSE" not in html.upper()


def test_the_card_knows_every_runner_action_state_and_coin() -> None:
    assert panel.ASSETS == rn.ASSETS and panel.STRATEGY == rn.STRATEGY
    for action in (rn.NO_INPUTS, rn.COIN_OFF, rn.NO_MODEL, rn.MODEL_ERROR, rn.NO_BID, rn.BID,
                   rn.NOT_PLACED, rn.REFUSED, "switched_off"):
        assert action in panel.ACTIONS
    assert set(panel.EXECUTOR_PILLS) == {ex.PAPER_STATE, ex.LIVE_STATE, ex.KILL_STATE,
                                         ex.UNKNOWN_MODE_STATE}


def test_picking_the_current_decision() -> None:
    older, newer = decision(id=3), decision(id=9, action="no_bid")
    refusal = {"id": 12, "asset": "btc", "action": "refused", "reason": "no",
               "inputs": {"status": "refused", "decision_id": 9}}
    picks = panel.current_decisions([older, refusal, newer, newer, decision("eth", id=4)])
    assert picks["btc"] == (newer, "no") and picks["eth"][1] is None
    assert panel.current_decisions([]) == {}


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
    html = panel.render(**data, enabled=True, mode="paper", now=NOW + 5)

    btc = block(html, "btc")
    (row,) = [r for r in await ledger.recent_decisions(10) if r["asset"] == "btc"]
    assert "BIDDING" in btc and "resting" in btc and "fade-wf-total" in btc
    assert row["factors"]["explanation"].replace("'", "&#x27;") in btc
    assert f"Up {100 * row['p']:.1f}%" in btc
    assert "NO INPUTS" in block(html, "eth") and "No market data hub." in block(html, "eth")
    assert "Last pass 5s ago" in html and "No errors." in html


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
