"""Fade 1h Momentum on 15m: the model hook (decide.py), from fixed inputs to a Decision.

The inputs are built directly (no hub, no network). The dials are the starting dials (version
1) unless a test says otherwise.
"""

from __future__ import annotations

import json
import math
import time

import pytest

from polymarket_bot.fade_1h_momentum_15m import decide as D
from polymarket_bot.fade_1h_momentum_15m import ledger, learner, sizing
from polymarket_bot.fade_1h_momentum_15m import runner as rn
from polymarket_bot.fade_1h_momentum_15m.inputs import Book, Inputs, PriceNow, WindowAverage

HOUR = 1_789_934_400  # a UTC hour boundary
START = HOUR + 900  # the hour's second quarter
END = START + 900
DIALS = dict(learner.STARTING_DIALS)
BANNED = ("coin flip", "coin-flip", "coinflip", "50/50", "fifty-fifty", "toss-up", "gamble",
          "lottery")


def book(side: str, token: str, bid: float, ask: float) -> Book:
    return Book(side=side, token_id=token, best_bid=bid, best_ask=ask, bid_size=100.0,
                ask_size=100.0, bids=((bid, 100.0), (round(bid - 0.02, 6), 60.0)),
                asks=((ask, 100.0),), source="stream", age_s=0.3)


def make(asset: str = "btc", *, now: float = START + 120, up_bid: float = 0.49,
         up_ask: float = 0.51, spot: float = 100.0, start_ref: float = 100.0,
         r15: tuple[float, ...] = (0.0,) * 12, vol: float = 0.0008, trend: float = 0.0,
         hour_up: float = 0.5, close_log: float | None = None, tick: float = 0.01,
         down: tuple[float, float] | None = None) -> Inputs:
    down_bid, down_ask = down or (round(1 - up_ask, 6), round(1 - up_bid, 6))

    def price(source: str, value: float) -> PriceNow:
        return PriceNow(source=source, value=value, obs_s=now - 1, age_s=1.0)

    close = None
    if close_log is not None:
        close = WindowAverage(value=math.exp(close_log), log_value=close_log,
                              through_s=int(now) - 1, seconds=int(now - (END - 60)),
                              printed=20, longest_gap_s=2)
    returns = tuple(trend / 60 + vol * (-1) ** i for i in range(60))
    return Inputs(
        asset=asset, ts=float(now), window_slug=f"{asset}-updown-15m-{START}",
        window_start=float(START), window_end=float(END), condition_id=f"0x{asset}",
        up_token=f"UP-{asset}", down_token=f"DN-{asset}", tick_size=tick,
        up_book=book("Up", f"UP-{asset}", up_bid, up_ask),
        down_book=book("Down", f"DN-{asset}", down_bid, down_ask),
        hour_start=float(HOUR), hour_slug=f"{asset}-1h", hour_condition_id=None,
        hour_up_bid=hour_up - 0.01, hour_up_ask=hour_up + 0.01, hour_book_age_s=2.0,
        hour_open=100.0, twap60=price("chainlink_twap60", spot),
        chainlink=price("chainlink", spot), binance=price("binance", spot),
        start_ref=start_ref, start_ref_source="Gamma priceToBeat",
        window_avg=WindowAverage(value=start_ref, log_value=math.log(start_ref),
                                 through_s=int(now) - 1, seconds=int(now - START),
                                 printed=100, longest_gap_s=2),
        minute_returns=returns, minute_returns_end=float(now - now % 60), r15=tuple(r15),
        close_avg=close,
    )


def settings(**over) -> rn.Settings:  # noqa: ANN003
    base = dict(poll_s=60.0, bankroll_usd=100.0, kelly_multiplier=0.5, max_order_usd=100.0,
                band_lo=0.01, band_hi=0.15, hedge=True, spot_feed="chainlink_twap60",
                coins={a: True for a in rn.ASSETS})
    base.update(over)
    return rn.Settings(**base)


def plan(inp: Inputs, dec: D.Decision, held: dict | None = None, **over):  # noqa: ANN003
    return rn.plan_orders({inp.asset: (inp, dec)}, {inp.asset: held} if held else {},
                          bankroll_usd=100.0, rho=0.75, settings=settings(**over))[inp.asset]


def check_ladder(dec: D.Decision, inp: Inputs) -> None:
    b = inp.up_book if dec.side == "Up" else inp.down_book
    grid = D.ladder_prices(b.best_ask, inp.tick_size, 0.01, 0.15)
    prices = [r.price for r in dec.rungs]
    assert prices == list(grid[:len(prices)])  # nearest first, on the grid
    assert all(0.0 < p < b.best_ask for p in prices)
    fills = [r.p_fill for r in dec.rungs]
    assert fills == sorted(fills, reverse=True)
    sizing.ladder([(r.price, r.p_fill, r.q_fill) for r in dec.rungs], 100.0, 1.0)  # consistent


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------


def test_the_prior_follows_the_market_and_nothing_pays() -> None:
    inp = make(spot=100.3, up_bid=0.55, up_ask=0.57)
    dec = D.decide(inp, dict(ledger.PRIOR_DIALS))
    assert dec.p == pytest.approx(inp.market_up, abs=1e-12)  # w_M = 1, w_S = 0: p is the mid
    check_ladder(dec, inp)
    p = plan(inp, dec)
    assert p.entries == [] and p.full_kelly_usd == pytest.approx(0.0, abs=1e-9)


def test_a_leg_well_up_with_a_cheap_up_book_rests_up_bids_under_the_ask() -> None:
    inp = make(spot=100.25, up_bid=0.40, up_ask=0.42)
    dec = D.decide(inp, DIALS)
    assert dec.side == "Up" and dec.p_model > dec.p > inp.market_up
    check_ladder(dec, inp)
    assert dec.rungs and all(r.q_fill > 0.0 for r in dec.rungs)
    p = plan(inp, dec)
    assert p.entries, p.notes
    for bid in p.entries:
        assert bid.side == "Up" and bid.token_id == inp.up_token
        assert bid.price < inp.up_book.best_ask and bid.shares >= 5.0
    text = dec.explanation
    assert text.startswith("BTC's 15m leg is up 0.25% with 13 min left")
    assert "Up bid" in text and "rests" in text
    assert not any(word in text.lower() for word in BANNED)


def test_the_mirror_case_bids_down() -> None:
    inp = make(spot=99.75, up_bid=0.58, up_ask=0.60)  # Down book 0.40/0.42
    dec = D.decide(inp, DIALS)
    assert dec.side == "Down" and dec.p < 0.5
    check_ladder(dec, inp)
    p = plan(inp, dec)
    assert p.entries and all(b.side == "Down" and b.token_id == inp.down_token
                             for b in p.entries)
    assert all(b.price < inp.down_book.best_ask for b in p.entries)


@pytest.mark.parametrize("books", [
    dict(up_bid=0.30, up_ask=0.45),  # a wide spread: rungs above the mid still rest
    dict(up_bid=0.97, up_ask=0.98, spot=100.4),
    dict(up_bid=0.02, up_ask=0.03, spot=99.6),
    dict(up_bid=0.985, up_ask=0.986, tick=0.001, spot=100.5),
])
def test_no_rung_is_ever_at_or_above_the_ask(books) -> None:  # noqa: ANN001
    inp = make(**books)
    dec = D.decide(inp, DIALS)
    check_ladder(dec, inp)
    for h in dec.hedges:
        other = inp.down_book if h.side == "Down" else inp.up_book
        assert h.price == other.best_bid < other.best_ask
    for bid in plan(inp, dec, held={"Up": 20.0}).bids:
        b = inp.up_book if bid.side == "Up" else inp.down_book
        assert bid.price < b.best_ask


def test_zero_stakes_when_the_maths_only_follows_the_market() -> None:
    inp = make(spot=100.2, up_bid=0.30, up_ask=0.32)
    market_only = {**DIALS, "w_M": 1.0, "w_S": 0.0}
    dec = D.decide(inp, market_only)
    assert dec.p == pytest.approx(inp.market_up, abs=1e-12)
    assert plan(inp, dec).entries == []
    assert "nothing rests" in dec.explanation


def test_a_hedge_appears_only_when_the_odds_turn() -> None:
    held = {"Up": 20.0}
    # The leg is well up: holding Up is fine, the hedge bid is sized at nothing.
    good = make(spot=100.3, up_bid=0.70, up_ask=0.72)
    dec = D.decide(good, DIALS)
    quote = dec.hedge_for("Up")
    assert quote is not None and quote.side == "Down" and quote.price == good.down_book.best_bid
    assert quote.p_held_given_fill > 0.5
    assert not [b for b in plan(good, dec, held=held).bids if b.kind == "hedge"]
    # The leg has turned down: Up is now losing, and a resting Down bid hedges it.
    bad = make(spot=99.7, up_bid=0.30, up_ask=0.32)
    dec = D.decide(bad, DIALS)
    quote = dec.hedge_for("Up")
    assert quote is not None and quote.p_held_given_fill < 0.5
    (hedge,) = [b for b in plan(bad, dec, held=held).bids if b.kind == "hedge"]
    assert hedge.side == "Down" and hedge.price == bad.down_book.best_bid
    assert hedge.price < bad.down_book.best_ask and hedge.shares > 0
    # Hedging switched off: no hedge bid whatever the odds.
    assert not [b for b in plan(bad, dec, held=held, hedge=False).bids if b.kind == "hedge"]


def test_the_simulation_is_seeded_per_decision() -> None:
    inp = make(spot=100.1, up_bid=0.45, up_ask=0.47)
    first, again = D.decide(inp, DIALS), D.decide(inp, DIALS)
    assert first.rungs == again.rungs and first.hedges == again.hedges
    later = D.decide(make(now=START + 121, spot=100.1, up_bid=0.45, up_ask=0.47), DIALS)
    assert later.rungs != first.rungs


def test_a_recorded_row_prices_the_same_as_the_live_inputs() -> None:
    inp = make(now=END - 30, spot=100.05, close_log=math.log(100.08), r15=(0.002,) * 12,
               hour_up=0.62, trend=0.001)
    record = json.loads(json.dumps(inp.as_record()))  # as stored
    for feed in D.SPOT_FEEDS:
        record["settings"] = {"spot_feed": feed}
        live, back = D.state_from_inputs(inp, feed), D.state_from_record(record)
        assert back == live
        assert D.evaluate(back, D.resolve_dials(DIALS)).p == \
            D.evaluate(live, D.resolve_dials(DIALS)).p


def test_the_closing_average_counts_in_the_last_minute() -> None:
    above = make(now=END - 20, spot=100.0, close_log=math.log(100.06))
    below = make(now=END - 20, spot=100.0, close_log=math.log(99.94))
    dials = D.resolve_dials(DIALS)
    p_above = D.evaluate(D.state_from_inputs(above), dials).p_model
    p_below = D.evaluate(D.state_from_inputs(below), dials).p_model
    assert p_above > 0.9 > 0.1 > p_below


def test_the_waterfall_adds_up_and_the_card_gets_numbers() -> None:
    inp = make(spot=100.1, r15=(0.004,) * 12, hour_up=0.65, trend=0.002, up_bid=0.5,
               up_ask=0.52)
    dec = D.decide(inp, DIALS)
    f = dec.factors
    total = f["leg_pts"] + f["snapback_pts"] + f["momentum_pts"] + f["anchor_pts"]
    assert total == pytest.approx(100 * (dec.p - 0.5), abs=1e-9)
    assert f["snapback_pts"] < 0  # the last candles ran up: the snap-back takes off Up
    assert f["stretch_M"] > 0 and f["mu_H"] > 0
    assert all(math.isfinite(v) for v in f.values())
    assert "snap-back takes" in dec.explanation


def test_flat_prices_cannot_be_priced() -> None:
    with pytest.raises(D.NoDecision, match="volatility is zero"):
        D.decide(make(vol=0.0), DIALS)
    with pytest.raises(ValueError):
        D.decide(make(), {"w_M": 1.0})  # dials missing


def test_rung_quotes_are_made_consistent() -> None:
    # Raw fill-conditional sums that break "exactly k filled, then won" in both directions.
    table = D.FillTable(n_paths=100, up={0.5: (100, 60.0), 0.49: (40, 39.0), 0.48: (39, 5.0)},
                        down={})
    rungs = D.rung_quotes("Up", [0.5, 0.49, 0.48, 0.47], table)
    assert [r.price for r in rungs] == [0.5, 0.49, 0.48]  # 0.47 never filled
    sizing.ladder([(r.price, r.p_fill, r.q_fill) for r in rungs], 100.0, 1.0)
    assert rungs[0].p_fill == pytest.approx(1.0) and rungs[0].p_fill < 1.0


def test_four_coins_decide_quickly() -> None:
    cases = [make(a, spot=100.1, up_bid=0.44, up_ask=0.46) for a in rn.ASSETS]
    started = time.perf_counter()
    for inp in cases:
        D.decide(inp, DIALS)
    assert time.perf_counter() - started < 4.0  # about 0.2 s on the Mac
