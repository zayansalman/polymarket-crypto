"""The live venue (``ems/execution/clob.py``) against a fake CLOB client: the post-only GTD
arguments, the kill switch, rejections journaled, cancel by id (never cancel-all), fills from
``size_matched``, and the wallet checks. No network; the private key never appears."""

from __future__ import annotations

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

pytestmark = pytest.mark.asyncio

START = 1_789_935_300
END = START + 900
NOW = START + 30
UP, DOWN = "UP-btc", "DN-btc"
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


TYPES = SimpleNamespace(OrderArgs=Args, PartialCreateOrderOptions=Options,
                        OrderType=SimpleNamespace(GTD="GTD", GTC="GTC"))


class FakeClob:
    def __init__(self) -> None:
        self.posted: list[tuple] = []
        self.cancel_calls: list[list[str]] = []
        self.cancel_all_calls = 0
        self.orders: dict[str, dict[str, Any]] = {}
        self.post_reply: Any = {"success": True, "orderID": "0xORDER1", "status": "live",
                                "errorMsg": ""}
        self.post_error: Exception | None = None

    def create_and_post_order(self, order_args, options=None, order_type="GTC",
                              post_only=False, defer_exec=False):
        if self.post_error is not None:
            raise self.post_error
        self.posted.append((order_args, options, order_type, post_only))
        return self.post_reply

    def cancel_orders(self, order_hashes: list) -> dict:
        self.cancel_calls.append(list(order_hashes))
        return {"canceled": [h for h in order_hashes if h in self.orders],
                "not_canceled": {h: "not found" for h in order_hashes if h not in self.orders}}

    def cancel_all(self) -> dict:  # must never be called
        self.cancel_all_calls += 1
        return {}

    def get_order(self, order_id: str):
        return self.orders.get(order_id)


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


def venue(fake: FakeClob) -> clob.ClobRestingVenue:
    return clob.ClobRestingVenue(fake, api=TYPES, clock=lambda: NOW)


async def test_a_post_only_gtd_buy_expiring_at_the_window_end(live_db) -> None:
    fake = FakeClob()
    placed = await venue(fake).place(request(), now=NOW)
    assert placed == resting.Placed(order_id="0xORDER1", placed_ts=NOW)
    ((args, options, order_type, post_only),) = fake.posted
    assert (args.token_id, args.price, args.size, args.side) == (UP, 0.50, 7.5, "BUY")
    assert args.expiration == END and options.tick_size == "0.01"
    assert order_type == "GTD" and post_only is True
    (row,) = await journal.journal_rows()
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


async def test_a_failed_send_is_journaled_without_the_key(live_db, monkeypatch) -> None:
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", KEY)
    fake = FakeClob()
    fake.post_error = RuntimeError(f"signer {KEY} rejected")
    with pytest.raises(PlacementRefused) as refused:
        await venue(fake).place(request(), now=NOW)
    assert refused.value.reason == "venue_error" and KEY not in str(refused.value)
    (row,) = await journal.journal_rows()
    assert row["status"] == "ERROR" and KEY not in row["error"] and "<redacted" in row["error"]


async def test_cancel_by_id_never_cancel_all(live_db) -> None:
    fake = FakeClob()
    fake.orders["0xORDER1"] = {"status": "live", "original_size": "7.5", "size_matched": "0"}
    assert await venue(fake).cancel(["0xORDER1", "0xGONE"], reason="window_end", now=NOW) == 1
    assert fake.cancel_calls == [["0xORDER1", "0xGONE"]] and fake.cancel_all_calls == 0
    rows = {r["clob_order_id"]: r for r in await journal.journal_rows()}
    assert rows["0xORDER1"]["status"] == "CANCELLED" and rows["0xGONE"]["status"] == "ERROR"


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
    fake.orders["0xA"] = {"status": "live", "original_size": "5", "size_matched": "3"}
    await v.fills(["0xA"], now=NOW)
    del fake.orders["0xA"]
    view = (await v.fills(["0xA"], now=END + 600))["0xA"]
    assert view.final and view.filled_size == 3.0


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
