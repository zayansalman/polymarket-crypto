"""Fade 1h Momentum on 15m: the runner loop, its sizing plan and its registration.

Runs against the real schema from ``db.init_db`` on a temp file. The venue (trade tape and
order-book service) is a small fake serving real ``httpx.Response`` objects; the inputs are
built directly, so no market-data hub or Binance call is involved.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import math
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from ems import config as _config
from ems import db as _db
from ems import runtime_knobs as _knobs
from ems import strategies as _strategies
from ems.fade_1h_momentum_15m import decide as _decide
from ems.fade_1h_momentum_15m import executor as ex
from ems.fade_1h_momentum_15m import inputs as _inputs
from ems.fade_1h_momentum_15m import ledger
from ems.fade_1h_momentum_15m import runner as rn
from ems.fade_1h_momentum_15m import sizing
from ems.fade_1h_momentum_15m.decide import ChildOrder, Decision, ParentOrder
from ems.fade_1h_momentum_15m.inputs import (
    Book, Inputs, PriceNow, Problem, WindowAverage,
)
from ems.fade_1h_momentum_15m.ledger import NewOrder

HOUR = 1_789_934_400  # a UTC hour boundary
START = HOUR + 900  # the hour's second quarter
END = START + 900
NOW = START + 300
COINED = ("lad" + "der", "ru" + "ng")  # words the operator's standard terms rule out


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeVenue:
    """The data-api trade tape and the CLOB market lookup, in memory."""

    def __init__(self) -> None:
        self.tape: dict[str, list[dict]] = {}
        self.markets: dict[str, dict] = {}

    def add(self, cid: str, **rec) -> None:
        self.tape.setdefault(cid, []).append({"conditionId": cid, "proxyWallet": "0xw",
                                              "transactionHash": f"0x{len(self.tape[cid])}",
                                              **rec})

    def resolve(self, cid: str, *, up: str, down: str, winner: str) -> None:
        self.markets[cid] = {"condition_id": cid, "closed": True, "tokens": [
            {"token_id": up, "outcome": "Up", "winner": winner == "Up"},
            {"token_id": down, "outcome": "Down", "winner": winner == "Down"},
        ]}

    async def get(self, url, *, params=None, headers=None, timeout=None):
        params = dict(params or {})
        request = httpx.Request("GET", url)
        if url == f"{ex.DATA_API}/trades":
            rows = sorted(self.tape.get(params["market"], []), key=lambda r: -r["timestamp"])
            offset, limit = int(params["offset"]), int(params["limit"])
            return httpx.Response(200, json=rows[offset:offset + limit], request=request)
        if url.startswith(f"{ex.CLOB}/markets/"):
            cid = url.rsplit("/", 1)[1]
            if cid not in self.markets:
                return httpx.Response(200, json={"closed": False, "tokens": []},
                                      request=request)
            return httpx.Response(200, json=self.markets[cid], request=request)
        raise AssertionError(f"unexpected request: {url}")


class FakeHub:
    def __init__(self) -> None:
        self.released: list[str] = []

    def release(self, owner, asset=None, timeframe=None):  # noqa: ANN001
        self.released.append(owner)
        return 1


def cid_of(asset: str, start: int = START) -> str:
    return f"0xcid-{asset}-{start}"


def up_of(asset: str, start: int = START) -> str:
    return f"UP-{asset}-{start}"


def down_of(asset: str, start: int = START) -> str:
    return f"DN-{asset}-{start}"


def slug_of(asset: str, start: int = START) -> str:
    return f"{asset}-updown-15m-{start}"


def make_inputs(asset: str, *, now: float = NOW, start: int = START, up_bid: float = 0.53,
                up_ask: float = 0.55, chainlink: float = 100.25) -> Inputs:
    """One coin's inputs. The default leg is up 0.15% on Chainlink against a price to beat of
    100.1, with a quiet hour, so the starting dials buy Up at the best bid."""
    down_bid, down_ask = round(1 - up_ask, 6), round(1 - up_bid, 6)

    def book(side: str, token: str, bid: float, ask: float) -> Book:
        return Book(side=side, token_id=token, best_bid=bid, best_ask=ask, bid_size=100.0,
                    ask_size=100.0, bids=((bid, 100.0), (round(bid - 0.03, 6), 50.0)),
                    asks=((ask, 80.0), (round(ask + 0.02, 6), 40.0)), source="stream",
                    age_s=0.2)

    def price(source: str, value: float) -> PriceNow:
        return PriceNow(source=source, value=value, obs_s=now - 2, age_s=2.0)

    return Inputs(
        asset=asset, ts=now, window_slug=slug_of(asset, start), window_start=float(start),
        window_end=float(start + 900), condition_id=cid_of(asset, start),
        up_token=up_of(asset, start), down_token=down_of(asset, start), tick_size=0.01,
        up_book=book("Up", up_of(asset, start), up_bid, up_ask),
        down_book=book("Down", down_of(asset, start), down_bid, down_ask),
        hour_start=float(start - start % 3600), hour_slug=f"{asset}-hour",
        hour_condition_id=None, hour_up_bid=0.58, hour_up_ask=0.60, hour_book_age_s=3.0,
        hour_open=100.0, twap60=price("chainlink_twap60", 100.2),
        chainlink=price("chainlink", chainlink), binance=price("binance", 100.3),
        start_ref=100.1, start_ref_source="Gamma priceToBeat",
        window_avg=WindowAverage(value=100.15, log_value=math.log(100.15),
                                 through_s=int(now) - 2, seconds=299, printed=290,
                                 longest_gap_s=3),
        minute_returns=tuple(0.0005 * (-1) ** i for i in range(60)),
        minute_returns_end=float(now - now % 60), r15=tuple(0.001 for _ in range(12)),
    )


FACTORS = {"leg_pts": 20.0, "snapback_pts": -2.0, "momentum_pts": 1.0, "anchor_pts": 3.0}
UP_LEVELS = ((0.53, 0.90, 0.70, 30.0), (0.52, 0.80, 0.68, 0.0), (0.50, 0.60, 0.64, 12.0))
DOWN_LEVELS = ((0.45, 0.90, 0.62, 20.0), (0.42, 0.50, 0.56, 10.0))


def buy(side: str = "Up", levels=UP_LEVELS, *, p: float = 0.72, p_model: float = 0.80,
        held: float = 0.0) -> Decision:  # noqa: ANN001
    order = ParentOrder("buy", side, tuple(ChildOrder(*lv) for lv in levels), growth=0.05)
    return Decision(p_model=p_model, p=p, order=order, options=(order,),
                    held_side=side if held else None, held_shares=held, account_cash=50.0,
                    factors=FACTORS, explanation=f"The maths rests a scaled {side} buy order.")


def sell(side: str = "Up", held: float = 40.0, level=(0.57, 0.80, 0.30, 25.0)) -> Decision:  # noqa: ANN001
    order = ParentOrder("sell", side, (ChildOrder(*level),), growth=0.03)
    options = (ParentOrder("buy", side, (), 0.0), order)
    return Decision(p_model=0.2, p=0.3, order=order, options=options, held_side=side,
                    held_shares=held, account_cash=50.0, factors=FACTORS,
                    explanation=f"Selling some of the {side} shares pays more than keeping them.")


def nothing(held_side: str | None = None, held: float = 0.0) -> Decision:
    return Decision(p_model=0.55, p=0.54, order=None, options=(), held_side=held_side,
                    held_shares=held, account_cash=50.0, factors=FACTORS,
                    explanation="Nothing pays.")


def settings(**over) -> rn.Settings:
    base = dict(poll_s=60.0, bankroll_usd=100.0, kelly_multiplier=0.5, max_order_usd=25.0,
                band_lo=0.0, band_hi=0.15, reduce_positions=True, spot_feed="chainlink",
                coins={a: True for a in rn.ASSETS})
    base.update(over)
    return rn.Settings(**base)


def plan(cases: dict, *, cash: float = 100.0, held: dict | None = None, rho: float = 0.76,
         **over) -> dict[str, rn.CoinPlan]:  # noqa: ANN003
    return rn.plan_orders(cases, cash_usd=cash, bankrolls={a: cash for a in cases},
                          held=held or {}, rho=rho, settings=settings(**over))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def fade_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "fade.db")
    monkeypatch.setattr(_config, "KILL_SWITCH_PATH", tmp_path / "KILL")
    await _db.init_db()
    await _db.set_config(ex.MODE_KEY, "paper")
    monkeypatch.setattr(rn, "_STATUS", {"state": "not_started"})
    return _db


@pytest.fixture
def venue() -> FakeVenue:
    return FakeVenue()


async def save_window(asset: str, start: int = START) -> None:
    await ledger.upsert_window(
        window_slug=slug_of(asset, start), asset=asset, window_start=start,
        window_end=start + 900, ts=start, condition_id=cid_of(asset, start),
        up_token=up_of(asset, start), down_token=down_of(asset, start),
    )


def fake_gather(monkeypatch: pytest.MonkeyPatch, results: dict, calls: list | None = None,
                on_call=None):  # noqa: ANN001
    async def _gather(hub, client, now, *, memory=None):  # noqa: ANN001
        if calls is not None:
            calls.append(now)
        if on_call is not None:
            on_call()
        return dict(results)

    monkeypatch.setattr(_inputs, "gather", _gather)


def fake_decide(monkeypatch: pytest.MonkeyPatch, by_asset: dict, calls: list | None = None):
    def _decide_fn(inputs, dials, settings=_decide.DEFAULT_SETTINGS, *, held=None,  # noqa: ANN001
                   bankroll=None):
        if calls is not None:
            calls.append({"asset": inputs.asset, "held": dict(held or {}),
                          "bankroll": bankroll, "settings": settings})
        value = by_asset.get(inputs.asset)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(_decide, "decide", _decide_fn)


def only_btc(inp: Inputs | None = None, now: float = NOW) -> dict:
    others = {a: Problem(asset=a, code="no_hub", ts=now, message="No hub.")
              for a in ("eth", "sol", "xrp")}
    return {"btc": inp if inp is not None else make_inputs("btc", now=now), **others}


async def all_orders() -> list[dict]:
    async with _db.connect() as conn:
        cur = await conn.execute("SELECT * FROM fade_orders ORDER BY id")
        return [dict(r) for r in await cur.fetchall()]


async def decision_rows() -> list[dict]:
    return list(reversed(await ledger.recent_decisions(100)))


def tape_sale(venue: FakeVenue, asset: str, ts: int, price: float, size: float,
              start: int = START, side: str = "Up") -> None:
    """A taker selling ``side``'s token at ``price``: it sells into that side's bids."""
    token = up_of(asset, start) if side == "Up" else down_of(asset, start)
    venue.add(cid_of(asset, start), timestamp=ts, side="SELL", asset=token,
              outcomeIndex=0 if side == "Up" else 1, size=size, price=price)


def tape_buy(venue: FakeVenue, asset: str, ts: int, price: float, size: float,
             start: int = START, side: str = "Up") -> None:
    """A taker buying ``side``'s token at ``price``: it takes that side's asks."""
    token = up_of(asset, start) if side == "Up" else down_of(asset, start)
    venue.add(cid_of(asset, start), timestamp=ts, side="BUY", asset=token,
              outcomeIndex=0 if side == "Up" else 1, size=size, price=price)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_the_switch_is_registered_and_on_by_default() -> None:
    strategy = _strategies.STRATEGIES["fade_1h_momentum_15m"]
    assert strategy.label == "Fade 1h Momentum on 15m"
    assert strategy.default is True
    text = strategy.description.lower()
    assert not any(word in text for word in COINED)
    assert "places no" not in text  # the model is plugged in


def test_the_settings_are_registered_in_their_own_group_in_standard_terms() -> None:
    names = [n for n, k in _knobs.KNOBS.items() if k.group == "Fade 1h Momentum on 15m"]
    assert names == ["fade1h_poll_interval_seconds", "fade1h_bankroll_usd",
                     "fade1h_kelly_multiplier", "fade1h_max_order_usd",
                     "fade1h_levels_near_cents", "fade1h_levels_far_cents",
                     "fade1h_reduce_positions", "fade1h_spot_feed", "fade1h_trade_btc",
                     "fade1h_trade_eth", "fade1h_trade_sol", "fade1h_trade_xrp"]
    knobs = _knobs.KNOBS
    assert knobs["fade1h_poll_interval_seconds"].default == 60.0
    assert knobs["fade1h_bankroll_usd"].default == 100.0
    assert knobs["fade1h_kelly_multiplier"].default == 0.5
    near, far = knobs["fade1h_levels_near_cents"], knobs["fade1h_levels_far_cents"]
    assert (near.default, far.default) == (0.0, 15.0)  # 0 joins the best bid
    assert "child orders" in near.label and "below the best bid" in near.label
    assert knobs["fade1h_reduce_positions"].default is True
    assert "resting sell" in knobs["fade1h_reduce_positions"].label
    assert knobs["fade1h_spot_feed"].default == "chainlink"
    assert knobs["fade1h_spot_feed"].choices == ("chainlink", "binance")
    for name in names:  # none clashes with the hand-written /api/runtime-config keys
        assert name not in ("max_trade_usd", "trade_shares", "market", "strategy")
        assert knobs[name].default is not None
        text = f"{name} {knobs[name].key} {knobs[name].label}".lower()
        assert not any(word in text for word in COINED), name


def test_the_app_starts_the_runner_after_the_hub_and_stops_it() -> None:
    text = (Path(__file__).resolve().parents[2]
            / "ems/dashboard/app.py").read_text()
    assert text.index("_marketdata_hub.set_current(market_data)") < text.index("_run_fade(")
    assert "(fade_stop_event, fade_task)" in text


def test_settings_reach_the_model() -> None:
    s = settings(kelly_multiplier=0.25, reduce_positions=False, band_lo=0.02, band_hi=0.08,
                 spot_feed="binance").decide
    assert (s.kelly_multiplier, s.reduce_positions, s.band_lo, s.band_hi, s.spot_feed) == \
        (0.25, False, 0.02, 0.08, "binance")
    # An operator override stored under the old name reads as the live Chainlink price.
    assert settings(spot_feed="chainlink_twap60").decide.spot_feed == "chainlink"
    record = settings().as_record()
    assert record["reduce_positions"] is True and record["band_lo"] == 0.0


# ---------------------------------------------------------------------------
# The plan (pure)
# ---------------------------------------------------------------------------


def test_the_plan_rests_each_paying_child_order_on_the_passive_side() -> None:
    inp = make_inputs("btc")
    p = plan({"btc": (inp, buy())})["btc"]
    assert (p.action, p.side, p.joint_scale, p.fit_scale) == ("buy", "Up", 1.0, 1.0)
    # The level with no shares is left out; the others keep their place in the parent order.
    assert [(o.level, o.price, o.shares) for o in p.orders] == [(0, 0.53, 30.0),
                                                                 (2, 0.50, 12.0)]
    for o in p.orders:
        assert (o.kind, o.order_side, o.side, o.token_id) == ("entry", "BUY", "Up",
                                                              inp.up_token)
        assert o.price <= inp.up_book.best_bid  # at or under the best bid: never crosses
        assert o.levels_ahead == inp.up_book.bids
    assert p.orders[0].depth_ahead == 100.0 and p.orders[1].depth_ahead == 150.0
    assert p.orders[0].optimal_shares == 30.0 and p.orders[0].p_fill == 0.90
    assert p.buy_usd == pytest.approx(0.53 * 30 + 0.50 * 12)
    new = p.orders[0].new_order(inp.window_slug, 7)
    assert (new.kind, new.level, new.decision_id, new.depth_ahead) == ("entry", 0, 7, 100.0)
    assert new.queue() == ((0.53, 100.0),)


def test_a_down_order_rests_on_the_down_token_with_the_down_book_depth() -> None:
    inp = make_inputs("btc")
    p = plan({"btc": (inp, buy("Down", DOWN_LEVELS, p=0.3))})["btc"]
    assert {(o.side, o.token_id) for o in p.orders} == {("Down", inp.down_token)}
    assert all(o.levels_ahead == inp.down_book.bids for o in p.orders)
    assert [o.depth_ahead for o in p.orders] == [100.0, 150.0]


def test_coins_buying_the_same_side_are_sized_down_together() -> None:
    one = plan({"btc": (make_inputs("btc"), buy())})["btc"]
    cases = {a: (make_inputs(a), buy()) for a in rn.ASSETS}
    four = plan(cases)
    bet = buy().order.bet
    want = sizing.joint_kelly([bet] * 4, 0.76, 1.0)[0] / sizing.kelly_maker(bet.q, bet.price)
    for asset in rn.ASSETS:
        assert four[asset].joint_scale == pytest.approx(want)
        assert four[asset].joint_scale < 0.6
        assert four[asset].buy_usd < one.buy_usd
    # Less correlation leaves more room for each coin.
    loose = plan(cases, rho=0.0)
    assert loose["btc"].joint_scale > four["btc"].joint_scale


def test_bets_on_opposite_sides_hedge_each_other_and_are_not_shrunk() -> None:
    cases = {"btc": (make_inputs("btc"), buy("Up")),
             "eth": (make_inputs("eth"), buy("Down", DOWN_LEVELS, p=0.3))}
    mixed = plan(cases)
    assert mixed["btc"].joint_scale == 1.0 and mixed["eth"].joint_scale == 1.0
    same = plan({"btc": cases["btc"], "eth": (make_inputs("eth"), buy("Up"))})
    assert same["btc"].joint_scale < 1.0


def test_each_buy_child_order_is_capped_rounded_and_kept_over_the_minimum() -> None:
    inp = make_inputs("btc")
    levels = ((0.53, 0.9, 0.7, 60.0), (0.50, 0.6, 0.64, 7.456), (0.47, 0.3, 0.60, 4.99))
    p = plan({"btc": (inp, buy(levels=levels))}, max_order_usd=10.0)["btc"]
    by_price = {o.price: o.shares for o in p.orders}
    assert by_price[0.53] == pytest.approx(18.86)  # $10 cap at 53c, rounded down
    assert by_price[0.50] == pytest.approx(7.45)  # rounded down to the share step
    assert 0.47 not in by_price  # under the venue's 5-share minimum
    tiny = plan({"btc": (inp, buy(levels=((0.53, 0.9, 0.7, 4.0),)))})["btc"]
    assert tiny.orders == [] and "minimum" in " ".join(tiny.notes)


def test_the_buys_are_fitted_to_the_free_cash() -> None:
    cases = {a: (make_inputs(a), buy()) for a in rn.ASSETS}
    full = plan(cases, cash=1000.0, rho=0.0)
    assert sum(p.buy_usd for p in full.values()) > 12.0
    tight = plan(cases, cash=12.0, rho=0.0)
    assert sum(p.buy_usd for p in tight.values()) <= 12.0 + 1e-9
    assert tight["btc"].fit_scale < 1.0 and "fit the free cash" in " ".join(tight["btc"].notes)
    assert all(o.shares >= rn.MIN_SHARES for p in tight.values() for o in p.orders)
    broke = plan(cases, cash=-5.0)
    assert all(p.orders == [] for p in broke.values())


def test_a_sale_of_shares_held_is_neither_shrunk_nor_capped() -> None:
    inp = make_inputs("btc")
    cases = {"btc": (inp, sell("Up", held=40.0)),
             **{a: (make_inputs(a), buy()) for a in ("eth", "sol", "xrp")}}
    plans = plan(cases, held={"btc": {"Up": 40.0}}, max_order_usd=5.0, cash=0.0)
    p = plans["btc"]
    (o,) = p.orders
    assert (o.kind, o.order_side, o.side, o.token_id) == ("hedge", "SELL", "Up", inp.up_token)
    assert o.price >= inp.up_book.best_ask and o.shares == 25.0  # not capped at $5
    assert o.levels_ahead == inp.up_book.asks and o.depth_ahead == 120.0  # asks at 55c and 57c
    assert (p.held_side, p.held_shares) == ("Up", 40.0)
    # No free cash: the other coins' buys go, the sale stays (it spends no cash).
    assert all(plans[a].orders == [] for a in ("eth", "sol", "xrp"))
    # Never more than the shares held.
    (o,) = plan({"btc": (inp, sell("Up", held=40.0, level=(0.57, 0.8, 0.3, 90.0)))},
                held={"btc": {"Up": 40.0}})["btc"].orders
    assert o.shares == 40.0


def test_the_position_record_says_what_the_maths_does_with_it() -> None:
    inp = make_inputs("btc")
    selling = plan({"btc": (inp, sell())}, held={"btc": {"Up": 40.0}})["btc"]
    record = selling.position_record(sell())
    assert record["held_side"] == "Up" and record["sell"]["shares"] == 25.0
    keeping = plan({"btc": (inp, nothing("Up", 40.0))}, held={"btc": {"Up": 40.0}})["btc"]
    assert "Keeping the shares pays more" in keeping.position_record(
        Decision(p_model=0.6, p=0.6, options=(ParentOrder("sell", "Up", (), 0.0),),
                 held_side="Up", held_shares=40.0))["note"]
    assert "switched off" in keeping.position_record(nothing("Up", 40.0))["note"]
    assert "Neither adding to the 40.00 Up shares held nor selling them" in keeping.notes[0]
    fresh = plan({"btc": (inp, nothing())})["btc"]
    assert fresh.position_record(nothing()) is None
    assert "Neither side adds expected growth" in fresh.notes[0]
    # A price range set the wrong way round has no price level at all: the card says so.
    empty = plan({"btc": (inp, nothing())}, band_lo=0.10, band_hi=0.05)["btc"]
    assert "no price level to rest a child order at" in empty.notes[0]


def test_each_coin_sizes_from_the_cash_plus_the_other_coins_positions() -> None:
    btc, eth = make_inputs("btc"), make_inputs("eth")
    by_window = {btc.window_slug: {"Up": 20.0}, eth.window_slug: {"Down": 10.0}}
    got = rn.bankrolls_for({"btc": btc, "eth": eth}, by_window, 50.0)
    assert got["btc"] == pytest.approx(50.0 + 10.0 * eth.down_book.mid)
    assert got["eth"] == pytest.approx(50.0 + 20.0 * btc.up_book.mid)


# ---------------------------------------------------------------------------
# One pass
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_switch_off_still_checks_fills_and_settles(fade_db, venue, monkeypatch) -> None:
    # An ended window with a paper order the tape filled, and a live window with one resting.
    start2 = END + 900
    now = END + 1000
    await _strategies.set_enabled("fade_1h_momentum_15m", False)
    calls: list = []
    fake_gather(monkeypatch, {}, calls)
    hub = FakeHub()
    runner = rn.Runner(venue, hub_fn=lambda: hub, clock=lambda: now)
    await runner.pass_once()  # setup (restart recovery) runs with the switch off too

    await save_window("btc")
    await save_window("btc", start2)
    (filled_id,) = (await ledger.place_orders(
        [NewOrder(slug_of("btc"), up_of("btc"), "Up", "entry", 0.45, 10.0)],
        ts=START + 10)).ids
    (resting_id,) = (await ledger.place_orders(
        [NewOrder(slug_of("btc", start2), down_of("btc", start2), "Down", "entry", 0.40,
                  10.0)], ts=start2 + 10)).ids
    tape_sale(venue, "btc", START + 100, 0.44, 20.0)
    # The newer window's tape has got past the old window's end: its stretch is complete.
    tape_buy(venue, "btc", END + 950, 0.30, 5.0, start=start2, side="Down")
    venue.resolve(cid_of("btc"), up=up_of("btc"), down=down_of("btc"), winner="Up")

    report = await runner.pass_once()

    orders = {o["id"]: o for o in await all_orders()}
    assert orders[filled_id]["filled_shares"] == pytest.approx(10.0)
    assert orders[filled_id]["pnl"] == pytest.approx(10.0 * (1 - 0.45))
    assert (await ledger.get_window(slug_of("btc")))["outcome"] == "Up"
    assert orders[resting_id]["state"] == "cancelled"
    assert orders[resting_id]["cancel_reason"] == "switched_off"
    assert report.fills == 1 and report.settled == 1, report.errors
    assert calls == [] and await decision_rows() == []  # nothing new was looked at
    assert hub.released and set(hub.released) == {rn.OWNER}
    status = rn.status()
    assert status["state"] == "switched_off" and status["settled"] == 1
    stored = json.loads(await _db.get_config(rn.STATUS_KEY))
    assert stored["state"] == "switched_off"
    feed = await _feed_events()
    assert rn.FILL_EVENT in feed and rn.SETTLED_EVENT in feed


async def _feed_events() -> list[str]:
    async with _db.connect() as conn:
        cur = await conn.execute("SELECT event_type FROM notification_feed")
        return [r[0] for r in await cur.fetchall()]


@pytest.mark.asyncio
async def test_no_decision_places_nothing_and_records_every_coin(fade_db, venue,
                                                                  monkeypatch) -> None:
    for asset in ("btc", "eth", "xrp"):
        await save_window(asset)
    await _knobs.set("fade1h_trade_xrp", False)
    problem = Problem(asset="sol", code="price_stale", ts=NOW, window_slug=slug_of("sol"),
                      message="The newest Chainlink print for SOL is 9 s old.")
    fake_gather(monkeypatch, {"btc": make_inputs("btc"), "eth": make_inputs("eth"),
                              "sol": problem, "xrp": make_inputs("xrp")})
    fake_decide(monkeypatch, {"eth": _decide.NoDecision("The window has ended.")})

    report = await rn.Runner(venue, hub_fn=FakeHub, clock=lambda: NOW).pass_once()

    assert await all_orders() == []
    rows = {r["asset"]: r for r in await decision_rows()}
    assert set(rows) == set(rn.ASSETS)
    assert rows["btc"]["action"] == rn.NO_MODEL and rows["btc"]["reason"] == rn.NO_MODEL_REASON
    assert rows["eth"]["action"] == rn.NO_MODEL
    assert "cannot price this window: The window has ended." in rows["eth"]["reason"]
    assert rows["sol"]["action"] == rn.NO_INPUTS and "9 s old" in rows["sol"]["reason"]
    assert rows["xrp"]["action"] == rn.COIN_OFF
    assert rows["btc"]["inputs"]["status"] == "ok"
    assert rows["btc"]["inputs"]["window_slug"] == slug_of("btc")
    assert rows["btc"]["inputs"]["settings"]["bankroll_usd"] == 100.0
    # Setup seeded the prior (version 0) and the starting dials (version 1).
    assert rows["btc"]["mode"] == ex.PAPER_STATE and rows["btc"]["dials_version"] == 1
    assert rows["sol"]["inputs"]["code"] == "price_stale"
    assert report.errors == []
    status = rn.status()
    assert status["state"] == "running" and status["last_error"] is None
    assert status["assets"]["btc"]["action"] == rn.NO_MODEL
    assert status["bankroll"]["free_usd"] == pytest.approx(100.0)


@pytest.mark.asyncio
async def test_an_order_rests_then_keeps_its_place_then_is_cancelled(fade_db, venue,
                                                                      monkeypatch) -> None:
    await save_window("btc")
    inp = make_inputs("btc")
    fake_gather(monkeypatch, only_btc(inp))
    calls: list = []
    fake_decide(monkeypatch, {"btc": buy()}, calls)
    clock = {"now": NOW}
    runner = rn.Runner(venue, hub_fn=FakeHub, clock=lambda: clock["now"])

    await runner.pass_once()
    placed = await all_orders()
    assert [(o["price"], o["shares"], o["level"]) for o in placed] == [(0.53, 30.0, 0),
                                                                         (0.50, 12.0, 2)]
    assert all(o["state"] == "resting" and o["order_side"] == "BUY" for o in placed)
    assert all(o["placed_ts"] == NOW + 1 for o in placed)  # the second after the write
    (row,) = [r for r in await decision_rows() if r["asset"] == "btc"]
    assert row["action"] == rn.ORDERS and row["side"] == "Up"
    assert {o["decision_id"] for o in placed} == {row["id"]}
    assert [c["price"] for c in row["child_orders"]] == [o["price"] for o in placed]
    assert row["child_orders"][0]["p_fill"] == 0.90
    assert row["factors"]["model"] == FACTORS
    assert row["factors"]["sizing"]["joint_scale"] == 1.0
    assert row["stake_usd"] == pytest.approx(0.53 * 30 + 0.50 * 12)
    # The depth recorded is the book at our price or better, price level by price level.
    assert json.loads(placed[0]["levels_ahead_json"]) == [[0.53, 100.0]]
    assert json.loads(placed[1]["levels_ahead_json"]) == [[0.53, 100.0], [0.5, 50.0]]
    # The model was sized from the free cash, with nothing held yet.
    assert calls[-1]["held"] == {} and calls[-1]["bankroll"] == pytest.approx(100.0)
    entry = rn.status()["assets"]["btc"]
    assert (entry["kept"], entry["placed"], entry["cancelled"]) == (0, 2, 0)

    # The same plan a minute later: every order keeps its place in the queue.
    clock["now"] = NOW + 60
    fake_gather(monkeypatch, only_btc(make_inputs("btc", now=NOW + 60), NOW + 60))
    await runner.pass_once()
    assert [(o["id"], o["state"]) for o in await all_orders()] == \
        [(o["id"], "resting") for o in placed]
    entry = rn.status()["assets"]["btc"]
    assert (entry["kept"], entry["placed"], entry["cancelled"]) == (2, 0, 0)

    # A bigger plan at 53c adds one child order for the difference; the first keeps its place.
    clock["now"] = NOW + 120
    bigger = ((0.53, 0.9, 0.7, 40.0), (0.52, 0.8, 0.68, 0.0), (0.50, 0.6, 0.64, 12.0))
    fake_decide(monkeypatch, {"btc": buy(levels=bigger)})
    await runner.pass_once()
    after = await all_orders()
    assert [o["state"] for o in after[:2]] == ["resting", "resting"]
    assert [(o["price"], o["shares"]) for o in after[2:]] == [(0.53, 10.0)]

    # The model has nothing to say: the orders no longer have maths behind them.
    clock["now"] = NOW + 180
    fake_decide(monkeypatch, {})
    await runner.pass_once()
    after = await all_orders()
    assert all(o["state"] == "cancelled" and o["cancel_reason"] == rn.NO_MODEL for o in after)


@pytest.mark.asyncio
async def test_orders_are_stamped_when_written_not_when_the_pass_began(fade_db, venue,
                                                                       monkeypatch) -> None:
    await save_window("btc")
    clock = {"now": NOW}

    def slow_gather() -> None:
        clock["now"] = NOW + 8  # bookkeeping, knob reads and the gather took 8 s

    fake_gather(monkeypatch, only_btc(), on_call=slow_gather)
    fake_decide(monkeypatch, {"btc": buy(levels=((0.53, 0.9, 0.7, 30.0),))})
    runner = rn.Runner(venue, hub_fn=FakeHub, clock=lambda: clock["now"])
    await runner.pass_once()
    (order,) = await all_orders()
    assert order["placed_ts"] == NOW + 9

    # A seller hit the book three seconds into the pass, before our order existed; another
    # came after it. Only the second can reach our order (behind the 100 shares ahead of it).
    tape_sale(venue, "btc", NOW + 3, 0.50, 150.0)
    tape_sale(venue, "btc", NOW + 20, 0.50, 150.0)
    tape_buy(venue, "btc", NOW + 30, 0.60, 5.0)  # the tape has got past both
    clock["now"] = NOW + 60
    fake_gather(monkeypatch, only_btc(make_inputs("btc", now=NOW + 60), NOW + 60))
    fake_decide(monkeypatch, {"btc": nothing()})
    await runner.pass_once()
    (order,) = await all_orders()
    assert order["filled_shares"] == pytest.approx(30.0)
    assert order["filled_ts"] == NOW + 20  # never the trade from before the order existed


@pytest.mark.asyncio
async def test_a_filled_position_is_passed_to_the_model_and_reduced_by_a_sale(
        fade_db, venue, monkeypatch) -> None:
    await save_window("btc")
    calls: list = []
    fake_gather(monkeypatch, only_btc())
    fake_decide(monkeypatch, {"btc": buy(levels=((0.53, 0.9, 0.7, 30.0),))}, calls)
    clock = {"now": NOW}
    runner = rn.Runner(venue, hub_fn=FakeHub, clock=lambda: clock["now"])
    await runner.pass_once()
    tape_sale(venue, "btc", NOW + 10, 0.52, 200.0)  # fills all 30 behind the 100 ahead
    tape_buy(venue, "btc", NOW + 40, 0.70, 5.0)

    # The market turns: the model sells some of the 30 Up shares held.
    clock["now"] = NOW + 60
    turned = make_inputs("btc", now=NOW + 60, up_bid=0.40, up_ask=0.42, chainlink=100.0)
    fake_gather(monkeypatch, only_btc(turned, NOW + 60))
    fake_decide(monkeypatch, {"btc": sell("Up", held=30.0, level=(0.44, 0.8, 0.3, 20.0))},
                calls)
    report = await runner.pass_once()
    assert report.fills == 1
    assert calls[-1]["held"] == {"Up": 30.0}
    assert calls[-1]["bankroll"] == pytest.approx(100.0 - 30 * 0.53)
    orders = await all_orders()
    (sale,) = [o for o in orders if o["order_side"] == "SELL"]
    assert (sale["side"], sale["token_id"], sale["kind"]) == ("Up", turned.up_token, "hedge")
    assert sale["price"] == 0.44 and sale["shares"] == 20.0 and sale["state"] == "resting"
    assert not any(o["side"] == "Down" for o in orders)  # never the other side
    (row,) = [r for r in await decision_rows() if r["window_slug"] == slug_of("btc")][-1:]
    assert row["hedge"]["held_side"] == "Up" and row["hedge"]["sell"]["shares"] == 20.0
    assert row["stake_usd"] == 0.0

    # A taker buys Up at 45c: the sale fills and the position falls.
    # The 80 shares offered at 42c and the 40 at 44c ahead of us go first.
    tape_buy(venue, "btc", NOW + 70, 0.45, 80.0 + 40.0 + 20.0)
    tape_buy(venue, "btc", NOW + 90, 0.70, 1.0)
    clock["now"] = NOW + 120
    fake_decide(monkeypatch, {"btc": nothing("Up", 10.0)}, calls)
    await runner.pass_once()
    (position,) = await ledger.open_positions()
    assert position["shares"] == pytest.approx(10.0) and position["sold_shares"] == 20.0
    assert calls[-1]["held"] == {"Up": pytest.approx(10.0)}


@pytest.mark.asyncio
async def test_the_real_model_sells_a_position_that_turned_and_never_buys_the_other_side(
        fade_db, venue, monkeypatch) -> None:
    await save_window("btc")
    fake_gather(monkeypatch, only_btc())
    clock = {"now": NOW}
    runner = rn.Runner(venue, hub_fn=FakeHub, clock=lambda: clock["now"])
    await runner.pass_once()  # the real model with the starting dials: it buys Up
    bought = await all_orders()
    assert bought and all(o["side"] == "Up" and o["order_side"] == "BUY" for o in bought)
    tape_sale(venue, "btc", NOW + 10, 0.40, 500.0)
    tape_buy(venue, "btc", NOW + 40, 0.70, 5.0)

    clock["now"] = NOW + 60
    turned = make_inputs("btc", now=NOW + 60, up_bid=0.30, up_ask=0.32, chainlink=99.95)
    fake_gather(monkeypatch, only_btc(turned, NOW + 60))
    report = await runner.pass_once()
    assert report.fills >= 1, report.errors
    orders = await all_orders()
    sales = [o for o in orders if o["order_side"] == "SELL"]
    assert sales and all(o["side"] == "Up" and o["price"] > turned.up_book.best_bid
                         for o in sales)
    assert not any(o["side"] == "Down" for o in orders)
    held = sum(o["filled_shares"] for o in orders if o["order_side"] == "BUY")
    assert sum(o["shares"] for o in sales) <= held + 1e-9
    btc = rn.status()["assets"]["btc"]
    assert btc["order_action"] == "sell" and btc["position"]["held_side"] == "Up"


@pytest.mark.asyncio
async def test_a_buy_of_the_other_side_waits_and_the_card_says_why(fade_db, venue,
                                                                     monkeypatch) -> None:
    await save_window("btc")
    fake_gather(monkeypatch, only_btc())
    fake_decide(monkeypatch, {"btc": buy()})
    clock = {"now": NOW}
    runner = rn.Runner(venue, hub_fn=FakeHub, clock=lambda: clock["now"])
    await runner.pass_once()

    # The model now prefers Down. The Up orders are cancelled, but their tape is not read
    # yet, so they may have filled: the Down buys wait rather than risk holding both sides.
    clock["now"] = NOW + 60
    fake_decide(monkeypatch, {"btc": buy("Down", DOWN_LEVELS, p=0.3)})
    report = await runner.pass_once()
    orders = await all_orders()
    assert all(o["side"] == "Up" and o["state"] == "cancelled" for o in orders)
    entry = rn.status()["assets"]["btc"]
    assert entry["placed"] == 0 and entry["cancelled"] == 2
    assert entry["held_back"] and "never holds both sides" in entry["held_back"][0]
    assert report.cancelled == 2


@pytest.mark.asyncio
async def test_orders_left_in_an_earlier_window_are_stopped(fade_db, venue,
                                                            monkeypatch) -> None:
    start2 = END
    await save_window("btc")
    await save_window("btc", start2)
    (old,) = (await ledger.place_orders(
        [NewOrder(slug_of("btc"), up_of("btc"), "Up", "entry", 0.45, 10.0)],
        ts=START + 10)).ids
    now = start2 + 60
    runner = rn.Runner(venue, hub_fn=FakeHub, clock=lambda: now)

    async def no_bookkeeping(**kwargs):  # noqa: ANN003 - the expiry sweep did not run
        return ex.FillReport()

    await runner.pass_once()  # setup stops what a previous run left: place it again after
    (old,) = (await ledger.place_orders(
        [NewOrder(slug_of("btc"), up_of("btc"), "Up", "entry", 0.44, 10.0)],
        ts=START + 20)).ids
    monkeypatch.setattr(runner.bookkeeper, "sync_fills", no_bookkeeping)
    fake_gather(monkeypatch, only_btc(make_inputs("btc", now=now, start=start2), now))
    fake_decide(monkeypatch, {"btc": nothing()})
    await runner.pass_once()
    row = next(o for o in await all_orders() if o["id"] == old)
    assert row["state"] in ("cancelled", "expired") and row["cancelled_ts"] <= END


@pytest.mark.asyncio
async def test_the_model_rests_orders_and_a_settled_window_teaches_the_dials(
        fade_db, venue, monkeypatch) -> None:
    await save_window("btc")
    fake_gather(monkeypatch, only_btc())
    clock = {"now": NOW}
    runner = rn.Runner(venue, hub_fn=FakeHub, clock=lambda: clock["now"])

    report = await runner.pass_once()  # the real model, with the starting dials

    assert report.errors == [] and report.dials_version == 1
    (row,) = [r for r in await decision_rows() if r["asset"] == "btc"]
    assert row["action"] == rn.ORDERS and row["side"] == "Up"
    assert 0.0 < row["p"] < 1.0 and 0.0 < row["p_model"] < 1.0
    assert row["factors"]["explanation"].startswith("BTC's 15m leg is up")
    assert "leg_pts" in row["factors"]["model"]
    assert row["factors"]["model"]["growth_buy_up"] > 0.0
    assert row["factors"]["model"]["growth_buy_down"] == 0.0
    placed = await all_orders()
    assert placed and all(o["state"] == "resting" for o in placed)
    assert all(o["side"] == "Up" and o["price"] <= 0.53 for o in placed)  # at or under the bid
    assert rn.status()["assets"]["btc"]["explanation"] == row["factors"]["explanation"]

    # The window ends and settles Up: the learner takes a step and stores the dials.
    venue.resolve(cid_of("btc"), up=up_of("btc"), down=down_of("btc"), winner="Up")
    await save_window("btc", END)  # the next window's tape has got past this one's end
    tape_buy(venue, "btc", END + 100, 0.5, 5.0, start=END)
    clock["now"] = END + 950
    fake_gather(monkeypatch, {a: Problem(asset=a, code="no_hub", ts=END + 950,
                                         message="No hub.") for a in rn.ASSETS})
    report = await runner.pass_once()
    assert report.settled == 1 and report.learned == 1, (report.errors, report.settle_waiting)
    dials = await ledger.dials()
    assert dials["version"] == 2 and dials["source"] == "live"
    assert report.dials_version == 2 and "BTC Up" in (report.learn_note or "")
    assert rn.status()["learned"] == 1


@pytest.mark.asyncio
async def test_unchanged_books_keep_their_orders_and_the_free_cash(fade_db, venue,
                                                                    monkeypatch) -> None:
    await save_window("btc")
    clock = {"now": NOW}
    runner = rn.Runner(venue, hub_fn=FakeHub, clock=lambda: clock["now"])
    free = []
    for i in range(4):  # the real model, the same books, one pass a second
        clock["now"] = NOW + i
        fake_gather(monkeypatch, only_btc(make_inputs("btc", now=NOW), NOW + i))
        await runner.pass_once()
        free.append(rn.status()["bankroll"]["free_usd"])
    orders = await all_orders()
    assert orders and all(o["state"] == "resting" for o in orders)  # none replaced
    assert free == [pytest.approx(100.0)] * 4  # nothing drains the cash


@pytest.mark.asyncio
async def test_as_time_runs_on_the_orders_keep_their_place_in_the_queue(fade_db, venue,
                                                                        monkeypatch) -> None:
    await save_window("btc")
    clock = {"now": NOW}
    runner = rn.Runner(venue, hub_fn=FakeHub, clock=lambda: clock["now"])
    first: list[int] = []
    for i in range(5):  # the real model, the same books, a pass a minute: the time left
        now = NOW + 60 * i  # shrinks, so the size the maths wants moves a little every pass
        clock["now"] = now
        fake_gather(monkeypatch, only_btc(make_inputs("btc", now=now), now))
        await runner.pass_once()
        entry = rn.status()["assets"]["btc"]
        assert entry["action"] == rn.ORDERS and entry["cancelled"] == 0, entry
        assert rn.status()["bankroll"]["free_usd"] == pytest.approx(100.0)
        if not first:
            first = [o["id"] for o in await all_orders()]
    orders = await all_orders()
    # The first child orders were never replaced; growth in the plan only ever added orders.
    assert all(o["state"] == "resting" for o in orders)
    assert [o["id"] for o in orders][:len(first)] == first
    assert len(orders) < 5  # not a new row every pass


@pytest.mark.asyncio
async def test_input_warnings_are_errors_of_the_pass_and_notes_reach_the_card(
        fade_db, venue, monkeypatch) -> None:
    await save_window("btc")
    unsaved = ("Could not save this window's row, so it cannot be settled or learned from "
               "until a later pass saves it: RuntimeError: disk full")
    tick = "The books carry no tick size; using the venue's standard 0.01."
    inp = dataclasses.replace(make_inputs("btc"), warnings=(unsaved,), notes=(tick,))
    problem = Problem(asset="eth", code="price_stale", ts=NOW, message="Stale.",
                      warnings=("Could not read this window's stored start reference: x",))
    fake_gather(monkeypatch, {**only_btc(inp), "eth": problem})
    fake_decide(monkeypatch, {"btc": buy()})
    report = await rn.Runner(venue, hub_fn=FakeHub, clock=lambda: NOW).pass_once()
    assert f"BTC: {unsaved}" in report.errors
    assert any(e.startswith("ETH: Could not read") for e in report.errors)
    status = rn.status()
    assert status["last_error"] and f"BTC: {unsaved}" in status["errors"]
    btc = status["assets"]["btc"]
    assert btc["warnings"] == [unsaved] and btc["notes"] == [tick]
    assert status["assets"]["eth"]["warnings"] and status["assets"]["eth"]["codes"]
    # The card's per-coin entry carries the sizing as well as the plan.
    assert btc["sizing"]["bankroll_usd"] == pytest.approx(100.0)
    assert btc["sizing"]["joint_scale"] == 1.0 and btc["orders"][0]["kind"] == "entry"
    (row,) = [r for r in await decision_rows() if r["asset"] == "btc"]
    assert row["inputs"]["warnings"] == [unsaved] and row["inputs"]["notes"] == [tick]


@pytest.mark.asyncio
async def test_live_selected_places_nothing_and_says_so(fade_db, venue, monkeypatch) -> None:
    await _db.set_config(ex.MODE_KEY, "live")
    await save_window("btc")
    fake_gather(monkeypatch, {"btc": make_inputs("btc")})
    fake_decide(monkeypatch, {"btc": buy()})

    report = await rn.Runner(venue, hub_fn=FakeHub, clock=lambda: NOW).pass_once()

    assert await all_orders() == []
    btc = next(r for r in await decision_rows() if r["asset"] == "btc")
    assert btc["action"] == rn.NOT_PLACED and "not authorised" in btc["reason"]
    assert btc["mode"] == ex.LIVE_STATE and btc["child_orders"]  # the plan is recorded
    assert report.state == ex.LIVE_STATE
    assert rn.status()["executor"]["can_place"] is False


@pytest.mark.asyncio
async def test_a_raising_dependency_is_caught_and_shown(fade_db, venue, monkeypatch) -> None:
    runner = rn.Runner(venue, hub_fn=FakeHub, clock=lambda: NOW)

    async def boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("tape service down")

    async def mode_boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("mode store down")

    monkeypatch.setattr(runner.bookkeeper, "sync_fills", boom)
    monkeypatch.setattr(ex, "choose_executor", mode_boom)
    fake_gather(monkeypatch, {"btc": make_inputs("btc")})
    fake_decide(monkeypatch, {"btc": ValueError("model blew up")})

    report = await runner.pass_once()  # does not raise

    text = " | ".join(report.errors)
    assert "Checking fills failed: RuntimeError: tape service down" in text
    assert "Choosing the executor failed: RuntimeError: mode store down" in text
    assert report.state == "no_executor"
    btc = next(r for r in await decision_rows() if r["asset"] == "btc")
    assert btc["action"] == rn.MODEL_ERROR and "model blew up" in btc["reason"]
    status = rn.status()
    assert status["last_error"] and status["last_error_ts"] == NOW
    assert len(status["errors"]) == 2  # every error of the pass, not only the last
    stored = json.loads(await _db.get_config(rn.STATUS_KEY))
    assert any("tape service down" in e for e in stored["errors"])

    # The switch itself cannot be read: fail closed (nothing new), and say so.
    async def switch_boom(name):  # noqa: ANN001
        raise RuntimeError("config table locked")

    monkeypatch.setattr(_strategies, "enabled", switch_boom)
    report = await runner.pass_once()
    assert report.state == "switched_off"
    assert any("strategy switch failed" in e for e in report.errors)


@pytest.mark.asyncio
async def test_a_failed_trading_step_stops_the_resting_orders(fade_db, venue,
                                                              monkeypatch) -> None:
    await save_window("btc")
    fake_gather(monkeypatch, only_btc())
    fake_decide(monkeypatch, {"btc": buy()})
    clock = {"now": NOW}
    runner = rn.Runner(venue, hub_fn=FakeHub, clock=lambda: clock["now"])
    await runner.pass_once()
    assert all(o["state"] == "resting" for o in await all_orders())

    async def broken() -> rn.Settings:
        raise RuntimeError("knob table locked")

    monkeypatch.setattr(rn, "read_settings", broken)
    clock["now"] = NOW + 60
    report = await runner.pass_once()
    assert any("The trading step failed: RuntimeError: knob table locked" in e
               for e in report.errors)
    after = await all_orders()
    assert after and all(o["state"] == "cancelled"
                         and o["cancel_reason"] == rn.TRADING_STEP_FAILED for o in after)


@pytest.mark.asyncio
async def test_an_order_the_executor_refuses_is_recorded(fade_db, venue, monkeypatch) -> None:
    await save_window("btc")
    fake_gather(monkeypatch, {"btc": make_inputs("btc")})
    fake_decide(monkeypatch, {"btc": buy()})
    clock = {"now": NOW}
    runner = rn.Runner(venue, hub_fn=FakeHub, clock=lambda: clock["now"])
    await runner.pass_once()  # setup, and the first parent order
    Path(_config.KILL_SWITCH_PATH).write_text("stop")  # appears after the executor was chosen

    async def keep_paper(client, **kwargs):  # noqa: ANN001, ANN003
        return ex.ExecutorChoice("paper", ex.PAPER_STATE, "paper",
                                 ex.PaperExecutor(client, bookkeeper=runner.bookkeeper),
                                 runner.bookkeeper)

    monkeypatch.setattr(ex, "choose_executor", keep_paper)
    clock["now"] = NOW + 60
    moved = ((0.52, 0.9, 0.7, 30.0),)  # a new price level to place
    fake_decide(monkeypatch, {"btc": buy(levels=moved)})
    report = await runner.pass_once()
    rows = [r for r in await decision_rows() if r["asset"] == "btc"]
    assert rows[-1]["action"] == rn.REFUSED and "kill_switch" in rows[-1]["reason"]
    assert rows[-2]["action"] == rn.ORDERS and rows[-2]["child_orders"][0]["price"] == 0.52
    assert any("refused" in e for e in report.errors)
    assert all(o["price"] in (0.53, 0.50) and o["state"] == "resting"
               for o in await all_orders())  # nothing was written, the cancels included


# ---------------------------------------------------------------------------
# The loop and teardown
# ---------------------------------------------------------------------------


@pytest.fixture
def real_loop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """The real run_forever (conftest idles it for the dashboard tests)."""
    loop = getattr(rn, "real_run_forever", rn.run_forever)
    monkeypatch.setattr(rn, "MIN_SLEEP_S", 0.01)
    monkeypatch.setattr(rn, "_STATUS", {"state": "not_started"})
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "loop.db")

    async def quick() -> float:
        return 0.01

    monkeypatch.setattr(rn, "read_poll_interval", quick)
    return loop


@pytest.mark.asyncio
async def test_stop_ends_the_loop_and_releases_the_market_data(real_loop, monkeypatch) -> None:
    await _db.init_db()
    hub = FakeHub()
    monkeypatch.setattr(rn, "_current_hub", lambda: hub)
    passes: list[int] = []
    first = asyncio.Event()

    async def one_pass(self):  # noqa: ANN001
        passes.append(1)
        first.set()
        raise RuntimeError("a pass that fails")  # the loop must carry on regardless

    monkeypatch.setattr(rn.Runner, "pass_once", one_pass)
    stop = asyncio.Event()
    task = asyncio.create_task(real_loop(stop))
    await asyncio.wait_for(first.wait(), 2)
    await asyncio.sleep(0.05)
    # A pass that fails past its own guards is on the card, and saved for the page.
    status = rn.status()
    assert status["state"] == rn.PASS_FAILED
    assert "A pass failed before it finished: RuntimeError: a pass that fails" in \
        status["last_error"]
    stored = json.loads(await _db.get_config(rn.STATUS_KEY))
    assert "a pass that fails" in stored["last_error"]
    stop.set()
    assert await asyncio.wait_for(task, 2) is None
    assert len(passes) >= 2
    assert hub.released and set(hub.released) == {rn.OWNER}
    assert rn.status()["state"] == "stopped"


@pytest.mark.asyncio
async def test_cancel_propagates_after_releasing_the_market_data(real_loop,
                                                                 monkeypatch) -> None:
    hub = FakeHub()
    monkeypatch.setattr(rn, "_current_hub", lambda: hub)
    first = asyncio.Event()

    async def one_pass(self):  # noqa: ANN001
        first.set()

    monkeypatch.setattr(rn.Runner, "pass_once", one_pass)
    task = asyncio.create_task(real_loop(asyncio.Event()))
    await asyncio.wait_for(first.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert hub.released == [rn.OWNER]


@pytest.mark.asyncio
async def test_the_loop_never_raises_even_if_it_cannot_start(real_loop, monkeypatch) -> None:
    await _db.init_db()
    # The run before this one ended cleanly and saved its status.
    await _db.set_config(rn.STATUS_KEY, json.dumps(
        {"state": "running", "last_pass_ts": NOW - 600, "passes": 42, "last_error": None}))

    def broken_client(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("no sockets")

    monkeypatch.setattr(rn.httpx, "AsyncClient", broken_client)
    assert await asyncio.wait_for(real_loop(asyncio.Event()), 2) is None
    status = rn.status()
    assert status["state"] == "stopped_on_error" and "no sockets" in status["last_error"]
    assert status["last_error_ts"] is not None
    # Only the earlier run's last pass time is borrowed, for context.
    assert status["last_pass_ts"] == NOW - 600 and status["from_earlier_run"] is True
    stored = json.loads(await _db.get_config(rn.STATUS_KEY))
    assert "no sockets" in stored["last_error"] and stored["state"] == "stopped_on_error"

    # The card shows this process's dead loop, never the earlier run's clean status.
    from ems.dashboard import execution_view

    card = (await execution_view.fade_1h_data())["status"]
    assert card["state"] == "stopped_on_error" and "no sockets" in card["last_error"]


def test_the_runner_speaks_in_standard_terms() -> None:
    for module in (rn, _inputs, ledger, ex):
        source = Path(module.__file__).read_text().lower()
        assert not any(word in source for word in COINED), module.__name__
    from ems.fade_1h_momentum_15m import learner

    assert not any(word in Path(learner.__file__).read_text().lower() for word in COINED)
