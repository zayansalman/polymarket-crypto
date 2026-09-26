"""Kelly horse-race runner (``ems/kelly_horse_race/runner.py``) against a fake hub, a fake
venue (tape, CLOB markets and /book, Gamma, Binance) and a temporary database.

One decision per window with the same order to each active endpoint; never above the best bid;
a gate block recorded; cancel before the window end with the credit back; settlement P&L; the
switch and the kill switch; inputs that wait, and inputs that never come.
"""

from __future__ import annotations

import asyncio
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from ems import config as _config
from ems import db as _db
from ems import runtime_knobs as _knobs
from ems import strategies as _strategies
from ems.execution import clob, live_control, resting
from ems.execution.controls import OutcomeUnknown
from ems.execution.live_control import LiveVenueHolder
from ems.execution.resting import OrderView, Placed, PlaceRequest
from ems.kelly_horse_race import inputs, ledger, maths
from ems.kelly_horse_race import runner as rn
from ems.marketdata.rtds_stream import PricePoint
from ems.marketdata.universe import MarketRef
from tests.unit.venue_fakes import FakeVenue, trade

pytestmark = pytest.mark.asyncio

START = 1_789_935_300  # a 15m window boundary
END = START + 900
NOW = START + 30
SLUG = f"btc-updown-15m-{START}"
CID, UP, DOWN = "0xcid", "UP-btc", "DN-btc"
K = 100_000.0


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def point(s: int, value: float) -> PricePoint:
    return PricePoint(source=inputs.TWAP60, asset="btc", value=value, obs_ms=s * 1000,
                      publish_ms=None, received_ms=s * 1000 + 2000)


@dataclass
class FakeHub:
    ref: MarketRef | None = None
    prints: list[PricePoint] = field(default_factory=list)
    wants: list[tuple] = field(default_factory=list)
    released: list[str] = field(default_factory=list)

    def want(self, asset: str, timeframe: str, owner: str, *, hot: bool = False) -> None:
        self.wants.append((asset, timeframe, owner, hot))

    def release(self, owner: str, asset: str | None = None, timeframe: str | None = None) -> int:
        self.released.append(owner)
        return 1

    def market(self, asset: str, timeframe: str, which: str = "current") -> MarketRef | None:
        return self.ref

    def prices(self, source: str, asset: str, seconds: float | None = None):
        return tuple(self.prints)

    def price(self, source: str, asset: str) -> PricePoint | None:
        return self.prints[-1] if self.prints else None


def make_hub(*, start: int = START, k: float = K, x: float = K, until: int = NOW - 1,
             skip_open: bool = False, cid: str | None = CID) -> FakeHub:
    ref = MarketRef(asset="btc", timeframe="15m", slug=f"btc-updown-15m-{start}",
                    window_start=float(start), window_end=float(start + 900), up_token=UP,
                    down_token=DOWN, condition_id=cid)
    prints = [point(s, k if s <= start else x) for s in range(start - 60, until + 1)
              if not (skip_open and s == start)]
    return FakeHub(ref=ref, prints=prints)


class FakeLive:
    """A live venue double: records every call, answers from what the test sets."""

    mode = "live"

    def __init__(self) -> None:
        self.placed: list[PlaceRequest] = []
        self.cancelled: list[tuple[list[str], str]] = []
        self.views: dict[str, OrderView] = {}
        self.refuse: Exception | None = None
        self.open_ids: list[str] = []  # what find_order finds, in turn
        self.find_error: Exception | None = None
        self.cancel_error: str | None = None
        self.last_errors: list[str] = []

    async def place(self, request: PlaceRequest, *, now: float | None = None) -> Placed:
        if self.refuse is not None:
            raise self.refuse
        self.placed.append(request)
        order_id = f"0xLIVE{len(self.placed)}"
        self.views[order_id] = OrderView(order_id, "resting", request.size, 0.0, None, False)
        return Placed(order_id=order_id, placed_ts=int(now or 0) + 1)

    async def find_order(self, *, token_id: str, price: float, size: float,
                         expires_ts: int) -> str | None:
        if self.find_error is not None:
            raise self.find_error
        return self.open_ids.pop(0) if self.open_ids else None

    async def cancel(self, order_ids, *, reason: str, now: float | None = None) -> int:
        ids = list(order_ids)
        self.cancelled.append((ids, reason))
        if self.cancel_error is not None:
            self.last_errors.append(self.cancel_error)
            return 0
        for i in ids:
            v = self.views[i]
            self.views[i] = OrderView(i, "cancelled", v.size, v.filled_size, int(now or 0), True)
        return len(ids)

    async def fills(self, order_ids, *, now: float | None = None) -> dict[str, OrderView]:
        return {i: self.views[i] for i in order_ids if i in self.views}


# The last hour's minute returns: up 0.2%, down 0.15%, in turn. P(Up) at the open is ~0.70.
STEPS = [0.002 if i % 2 == 0 else -0.0015 for i in range(60)]


def seed_venue(venue: FakeVenue, *, now: float = NOW) -> None:
    """61 completed Binance minutes before ``now`` following ``STEPS``, and the two books."""
    end = int((now - inputs.KLINE_SETTLE_S) // 60) * 60
    first = end - 60 * 61
    level, venue.closes = 100_000.0, {first: 100_000.0}
    for i, step in enumerate(STEPS, start=1):
        level *= math.exp(step)
        venue.closes[first + 60 * i] = level
    venue.book(UP, bids=[(0.50, 20.0), (0.49, 100.0)], asks=[(0.52, 30.0), (0.60, 5.0)])
    venue.book(DOWN, bids=[(0.47, 12.0), (0.40, 50.0)], asks=[(0.49, 30.0)])


def draws(*values: float):
    it = iter(values)
    calls: list[float] = []

    def rng() -> float:
        calls.append(0.0)
        return next(it)

    rng.calls = calls  # type: ignore[attr-defined]
    return rng


@pytest_asyncio.fixture
async def kelly_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "kelly.db")
    monkeypatch.setattr(_config, "KILL_SWITCH_PATH", tmp_path / "KILL")
    await _db.init_db()
    for name, knob in _knobs.KNOBS.items():
        _knobs._cache[name] = knob.default
    return tmp_path


@pytest.fixture
def venue() -> FakeVenue:
    v = FakeVenue()
    seed_venue(v)
    return v


def make_runner(venue: FakeVenue, hub: FakeHub, clock: dict, *, rng=None,
                live: FakeLive | None = None) -> rn.Runner:
    holder = None
    if live is not None:
        async def opener():
            return live

        holder = LiveVenueHolder(clock=lambda: clock["now"], opener=opener)
    return rn.Runner(venue, hub_fn=lambda: hub, clock=lambda: clock["now"],
                     rng=rng or draws(0.1, 0.5), live=holder)


@pytest.fixture
def armed(monkeypatch: pytest.MonkeyPatch):
    """LIVE selected and clicked in this process, with a wallet that passes."""

    async def arm() -> None:
        await live_control.select_mode("live", clicked=True)

    monkeypatch.setattr(clob, "wallet_problems", lambda: [])
    yield arm
    live_control.record_click("paper")


async def rows(table: str) -> list[dict[str, Any]]:
    async with _db.connect() as conn:
        async with conn.execute(f"SELECT * FROM {table} ORDER BY id") as cur:
            return [dict(r) for r in await cur.fetchall()]


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------


async def test_one_decision_per_window_with_every_input_recorded(kelly_db, venue) -> None:
    hub, clock = make_hub(), {"now": NOW}
    runner = make_runner(venue, hub, clock)
    report = await runner.pass_once()
    assert report.state == rn.RUNNING and not report.errors, report.errors
    (d,) = await rows("kelly_horse_race_decisions")
    assert d["window_slug"] == SLUG and d["k_price"] == K
    assert d["k_source"] == inputs.K_FROM_PRINT and d["x_price"] == K
    r60, sigma_h = maths.hour_moves(STEPS)
    assert d["r60"] == pytest.approx(r60) and d["sigma_h"] == pytest.approx(sigma_h)
    assert 0.6 < d["p_up"] < 0.8
    assert d["tau_h"] == pytest.approx((END - NOW) / 3600)
    expected = maths.chance_of_up(K, K, d["r60"], d["sigma_h"], d["tau_h"]).p_up
    assert d["p_up"] == pytest.approx(expected)
    assert (d["u1"], d["side"], d["u2"]) == (0.1, "Up", 0.5)
    assert (d["best_bid"], d["best_ask"], d["bid_size"]) == (0.50, 0.52, 20.0)
    assert d["price"] == 0.50 and d["reason"] is None
    assert d["shares"] == maths.draw_size(0.50, 5, 5.0, 0.5).shares
    # A second pass in the same window decides nothing new.
    clock["now"] = NOW + 5
    await runner.pass_once()
    assert len(await rows("kelly_horse_race_decisions")) == 1
    assert len(await rows("paper_resting_orders")) == 1


async def test_the_order_rests_at_the_best_bid_behind_the_queue(kelly_db, venue) -> None:
    await make_runner(venue, make_hub(), {"now": NOW}).pass_once()
    (order,) = await rows("kelly_horse_race_orders")
    (paper,) = await rows("paper_resting_orders")
    assert order["mode"] == "paper" and order["state"] == "resting"
    assert order["venue_order_id"] == "paper-1" and order["outcome_index"] == 0
    assert paper["price"] == 0.50 and paper["queue_ahead"] == 20.0
    assert paper["expires_ts"] == END and paper["token_id"] == UP
    commits = [e for e in await rows("risk_events") if e["kind"] == "commit"]
    assert commits and commits[0]["amount_usd"] == pytest.approx(order["notional_usd"])


async def test_a_down_draw_buys_down_at_its_own_bid(kelly_db, venue) -> None:
    await make_runner(venue, make_hub(), {"now": NOW}, rng=draws(0.99, 0.0)).pass_once()
    (d,) = await rows("kelly_horse_race_decisions")
    (order,) = await rows("kelly_horse_race_orders")
    assert d["side"] == "Down" and order["token_id"] == DOWN and order["price"] == 0.47
    assert order["size"] == 5.0 and order["outcome_index"] == 1


async def test_the_same_order_goes_to_paper_and_live(kelly_db, venue, armed) -> None:
    await armed()
    live = FakeLive()
    await _knobs.set("live_max_trade_usd", 5.0)
    await make_runner(venue, make_hub(), {"now": NOW}, live=live).pass_once()
    orders = await rows("kelly_horse_race_orders")
    assert [o["mode"] for o in orders] == ["paper", "live"]
    (sent,) = live.placed
    (paper,) = await rows("paper_resting_orders")
    assert (sent.token_id, sent.price, sent.size, sent.expires_ts, sent.queue_ahead) == (
        paper["token_id"], paper["price"], paper["size"], paper["expires_ts"],
        paper["queue_ahead"])
    assert {o["price"] for o in orders} == {0.50} and len({o["size"] for o in orders}) == 1


async def test_a_gate_block_is_recorded_against_its_mode(kelly_db, venue, armed) -> None:
    await armed()
    live = FakeLive()  # the live per-trade cap is $3 by default
    await make_runner(venue, make_hub(), {"now": NOW}, rng=draws(0.1, 0.99), live=live).pass_once()
    paper, blocked = await rows("kelly_horse_race_orders")
    assert paper["state"] == "resting"
    assert blocked["mode"] == "live" and blocked["state"] == "blocked"
    assert blocked["reason"].startswith("max_trade:") and blocked["venue_order_id"] is None
    assert live.placed == []


async def test_a_venue_refusal_is_recorded(kelly_db, venue, armed) -> None:
    await armed()
    live = FakeLive()
    live.refuse = resting.PlacementRefused("would_cross", "A bid at the ask.")
    await _knobs.set("live_max_trade_usd", 5.0)
    await make_runner(venue, make_hub(), {"now": NOW}, live=live).pass_once()
    _, refused = await rows("kelly_horse_race_orders")
    assert refused["state"] == "rejected" and refused["reason"].startswith("would_cross")


async def test_no_bid_records_the_window_with_no_order(kelly_db, venue) -> None:
    venue.book(UP, bids=[], asks=[(0.52, 30.0)])
    await make_runner(venue, make_hub(), {"now": NOW}).pass_once()
    (d,) = await rows("kelly_horse_race_decisions")
    assert d["reason"].startswith("no_bid") and d["side"] == "Up"
    assert await rows("kelly_horse_race_orders") == []


async def test_a_locked_book_records_the_window_with_no_order(kelly_db, venue) -> None:
    venue.book(UP, bids=[(0.52, 5.0)], asks=[(0.52, 30.0)])
    await make_runner(venue, make_hub(), {"now": NOW}).pass_once()
    (d,) = await rows("kelly_horse_race_decisions")
    assert d["reason"].startswith("book_locked")


async def test_a_minimum_order_over_the_cap_records_the_window(kelly_db, venue) -> None:
    await _knobs.set("kelly_horse_race_max_notional_usd", 2.0)
    await make_runner(venue, make_hub(), {"now": NOW}).pass_once()
    (d,) = await rows("kelly_horse_race_decisions")
    assert d["reason"].startswith("min_order_over_cap")


# ---------------------------------------------------------------------------
# Inputs that wait, and inputs that never come
# ---------------------------------------------------------------------------


async def test_waiting_never_rolls_the_die_again(kelly_db, venue) -> None:
    venue.klines_status = 503
    rng = draws(0.1, 0.5, 0.9, 0.9)
    hub, clock = make_hub(), {"now": NOW}
    runner = make_runner(venue, hub, clock, rng=rng)
    report = await runner.pass_once()
    assert report.window["waiting"] == "klines_failed"
    assert await rows("kelly_horse_race_decisions") == []
    venue.klines_status = 200
    clock["now"] = NOW + 5
    hub.prints.append(point(NOW + 4, K))
    await runner.pass_once()
    (d,) = await rows("kelly_horse_race_decisions")
    assert (d["u1"], d["u2"]) == (0.1, 0.5) and len(rng.calls) == 2


async def test_k_falls_back_to_gamma_when_the_open_print_is_not_held(kelly_db, venue) -> None:
    venue.price_to_beat[SLUG] = 99_950.0
    await make_runner(venue, make_hub(skip_open=True), {"now": NOW}).pass_once()
    (d,) = await rows("kelly_horse_race_decisions")
    assert d["k_price"] == 99_950.0 and d["k_source"] == inputs.K_FROM_GAMMA


async def test_k_waits_for_the_open_print_just_after_the_open(kelly_db, venue) -> None:
    hub = make_hub(until=START - 1)
    report = await make_runner(venue, hub, {"now": START + 3}).pass_once()
    assert report.window["waiting"] == "k_pending"


async def test_inputs_still_missing_at_the_cutoff_are_the_windows_reason(kelly_db, venue) -> None:
    hub, clock = make_hub(skip_open=True), {"now": NOW}
    runner = make_runner(venue, hub, clock)
    report = await runner.pass_once()
    assert report.window["waiting"] == "k_missing"
    clock["now"] = END - rn.DECISION_CUTOFF_S
    await runner.pass_once()
    (d,) = await rows("kelly_horse_race_decisions")
    assert d["reason"].startswith("k_missing") and d["side"] is None


async def test_a_window_joined_after_the_cutoff_is_left_alone(kelly_db, venue) -> None:
    hub = make_hub(until=END - 100)
    report = await make_runner(venue, hub, {"now": END - 100}).pass_once()
    assert report.window["waiting"] == "too_late"
    assert await rows("kelly_horse_race_decisions") == []


# ---------------------------------------------------------------------------
# Bookkeeping: fills, the cancel before the end, the credit, settlement
# ---------------------------------------------------------------------------


def sell_up(ts: int, size: float, price: float = 0.50) -> dict:
    return trade(ts, "Up", "SELL", size, price, cid=CID, up=UP, down=DOWN)


async def test_the_order_stops_before_the_end_and_credits_the_unfilled_notional(
    kelly_db, venue
) -> None:
    """The venue stops a GTD order 60 s before its expiry; once its tape is read through then,
    its unfilled notional goes back to the gate."""
    hub, clock = make_hub(), {"now": NOW}
    runner = make_runner(venue, hub, clock, rng=draws(0.1, 0.0))  # 5 shares at 0.50
    await runner.pass_once()
    venue.add(CID, sell_up(NOW + 60, 22.0), sell_up(NOW + 70, 1.0, price=0.99))  # 2 past queue
    clock["now"] = END - rn.CANCEL_LEAD_S
    await runner.pass_once()
    (order,) = await rows("kelly_horse_race_orders")
    assert order["state"] == "expired" and order["filled_size"] == pytest.approx(2.0)
    assert order["final"] == 0  # the tape has not shown the rest of its stretch yet
    venue.add(CID, sell_up(END + 30, 1.0, price=0.99))
    clock["now"] = END + 40
    await runner.pass_once()
    (order,) = await rows("kelly_horse_race_orders")
    assert order["final"] == 1 and order["credited"] == 1
    credit = [e for e in await rows("risk_events") if e["kind"] == "credit"]
    assert credit[0]["amount_usd"] == pytest.approx(1.5)


async def test_what_still_rests_at_the_cancel_lead_is_cancelled(kelly_db, venue,
                                                                armed) -> None:
    await armed()
    live = FakeLive()
    await _knobs.set("live_max_trade_usd", 5.0)
    hub, clock = make_hub(), {"now": NOW}
    runner = make_runner(venue, hub, clock, live=live)
    await runner.pass_once()
    clock["now"] = END - rn.CANCEL_LEAD_S
    report = await runner.pass_once()
    assert live.cancelled == [(["0xLIVE1"], "window_end")] and report.cancelled == 1
    orders = await rows("kelly_horse_race_orders")
    assert orders[1]["state"] == "cancelled" and orders[1]["credited"] == 0
    await runner.pass_once()
    orders = await rows("kelly_horse_race_orders")
    assert orders[1]["credited"] == 1


async def test_settlement_pays_one_minus_price_per_filled_share(kelly_db, venue) -> None:
    hub, clock = make_hub(), {"now": NOW}
    runner = make_runner(venue, hub, clock, rng=draws(0.1, 0.0))
    await runner.pass_once()
    venue.add(CID, sell_up(NOW + 60, 30.0), sell_up(NOW + 70, 1.0, price=0.99))
    clock["now"] = NOW + 100
    report = await runner.pass_once()
    assert [f["shares"] for f in report.fills] == [5.0]
    venue.add(CID, sell_up(END + 30, 1.0, price=0.99))
    venue.resolve(CID, winner="Up", up=UP, down=DOWN)
    clock["now"] = END + 200
    report = await runner.pass_once()
    assert len(report.settled) == 1
    (order,) = await rows("kelly_horse_race_orders")
    assert order["won"] == 1 and order["pnl_usd"] == pytest.approx(5 * (1 - 0.50))
    (d,) = await rows("kelly_horse_race_decisions")
    assert d["outcome"] == "Up"
    realized = [e for e in await rows("risk_events") if e["kind"] == "realize"]
    assert realized[0]["amount_usd"] == pytest.approx(2.5)
    events = [r["event_type"] for r in await rows("notification_feed")]
    assert rn.FILL_EVENT in events and rn.SETTLED_EVENT in events
    summary = await ledger.summary()
    assert summary["paper"]["settled"] == 1 and summary["paper"]["pnl_usd"] == pytest.approx(2.5)
    assert summary["paper"]["win_rate"] == 1.0


async def test_a_losing_side_loses_its_price(kelly_db, venue) -> None:
    hub, clock = make_hub(), {"now": NOW}
    runner = make_runner(venue, hub, clock, rng=draws(0.1, 0.0))
    await runner.pass_once()
    venue.add(CID, sell_up(NOW + 60, 30.0), sell_up(END + 30, 1.0, price=0.99))
    venue.resolve(CID, winner="Down", up=UP, down=DOWN)
    clock["now"] = END + 200
    await runner.pass_once()
    (order,) = await rows("kelly_horse_race_orders")
    assert order["won"] == 0 and order["pnl_usd"] == pytest.approx(-2.5)


async def test_a_window_waits_to_settle_until_its_orders_are_final(kelly_db, venue) -> None:
    hub, clock = make_hub(), {"now": NOW}
    runner = make_runner(venue, hub, clock)
    await runner.pass_once()
    venue.resolve(CID, winner="Up", up=UP, down=DOWN)
    clock["now"] = END + 10  # the tape has shown nothing past the cancel yet
    report = await runner.pass_once()
    assert report.settled == []
    (d,) = await rows("kelly_horse_race_decisions")
    assert d["outcome"] is None


# ---------------------------------------------------------------------------
# The switch, the kill switch, the card
# ---------------------------------------------------------------------------


async def test_switch_off_cancels_and_decides_nothing(kelly_db, venue) -> None:
    hub, clock = make_hub(), {"now": NOW}
    runner = make_runner(venue, hub, clock)
    await runner.pass_once()
    await _strategies.set_enabled(rn.STRATEGY, False)
    clock["now"] = NOW + 5
    report = await runner.pass_once()
    assert report.state == rn.SWITCHED_OFF and report.cancelled == 1
    assert hub.released == [rn.OWNER]
    (paper,) = await rows("paper_resting_orders")
    assert paper["cancelled_ts"] == NOW + 5


async def test_switch_off_before_the_window_places_nothing(kelly_db, venue) -> None:
    await _strategies.set_enabled(rn.STRATEGY, False)
    await make_runner(venue, make_hub(), {"now": NOW}).pass_once()
    assert await rows("kelly_horse_race_decisions") == []


async def test_the_kill_switch_cancels_and_decides_nothing(kelly_db, venue) -> None:
    hub, clock = make_hub(), {"now": NOW}
    runner = make_runner(venue, hub, clock)
    await runner.pass_once()
    (kelly_db / "KILL").touch()
    clock["now"] = NOW + 5
    report = await runner.pass_once()
    assert report.state == rn.NO_ENDPOINT and report.cancelled == 1
    assert report.endpoints["paper"]["state"] == "kill_switch"


async def test_a_new_runner_never_decides_a_window_twice(kelly_db, venue) -> None:
    await make_runner(venue, make_hub(), {"now": NOW}).pass_once()
    await make_runner(venue, make_hub(), {"now": NOW + 10}).pass_once()
    assert len(await rows("kelly_horse_race_decisions")) == 1


async def test_the_pass_is_saved_for_the_card(kelly_db, venue) -> None:
    await make_runner(venue, make_hub(), {"now": NOW}).pass_once()
    saved = json.loads(await _db.get_config(rn.STATUS_KEY))
    assert saved["state"] == rn.RUNNING and saved["last_pass_ts"] == NOW
    assert saved["endpoints"]["paper"]["active"] and not saved["endpoints"]["live"]["active"]
    assert rn.status()["window"]["slug"] == SLUG


async def test_a_failing_step_is_shown_and_the_pass_goes_on(kelly_db, venue,
                                                            monkeypatch) -> None:
    async def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(ledger, "open_orders", boom)
    report = await make_runner(venue, make_hub(), {"now": NOW}).pass_once()
    assert rn.status()["state"] == rn.PASS_FAILED
    assert any("Checking fills failed" in e for e in report.errors)
    assert len(await rows("kelly_horse_race_decisions")) == 1


async def test_turning_live_off_cancels_live_orders_and_keeps_following_them(
    kelly_db, venue, armed
) -> None:
    await armed()
    await _knobs.set("live_max_trade_usd", 5.0)
    live = FakeLive()
    hub, clock = make_hub(), {"now": NOW}
    runner = make_runner(venue, hub, clock, live=live)
    await runner.pass_once()
    await live_control.select_mode("paper", clicked=True)
    clock["now"] = NOW + 5
    report = await runner.pass_once()
    assert report.endpoints["live"]["state"] == live_control.NOT_SELECTED
    assert live.cancelled == [(["0xLIVE1"], "endpoint_off")]
    _, live_order = await rows("kelly_horse_race_orders")
    assert live_order["state"] == "cancelled" and live_order["final"] == 1
    (paper_order, _) = await rows("kelly_horse_race_orders")
    assert paper_order["state"] == "resting"  # paper goes on


async def test_a_live_order_waits_for_consent(kelly_db, venue, armed) -> None:
    await live_control.select_mode("live", clicked=False)
    live = FakeLive()
    report = await make_runner(venue, make_hub(), {"now": NOW}, live=live).pass_once()
    assert report.endpoints["live"]["state"] == live_control.NOT_CLICKED
    assert live.placed == [] and len(await rows("kelly_horse_race_orders")) == 1


async def test_paper_clicked_during_the_decision_stops_the_live_order(kelly_db, venue, armed,
                                                                     monkeypatch) -> None:
    await armed()
    await _knobs.set("live_max_trade_usd", 5.0)
    live = FakeLive()
    real_read_book = inputs.read_book

    async def read_book_then_click_paper(client, token):
        await live_control.select_mode("paper", clicked=True)  # the operator, mid-decision
        return await real_read_book(client, token)

    monkeypatch.setattr(inputs, "read_book", read_book_then_click_paper)
    await make_runner(venue, make_hub(), {"now": NOW}, live=live).pass_once()
    assert live.placed == []
    paper, stopped = await rows("kelly_horse_race_orders")
    assert paper["state"] == "resting"
    assert stopped["state"] == "blocked" and stopped["reason"].startswith("live_disarmed")


async def test_switched_off_during_the_decision_sends_nothing(kelly_db, venue,
                                                              monkeypatch) -> None:
    real_read_book = inputs.read_book

    async def read_book_then_switch_off(client, token):
        await _strategies.set_enabled(rn.STRATEGY, False)
        return await real_read_book(client, token)

    monkeypatch.setattr(inputs, "read_book", read_book_then_switch_off)
    await make_runner(venue, make_hub(), {"now": NOW}).pass_once()
    (order,) = await rows("kelly_horse_race_orders")
    assert order["state"] == "blocked" and order["reason"].startswith("switched_off")
    assert await rows("paper_resting_orders") == []


# ---------------------------------------------------------------------------
# A live send with no reply, an order that cannot be recorded, cancel errors
# ---------------------------------------------------------------------------


async def live_runner(venue: FakeVenue, armed, clock: dict) -> tuple[rn.Runner, FakeLive]:
    await armed()
    await _knobs.set("live_max_trade_usd", 5.0)
    live = FakeLive()
    return make_runner(venue, make_hub(), clock, live=live), live


async def test_a_send_with_no_reply_is_unknown_until_found(kelly_db, venue, armed) -> None:
    clock = {"now": NOW}
    runner, live = await live_runner(venue, armed, clock)
    live.refuse = OutcomeUnknown("The send failed and the open orders could not be read.")
    report = await runner.pass_once()
    _, unknown = await rows("kelly_horse_race_orders")
    assert (unknown["state"], unknown["final"], unknown["venue_order_id"]) == ("unknown", 0, None)
    assert any("looked for every pass" in e for e in report.errors)
    assert not [e for e in await rows("risk_events") if e["mode"] == "live"]

    live.open_ids = ["0xFOUND"]
    live.views["0xFOUND"] = OrderView("0xFOUND", "resting", unknown["size"], 0.0, None, False)
    clock["now"] = NOW + 5
    report = await runner.pass_once()
    _, found = await rows("kelly_horse_race_orders")
    assert (found["state"], found["venue_order_id"], found["final"]) == ("resting", "0xFOUND", 0)
    commits = [e for e in await rows("risk_events") if e["mode"] == "live"]
    assert [c["kind"] for c in commits] == ["commit"]
    assert any("found resting on the exchange" in e for e in report.errors)


async def test_an_unknown_order_never_found_is_closed_at_the_stop(kelly_db, venue,
                                                                  armed) -> None:
    clock = {"now": NOW}
    runner, live = await live_runner(venue, armed, clock)
    live.refuse = OutcomeUnknown("no reply")
    await runner.pass_once()
    clock["now"] = END - resting.GTD_STOP_S
    report = await runner.pass_once()
    _, closed = await rows("kelly_horse_race_orders")
    assert (closed["state"], closed["final"], closed["credited"]) == ("unknown", 1, 1)
    assert "check your Polymarket orders" in closed["reason"]
    assert any("check your Polymarket orders" in e for e in report.errors)


async def test_an_open_unknown_order_holds_new_live_orders(kelly_db, venue, armed) -> None:
    clock = {"now": NOW}
    runner, live = await live_runner(venue, armed, clock)
    await ledger.record_order(decision_id=999, window_slug="btc-updown-15m-older", mode="live",
                              token_id=UP, outcome="Up", price=0.5, size=6.0, queue_ahead=0.0,
                              window_end_ts=END + 900, state="unknown", reason="no reply")
    await runner.pass_once()
    assert live.placed == []
    held = [o for o in await rows("kelly_horse_race_orders") if o["window_slug"] == SLUG
            and o["mode"] == "live"]
    assert held[0]["state"] == "blocked" and held[0]["reason"].startswith("outcome_unknown")


async def test_an_order_that_cannot_be_recorded_is_cancelled(kelly_db, venue, armed,
                                                             monkeypatch) -> None:
    clock = {"now": NOW}
    runner, live = await live_runner(venue, armed, clock)
    real_record = ledger.record_order

    async def record_order(**kw):
        if kw["mode"] == "live" and kw["state"] == "resting":
            raise OSError("database is locked")
        return await real_record(**kw)

    monkeypatch.setattr(ledger, "record_order", record_order)
    report = await runner.pass_once()
    assert live.cancelled == [(["0xLIVE1"], "not_recorded")]
    assert any("Recording the live order" in e for e in report.errors)


async def test_a_failed_cancel_reaches_the_report(kelly_db, venue, armed) -> None:
    clock = {"now": NOW}
    runner, live = await live_runner(venue, armed, clock)
    await runner.pass_once()
    live.cancel_error = "the cancel of 1 order(s) failed: HTTP 503"
    clock["now"] = END - rn.CANCEL_LEAD_S
    report = await runner.pass_once()
    assert "live: the cancel of 1 order(s) failed: HTTP 503" in report.errors
    assert live.last_errors == []



# ---------------------------------------------------------------------------
# A failed realize, a shutdown mid-send, the time an order goes
# ---------------------------------------------------------------------------


async def test_a_failed_realize_is_tried_again_next_pass(kelly_db, venue, monkeypatch) -> None:
    hub, clock = make_hub(), {"now": NOW}
    runner = make_runner(venue, hub, clock, rng=draws(0.1, 0.0))
    await runner.pass_once()
    venue.add(CID, sell_up(NOW + 60, 30.0), sell_up(END + 30, 1.0, price=0.99))
    venue.resolve(CID, winner="Down", up=UP, down=DOWN)
    real_realize = rn.RiskGate.realize
    failures = [OSError("database is locked")]

    async def realize_once_failing(self, **kw):
        if failures:
            raise failures.pop()
        return await real_realize(self, **kw)

    monkeypatch.setattr(rn.RiskGate, "realize", realize_once_failing)
    clock["now"] = END + 200
    report = await runner.pass_once()
    assert len(report.settled) == 1 and any("settled P&L" in e for e in report.errors)
    assert not [e for e in await rows("risk_events") if e["kind"] == "realize"]
    clock["now"] = END + 205
    await runner.pass_once()
    (realized,) = [e for e in await rows("risk_events") if e["kind"] == "realize"]
    assert realized["amount_usd"] == pytest.approx(-2.5)


async def test_a_shutdown_mid_send_still_records_the_order(kelly_db, venue, armed) -> None:
    clock = {"now": NOW}
    runner, live = await live_runner(venue, armed, clock)
    started = asyncio.Event()
    real_place = live.place

    async def slow_place(request, *, now=None):
        started.set()
        await asyncio.sleep(0.2)  # the exchange takes its time to answer
        return await real_place(request, now=now)

    live.place = slow_place  # type: ignore[method-assign]
    task = asyncio.create_task(runner.pass_once())
    await started.wait()
    task.cancel()  # the app's teardown
    with pytest.raises(asyncio.CancelledError):
        await task
    _, recorded = await rows("kelly_horse_race_orders")
    assert (recorded["mode"], recorded["state"], recorded["venue_order_id"]) == (
        "live", "resting", "0xLIVE1")
    assert [e["kind"] for e in await rows("risk_events") if e["mode"] == "live"] == ["commit"]


async def test_a_paper_order_counts_trades_from_when_it_is_sent(kelly_db, venue,
                                                                monkeypatch) -> None:
    clock = {"now": NOW}
    real_read_book = inputs.read_book

    async def slow_read_book(client, token):
        clock["now"] = NOW + 5  # the reads took five seconds
        return await real_read_book(client, token)

    monkeypatch.setattr(inputs, "read_book", slow_read_book)
    await make_runner(venue, make_hub(), clock).pass_once()
    (paper,) = await rows("paper_resting_orders")
    assert paper["placed_ts"] == NOW + 5 + 1
