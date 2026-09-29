"""The live venue (``ems/execution/clob.py``) against a fake CLOB client: the post-only GTD
arguments, the kill switch, rejections journaled, the order's id known before it is sent and a
lost reply asked for by that id, cancel by id (never cancel-all), fills from ``size_matched``,
and the wallet checks. The installed library, where present, signs with a throwaway key made
in the test. No network; no real key ever appears."""

from __future__ import annotations

import base64
import dataclasses
import json
import math
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio

from ems import config as _config
from ems import db as _db
from ems import logging_setup as _logging
from ems.execution import clob, journal, resting
from ems.execution.controls import PlacementRefused
from ems.kelly_horse_race import maths

pytestmark = pytest.mark.asyncio

START = 1_789_935_300
END = START + 900
NOW = START + 30
UP, DOWN = "UP-btc", "DN-btc"
TOKEN = str(2**255 + 12_345)  # shaped like a real outcome token id, for the real library
KEY = "0x" + "ab" * 32  # a stand-in, not a real key


@dataclass
class Args:
    token_id: str
    price: float
    size: float
    side: str
    expiration: int = 0


@dataclass
class Options:
    tick_size: str | None = None


@dataclass
class OpenParams:
    id: str | None = None
    market: str | None = None
    asset_id: str | None = None


class ApiError(Exception):
    """Shaped like the library's ``PolyApiException``: raised for every reply that is not a
    200, with its HTTP status (None when no reply came) and its body."""

    def __init__(self, status_code: int | None, error_msg: Any) -> None:
        super().__init__(f"PolyApiException[status_code={status_code}, "
                         f"error_message={error_msg}]")
        self.status_code = status_code
        self.error_msg = error_msg


@dataclass
class Signed:
    """What the fake ``create_order`` builds: the arguments it was given, and the id the
    exchange gives it (None: an order whose id cannot be worked out before it is sent)."""

    args: Any
    options: Any
    order_id: str | None


TYPES = SimpleNamespace(OrderArgs=Args, PartialCreateOrderOptions=Options,
                        OrderType=SimpleNamespace(GTD="GTD", GTC="GTC"),
                        OpenOrderParams=OpenParams, ApiError=ApiError,
                        order_hash=lambda signed, chain_id: signed.order_id)


class FakeClob:
    def __init__(self) -> None:
        self.posted: list[tuple] = []
        self.cancel_calls: list[list[str]] = []
        self.cancel_all_calls = 0
        self.orders: dict[str, dict[str, Any]] = {}
        self.post_reply: Any = {"success": True, "orderID": "0xORDER1", "status": "live",
                                "errorMsg": ""}
        self.next_id: str | None = "0xORDER1"  # the id the next order built will have
        self.build_error: Exception | None = None
        self.post_error: Exception | None = None
        # The exchange takes the order, lists it as the library sent it, and the reply is lost.
        self.lost_reply: Exception | None = None
        self.open_orders: list[dict[str, Any]] = []
        self.cancel_refusals: dict[str, str] = {}  # id -> the reason the exchange gives
        self.open_errors: list[Exception] = []  # raised by the next open-order reads, in turn
        self.open_calls: list[Any] = []
        self.order_errors: dict[str, Exception] = {}  # get_order raises this for that id
        self.order_calls: list[str] = []

    def create_order(self, order_args, options=None) -> Signed:
        if self.build_error is not None:
            raise self.build_error
        return Signed(order_args, options, self.next_id)

    def post_order(self, order: Signed, order_type="GTC", post_only=False, defer_exec=False):
        if self.post_error is not None:
            raise self.post_error
        self.posted.append((order.args, order.options, order_type, post_only))
        if self.lost_reply is not None:
            sent = math.floor(order.args.size * 100) / 100  # the library's cut to hundredths
            listed = open_order(order.order_id or "0xLOST", price=str(order.args.price),
                                original_size=str(sent))
            self.open_orders.append(listed)
            if order.order_id:
                self.orders[order.order_id] = listed
            raise self.lost_reply
        return self.post_reply

    def cancel_orders(self, order_hashes: list) -> dict:
        self.cancel_calls.append(list(order_hashes))
        done = [h for h in order_hashes if h in self.orders and h not in self.cancel_refusals]
        return {"canceled": done,
                "not_canceled": {h: self.cancel_refusals.get(h, "Order not found")
                                 for h in order_hashes if h not in done}}

    def cancel_all(self) -> dict:  # must never be called
        self.cancel_all_calls += 1
        return {}

    def get_order(self, order_id: str):
        self.order_calls.append(order_id)
        if order_id in self.order_errors:
            raise self.order_errors[order_id]
        return self.orders.get(order_id)

    def get_open_orders(self, params=None, only_first_page=False, next_cursor=None) -> list:
        self.open_calls.append(params)
        if self.open_errors:
            raise self.open_errors.pop(0)
        return [o for o in self.open_orders
                if params is None or o.get("asset_id") == params.asset_id]


@pytest_asyncio.fixture
async def live_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "live.db")
    monkeypatch.setattr(_config, "KILL_SWITCH_PATH", tmp_path / "KILL")
    await _db.init_db()
    return tmp_path


def request(**kw) -> resting.PlaceRequest:
    base = dict(strategy="kelly_horse_race", condition_id="0xcid", token_id=UP, outcome="Up",
                up_token=UP, down_token=DOWN, price=0.50, size=7.5, expires_ts=END,
                tick_size=0.01, queue_ahead=20.0, best_ask=0.52)
    base.update(kw)
    return resting.PlaceRequest(**base)


async def _no_wait(seconds: float) -> None:
    return None


def venue(fake: FakeClob) -> clob.ClobRestingVenue:
    return clob.ClobRestingVenue(fake, api=TYPES, clock=lambda: NOW, sleep=_no_wait)


def open_order(order_id: str, **kw) -> dict[str, Any]:
    """One open order as the exchange lists it (the request() defaults)."""
    base = {"id": order_id, "status": "LIVE", "side": "BUY", "asset_id": UP, "price": "0.5",
            "original_size": "7.5", "size_matched": "0", "expiration": str(END)}
    base.update(kw)
    return base


async def test_a_post_only_gtd_buy_expiring_at_the_window_end(live_db) -> None:
    fake = FakeClob()
    placed = await venue(fake).place(request(), now=NOW)
    assert placed == resting.Placed(order_id="0xORDER1", placed_ts=NOW)
    ((args, options, order_type, post_only),) = fake.posted
    assert (args.token_id, args.price, args.side) == (UP, 0.50, "BUY")
    assert args.size == 7.5 + clob.SIZE_PAD  # so the library's cut to hundredths keeps 7.5
    assert args.expiration == END and options.tick_size == "0.01"
    assert order_type == "GTD" and post_only is True
    (row,) = await journal.journal_rows()
    assert row["size"] == 7.5
    assert (row["intent"], row["status"], row["order_type"], row["clob_order_id"]) == (
        "ENTRY", "SUBMITTED", "GTD", "0xORDER1")
    assert '"post_only": true' in row["details_json"]
    assert '"strategy": "kelly_horse_race"' in row["details_json"]
    assert row["placement_status"] == "live" and row["mode"] == "live"


async def test_the_kill_switch_blocks_before_anything_is_sent(live_db) -> None:
    (live_db / "KILL").touch()
    fake = FakeClob()
    with pytest.raises(PlacementRefused) as refused:
        await venue(fake).place(request(), now=NOW)
    assert refused.value.reason == "kill_switch" and fake.posted == []
    (row,) = await journal.journal_rows()
    assert row["status"] == "BLOCKED" and row["error"].startswith("kill_switch")


async def test_a_crossing_bid_is_refused_before_it_is_sent(live_db) -> None:
    fake = FakeClob()
    with pytest.raises(PlacementRefused) as refused:
        await venue(fake).place(request(price=0.52), now=NOW)
    assert refused.value.reason == "would_cross" and fake.posted == []


async def test_an_exchange_rejection_is_journaled_and_refused(live_db) -> None:
    fake = FakeClob()
    fake.post_reply = {"success": False, "errorMsg": "invalid post-only order: order crosses book"}
    with pytest.raises(PlacementRefused) as refused:
        await venue(fake).place(request(), now=NOW)
    assert refused.value.reason == "venue_rejected"
    (row,) = await journal.journal_rows()
    assert row["status"] == "REJECTED" and "crosses book" in row["error"]


async def test_an_http_refusal_is_a_rejection_not_a_lost_reply(live_db) -> None:
    """The library raises for every reply that is not a 200, and the exchange refuses an
    order with a 4xx: that is its answer, so nothing is searched for and the row says
    REJECTED with the exchange's words."""
    fake = FakeClob()
    fake.post_error = ApiError(400, {"error": "invalid expiration"})
    with pytest.raises(PlacementRefused) as refused:
        await venue(fake).place(request(), now=NOW)
    assert refused.value.reason == "venue_rejected" and "invalid expiration" in str(refused.value)
    assert fake.open_calls == [] and fake.order_calls == []
    (row,) = await journal.journal_rows()
    assert row["status"] == "REJECTED" and "invalid expiration" in row["error"]
    assert row["size"] == 7.5 and row["clob_order_id"] == "0xORDER1"


@pytest.mark.parametrize("error", [ApiError(None, "Request exception!"),
                                   ApiError(500, {"error": "could not insert order"})],
                         ids=["no_reply", "server_error"])
async def test_no_reply_or_a_server_error_is_still_looked_for(live_db, error) -> None:
    fake = FakeClob()
    fake.post_error = error
    fake.orders["0xORDER1"] = open_order("0xORDER1")
    assert (await venue(fake).place(request(), now=NOW)).order_id == "0xORDER1"


async def test_a_lost_reply_is_asked_for_by_its_id_in_any_status(live_db) -> None:
    """The order's id is known before it is sent. One that filled in full before anyone
    looked is on no open-order list, but the exchange still answers for its id: it is found
    and followed, and its fill is in the record."""
    fake = FakeClob()
    fake.post_error = TimeoutError("read timed out")
    fake.orders["0xORDER1"] = open_order("0xORDER1", status="MATCHED", size_matched="7.5")
    v = venue(fake)
    placed = await v.place(request(), now=NOW)
    assert placed == resting.Placed(order_id="0xORDER1", placed_ts=NOW)
    assert fake.order_calls == ["0xORDER1"] and fake.open_calls == []
    (row,) = await journal.journal_rows()
    assert (row["status"], row["clob_order_id"]) == ("SUBMITTED", "0xORDER1")
    assert "by its id" in row["details_json"]
    view = (await v.fills(["0xORDER1"], now=NOW + 5))["0xORDER1"]
    assert (view.state, view.filled_size, view.final) == ("filled", 7.5, True)


@pytest.mark.parametrize("answer", [ApiError(404, {"error": "Order not found"}),
                                    ApiError(400, {"error": "Invalid orderID"})],
                         ids=["not_found", "invalid_order_id"])
async def test_a_lost_reply_the_exchange_has_no_order_for_is_unknown_with_its_id(
        live_db, answer) -> None:
    """No order with its id yet may still be an insert in flight: never called refused, it is
    unknown, with the id the caller keeps asking for."""
    fake = FakeClob()
    fake.post_error = ApiError(None, "Request exception!")
    fake.order_errors["0xORDER1"] = answer
    with pytest.raises(clob.OutcomeUnknown) as unknown:
        await venue(fake).place(request(), now=NOW)
    assert isinstance(unknown.value, clob.NoReply) and unknown.value.order_id == "0xORDER1"
    assert unknown.value.reason == "outcome_unknown"
    assert fake.order_calls == ["0xORDER1"] * clob.LOOKUPS and fake.open_calls == []
    (row,) = await journal.journal_rows()
    assert (row["status"], row["clob_order_id"]) == ("ERROR", "0xORDER1")
    assert "no order with its id yet" in row["error"]


async def test_a_lost_reply_whose_id_cannot_be_asked_for_is_unknown_with_its_id(
        live_db) -> None:
    fake = FakeClob()
    fake.post_error = TimeoutError("read timed out")
    fake.order_errors["0xORDER1"] = ConnectionError("down")
    with pytest.raises(clob.NoReply) as unknown:
        await venue(fake).place(request(), now=NOW)
    assert unknown.value.order_id == "0xORDER1" and "could not be asked" in str(unknown.value)
    (row,) = await journal.journal_rows()
    assert (row["status"], row["clob_order_id"]) == ("ERROR", "0xORDER1")


async def test_has_order_answers_for_any_status(live_db) -> None:
    """True for an order in any status, False when the exchange has none with that id (404,
    400 "Invalid orderID" for the well-formed id it is asked, or an empty reply); any other
    failure is no answer, so it raises."""
    fake = FakeClob()
    fake.orders = {"0xM": open_order("0xM", status="MATCHED"),
                   "0xC": open_order("0xC", status="CANCELED"), "0xEMPTY": {}}
    fake.order_errors = {"0x404": ApiError(404, {"error": "Order not found"}),
                         "0x400": ApiError(400, {"error": "Invalid orderID"}),
                         "0x401": ApiError(401, {"error": "Unauthorized/Invalid api key"}),
                         "0x425": ApiError(425, {"error": "Too early"}),
                         "0x429": ApiError(429, {"error": "Too many requests"}),
                         "0x500": ApiError(500, {"error": "boom"}),
                         "0xNOREPLY": ApiError(None, "Request exception!"),
                         "0xDOWN": ConnectionError("down")}
    v = venue(fake)
    assert await v.has_order("0xM") and await v.has_order("0xC")
    assert not await v.has_order("0x404") and not await v.has_order("0xEMPTY")
    assert not await v.has_order("0x400") and not await v.has_order("0xNEVER")
    for broken in ("0x401", "0x425", "0x429", "0x500", "0xNOREPLY", "0xDOWN"):
        with pytest.raises(Exception):  # noqa: B017 - any failure is no answer
            await v.has_order(broken)


async def test_an_order_that_cannot_be_built_is_not_sent(live_db) -> None:
    fake = FakeClob()
    fake.build_error = RuntimeError("invalid tick size (0.01), minimum for the market is 0.001")
    with pytest.raises(PlacementRefused) as refused:
        await venue(fake).place(request(), now=NOW)
    assert refused.value.reason == "venue_error"
    assert not isinstance(refused.value, clob.OutcomeUnknown)
    assert fake.posted == [] and fake.order_calls == [] and fake.open_calls == []
    (row,) = await journal.journal_rows()
    assert row["status"] == "ERROR" and "not sent" in row["error"]


async def test_an_id_the_exchange_does_not_confirm_is_reported(live_db) -> None:
    """The exchange's id is the one followed; one that differs from the id worked out before
    sending says a lost reply could not be looked up by id, so the card is told."""
    fake = FakeClob()
    fake.post_reply = {"success": True, "orderID": "0xOTHER", "status": "live"}
    v = venue(fake)
    assert (await v.place(request(), now=NOW)).order_id == "0xOTHER"
    assert v.last_errors and "0xORDER1" in v.last_errors[0]
    (row,) = await journal.journal_rows()
    assert row["clob_order_id"] == "0xOTHER" and "0xORDER1" in row["details_json"]


async def test_the_real_librarys_refusal_is_a_rejection(live_db) -> None:
    """The same with the installed library's own exception and types (skipped without it)."""
    exceptions = pytest.importorskip(f"{clob.LIBRARY}.exceptions")
    httpx = pytest.importorskip("httpx")
    fake = FakeClob()
    fake.post_error = exceptions.PolyApiException(httpx.Response(
        400, json={"error": "invalid post-only order: order crosses book"}))
    v = clob.ClobRestingVenue(fake, clock=lambda: NOW, sleep=_no_wait)
    with pytest.raises(PlacementRefused) as refused:
        await v.place(request(), now=NOW)
    assert refused.value.reason == "venue_rejected" and fake.open_calls == []
    (row,) = await journal.journal_rows()
    assert row["status"] == "REJECTED" and "crosses book" in row["error"]


async def test_a_failed_send_is_journaled_without_the_key(live_db, monkeypatch) -> None:
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", KEY)
    fake = FakeClob()
    fake.post_error = RuntimeError(f"signer {KEY} rejected")
    with pytest.raises(PlacementRefused) as refused:
        await venue(fake).place(request(), now=NOW)
    assert refused.value.reason == "outcome_unknown" and KEY not in str(refused.value)
    (row,) = await journal.journal_rows()
    assert row["status"] == "ERROR" and KEY not in row["error"] and "<redacted" in row["error"]


async def test_without_an_id_a_lost_reply_is_searched_for_among_the_open_orders(
        live_db) -> None:
    """The fallback when the id could not be worked out: the send raised, but the exchange
    took the order, and it is found among the open orders and followed."""
    fake = FakeClob()
    fake.next_id = None
    fake.post_error = TimeoutError("read timed out")
    fake.open_orders = [open_order("0xMINE_ALREADY"), open_order("0xOTHER", price="0.49"),
                        open_order("0xLOST")]
    v = venue(fake)
    v._expiry["0xMINE_ALREADY"] = END  # one this venue already follows is never adopted
    placed = await v.place(request(), now=NOW)
    assert placed.order_id == "0xLOST" and fake.open_calls[0].asset_id == UP
    (row,) = await journal.journal_rows()
    assert row["status"] == "SUBMITTED" and row["clob_order_id"] == "0xLOST"
    assert "found among the open orders" in row["details_json"]


async def test_a_lost_reply_with_unreadable_open_orders_is_unknown(live_db) -> None:
    fake = FakeClob()
    fake.next_id = None
    fake.post_error = TimeoutError("read timed out")
    fake.open_errors = [ConnectionError("down")] * clob.LOOKUPS
    with pytest.raises(clob.OutcomeUnknown) as unknown:
        await venue(fake).place(request(), now=NOW)
    assert unknown.value.reason == "outcome_unknown" and len(fake.open_calls) == clob.LOOKUPS
    assert getattr(unknown.value, "order_id", None) is None
    (row,) = await journal.journal_rows()
    assert row["status"] == "ERROR" and "outcome unknown" in row["error"]


async def test_a_second_lookup_can_find_it(live_db) -> None:
    fake = FakeClob()
    fake.next_id = None
    fake.post_error = TimeoutError("read timed out")
    fake.open_errors = [ConnectionError("down")]
    fake.open_orders = [open_order("0xLOST")]
    assert (await venue(fake).place(request(), now=NOW)).order_id == "0xLOST"


async def test_a_lost_reply_is_found_at_the_size_it_was_sent(live_db) -> None:
    """5.02 * 100 is 501.99999999999994 in floats, so the library's cut alone would send
    5.01 shares, and the search for a 5.02 order would miss it on the exchange."""
    fake = FakeClob()
    fake.next_id = None
    fake.lost_reply = TimeoutError("read timed out")
    placed = await venue(fake).place(request(size=5.02, price=0.47, best_ask=0.49), now=NOW)
    assert placed.order_id == "0xLOST" and fake.open_orders[0]["original_size"] == "5.02"


async def test_the_search_matches_the_size_to_the_hundredth(live_db) -> None:
    fake = FakeClob()
    fake.open_orders = [open_order("0xNEXT", original_size="7.51"),
                        open_order("0xSAME", original_size="7.504")]
    v = venue(fake)
    assert await v.find_order(token_id=UP, price=0.5, size=7.5, expires_ts=END) == "0xSAME"
    fake.open_orders.pop()
    assert await v.find_order(token_id=UP, price=0.5, size=7.5, expires_ts=END) is None


async def test_a_journal_failure_after_the_exchange_took_it_still_places(
        live_db, monkeypatch) -> None:
    fake = FakeClob()
    v = venue(fake)

    async def broken(**kw):
        raise OSError("disk full")

    monkeypatch.setattr(journal, "journal_live_order", broken)
    placed = await v.place(request(), now=NOW)
    assert placed.order_id == "0xORDER1"
    assert v.last_errors and "not journaled" in v.last_errors[0]


async def test_cancel_by_id_never_cancel_all(live_db) -> None:
    fake = FakeClob()
    fake.orders["0xORDER1"] = {"status": "live", "original_size": "7.5", "size_matched": "0"}
    fake.cancel_refusals["0xSTUCK"] = "internal error"
    assert await venue(fake).cancel(["0xORDER1", "0xSTUCK"], reason="window_end", now=NOW) == 1
    assert fake.cancel_calls == [["0xORDER1", "0xSTUCK"]] and fake.cancel_all_calls == 0
    rows = {r["clob_order_id"]: r for r in await journal.journal_rows()}
    assert rows["0xORDER1"]["status"] == "CANCELLED" and rows["0xSTUCK"]["status"] == "ERROR"


async def test_fills_come_from_size_matched(live_db) -> None:
    fake = FakeClob()
    v = venue(fake)
    await v.place(request(), now=NOW)
    fake.orders["0xORDER1"] = {"status": "LIVE", "original_size": "7.5", "size_matched": "2.5",
                               "expiration": str(END)}
    view = (await v.fills(["0xORDER1"], now=NOW + 60))["0xORDER1"]
    assert (view.state, view.filled_size, view.final) == ("resting", 2.5, False)
    fake.orders["0xORDER1"].update(status="MATCHED", size_matched="7.5")
    view = (await v.fills(["0xORDER1"], now=NOW + 120))["0xORDER1"]
    assert (view.state, view.filled_size, view.final) == ("filled", 7.5, True)


async def test_our_cancel_is_cancelled_and_the_venues_stop_is_expired(live_db) -> None:
    fake = FakeClob()
    v = venue(fake)
    fake.orders["0xA"] = {"status": "live", "original_size": "5", "size_matched": "1",
                          "expiration": str(END)}
    await v.cancel(["0xA"], reason="switched_off", now=NOW + 10)
    fake.orders["0xA"]["status"] = "CANCELED"
    fake.orders["0xB"] = {"status": "CANCELED", "original_size": "5", "size_matched": "0",
                          "expiration": str(END)}
    views = await v.fills(["0xA", "0xB"], now=END)
    assert (views["0xA"].state, views["0xA"].closed_ts, views["0xA"].filled_size) == (
        "cancelled", NOW + 10, 1.0)
    assert (views["0xB"].state, views["0xB"].closed_ts) == ("expired", END - 60)
    assert views["0xA"].final and views["0xB"].final


async def test_a_pruned_order_keeps_its_last_view_made_final(live_db) -> None:
    fake = FakeClob()
    v = venue(fake)
    fake.orders["0xA"] = {"status": "live", "original_size": "5", "size_matched": "3",
                          "expiration": str(END)}
    await v.fills(["0xA"], now=NOW)
    del fake.orders["0xA"]
    view = (await v.fills(["0xA"], now=END + 600))["0xA"]
    assert view.final and view.filled_size == 3.0


async def test_an_empty_reply_before_the_stop_is_not_an_end(live_db) -> None:
    """One empty status reply while the order can still trade is a miss, asked again."""
    fake = FakeClob()
    v = venue(fake)
    fake.orders["0xA"] = {"status": "live", "original_size": "5", "size_matched": "1",
                          "expiration": str(END)}
    await v.fills(["0xA"], now=NOW)
    fake.orders["0xA"] = None
    assert await v.fills(["0xA"], now=NOW + 5) == {}
    assert v.last_errors and "no status" in v.last_errors[-1]
    fake.orders["0xA"] = {"status": "live", "original_size": "5", "size_matched": "2",
                          "expiration": str(END)}
    view = (await v.fills(["0xA"], now=NOW + 10))["0xA"]
    assert (view.state, view.filled_size, view.final) == ("resting", 2.0, False)


async def test_an_order_never_seen_is_gone_after_enough_misses(live_db) -> None:
    fake = FakeClob()
    v = venue(fake)
    for i in range(clob.MISSES_BEFORE_GONE - 1):
        assert await v.fills(["0xNEVER"], now=NOW + i) == {}
    view = (await v.fills(["0xNEVER"], now=NOW + 99))["0xNEVER"]
    assert view.final and view.forced and view.filled_size == 0.0


async def test_a_refused_cancel_reaches_last_errors(live_db) -> None:
    fake = FakeClob()
    fake.cancel_refusals["0xSTUCK"] = "internal error"
    v = venue(fake)
    await v.cancel(["0xSTUCK"], reason="window_end", now=NOW)
    assert v.last_errors and "0xSTUCK" in v.last_errors[0] and "refused" in v.last_errors[0]


async def test_a_cancel_of_an_order_already_closed_is_no_error(live_db) -> None:
    """The exchange stops a GTD order the second the window-end cancel starts, so a cancel
    can find it already expired, matched or gone. Nothing is left to cancel: the row says
    CANCELLED with the exchange's reason, the card shows no error, and the order's own
    status still says how it ended."""
    fake = FakeClob()
    fake.cancel_refusals = {"0xEXP": "Order already canceled", "0xFULL": "Order already matched"}
    fake.orders["0xEXP"] = {"status": "CANCELED", "original_size": "5", "size_matched": "0",
                            "expiration": str(END)}
    v = venue(fake)
    ids = ["0xEXP", "0xFULL", "0xGONE"]
    assert await v.cancel(ids, reason="window_end", now=END - 60) == 0
    assert v.last_errors == []
    rows = {r["clob_order_id"]: r for r in await journal.journal_rows()}
    assert {rows[i]["status"] for i in ids} == {"CANCELLED"}
    assert "Order already matched" in rows["0xFULL"]["details_json"]
    assert "Order not found" in rows["0xGONE"]["details_json"]
    view = (await v.fills(["0xEXP"], now=END - 55))["0xEXP"]
    assert (view.state, view.closed_ts) == ("expired", END - 60)


async def test_the_venue_meets_the_protocol() -> None:
    assert isinstance(venue(FakeClob()), resting.RestingVenue)


async def test_wallet_problems_name_what_is_missing(monkeypatch) -> None:
    monkeypatch.setattr(_config, "POLYMARKET_PRIVATE_KEY", "")
    monkeypatch.setattr(_config, "POLYMARKET_FUNDER", "")
    monkeypatch.setattr(_config, "POLYMARKET_SIGNATURE_TYPE", 1)
    monkeypatch.setattr(_config, "CONFIG_PARSE_ERRORS", ["POLYMARKET_CHAIN_ID is not a number"])
    problems = " | ".join(clob.wallet_problems())
    for text in ("POLYMARKET_PRIVATE_KEY is not set", "is 1, not 2", "POLYMARKET_FUNDER",
                 "did not parse"):
        assert text in problems


async def test_a_complete_wallet_has_no_problems(monkeypatch) -> None:
    pytest.importorskip(clob.LIBRARY)
    monkeypatch.setattr(_config, "POLYMARKET_PRIVATE_KEY", KEY)
    monkeypatch.setattr(_config, "POLYMARKET_FUNDER", "0x" + "22" * 20)
    monkeypatch.setattr(_config, "POLYMARKET_SIGNATURE_TYPE", 2)
    monkeypatch.setattr(_config, "CONFIG_PARSE_ERRORS", [])
    assert clob.wallet_problems() == []


async def test_the_real_library_takes_these_arguments() -> None:
    """The call shapes used above match the installed client (skipped without it)."""
    lib = pytest.importorskip(clob.LIBRARY)
    args = lib.OrderArgs(token_id=UP, price=0.5, size=7.5, side="BUY", expiration=END)
    assert (args.expiration, args.side) == (END, "BUY")
    assert lib.PartialCreateOrderOptions(tick_size="0.01").tick_size == "0.01"
    assert lib.OrderType.GTD == "GTD"


def offline_client(lib: Any, *, neg_risk: bool = False) -> tuple[Any, Any]:
    """The installed client, signing with a throwaway key made here, its market reads
    answered locally so nothing reaches the network. Returns (the key's account, client)."""
    eth_account = pytest.importorskip("eth_account")
    account = eth_account.Account.create()
    client = lib.ClobClient("https://clob.invalid", chain_id=137, key=account.key.hex(),
                            signature_type=clob.SAFE_SIGNATURE_TYPE, funder="0x" + "22" * 20)
    client.get_tick_size = lambda token_id: "0.01"
    client.get_neg_risk = lambda token_id: neg_risk
    client.get_version = lambda: 2
    return account, client


def signer_of(order_id: str, signature: str) -> str:
    """The address whose key made ``signature`` over the digest ``order_id``."""
    eth_account = pytest.importorskip("eth_account")
    return eth_account.Account._recover_hash(bytes.fromhex(order_id[2:]), signature=signature)


@pytest.mark.parametrize("neg_risk", [False, True], ids=["exchange", "neg_risk_exchange"])
async def test_the_order_id_is_the_hash_its_own_signature_signs(neg_risk) -> None:
    """The id is worked out from the signed order before it is sent. The proof, offline, with
    a throwaway key made here: the signer recovered from the order's own signature over that
    id is the key's address, so the id is the order's EIP-712 hash under the exchange that
    checks it, the one the exchange gives as ``orderID``; and it is the library's own order
    hash. A signature by any other key gives no id (skipped without the library)."""
    lib = pytest.importorskip(clob.LIBRARY)
    v2 = pytest.importorskip(f"{clob.LIBRARY}.order_utils.exchange_order_builder_v2")
    contracts = pytest.importorskip(f"{clob.LIBRARY}.config").get_contract_config(137)
    messages = pytest.importorskip("eth_account.messages")
    account, client = offline_client(lib, neg_risk=neg_risk)
    args = clob._order_args(lib, request(token_id=TOKEN, up_token=TOKEN, size=5.02,
                                         price=0.47, best_ask=0.49))
    signed = client.create_order(args, lib.PartialCreateOrderOptions(tick_size="0.01"))
    order_id = clob.order_hash(signed, client.chain_id)
    assert order_id is not None and len(order_id) == 66
    assert signer_of(order_id, signed.signature) == account.address == signed.signer
    exchange = contracts.neg_risk_exchange_v2 if neg_risk else contracts.exchange_v2
    builder = v2.ExchangeOrderBuilderV2(exchange, 137, client.signer)
    typed = builder.build_order_typed_data(signed)
    assert order_id == builder.build_order_hash(typed)
    other, _ = offline_client(lib)
    forged = other.sign_message(messages.encode_typed_data(full_message=typed))
    signature = "0x" + forged.signature.hex().removeprefix("0x")
    assert clob.order_hash(dataclasses.replace(signed, signature=signature), 137) is None


async def test_the_real_client_goes_unknown_with_the_id_of_what_it_sent(live_db) -> None:
    """The whole live path through the installed client, offline: the order is built and
    signed with a throwaway key, sent GTD post-only, the send gets no reply, and the exchange
    has no order with its id. The venue says unknown with that id, which is the digest the
    signature on the wire signs, and journals it (skipped without the library)."""
    lib = pytest.importorskip(clob.LIBRARY)
    exceptions = pytest.importorskip(f"{clob.LIBRARY}.exceptions")
    httpx = pytest.importorskip("httpx")
    account, client = offline_client(lib)
    client.set_api_creds(lib.ApiCreds(
        api_key="test-key", api_secret=base64.urlsafe_b64encode(b"test-secret").decode(),
        api_passphrase="test-passphrase"))
    sent: list[dict[str, Any]] = []
    asked: list[str] = []

    def post(endpoint, headers=None, data=None, params=None):
        sent.append(json.loads(data))
        raise exceptions.PolyApiException(error_msg="Request exception!")  # no reply

    def get(endpoint, headers=None, params=None):
        asked.append(endpoint)
        raise exceptions.PolyApiException(httpx.Response(404, json={"error": "Order not found"}))

    client._post, client._get = post, get
    v = clob.ClobRestingVenue(client, clock=lambda: NOW, sleep=_no_wait)
    with pytest.raises(clob.NoReply) as unknown:
        await v.place(request(token_id=TOKEN, up_token=TOKEN, size=5.02, price=0.47,
                              best_ask=0.49), now=NOW)
    (body,) = sent
    assert (body["orderType"], body["postOnly"]) == ("GTD", True)
    wire = body["order"]
    assert (wire["side"], wire["expiration"], wire["takerAmount"]) == ("BUY", str(END), "5020000")
    order_id = unknown.value.order_id
    assert signer_of(order_id, wire["signature"]) == account.address
    assert asked == [f"https://clob.invalid/data/order/{order_id}"] * clob.LOOKUPS
    (row,) = await journal.journal_rows()
    assert (row["status"], row["clob_order_id"], row["size"]) == ("ERROR", order_id, 5.02)


async def test_the_real_library_sends_every_drawn_size_whole() -> None:
    """The library cuts a size to hundredths as floor(size * 100) / 100 in floats, which on
    its own sends 5.01 shares for 5.02. Every size ``maths.draw_size`` can make goes out
    whole through the venue's arguments, and costs size x price: 5 to 10 shares at every
    price on a 0.01 tick, and up to 100 shares at a few (skipped without the library)."""
    lib = pytest.importorskip(clob.LIBRARY)
    builder = pytest.importorskip(f"{clob.LIBRARY}.order_builder.builder")
    amounts = object.__new__(builder.OrderBuilder).get_order_amounts  # no signer needed
    config = builder.ROUNDING_CONFIG["0.01"]
    types = SimpleNamespace(OrderArgs=lib.OrderArgs)
    cases = [(steps, cents) for cents in range(1, 100) for steps in range(500, 1001)]
    cases += [(steps, cents) for cents in (1, 47, 99) for steps in range(1001, 10_001)]
    wrong = []
    for steps, cents in cases:
        size = round(steps * maths.SHARE_STEP, 6)  # as draw_size rounds it
        args = clob._order_args(types, request(size=size, price=round(cents * 0.01, 6)))
        _, maker, taker = amounts(args.side, args.size, args.price, config)
        if (taker, maker) != (steps * 10_000, steps * cents * 100):
            wrong.append((size, cents, taker, maker))
    assert wrong == []


async def test_derived_secrets_are_scrubbed() -> None:
    secret = "s3cr3t-api-passphrase-value"
    _logging.register_secret(secret)
    assert _logging.redact_secrets(f"auth {secret} failed") == "auth <redacted:secret> failed"


async def test_an_order_still_open_long_past_its_stop_is_closed_forced(live_db) -> None:
    fake = FakeClob()
    v = venue(fake)
    fake.orders["0xA"] = {"status": "SOMETHING_NEW", "original_size": "5", "size_matched": "2",
                          "expiration": str(END)}
    assert not (await v.fills(["0xA"], now=END))["0xA"].final
    view = (await v.fills(["0xA"], now=END - 60 + clob.STUCK_AFTER_STOP_S))["0xA"]
    assert view.final and view.forced and view.state == "expired" and view.filled_size == 2.0


@pytest.mark.parametrize("status, state", [("EXPIRED", "expired"), ("INVALID", "cancelled")])
async def test_other_closed_statuses_are_final(live_db, status, state) -> None:
    fake = FakeClob()
    fake.orders["0xA"] = {"status": status, "original_size": "5", "size_matched": "0"}
    view = (await venue(fake).fills(["0xA"], now=NOW))["0xA"]
    assert view.final and view.state == state
