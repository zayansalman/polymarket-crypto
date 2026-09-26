"""Fade 1h Momentum on 15m: the model hook (decide.py), from fixed inputs to a Decision.

The inputs are built directly (no hub, no network). The dials are the starting dials (version
1) unless a test says otherwise. Every test here reads decide.py's own output: the chosen
parent order, its child orders and every candidate's expected log growth.
"""

from __future__ import annotations

import json
import math
import time

import pytest

from ems.fade_1h_momentum_15m import decide as D
from ems.fade_1h_momentum_15m import ledger, learner, sizing
from ems.fade_1h_momentum_15m.inputs import Book, Inputs, PriceNow, WindowAverage

HOUR = 1_789_934_400  # a UTC hour boundary
START = HOUR + 900  # the hour's second quarter
END = START + 900
DIALS = dict(learner.STARTING_DIALS)
MARKET_ONLY = {**DIALS, "w_M": 1.0, "w_S": 0.0}
ASSETS = ("btc", "eth", "sol", "xrp")
BANNED = ("coin flip", "coin-flip", "coinflip", "50/50", "fifty-fifty", "toss-up", "gamble",
          "lottery")
COINED = ("lad" + "der", "ru" + "ng")  # words the operator's standard terms rule out
S = D.DecideSettings(band_lo=0.0, band_hi=0.15, kelly_multiplier=0.5)


def book(side: str, token: str, bid: float, ask: float) -> Book:
    return Book(side=side, token_id=token, best_bid=bid, best_ask=ask, bid_size=100.0,
                ask_size=100.0, bids=((bid, 100.0), (round(bid - 0.02, 6), 60.0)),
                asks=((ask, 100.0),), source="stream", age_s=0.3)


def make(asset: str = "btc", *, now: float = START + 120, up_bid: float = 0.49,
         up_ask: float = 0.51, spot: float = 100.0, start_ref: float = 100.0,
         r15: tuple[float, ...] = (0.0,) * 12, vol: float = 0.0008, trend: float = 0.0,
         hour_up: float = 0.5, close_log: float | None = None, tick: float = 0.01,
         down: tuple[float, float] | None = None, twap: float | None = None,
         chainlink: float | None = None, binance: float | None = None) -> Inputs:
    """One coin's inputs. ``spot`` sets all three price feeds unless one is given."""
    down_bid, down_ask = down or (round(1 - up_ask, 6), round(1 - up_bid, 6))

    def price(source: str, value: float | None) -> PriceNow:
        return PriceNow(source=source, value=spot if value is None else value, obs_s=now - 1,
                        age_s=1.0)

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
        hour_open=100.0, twap60=price("chainlink_twap60", twap),
        chainlink=price("chainlink", chainlink), binance=price("binance", binance),
        start_ref=start_ref, start_ref_source="Gamma priceToBeat",
        window_avg=WindowAverage(value=start_ref, log_value=math.log(start_ref),
                                 through_s=int(now) - 1, seconds=int(now - START),
                                 printed=100, longest_gap_s=2),
        minute_returns=returns, minute_returns_end=float(now - now % 60), r15=tuple(r15),
        close_avg=close,
    )


def decide(inp: Inputs, dials: dict | None = None, settings: D.DecideSettings = S,
           **kw) -> D.Decision:  # noqa: ANN003
    kw.setdefault("bankroll", 100.0)
    return D.decide(inp, DIALS if dials is None else dials, settings, **kw)


def check_orders(dec: D.Decision, inp: Inputs) -> None:
    """Every candidate sits on its grid, on the passive side of the touch, and its fill and win
    chances can be sized."""
    for option in dec.options:
        b = inp.up_book if option.side == "Up" else inp.down_book
        prices = [c.price for c in option.child_orders]
        if option.action == "buy":
            grid = D.buy_price_levels(b.best_bid, inp.tick_size, S.band_lo, S.band_hi)
            assert prices == list(grid[:len(prices)])  # nearest first, on the grid
            assert all(0.0 < p <= b.best_bid for p in prices)  # never across the spread
            fills = [c.p_fill for c in option.child_orders]
            assert fills == sorted(fills, reverse=True)
            sizing.scaled_limits([(c.price, c.p_fill, c.q_fill) for c in option.child_orders],
                                 100.0, 1.0)  # consistent
        else:
            assert all(b.best_ask <= p < 1.0 for p in prices)
            assert len(option.paying) <= 1
        assert all(c.shares >= 0.0 for c in option.child_orders)
        assert math.isfinite(option.growth) and option.account_growth >= 0.0
        if not option.paying:
            assert option.growth == 0.0 and option.account_growth == 0.0
    if dec.order is not None:
        assert dec.order in dec.options
        assert dec.order.account_growth == max(o.account_growth for o in dec.options) > 0.0
    text = dec.explanation.lower()
    assert not any(word in text for word in BANNED + COINED)


# ---------------------------------------------------------------------------
# No edge from the market alone
# ---------------------------------------------------------------------------


def test_the_prior_follows_the_market_and_nothing_pays() -> None:
    inp = make(spot=100.3, up_bid=0.55, up_ask=0.57)
    dec = decide(inp, dict(ledger.PRIOR_DIALS))
    assert dec.p == pytest.approx(inp.market_up, abs=1e-12)  # w_M = 1, w_S = 0: p is the mid
    check_orders(dec, inp)
    assert dec.order is None and dec.action == "none" and dec.side is None
    assert "nothing rests" in dec.explanation


@pytest.mark.parametrize("books", [
    dict(spot=100.2, up_bid=0.30, up_ask=0.32),
    dict(spot=99.9, up_bid=0.62, up_ask=0.64),
    dict(spot=100.0, up_bid=0.30, up_ask=0.45),
])
def test_with_market_only_dials_every_level_wins_at_exactly_its_own_price(books) -> None:  # noqa: ANN001
    # The chance at a fill is taken where the order fills: the market is exactly at the
    # level. Following the market alone, every child order then wins at its own price, so no
    # level is worth anything. (Evaluated at the grid point after the crossing, as before,
    # every level came out 3-4 points below its price.)
    inp = make(**books)
    dec = decide(inp, MARKET_ONLY)
    check_orders(dec, inp)
    assert dec.order is None
    for option in dec.options:
        assert option.child_orders
        for c in option.child_orders:
            assert c.q_fill == pytest.approx(c.price, abs=1e-9)
            assert c.shares == pytest.approx(0.0, abs=1e-6)


# ---------------------------------------------------------------------------
# The fill simulation
# ---------------------------------------------------------------------------


def test_fill_chances_match_a_martingale_price_on_the_default_grid() -> None:
    # No pull, no momentum, market-only: the crowd's price is a martingale that ends at 0 or
    # 1, so a resting buy at x under a 50c mid fills with chance (1 - 0.5) / (1 - x) exactly.
    # Checking only at the 10 s grid points missed every path that dipped through a level and
    # came back inside a step (0.935 against 0.980 at 49c); the bridge minimum catches them.
    dials = D.resolve_dials({**MARKET_ONLY, "theta": 0.0, "kappa0": 0.0, "lam": 0.0})
    state = D.state_from_inputs(make(spot=100.0, up_bid=0.49, up_ask=0.51))
    view = D.evaluate(state, dials)
    levels = [0.49, 0.48, 0.45, 0.40, 0.30]
    n = 8000
    table = D.simulate_fills(state, dials, view, levels, [1 - x for x in levels], paths=n,
                             seed=11)
    for x in levels:
        want = 0.5 / (1 - x)
        assert table.falls[x][0] / n == pytest.approx(want, abs=0.012)
        assert table.rises[round(1 - x, 6)][0] / n == pytest.approx(want, abs=0.012)


def test_the_fill_and_win_chances_do_not_depend_on_the_grid() -> None:
    inp = make(now=END - 200, spot=100.03, up_bid=0.52, up_ask=0.54, r15=(0.002,) * 12)
    dials = D.resolve_dials(DIALS)
    state = D.state_from_inputs(inp)
    view = D.evaluate(state, dials)
    falls, rises = [0.52, 0.50, 0.47, 0.43], [0.54, 0.56, 0.59, 0.63]
    n = 4000
    coarse = D.simulate_fills(state, dials, view, falls, rises, paths=n, seed=5)
    fine = D.simulate_fills(state, dials, view, falls, rises, paths=n, seed=6, step_s=1.0,
                            close_step_s=1.0)
    assert fine.steps > 5 * coarse.steps
    for key in ("falls", "rises"):
        for x, (n_c, s_c) in getattr(coarse, key).items():
            n_f, s_f = getattr(fine, key)[x]
            assert n_c / n == pytest.approx(n_f / n, abs=0.025)
            assert s_c / n_c == pytest.approx(s_f / n_f, abs=0.01)


def test_no_level_fills_at_once_and_a_fill_comes_with_the_price_moving_against_it() -> None:
    # A wide book (40c/44c). The nearest buy rests at the best bid, under the mid: it fills
    # only when the market comes down to it, never on every path at today's chance.
    inp = make(spot=100.05, up_bid=0.40, up_ask=0.44)
    dec = decide(inp)
    up = dec.option("buy", "Up")
    near = up.child_orders[0]
    assert near.price == inp.up_book.best_bid
    assert near.p_fill < 0.99
    assert near.q_fill < dec.p  # adverse selection is inside the fill's win chance
    # A level at or above the market's price is not a resting order: it is left out, not
    # counted as a certain fill.
    state = D.state_from_inputs(inp)
    view = D.evaluate(state, D.resolve_dials(DIALS))
    table = D.simulate_fills(state, D.resolve_dials(DIALS), view, [0.43, 0.42, 0.41], [0.41],
                             paths=200)
    assert set(table.falls) == {0.41} and table.falls[0.41][0] < 200
    assert table.rises == {}


def test_the_simulation_is_seeded_per_window() -> None:
    inp = make(spot=100.1, up_bid=0.45, up_ask=0.47)
    first, again = decide(inp), decide(inp)
    assert first.options == again.options and first.order == again.order
    later = D.state_from_inputs(make(now=START + 181, spot=100.1, up_bid=0.45, up_ask=0.47))
    assert D._seed(later) == D._seed(D.state_from_inputs(inp))  # same window, same noise
    other = D.state_from_inputs(make("eth", spot=100.1, up_bid=0.45, up_ask=0.47))
    assert D._seed(other) != D._seed(D.state_from_inputs(inp))


def test_buy_quotes_trim_a_deeper_fill_chance_the_win_chances_do_not_allow() -> None:
    # 100 of 100 paths fill 50c and win 60% there; 99 fill 49c and win 50%. At most
    # 100 * 0.4 / 0.5 = 80 of the 49c fills fit those win chances, so its fill chance is
    # trimmed to 0.8; the win chances stay what the simulation found at each fill.
    table = D.FillTable(n_paths=100, falls={0.5: (100, 60.0), 0.49: (99, 49.5), 0.48: (30, 13.0)},
                        rises={})
    quotes = D.buy_quotes("Up", [0.5, 0.49, 0.48, 0.47], table)
    assert [c.price for c in quotes] == [0.5, 0.49, 0.48]  # 0.47 not simulated
    assert [c.q_fill for c in quotes] == pytest.approx([0.6, 0.5, 13 / 30])
    assert quotes[0].p_fill == pytest.approx(1.0) and quotes[0].p_fill < 1.0
    assert quotes[1].p_fill == pytest.approx(0.8)
    assert quotes[2].p_fill == pytest.approx(0.3)
    sizing.scaled_limits([(c.price, c.p_fill, c.q_fill) for c in quotes], 100.0, 1.0)


# ---------------------------------------------------------------------------
# The side: both sides priced, the maths picks
# ---------------------------------------------------------------------------


def test_a_leg_well_up_with_a_cheap_up_book_buys_up_under_the_best_bid() -> None:
    inp = make(spot=100.25, up_bid=0.40, up_ask=0.42)
    dec = decide(inp)
    check_orders(dec, inp)
    assert dec.action == "buy" and dec.side == "Up" and dec.p_model > dec.p > inp.market_up
    assert dec.option("buy", "Down").growth == 0.0
    for c in dec.order.paying:
        assert c.price <= inp.up_book.best_bid and c.q_fill > c.price
    text = dec.explanation
    assert text.startswith("BTC's 15m leg is up 0.25% with 13 min left")
    assert "Up buy order" in text and "rests" in text


def test_the_mirror_case_buys_down() -> None:
    inp = make(spot=99.75, up_bid=0.58, up_ask=0.60)  # Down book 0.40/0.42
    dec = decide(inp)
    check_orders(dec, inp)
    assert dec.action == "buy" and dec.side == "Down" and dec.p < 0.5
    assert all(c.price <= inp.down_book.best_bid for c in dec.order.paying)


@pytest.mark.parametrize("spot,up_bid,up_ask", [
    (99.97, 0.59, 0.61),  # p 0.52: the market leans further Up than the maths
    (99.95, 0.74, 0.76),  # p 0.58
    (99.95, 0.70, 0.72),
])
def test_the_fade_is_taken_when_the_other_side_pays(spot: float, up_bid: float,
                                                    up_ask: float) -> None:
    # The chance traded on leans Up, but the market leans further: Down is cheap for its
    # chance. A fixed "Up when p >= 1/2" rule priced only Up here and bid nothing.
    inp = make(spot=spot, up_bid=up_bid, up_ask=up_ask)
    dec = decide(inp)
    check_orders(dec, inp)
    assert dec.p >= 0.5 > dec.p_model
    assert dec.option("buy", "Up").growth == 0.0
    assert dec.action == "buy" and dec.side == "Down" and dec.order.shares > 0.0
    assert all(c.q_fill > c.price for c in dec.order.paying)
    assert "Down buy order" in dec.explanation


def test_up_is_bought_when_it_pays_even_with_the_chance_under_one_half() -> None:
    inp = make(spot=100.05, up_bid=0.20, up_ask=0.22)
    dec = decide(inp)
    check_orders(dec, inp)
    assert dec.p < 0.5
    assert dec.action == "buy" and dec.side == "Up"


def test_the_order_is_the_candidate_with_the_most_growth() -> None:
    orders = 0
    for kw in (dict(spot=100.02, up_bid=0.49, up_ask=0.50), dict(spot=99.98, up_bid=0.5,
                                                                   up_ask=0.51),
               dict(spot=100.1, up_bid=0.30, up_ask=0.45), dict(spot=100.25, up_bid=0.40,
                                                                  up_ask=0.42)):
        dec = decide(make(**kw))
        best = max(dec.options, key=lambda o: o.account_growth)
        assert dec.order == (best if best.account_growth > 0.0 else None)
        # Both figures are the order's own: the sizing's expected log growth of those stakes,
        # in the Kelly account ($50 of the $100) and on the operator's whole $100.
        for o in dec.options:
            levels = [(c.price, c.p_fill, c.q_fill) for c in o.child_orders]
            stakes = [c.shares * c.price for c in o.child_orders]
            if not o.paying:
                continue
            assert o.account_growth == pytest.approx(
                sizing.scaled_limits_log_growth(levels, dec.account_cash, stakes), rel=1e-9)
            assert o.growth == pytest.approx(
                sizing.scaled_limits_log_growth(levels, 100.0, stakes), rel=1e-9)
            assert 0.0 < o.growth < o.account_growth  # half the stakes' risk on twice the cash
        orders += dec.order is not None
    assert orders >= 1


def _fixed_option(action: str, growth: dict[str, float],  # noqa: ANN202
                  account: dict[str, float] | None = None):
    """A stand-in for ``buy_option`` / ``sell_option``: one child order of 10 shares at the
    nearest price level, with the growth (and the account's growth, the same by default)
    given per side."""
    account = growth if account is None else account

    def option(side, prices, table, cash, held=0.0, mark=None, *, bankroll=None):  # noqa: ANN001, ANN202, ARG001
        child = D.ChildOrder(prices[0], 0.5, 0.6, 10.0 if account[side] > 0.0 else 0.0)
        return D.ParentOrder(action, side, (child,), growth[side], account[side])

    return option


@pytest.mark.parametrize("up_growth, down_growth, want", [
    (0.002, 0.005, "Down"),  # Up is priced first; the later, larger candidate must still win
    (0.005, 0.002, "Up"),
])
def test_when_both_sides_pay_the_order_is_the_one_adding_more_growth(
    monkeypatch: pytest.MonkeyPatch, up_growth: float, down_growth: float, want: str,
) -> None:
    # Both candidate buys pay here. The order is the one with the larger expected log growth,
    # never simply the first side priced (that made Up win whenever both paid).
    monkeypatch.setattr(D, "buy_option", _fixed_option("buy", {"Up": up_growth,
                                                               "Down": down_growth}))
    dec = decide(make(spot=100.0, up_bid=0.40, up_ask=0.60))
    assert {o.side for o in dec.options} == {"Up", "Down"}
    assert all(o.growth > 0.0 and o.paying for o in dec.options)
    assert dec.action == "buy" and dec.side == want
    assert dec.order.growth == max(up_growth, down_growth)


def test_the_order_is_chosen_in_the_kelly_account_not_by_the_reported_growth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The Kelly account is what the sizes maximise, so it picks the order; the growth on the
    # operator's own money is only reported. Here they rank the two sides the other way.
    monkeypatch.setattr(D, "buy_option", _fixed_option(
        "buy", {"Up": 0.004, "Down": 0.003}, account={"Up": 0.002, "Down": 0.005}))
    dec = decide(make(spot=100.0, up_bid=0.40, up_ask=0.60))
    assert dec.side == "Down" and dec.order.growth == 0.003
    assert dec.factors["growth_buy_up"] == 0.004 and dec.factors["growth_buy_down"] == 0.003


@pytest.mark.parametrize("buy_growth, sell_growth, want", [
    (0.001, 0.003, "sell"),  # the buy of the held side is priced first
    (0.003, 0.001, "buy"),
])
def test_while_holding_the_larger_of_adding_and_selling_is_the_order(
    monkeypatch: pytest.MonkeyPatch, buy_growth: float, sell_growth: float, want: str,
) -> None:
    monkeypatch.setattr(D, "buy_option", _fixed_option("buy", {"Up": buy_growth}))
    monkeypatch.setattr(D, "sell_option", _fixed_option("sell", {"Up": sell_growth}))
    dec = decide(make(spot=100.0, up_bid=0.40, up_ask=0.60), held={"Up": 20.0})
    assert [(o.action, o.side) for o in dec.options] == [("buy", "Up"), ("sell", "Up")]
    assert dec.order.account_growth == max(buy_growth, sell_growth)
    assert dec.action == want and dec.side == "Up"
    assert dec.order.growth == max(buy_growth, sell_growth)


def test_the_kelly_multiplier_scales_a_fresh_order() -> None:
    inp = make(spot=100.25, up_bid=0.40, up_ask=0.42)
    half = decide(inp, settings=D.DecideSettings(band_lo=0.0, kelly_multiplier=0.5))
    full = decide(inp, settings=D.DecideSettings(band_lo=0.0, kelly_multiplier=1.0))
    assert half.account_cash == pytest.approx(50.0) and full.account_cash == pytest.approx(100.0)
    assert half.order.shares == pytest.approx(0.5 * full.order.shares, rel=1e-6)
    none = decide(inp, settings=D.DecideSettings(band_lo=0.0, kelly_multiplier=0.0))
    assert none.order is None


def test_the_order_as_one_bet_for_the_joint_sizing() -> None:
    dec = decide(make(spot=99.97, up_bid=0.59, up_ask=0.61))
    bet = dec.order.bet
    assert bet.side == "Down" and bet.payoff == 1.0
    assert bet.price == pytest.approx(dec.order.average_price)
    assert bet.q == pytest.approx(dec.order.q_bar)


# ---------------------------------------------------------------------------
# Shares already held
# ---------------------------------------------------------------------------


def test_holding_one_side_never_buys_the_other() -> None:
    for kw in (dict(spot=100.3, up_bid=0.70, up_ask=0.72), dict(spot=99.7, up_bid=0.30,
                                                                   up_ask=0.32)):
        inp = make(**kw)
        dec = decide(inp, held={"Up": 20.0})
        check_orders(dec, inp)
        assert dec.held_side == "Up" and dec.held_shares == 20.0
        assert all(o.side == "Up" for o in dec.options)
        assert dec.option("buy", "Down") is None
        assert dec.side in (None, "Up")


def test_shares_held_count_in_the_sizing() -> None:
    # The same favourable window: the more Up already held, the less more Up is bought, and
    # at some size a resting sell takes over.
    inp = make(spot=100.3, up_bid=0.70, up_ask=0.72)
    adds = []
    for n in (0.0, 10.0, 20.0):
        dec = decide(inp, held={"Up": n} if n else None)
        adds.append(dec.option("buy", "Up").shares)
    assert adds[0] > adds[1] > adds[2] > 0.0
    big = decide(inp, held={"Up": 80.0})
    assert big.option("buy", "Up").shares == 0.0
    assert big.action == "sell" and big.side == "Up"


def test_after_the_order_fills_at_unchanged_odds_nothing_more_is_bought() -> None:
    inp = make(spot=100.25, up_bid=0.40, up_ask=0.42)
    first = decide(inp)
    assert first.action == "buy" and first.side == "Up"
    held = first.order.shares
    cost = first.order.usd
    again = decide(inp, held={"Up": held}, bankroll=100.0 - cost)
    assert again.option("buy", "Up").shares < 0.01 * held
    assert again.action != "buy"


def test_when_the_odds_turn_a_held_position_is_sold_not_hedged() -> None:
    inp = make(spot=99.7, up_bid=0.30, up_ask=0.32)
    n = 20.0
    dec = decide(inp, held={"Up": n})
    check_orders(dec, inp)
    assert dec.action == "sell" and dec.side == "Up"
    (child,) = dec.order.paying
    assert child.price >= inp.up_book.best_ask  # never at or under the best bid
    assert 0.0 < child.shares <= n
    # The shares are the formula's, in the Kelly account, with the chance given the sale.
    assert child.shares == pytest.approx(
        sizing.reduce_position(child.q_fill, child.price, dec.account_cash, n))
    # The price is the level whose fill chance times gain is largest.
    gains = []
    for c in dec.order.child_orders:
        x = sizing.reduce_position(c.q_fill, c.price, dec.account_cash, n)
        gains.append(c.p_fill * sizing.sale_log_growth(c.q_fill, c.price, dec.account_cash,
                                                       n, x))
    assert dec.order.account_growth == pytest.approx(max(gains))
    assert dec.order.child_orders[gains.index(max(gains))] is child
    # What it adds to the operator's own $100 holding the 20 shares: the odds have turned,
    # so the sale adds growth there too.
    own = child.p_fill * sizing.sale_log_growth(child.q_fill, child.price, 100.0, n,
                                                child.shares)
    assert dec.order.growth == pytest.approx(own) and own > 0.0
    assert dec.factors["growth_sell_up"] == dec.order.growth
    assert dec.order.bet is None
    assert "selling" in dec.explanation and "pays more than keeping them" in dec.explanation


def test_when_the_odds_turn_a_held_down_position_is_sold_not_hedged() -> None:
    # The mirror: 20 Down held, and the leg runs up against Down (Up 68c/70c, Down 30c/32c).
    # A Down sale reads the fill table from the other side (it fills when the market's Up
    # price falls to 1 - s); this is the only place decide() mirrors a sell price.
    inp = make(spot=100.3, up_bid=0.68, up_ask=0.70)
    n = 20.0
    dec = decide(inp, held={"Down": n})
    check_orders(dec, inp)
    assert dec.held_side == "Down" and dec.held_shares == n
    assert dec.action == "sell" and dec.side == "Down"
    assert dec.option("buy", "Up") is None and dec.option("sell", "Up") is None
    (child,) = dec.order.paying
    assert child.price >= inp.down_book.best_ask  # never at or under the Down best bid
    assert 0.0 < child.shares <= n
    assert child.q_fill < 0.5  # Down is losing: the sale is worth making
    assert child.shares == pytest.approx(
        sizing.reduce_position(child.q_fill, child.price, dec.account_cash, n))
    assert dec.order.growth == pytest.approx(child.p_fill * sizing.sale_log_growth(
        child.q_fill, child.price, 100.0, n, child.shares))
    assert dec.factors["growth_sell_down"] == dec.order.growth > 0.0
    assert "we hold 20 Down shares" in dec.explanation and "selling" in dec.explanation


def test_the_sell_price_is_where_fill_chance_times_gain_is_largest() -> None:
    # Holding 100 Up with $100. The market pays up to 70c with a 90% fill chance while Up
    # wins only 54% even then: the higher price pays more despite the lower fill chance.
    table = D.FillTable(n_paths=1000, falls={},
                        rises={0.6: (990, 990 * 0.52), 0.65: (950, 950 * 0.53),
                               0.7: (900, 900 * 0.54)})
    order = D.sell_option("Up", [0.6, 0.65, 0.7], table, 100.0, 100.0)
    (child,) = order.paying
    assert child.price == 0.7 and child.shares == 100.0
    gains = {c.price: c.p_fill * sizing.sale_log_growth(
        c.q_fill, c.price, 100.0, 100.0, sizing.reduce_position(c.q_fill, c.price, 100.0, 100.0))
        for c in order.child_orders}
    assert max(gains, key=gains.get) == 0.7 and order.growth == pytest.approx(gains[0.7])
    # A Down sell reads the same table from the other side: selling Down at s fills when the
    # market's Up price falls to 1 - s.
    down = D.FillTable(n_paths=1000, rises={}, falls={0.4: (990, 990 * 0.48)})
    (only,) = D.sell_option("Down", [0.6], down, 100.0, 100.0).child_orders
    assert only.q_fill == pytest.approx(0.52) and only.p_fill == pytest.approx(0.99)


@pytest.mark.parametrize("held_side, n, kw", [
    ("Up", 20.0, dict(spot=99.7, up_bid=0.30, up_ask=0.32)),  # Up marked at 31c
    ("Down", 20.0, dict(spot=100.3, up_bid=0.68, up_ask=0.70)),  # Down marked at 31c
    ("Up", 60.0, dict(spot=100.3, up_bid=0.70, up_ask=0.72)),  # Up marked at 71c
])
def test_shares_held_are_marked_at_their_own_books_mid(held_side: str, n: float,
                                                       kw: dict) -> None:
    # The Kelly account holds the shares at what the market pays for them now: that side's
    # mid. A fixed or wrong-side mark moves the account's cash, and so every size.
    inp = make(**kw)
    b = inp.up_book if held_side == "Up" else inp.down_book
    k = S.kelly_multiplier
    dec = decide(inp, held={held_side: n}, bankroll=100.0)
    assert dec.held_side == held_side and dec.held_shares == n
    assert dec.account_cash == pytest.approx(sizing.kelly_cash(100.0, n, b.mid, k))
    assert dec.account_cash == pytest.approx(k * (100.0 + n * b.mid) - n * b.mid)
    assert dec.factors["held_usd"] == pytest.approx(n * b.mid)
    assert dec.factors["kelly_account_usd"] == pytest.approx(k * (100.0 + n * b.mid))
    # The sale's size is the formula's in that account, for the shares held.
    sale = dec.option("sell", held_side)
    assert sale.paying  # every case here sells: the loop below must check something
    for c in sale.paying:
        assert c.shares == pytest.approx(
            sizing.reduce_position(c.q_fill, c.price, dec.account_cash, n))
    assert dec.action == "sell" and dec.side == held_side


@pytest.mark.parametrize("up_bid", [0.49, 0.59, 0.69])
def test_a_position_over_the_kelly_multipliers_share_reports_its_real_growth(
    up_bid: float,
) -> None:
    # 125 Up held with $50 cash and a multiplier of one half: the shares are worth more than
    # half the wealth, so the Kelly account's cash sits at its floor, near zero. The sale is
    # sized there, but what it adds is measured on the operator's own $50 and 125 shares.
    # (Measured in the account it came out +849%: the log of a near-zero cash.)
    inp = make(up_bid=up_bid, up_ask=round(up_bid + 0.02, 6))
    n, cash = 125.0, 50.0
    dec = decide(inp, held={"Up": n}, bankroll=cash)
    mid = inp.up_book.mid
    assert dec.account_cash <= sizing.KELLY_CASH_FLOOR * (cash + n * mid) * (1 + 1e-9)
    assert dec.factors["held_usd"] == pytest.approx(n * mid)
    assert dec.factors["kelly_account_usd"] == pytest.approx(0.5 * (cash + n * mid))
    assert dec.factors["held_usd"] > dec.factors["kelly_account_usd"]
    assert dec.action == "sell" and dec.side == "Up"
    assert dec.option("buy", "Up").shares == 0.0
    (child,) = dec.order.paying
    own = child.p_fill * sizing.sale_log_growth(child.q_fill, child.price, cash, n,
                                                child.shares)
    assert dec.order.growth == pytest.approx(own)
    # No sale can add more than the log of the widest outcome: $50 against $175.
    assert 0.0 < dec.order.growth < math.log((cash + n) / cash)
    assert dec.factors["growth_sell_up"] == dec.order.growth
    record = dec.as_record()
    assert record["order"]["growth"] == dec.order.growth
    assert all(abs(o["growth"]) < 1.0 for o in record["options"])
    assert "more than the Kelly multiplier's share of the bankroll" in dec.explanation


def test_a_sale_back_to_the_kelly_multipliers_size_reports_the_growth_it_gives_up() -> None:
    # 60 Up held at 71c, the odds still with Up. Half Kelly wants a smaller position, so the
    # account sells some; full Kelly on the whole wealth would keep them. The sale is the
    # order, and its growth on the operator's own money is below 0: it trades a little
    # growth for less risk. With a multiplier of 1 the account is the whole wealth, the two
    # figures agree, and nothing is sold.
    inp = make(spot=100.3, up_bid=0.70, up_ask=0.72)
    half = decide(inp, held={"Up": 60.0})
    assert half.action == "sell" and half.order.account_growth > 0.0 > half.order.growth
    assert half.factors["growth_sell_up"] == half.order.growth
    assert "gives up a little expected growth for less risk" in half.explanation
    full = decide(inp, settings=D.DecideSettings(band_lo=0.0, band_hi=0.15, kelly_multiplier=1.0),
                  held={"Up": 60.0})
    assert full.action != "sell"
    for o in full.options:
        assert o.growth == pytest.approx(o.account_growth, rel=1e-9, abs=1e-15)


def test_a_sale_rests_from_the_best_ask_up_even_inside_a_wide_spread() -> None:
    # Up 25c/35c with 20 Up held and the odds turned: the sell levels run from the best ask
    # (band_lo 0) up, never from the best bid. A level inside the spread is not on the grid.
    inp = make(spot=99.7, up_bid=0.25, up_ask=0.35)
    dec = decide(inp, held={"Up": 20.0})
    check_orders(dec, inp)
    sale = dec.option("sell", "Up")
    grid = D.sell_price_levels(inp.up_book.best_ask, inp.tick_size, S.band_lo, S.band_hi)
    prices = [c.price for c in sale.child_orders]
    assert prices and set(prices) <= set(grid)
    assert prices[0] == inp.up_book.best_ask
    buy = dec.option("buy", "Up")
    assert [c.price for c in buy.child_orders][0] == inp.up_book.best_bid


def test_a_favourable_position_is_kept() -> None:
    inp = make(spot=100.3, up_bid=0.70, up_ask=0.72)
    dec = decide(inp, held={"Up": 10.0})
    assert dec.option("sell", "Up").growth == 0.0
    assert dec.action in ("buy", "none")


def test_selling_can_be_switched_off() -> None:
    inp = make(spot=99.7, up_bid=0.30, up_ask=0.32)
    off = D.DecideSettings(band_lo=0.0, reduce_positions=False)
    dec = decide(inp, settings=off, held={"Up": 20.0})
    assert dec.option("sell", "Up") is None and dec.order is None
    assert "we hold 20 Up shares" in dec.explanation


def test_shares_held_on_both_sides_pay_one_dollar_a_pair() -> None:
    inp = make(spot=99.7, up_bid=0.30, up_ask=0.32)
    dec = decide(inp, held={"Up": 25.0, "Down": 5.0})
    assert dec.held_side == "Up" and dec.held_shares == 20.0
    plain = decide(inp, held={"Up": 20.0}, bankroll=105.0)
    assert dec.account_cash == pytest.approx(plain.account_cash)


def test_shares_held_need_a_bankroll() -> None:
    with pytest.raises(ValueError, match="bankroll"):
        D.decide(make(), DIALS, S, held={"Up": 5.0})
    with pytest.raises(ValueError):
        decide(make(), held={"Up": -1.0})


# ---------------------------------------------------------------------------
# The price now
# ---------------------------------------------------------------------------


def test_the_price_now_is_the_live_chainlink_price_not_the_twap_print() -> None:
    dials = D.resolve_dials(DIALS)

    def p_model(**kw) -> float:  # noqa: ANN003
        return D.evaluate(D.state_from_inputs(make(**kw)), dials).p_model

    base = p_model(spot=100.0)
    # The TWAP-60s print now is a 60 s average running ~30 s behind: it never moves p.
    assert p_model(spot=100.0, twap=100.05) == base
    assert p_model(spot=100.0, twap=99.95) == base
    # The live Chainlink price does.
    assert p_model(spot=100.0, chainlink=100.04) > base + 0.05
    assert p_model(spot=100.0, chainlink=99.96) < base - 0.05
    # Binance is the price now only when the operator picks it (it is always the 1h
    # market's own basis, the hour's move).
    inp = make(spot=100.0, binance=100.04)
    assert D.state_from_inputs(inp).d == 0.0
    assert D.state_from_inputs(inp, "binance").d == pytest.approx(math.log(1.0004))
    assert D.state_from_inputs(make(spot=100.0, chainlink=100.04)).d == pytest.approx(
        math.log(1.0004))


def test_in_the_closing_minute_p_follows_the_raw_price() -> None:
    # 20 s before the close: the closing average so far 100.004, the raw Chainlink price
    # 99.985, the TWAP-60s print 100.012, the start 100. The 20 s still to come start from the
    # raw price, which sits under the start: Down is favoured. Reading the TWAP print as the
    # price now gave Up at 0.77.
    inp = make(now=END - 20, spot=100.0, chainlink=99.985, twap=100.012, binance=99.99,
               close_log=math.log(100.004))
    dials = D.resolve_dials(DIALS)
    p = D.evaluate(D.state_from_inputs(inp), dials).p_model
    assert p < 0.5
    moved = make(now=END - 20, spot=100.0, chainlink=99.985, twap=99.9, binance=99.99,
                 close_log=math.log(100.004))
    assert D.evaluate(D.state_from_inputs(moved), dials).p_model == p


def test_the_old_spot_feed_name_reads_the_chainlink_price() -> None:
    assert D.DecideSettings(spot_feed="chainlink_twap60").spot_feed == "chainlink"
    inp = make(spot=100.0, chainlink=100.03, twap=99.97)
    assert D.state_from_inputs(inp, "chainlink_twap60") == D.state_from_inputs(inp, "chainlink")
    with pytest.raises(ValueError):
        D.DecideSettings(spot_feed="kraken")


def test_a_recorded_row_prices_the_same_as_the_live_inputs() -> None:
    inp = make(now=END - 30, spot=100.05, close_log=math.log(100.08), r15=(0.002,) * 12,
               hour_up=0.62, trend=0.001, chainlink=100.06, twap=100.02, binance=100.04)
    record = json.loads(json.dumps(inp.as_record()))  # as stored
    for feed in (*D.SPOT_FEEDS, *D.LEGACY_SPOT_FEEDS):
        record["settings"] = {"spot_feed": feed}
        live, back = D.state_from_inputs(inp, feed), D.state_from_record(record)
        assert back == live
        assert D.evaluate(back, D.resolve_dials(DIALS)).p == \
            D.evaluate(live, D.resolve_dials(DIALS)).p


# ---------------------------------------------------------------------------
# The model's view and the card
# ---------------------------------------------------------------------------


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
    dec = decide(inp)
    f = dec.factors
    total = f["leg_pts"] + f["snapback_pts"] + f["momentum_pts"] + f["anchor_pts"]
    assert total == pytest.approx(100 * (dec.p - 0.5), abs=1e-9)
    assert f["snapback_pts"] < 0  # the last candles ran up: the snap-back takes off Up
    assert f["stretch_M"] > 0 and f["mu_H"] > 0
    assert f["account_cash"] == pytest.approx(50.0)
    assert f["held_usd"] == 0.0 and f["kelly_account_usd"] == pytest.approx(50.0)
    assert f["growth_buy_up"] >= 0.0 and f["growth_buy_down"] >= 0.0
    assert all(math.isfinite(v) for v in f.values())
    assert "snap-back takes" in dec.explanation


def test_flat_prices_cannot_be_priced() -> None:
    with pytest.raises(D.NoDecision, match="volatility is zero"):
        decide(make(vol=0.0))
    with pytest.raises(ValueError):
        decide(make(), {"w_M": 1.0})  # dials missing


# ---------------------------------------------------------------------------
# Price levels and the records
# ---------------------------------------------------------------------------


def test_price_levels_sit_on_the_passive_side_of_the_touch() -> None:
    assert D.buy_price_levels(0.55, 0.01, 0.0, 0.03) == (0.55, 0.54, 0.53, 0.52)
    assert D.buy_price_levels(0.55, 0.01, 0.01, 0.03) == (0.54, 0.53, 0.52)
    assert D.buy_price_levels(0.985, 0.001, 0.01, 0.03) == (0.975, 0.965, 0.955)
    assert D.buy_price_levels(0.03, 0.01, 0.0, 0.15) == (0.03, 0.02, 0.01)
    assert D.buy_price_levels(0.55, 0.01, 0.15, 0.01) == ()  # an empty band
    assert D.sell_price_levels(0.55, 0.01, 0.0, 0.02) == (0.55, 0.56, 0.57)
    assert D.sell_price_levels(0.97, 0.01, 0.0, 0.15) == (0.97, 0.98, 0.99)
    assert D.sell_price_levels(0.015, 0.001, 0.01, 0.02) == (0.025, 0.035)


def _at_or_across_the_touch(dec: D.Decision, inp: Inputs) -> list[tuple]:
    """Every priced child order that would meet its own book: a buy at or above that
    token's best ask, a sale at or below its best bid."""
    out = []
    for option in dec.options:
        b = inp.up_book if option.side == "Up" else inp.down_book
        for c in option.child_orders:
            if (c.price >= b.best_ask - 1e-9 if option.action == "buy"
                    else c.price <= b.best_bid + 1e-9):
                out.append((option.action, option.side, c.price, c.shares))
    return out


@pytest.mark.parametrize("kw", [
    dict(spot=100.05, up_bid=0.45, up_ask=0.45),  # Up locked at the mid
    dict(spot=99.95, up_bid=0.55, up_ask=0.55),
    dict(spot=100.2, up_bid=0.50, up_ask=0.51),  # a one-tick spread
    dict(spot=99.8, up_bid=0.50, up_ask=0.51),
])
def test_no_child_order_is_priced_at_or_across_the_touch(kw: dict) -> None:
    # A locked book (bid = ask, a snapshot mid-update) puts the best bid at the ask. A buy
    # there would meet the ask, so the level is not a resting order and is not priced.
    inp = make(**kw)
    for held in (None, {"Up": 20.0}, {"Down": 20.0}):
        dec = decide(inp, held=held)
        assert _at_or_across_the_touch(dec, inp) == []


def test_a_locked_down_book_prices_no_buy_at_its_own_ask() -> None:
    # The two tokens' books arrive on separate stream updates and can disagree for a moment:
    # Up 40c/44c (mid 42c), Down locked at 55c/55c. A Down buy at 55c meets the Down ask, so
    # the Down levels start a cent under it.
    inp = make(spot=99.9, up_bid=0.40, up_ask=0.44, down=(0.55, 0.55))
    dec = decide(inp)
    assert _at_or_across_the_touch(dec, inp) == []
    down = dec.option("buy", "Down")
    assert down is not None and down.child_orders
    assert max(c.price for c in down.child_orders) < 0.55


def test_price_levels_leave_out_the_sides_own_touch() -> None:
    # A buy level at or above the side's own ask, or a sale at or below its own bid, would
    # meet the book: it is not a resting order.
    assert D.buy_price_levels(0.55, 0.01, 0.0, 0.03, best_ask=0.55) == (0.54, 0.53, 0.52)
    assert D.buy_price_levels(0.55, 0.01, 0.0, 0.03, best_ask=0.56) == (0.55, 0.54, 0.53, 0.52)
    assert D.sell_price_levels(0.45, 0.01, 0.0, 0.02, best_bid=0.45) == (0.46, 0.47)
    assert D.sell_price_levels(0.45, 0.01, 0.0, 0.02, best_bid=0.44) == (0.45, 0.46, 0.47)
    assert D.buy_price_levels(0.55, 0.01, 0.0, 0.01) == (0.55, 0.54)


def test_orders_and_decisions_check_their_values() -> None:
    with pytest.raises(ValueError):
        D.ChildOrder(1.0, 0.5, 0.5)
    with pytest.raises(ValueError):
        D.ChildOrder(0.5, 0.5, 0.5, -1.0)
    with pytest.raises(ValueError):
        D.ParentOrder("hedge", "Up")
    with pytest.raises(ValueError):
        D.ParentOrder("buy", "Up", growth=-0.1)  # the account's growth defaults to it
    with pytest.raises(ValueError):
        D.ParentOrder("sell", "Up", growth=0.1, account_growth=-0.1)
    with pytest.raises(ValueError):
        D.ParentOrder("sell", "Up", growth=math.nan, account_growth=0.1)
    cut = D.ParentOrder("sell", "Up", growth=-0.002, account_growth=0.01)
    assert cut.growth == -0.002 and cut.account_growth == 0.01
    buy_down = D.ParentOrder("buy", "Down", (D.ChildOrder(0.4, 0.5, 0.5, 10.0),), 0.01)
    with pytest.raises(ValueError, match="held"):
        D.Decision(p_model=0.5, p=0.5, order=buy_down, held_side="Up", held_shares=5.0)
    sell = D.ParentOrder("sell", "Up", (D.ChildOrder(0.6, 0.5, 0.5, 5.0),), 0.01)
    with pytest.raises(ValueError, match="sell"):
        D.Decision(p_model=0.5, p=0.5, order=sell)
    record = buy_down.as_record()
    assert record["action"] == "buy" and record["child_orders"][0]["shares"] == 10.0
    assert json.loads(json.dumps(record)) == record
    dec = decide(make(spot=99.7, up_bid=0.30, up_ask=0.32), held={"Up": 20.0})
    whole = json.loads(json.dumps(dec.as_record(), allow_nan=False))
    assert whole["action"] == "sell" and whole["side"] == "Up" and whole["held_shares"] == 20.0
    assert [o["action"] for o in whole["options"]] == ["buy", "sell"]
    assert whole["order"] == dec.order.as_record()


def test_the_explanations_use_standard_terms() -> None:
    cases = [decide(make(spot=100.25, up_bid=0.40, up_ask=0.42)),
             decide(make(spot=99.7, up_bid=0.30, up_ask=0.32), held={"Up": 20.0}),
             decide(make(spot=100.3, up_bid=0.70, up_ask=0.72), held={"Up": 10.0}),
             decide(make(spot=100.3, up_bid=0.70, up_ask=0.72), held={"Up": 60.0}),
             decide(make(spot=100.3, up_bid=0.68, up_ask=0.70), held={"Down": 20.0}),
             decide(make(), held={"Up": 125.0}, bankroll=50.0),
             decide(make(), MARKET_ONLY)]
    for dec in cases:
        text = dec.explanation.lower()
        assert text.endswith(".")
        assert not any(word in text for word in BANNED + COINED)


def test_four_coins_decide_quickly() -> None:
    cases = [make(a, spot=100.1, up_bid=0.44, up_ask=0.46) for a in ASSETS]
    started = time.perf_counter()
    for inp in cases:
        decide(inp)
    assert time.perf_counter() - started < 4.0  # about 0.3 s on the Mac
