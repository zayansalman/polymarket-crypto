"""Fade 1h Momentum on 15m: the runner loop, its sizing plan and its registration.

Runs against the real schema from ``db.init_db`` on a temp file. The venue (trade tape and
order-book service) is a small fake serving real ``httpx.Response`` objects; the inputs are
built directly, so no market-data hub or Binance call is involved.
"""

from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

import config as _config
import db as _db
from polymarket_bot import runtime_knobs as _knobs
from polymarket_bot import strategies as _strategies
from polymarket_bot.fade_1h_momentum_15m import decide as _decide
from polymarket_bot.fade_1h_momentum_15m import executor as ex
from polymarket_bot.fade_1h_momentum_15m import inputs as _inputs
from polymarket_bot.fade_1h_momentum_15m import ledger
from polymarket_bot.fade_1h_momentum_15m import runner as rn
from polymarket_bot.fade_1h_momentum_15m import sizing
from polymarket_bot.fade_1h_momentum_15m.decide import Decision, HedgeQuote, RungQuote
from polymarket_bot.fade_1h_momentum_15m.inputs import (
    Book, Inputs, PriceNow, Problem, WindowAverage,
)
from polymarket_bot.fade_1h_momentum_15m.ledger import NewOrder

HOUR = 1_789_934_400  # a UTC hour boundary
START = HOUR + 900  # the hour's second quarter
END = START + 900
NOW = START + 300


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
                up_ask: float = 0.55) -> Inputs:
    down_bid, down_ask = round(1 - up_ask, 6), round(1 - up_bid, 6)

    def book(side: str, token: str, bid: float, ask: float) -> Book:
        return Book(side=side, token_id=token, best_bid=bid, best_ask=ask, bid_size=100.0,
                    ask_size=100.0, bids=((bid, 100.0), (round(bid - 0.03, 6), 50.0)),
                    asks=((ask, 100.0),), source="stream", age_s=0.2)

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
        chainlink=price("chainlink", 100.25), binance=price("binance", 100.3),
        start_ref=100.1, start_ref_source="Gamma priceToBeat",
        window_avg=WindowAverage(value=100.15, log_value=math.log(100.15),
                                 through_s=int(now) - 2, seconds=299, printed=290,
                                 longest_gap_s=3),
        minute_returns=tuple(0.0005 * (-1) ** i for i in range(60)),
        minute_returns_end=float(now - now % 60), r15=tuple(0.001 for _ in range(12)),
    )


def a_decision(*, side: str = "Up", hedges: tuple[HedgeQuote, ...] = ()) -> Decision:
    return Decision(
        side=side, p_model=0.62, p=0.60,
        rungs=(RungQuote(0.54, 0.60, 0.62), RungQuote(0.50, 0.40, 0.58),
               RungQuote(0.45, 0.20, 0.55)),
        hedges=hedges, factors={"z_window": 0.2, "z_revert": 0.05},
        explanation="The window is up and the hour leans up.",
    )


def settings(**over) -> rn.Settings:
    base = dict(poll_s=60.0, bankroll_usd=100.0, kelly_multiplier=0.5, max_order_usd=25.0,
                band_lo=0.01, band_hi=0.15, hedge=True, spot_feed="chainlink_twap60",
                coins={a: True for a in rn.ASSETS})
    base.update(over)
    return rn.Settings(**base)


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


def fake_gather(monkeypatch: pytest.MonkeyPatch, results: dict, calls: list | None = None):
    async def _gather(hub, client, now, *, memory=None):  # noqa: ANN001
        if calls is not None:
            calls.append(now)
        return dict(results)

    monkeypatch.setattr(_inputs, "gather", _gather)


def fake_decide(monkeypatch: pytest.MonkeyPatch, by_asset: dict):
    def _decide_fn(inputs, dials, settings=_decide.DEFAULT_SETTINGS):  # noqa: ANN001
        value = by_asset.get(inputs.asset)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(_decide, "decide", _decide_fn)


async def all_orders() -> list[dict]:
    async with _db.connect() as conn:
        cur = await conn.execute("SELECT * FROM fade_orders ORDER BY id")
        return [dict(r) for r in await cur.fetchall()]


async def decision_rows() -> list[dict]:
    return list(reversed(await ledger.recent_decisions(100)))


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_the_switch_is_registered_and_on_by_default() -> None:
    strategy = _strategies.STRATEGIES["fade_1h_momentum_15m"]
    assert strategy.label == "Fade 1h Momentum on 15m"
    assert strategy.default is True


def test_the_settings_are_registered_in_their_own_group() -> None:
    names = [n for n, k in _knobs.KNOBS.items() if k.group == "Fade 1h Momentum on 15m"]
    for name in ("fade1h_poll_interval_seconds", "fade1h_bankroll_usd",
                 "fade1h_kelly_multiplier", "fade1h_max_order_usd", "fade1h_ladder_lo_cents",
                 "fade1h_ladder_hi_cents", "fade1h_hedge_enabled", "fade1h_spot_feed",
                 "fade1h_trade_btc", "fade1h_trade_eth", "fade1h_trade_sol",
                 "fade1h_trade_xrp"):
        assert name in names
    knobs = _knobs.KNOBS
    assert knobs["fade1h_poll_interval_seconds"].default == 60.0
    assert knobs["fade1h_bankroll_usd"].default == 100.0
    assert knobs["fade1h_kelly_multiplier"].default == 0.5
    assert (knobs["fade1h_ladder_lo_cents"].default, knobs["fade1h_ladder_hi_cents"].default) \
        == (1.0, 15.0)
    assert knobs["fade1h_hedge_enabled"].default is True
    assert knobs["fade1h_spot_feed"].choices == ("chainlink_twap60", "chainlink", "binance")
    for name in names:  # none clashes with the hand-written /api/runtime-config keys
        assert name not in ("max_trade_usd", "trade_shares", "market", "strategy")
        assert knobs[name].default is not None


def test_the_app_starts_the_runner_after_the_hub_and_stops_it() -> None:
    text = (Path(__file__).resolve().parents[2]
            / "polymarket_exec/ops/dashboard/app.py").read_text()
    assert text.index("_marketdata_hub.set_current(market_data)") < text.index("_run_fade(")
    assert "(fade_stop_event, fade_task)" in text


# ---------------------------------------------------------------------------
# The ladder grid and the Decision contract
# ---------------------------------------------------------------------------


def test_the_ladder_grid_is_the_band_under_the_ask() -> None:
    grid = _decide.ladder_prices(0.55, 0.01, 0.01, 0.15)
    assert grid[0] == 0.54 and grid[-1] == 0.40 and len(grid) == 15
    assert all(p < 0.55 for p in grid)
    # A tenth-of-a-cent tick keeps a whole-cent step on its own grid.
    assert _decide.ladder_prices(0.995, 0.001, 0.01, 0.03) == (0.985, 0.975, 0.965)
    assert _decide.ladder_prices(0.05, 0.01, 0.01, 0.15) == (0.04, 0.03, 0.02, 0.01)
    assert _decide.ladder_prices(0.55, 0.01, 0.15, 0.01) == ()  # an empty band


def test_the_prior_dials_follow_the_market_and_rest_nothing() -> None:
    inp = make_inputs("btc")
    dec = _decide.decide(inp, dict(ledger.PRIOR_DIALS))
    assert dec.p == pytest.approx(inp.market_up, abs=1e-12)
    plans = rn.plan_orders({"btc": (inp, dec)}, {}, bankroll_usd=100.0, rho=0.76,
                           settings=settings())
    assert plans["btc"].bids == []


def test_a_decision_checks_what_it_is_given() -> None:
    with pytest.raises(ValueError):
        Decision(side="Sideways", p_model=0.5, p=0.5)
    with pytest.raises(ValueError):
        Decision(side="Up", p_model=1.0, p=0.5)
    with pytest.raises(ValueError):
        RungQuote(0.5, 0.0, 0.5)
    with pytest.raises(ValueError):
        Decision(side="Up", p_model=0.5, p=0.5,
                 hedges=(HedgeQuote("Up", 0.4, 0.3), HedgeQuote("Up", 0.3, 0.2)))
    d = a_decision(hedges=(HedgeQuote("Up", 0.40, 0.3),))
    assert d.hedge_for("Up").side == "Down" and d.hedge_for("Down") is None


# ---------------------------------------------------------------------------
# Sizing plan (pure)
# ---------------------------------------------------------------------------


def test_the_plan_rests_every_rung_below_the_ask_on_the_decided_side() -> None:
    inp = make_inputs("btc")
    plans = rn.plan_orders({"btc": (inp, a_decision())}, {}, bankroll_usd=100.0, rho=0.76,
                           settings=settings())
    plan = plans["btc"]
    assert plan.entries, plan.notes
    for bid in plan.entries:
        assert bid.side == "Up" and bid.token_id == inp.up_token
        assert bid.price < inp.up_book.best_ask
        assert bid.shares >= 5.0
        assert bid.cost_usd <= 25.0 + 1e-9
    # One coin alone: the joint sizing changes nothing; half Kelly halves the full ladder.
    assert plan.joint_scale == pytest.approx(1.0)
    full = sizing.ladder([(r.price, r.p_fill, r.q_fill) for r in a_decision().rungs],
                         100.0, 1.0)
    assert plan.entry_cost_usd == pytest.approx(0.5 * sum(full), abs=0.05 * 3)


def test_correlated_coins_are_sized_down_together() -> None:
    one = rn.plan_orders({"btc": (make_inputs("btc"), a_decision())}, {},
                         bankroll_usd=100.0, rho=0.76, settings=settings())["btc"]
    cases = {a: (make_inputs(a), a_decision()) for a in rn.ASSETS}
    four = rn.plan_orders(cases, {}, bankroll_usd=100.0, rho=0.76, settings=settings())
    for asset in rn.ASSETS:
        assert four[asset].joint_scale < 0.6
        assert four[asset].entry_cost_usd < one.entry_cost_usd
    # Less correlation leaves more room for each coin.
    loose = rn.plan_orders(cases, {}, bankroll_usd=100.0, rho=0.0, settings=settings())
    assert loose["btc"].joint_scale > four["btc"].joint_scale


def test_rungs_off_the_grid_or_without_edge_get_nothing() -> None:
    inp = make_inputs("btc")
    off = Decision(side="Up", p_model=0.6, p=0.6,
                   rungs=(RungQuote(0.555, 0.6, 0.7), RungQuote(0.20, 0.1, 0.9)))
    plan = rn.plan_orders({"btc": (inp, off)}, {}, bankroll_usd=100.0, rho=0.76,
                          settings=settings())["btc"]
    assert plan.bids == [] and "not on the ladder grid" in " ".join(plan.notes)
    no_edge = Decision(side="Up", p_model=0.4, p=0.4, rungs=(RungQuote(0.54, 0.6, 0.40),))
    plan = rn.plan_orders({"btc": (inp, no_edge)}, {}, bankroll_usd=100.0, rho=0.76,
                          settings=settings())["btc"]
    assert plan.bids == [] and plan.full_kelly_usd == 0.0


def test_a_held_position_is_hedged_with_a_resting_bid_on_the_other_side() -> None:
    # The spec's check: 20 Up held, $100 cash, Up now 30% if the hedge fills, Down bid 65c.
    inp = make_inputs("btc", up_bid=0.30, up_ask=0.34)  # Down ask 0.70
    dec = Decision(side="Up", p_model=0.3, p=0.3, hedges=(HedgeQuote("Up", 0.65, 0.30),))
    plan = rn.plan_orders({"btc": (inp, dec)}, {"btc": {"Up": 20.0}}, bankroll_usd=100.0,
                          rho=0.76, settings=settings(max_order_usd=1000.0))["btc"]
    (hedge,) = [b for b in plan.bids if b.kind == "hedge"]
    assert (hedge.side, hedge.token_id, hedge.price) == ("Down", inp.down_token, 0.65)
    assert hedge.shares == pytest.approx(43.51, abs=0.011)  # rounded down to the share step
    assert plan.hedge["optimal_shares"] == pytest.approx(43.52, abs=0.005)
    # Hedging off: no hedge bid.
    plan = rn.plan_orders({"btc": (inp, dec)}, {"btc": {"Up": 20.0}}, bankroll_usd=100.0,
                          rho=0.76, settings=settings(hedge=False))["btc"]
    assert plan.bids == []


def test_a_hedge_quote_at_the_ask_is_never_placed() -> None:
    inp = make_inputs("btc")  # Down ask 0.47
    dec = Decision(side="Up", p_model=0.3, p=0.3, hedges=(HedgeQuote("Up", 0.47, 0.30),))
    plan = rn.plan_orders({"btc": (inp, dec)}, {"btc": {"Up": 20.0}}, bankroll_usd=100.0,
                          rho=0.76, settings=settings())["btc"]
    assert plan.bids == [] and "only ever rests" in plan.hedge["note"]


def test_the_plan_never_costs_more_than_the_free_bankroll() -> None:
    cases = {a: (make_inputs(a), a_decision()) for a in rn.ASSETS}
    plans = rn.plan_orders(cases, {}, bankroll_usd=12.0, rho=0.0,
                           settings=settings(kelly_multiplier=1.0))
    assert sum(p.cost_usd for p in plans.values()) <= 12.0 + 1e-9
    none = rn.plan_orders(cases, {}, bankroll_usd=0.0, rho=0.76, settings=settings())
    assert all(p.bids == [] for p in none.values())


def test_diff_keeps_matching_bids_and_replaces_the_rest() -> None:
    bid = rn.PlannedBid(kind="entry", side="Up", token_id="T", price=0.5, shares=10.0,
                        rung=0, depth_ahead=0.0)
    other = rn.PlannedBid(kind="entry", side="Up", token_id="T", price=0.45, shares=8.0,
                          rung=1, depth_ahead=0.0)
    resting = [
        {"id": 1, "kind": "entry", "side": "Up", "token_id": "T", "price": 0.5,
         "shares": 12.0, "filled_shares": 2.0},  # 10 still unfilled: the same bid
        {"id": 2, "kind": "entry", "side": "Up", "token_id": "T", "price": 0.45,
         "shares": 5.0, "filled_shares": 0.0},  # a different size
        {"id": 3, "kind": "hedge", "side": "Down", "token_id": "D", "price": 0.3,
         "shares": 9.0, "filled_shares": 0.0},  # no longer planned
    ]
    keep, cancel, new = rn.diff_orders([bid, other], resting)
    assert keep == [1] and sorted(cancel) == [2, 3] and new == [other]


def test_free_bankroll_counts_bids_that_may_still_have_filled() -> None:
    summary = {"net_pnl_usd": 5.0, "open_exposure_usd": 10.0}
    pending = [
        {"state": "resting", "window_slug": "w1", "shares": 10.0, "filled_shares": 0.0,
         "price": 0.5},  # re-planned this pass: not a deduction
        {"state": "cancelled", "window_slug": "w1", "shares": 10.0, "filled_shares": 4.0,
         "price": 0.5},  # tape not read yet: 6 shares could still have filled
        {"state": "resting", "window_slug": "w2", "shares": 4.0, "filled_shares": 0.0,
         "price": 0.25},  # a coin not re-planned: cancelled this pass, may have filled
    ]
    assert rn.free_bankroll(100.0, summary, pending, {"w1"}) == pytest.approx(
        100.0 + 5.0 - 10.0 - 3.0 - 1.0)


# ---------------------------------------------------------------------------
# One pass
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_switch_off_still_checks_fills_and_settles(fade_db, venue, monkeypatch) -> None:
    # An ended window with a paper bid the tape filled, and a live window with a resting bid.
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
    (filled_id,) = await ledger.place_orders(
        [NewOrder(slug_of("btc"), up_of("btc"), "Up", "entry", 0.45, 10.0)], ts=START + 10)
    (resting_id,) = await ledger.place_orders(
        [NewOrder(slug_of("btc", start2), down_of("btc", start2), "Down", "entry", 0.40,
                  10.0)], ts=start2 + 10)
    venue.add(cid_of("btc"), timestamp=START + 100, side="SELL", asset=up_of("btc"),
              outcomeIndex=0, size=20.0, price=0.44)
    venue.resolve(cid_of("btc"), up=up_of("btc"), down=down_of("btc"), winner="Up")

    report = await runner.pass_once()

    orders = {o["id"]: o for o in await all_orders()}
    assert orders[filled_id]["filled_shares"] == pytest.approx(10.0)
    assert orders[filled_id]["pnl"] == pytest.approx(10.0 * (1 - 0.45))
    assert (await ledger.get_window(slug_of("btc")))["outcome"] == "Up"
    assert orders[resting_id]["state"] == "cancelled"
    assert orders[resting_id]["cancel_reason"] == "switched_off"
    assert report.fills == 1 and report.settled == 1
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
                      message="The newest Chainlink TWAP-60s print for SOL is 9 s old.")
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
async def test_a_decision_rests_a_ladder_then_keeps_it_then_cancels_it(fade_db, venue,
                                                                        monkeypatch) -> None:
    await save_window("btc")
    inp = make_inputs("btc")
    others = {a: Problem(asset=a, code="no_hub", ts=NOW, message="No hub.")
              for a in ("eth", "sol", "xrp")}
    fake_gather(monkeypatch, {"btc": inp, **others})
    fake_decide(monkeypatch, {"btc": a_decision()})
    clock = {"now": NOW}
    runner = rn.Runner(venue, hub_fn=FakeHub, clock=lambda: clock["now"])

    await runner.pass_once()
    placed = await all_orders()
    assert placed and all(o["state"] == "resting" for o in placed)
    assert all(o["side"] == "Up" and o["price"] < inp.up_book.best_ask for o in placed)
    (row,) = [r for r in await decision_rows() if r["asset"] == "btc"]
    assert row["action"] == rn.BID and row["side"] == "Up"
    assert {o["decision_id"] for o in placed} == {row["id"]}
    assert [r["price"] for r in row["ladder"]] == [o["price"] for o in placed]
    assert row["factors"]["model"] == {"z_window": 0.2, "z_revert": 0.05}
    assert row["factors"]["sizing"]["joint_scale"] == pytest.approx(1.0)
    # The queue depth recorded is the real book's size at our price or better.
    for o in placed:
        assert o["depth_ahead"] == pytest.approx(inp.up_book.depth_ahead(o["price"]))

    # Same inputs a minute later: the same plan, so every bid keeps its place in the queue.
    clock["now"] = NOW + 60
    fake_gather(monkeypatch, {"btc": make_inputs("btc", now=NOW + 60), **others})
    await runner.pass_once()
    assert [(o["id"], o["state"]) for o in await all_orders()] == \
        [(o["id"], "resting") for o in placed]

    # The model has nothing to say: the bids no longer have maths behind them.
    clock["now"] = NOW + 120
    fake_decide(monkeypatch, {})
    await runner.pass_once()
    after = await all_orders()
    assert all(o["state"] == "cancelled" and o["cancel_reason"] == rn.NO_MODEL for o in after)


@pytest.mark.asyncio
async def test_the_model_rests_a_paper_ladder_and_a_settled_window_teaches_the_dials(
        fade_db, venue, monkeypatch) -> None:
    await save_window("btc")
    others = {a: Problem(asset=a, code="no_hub", ts=NOW, message="No hub.")
              for a in ("eth", "sol", "xrp")}
    fake_gather(monkeypatch, {"btc": make_inputs("btc"), **others})
    clock = {"now": NOW}
    runner = rn.Runner(venue, hub_fn=FakeHub, clock=lambda: clock["now"])

    report = await runner.pass_once()  # the real model, with the starting dials

    assert report.errors == [] and report.dials_version == 1
    (row,) = [r for r in await decision_rows() if r["asset"] == "btc"]
    assert row["action"] == rn.BID and row["side"] == "Up"
    assert 0.0 < row["p"] < 1.0 and 0.0 < row["p_model"] < 1.0
    assert row["factors"]["explanation"].startswith("BTC's 15m leg is up")
    assert "leg_pts" in row["factors"]["model"]
    placed = await all_orders()
    assert placed and all(o["state"] == "resting" for o in placed)
    assert all(o["side"] == "Up" and o["price"] < 0.55 for o in placed)  # under the ask
    assert rn.status()["assets"]["btc"]["explanation"] == row["factors"]["explanation"]

    # The window ends and settles Up: the learner takes a step and stores the dials.
    venue.resolve(cid_of("btc"), up=up_of("btc"), down=down_of("btc"), winner="Up")
    clock["now"] = END + 950  # the empty tape counts as read once it is 15 minutes old
    fake_gather(monkeypatch, {a: Problem(asset=a, code="no_hub", ts=END + 950,
                                         message="No hub.") for a in rn.ASSETS})
    report = await runner.pass_once()
    assert report.settled == 1 and report.learned == 1, (report.errors, report.settle_waiting)
    dials = await ledger.dials()
    assert dials["version"] == 2 and dials["source"] == "live"
    assert report.dials_version == 2 and "BTC Up" in (report.learn_note or "")
    assert rn.status()["learned"] == 1


@pytest.mark.asyncio
async def test_live_selected_places_nothing_and_says_so(fade_db, venue, monkeypatch) -> None:
    await _db.set_config(ex.MODE_KEY, "live")
    await save_window("btc")
    fake_gather(monkeypatch, {"btc": make_inputs("btc")})
    fake_decide(monkeypatch, {"btc": a_decision()})

    report = await rn.Runner(venue, hub_fn=FakeHub, clock=lambda: NOW).pass_once()

    assert await all_orders() == []
    btc = next(r for r in await decision_rows() if r["asset"] == "btc")
    assert btc["action"] == rn.NOT_PLACED and "not authorised" in btc["reason"]
    assert btc["mode"] == ex.LIVE_STATE
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
async def test_a_bid_the_executor_refuses_is_recorded(fade_db, venue, monkeypatch) -> None:
    await save_window("btc")
    fake_gather(monkeypatch, {"btc": make_inputs("btc")})
    fake_decide(monkeypatch, {"btc": a_decision()})
    runner = rn.Runner(venue, hub_fn=FakeHub, clock=lambda: NOW)
    await runner.pass_once()  # setup, and the first ladder
    Path(_config.KILL_SWITCH_PATH).write_text("stop")  # appears after the executor was chosen

    async def keep_paper(client, **kwargs):  # noqa: ANN001, ANN003
        return ex.ExecutorChoice("paper", ex.PAPER_STATE, "paper",
                                 ex.PaperExecutor(client, bookkeeper=runner.bookkeeper),
                                 runner.bookkeeper)

    monkeypatch.setattr(ex, "choose_executor", keep_paper)
    fake_decide(monkeypatch, {"btc": a_decision(side="Down")})  # a new plan to place
    report = await runner.pass_once()
    rows = [r for r in await decision_rows() if r["asset"] == "btc"]
    assert rows[-1]["action"] == rn.REFUSED and "kill_switch" in rows[-1]["reason"]
    assert rows[-2]["action"] == rn.BID and rows[-2]["side"] == "Down"
    assert any("refused" in e for e in report.errors)
    assert all(o["side"] == "Up" and o["state"] == "resting" for o in await all_orders())


# ---------------------------------------------------------------------------
# The loop and teardown
# ---------------------------------------------------------------------------


@pytest.fixture
def real_loop(monkeypatch: pytest.MonkeyPatch):
    """The real run_forever (conftest idles it for the dashboard tests)."""
    loop = getattr(rn, "real_run_forever", rn.run_forever)
    monkeypatch.setattr(rn, "MIN_SLEEP_S", 0.01)

    async def quick() -> float:
        return 0.01

    monkeypatch.setattr(rn, "read_poll_interval", quick)
    return loop


@pytest.mark.asyncio
async def test_stop_ends_the_loop_and_releases_the_market_data(real_loop, monkeypatch) -> None:
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
    def broken_client(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("no sockets")

    monkeypatch.setattr(rn.httpx, "AsyncClient", broken_client)
    assert await asyncio.wait_for(real_loop(asyncio.Event()), 2) is None
    status = rn.status()
    assert status["state"] == "stopped_on_error" and "no sockets" in status["last_error"]
