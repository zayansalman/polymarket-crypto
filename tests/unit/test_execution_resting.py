"""The paper resting-order venue (``ems/execution/resting.py``) against a fake trade tape.

The queue ahead, the window-end cap and the cancel cap; when an order is final; two orders on
one outcome sharing a trade; and every refusal writing nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from ems import config as _config
from ems import db as _db
from ems.execution import resting
from ems.execution.controls import PlacementRefused
from tests.unit.venue_fakes import FakeVenue, trade

pytestmark = pytest.mark.asyncio

START = 1_789_935_300  # a 15m window
END = START + 900
STOP = END - resting.GTD_STOP_S
CID, UP, DOWN = "0xcid-btc", "UP-btc", "DN-btc"


@pytest_asyncio.fixture
async def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "resting.db")
    monkeypatch.setattr(_config, "KILL_SWITCH_PATH", tmp_path / "KILL")
    await _db.init_db()
    return tmp_path


@pytest.fixture
def venue() -> FakeVenue:
    return FakeVenue()


def request(**kw) -> resting.PlaceRequest:
    base = dict(strategy="test", condition_id=CID, token_id=UP, outcome="Up", up_token=UP,
                down_token=DOWN, price=0.40, size=5.0, expires_ts=END, tick_size=0.01,
                queue_ahead=10.0, best_ask=0.42)
    base.update(kw)
    return resting.PlaceRequest(**base)


def sell_up(ts: int, size: float, price: float = 0.40) -> dict:
    return trade(ts, "Up", "SELL", size, price, cid=CID, up=UP, down=DOWN)


def nudge(ts: int) -> dict:
    """A trade far above our bids, only there to show the tape has got to ``ts``: the newest
    second of a reply is not trusted to be complete."""
    return sell_up(ts, 1.0, price=0.99)


async def rows() -> list[dict]:
    async with _db.connect() as conn:
        async with conn.execute("SELECT * FROM paper_resting_orders ORDER BY id") as cur:
            return [dict(r) for r in await cur.fetchall()]


async def test_place_writes_the_order_resting_from_the_next_second(temp_db, venue) -> None:
    paper = resting.PaperRestingVenue(venue)
    placed = await paper.place(request(), now=START + 10.4)
    assert placed == resting.Placed(order_id="paper-1", placed_ts=START + 11)
    (row,) = await rows()
    assert row["stop_ts"] == STOP and row["expires_ts"] == END
    assert row["flow_cursor_ts"] == START + 11 and row["queue_ahead"] == 10.0


@pytest.mark.parametrize("kw, reason", [
    (dict(price=0.42), "would_cross"),           # a bid at the ask would take
    (dict(price=0.405), "bad_order"),            # off the 0.01 tick
    (dict(size=5.001), "bad_order"),             # not hundredths of a share
    (dict(token_id="other"), "bad_order"),       # not one of the window's tokens
    (dict(expires_ts=START + 70), "too_late"),   # the venue would stop it at once
])
async def test_refusals_write_nothing(temp_db, venue, kw, reason) -> None:
    paper = resting.PaperRestingVenue(venue)
    with pytest.raises(PlacementRefused) as refused:
        await paper.place(request(**kw), now=START + 10)
    assert refused.value.reason == reason
    assert await rows() == []


async def test_kill_switch_blocks_placement(temp_db, venue) -> None:
    (temp_db / "KILL").touch()
    with pytest.raises(PlacementRefused) as refused:
        await resting.PaperRestingVenue(venue).place(request(), now=START + 10)
    assert refused.value.reason == "kill_switch"
    assert await rows() == []


async def test_fills_only_past_the_queue_ahead(temp_db, venue) -> None:
    """filled = min(size, max(0, crossed - queue_ahead)), trades after placement only."""
    paper = resting.PaperRestingVenue(venue)
    placed = await paper.place(request(queue_ahead=10.0, size=5.0), now=START + 10)
    venue.add(CID, sell_up(START + 5, 50.0))    # before placement: not ours
    venue.add(CID, sell_up(START + 20, 8.0))    # uses 8 of the 10 ahead
    venue.add(CID, sell_up(START + 30, 3.0))    # 2 more ahead, then 1 is ours
    venue.add(CID, nudge(START + 35))
    view = (await paper.fills([placed.order_id], now=START + 60))[placed.order_id]
    assert view.filled_size == pytest.approx(1.0)
    assert view.state == resting.RESTING and not view.final
    venue.add(CID, sell_up(START + 40, 100.0), nudge(START + 45))  # the rest, capped at the size
    view = (await paper.fills([placed.order_id], now=START + 90))[placed.order_id]
    assert view.filled_size == pytest.approx(5.0)
    assert view.state == resting.FILLED and view.final


async def test_trades_above_our_price_never_fill(temp_db, venue) -> None:
    paper = resting.PaperRestingVenue(venue)
    placed = await paper.place(request(queue_ahead=0.0), now=START + 10)
    venue.add(CID, sell_up(START + 20, 50.0, price=0.41), nudge(START + 30))
    view = (await paper.fills([placed.order_id], now=START + 60))[placed.order_id]
    assert view.filled_size == 0.0


async def test_the_window_end_caps_the_fills(temp_db, venue) -> None:
    """The venue stops the order 60 s before its expiry; trades after that are not ours."""
    paper = resting.PaperRestingVenue(venue)
    placed = await paper.place(request(queue_ahead=0.0), now=START + 10)
    venue.add(CID, sell_up(STOP, 50.0), sell_up(END + 5, 1.0))
    view = (await paper.fills([placed.order_id], now=END + 10))[placed.order_id]
    assert view.filled_size == 0.0
    assert view.state == resting.EXPIRED and view.final and view.closed_ts == STOP


async def test_a_cancel_caps_the_fills_but_keeps_earlier_ones(temp_db, venue) -> None:
    paper = resting.PaperRestingVenue(venue)
    placed = await paper.place(request(queue_ahead=0.0), now=START + 10)
    assert await paper.cancel([placed.order_id], reason="test", now=START + 100.7) == 1
    # The tape runs late: a trade while it rested shows up after the cancel, and counts.
    venue.add(CID, sell_up(START + 50, 2.0), sell_up(START + 100, 9.0), sell_up(START + 200, 1.0))
    view = (await paper.fills([placed.order_id], now=START + 300))[placed.order_id]
    assert view.filled_size == pytest.approx(2.0)
    assert view.state == resting.CANCELLED and view.closed_ts == START + 100
    assert view.final
    assert await paper.cancel([placed.order_id], reason="again", now=START + 400) == 0


async def test_not_final_until_the_tape_vouches_for_the_whole_stretch(temp_db, venue) -> None:
    paper = resting.PaperRestingVenue(venue, max_tape_lag_s=900)
    placed = await paper.place(request(queue_ahead=0.0), now=START + 10)
    await paper.cancel([placed.order_id], reason="test", now=START + 100)
    venue.add(CID, sell_up(START + 40, 1.0), nudge(START + 50))  # the tape has got to +50
    view = (await paper.fills([placed.order_id], now=START + 120))[placed.order_id]
    assert view.filled_size == pytest.approx(1.0) and not view.final
    venue.add(CID, sell_up(START + 150, 0.5, price=0.99))  # a later record vouches for it
    view = (await paper.fills([placed.order_id], now=START + 160))[placed.order_id]
    assert view.final and view.filled_size == pytest.approx(1.0)


async def test_a_quiet_tape_is_trusted_once_old_enough_and_another_tape_is_ahead(
    temp_db, venue
) -> None:
    paper = resting.PaperRestingVenue(venue, max_tape_lag_s=300)
    placed = await paper.place(request(queue_ahead=0.0), now=START + 10)
    await paper.cancel([placed.order_id], reason="test", now=START + 100)
    view = (await paper.fills([placed.order_id], now=START + 450))[placed.order_id]
    assert not view.final  # its own tape is empty and nothing else shows the tape moved on
    other_cid = "0xcid-next"
    await paper.place(request(condition_id=other_cid, expires_ts=END + 900),
                      now=START + 200)
    venue.add(other_cid, trade(START + 440, "Up", "SELL", 1.0, 0.99, cid=other_cid,
                               up=UP, down=DOWN))
    view = (await paper.fills([placed.order_id], now=START + 450))[placed.order_id]
    assert view.final and view.filled_size == 0.0


async def test_two_orders_on_one_outcome_share_a_trade(temp_db, venue) -> None:
    """Paper orders are not in the real book: one trade fills them at most by its size."""
    paper = resting.PaperRestingVenue(venue)
    first = await paper.place(request(strategy="a", queue_ahead=0.0), now=START + 10)
    second = await paper.place(request(strategy="b", queue_ahead=0.0), now=START + 11)
    venue.add(CID, sell_up(START + 30, 6.0), nudge(START + 40))
    views = await paper.fills([first.order_id], now=START + 60)
    assert views[first.order_id].filled_size == pytest.approx(5.0)
    views = await paper.fills([second.order_id], now=START + 60)
    assert views[second.order_id].filled_size == pytest.approx(1.0)


async def test_an_unreadable_tape_moves_nothing_and_says_so(temp_db, venue) -> None:
    paper = resting.PaperRestingVenue(venue)
    placed = await paper.place(request(queue_ahead=0.0), now=START + 10)
    venue.add(CID, sell_up(START + 20, 5.0))
    venue.tape_status[CID] = 503
    view = (await paper.fills([placed.order_id], now=START + 60))[placed.order_id]
    assert view.filled_size == 0.0
    assert paper.last_errors and "HTTP 503" in paper.last_errors[0]
    (row,) = await rows()
    assert row["flow_cursor_ts"] == START + 11


async def test_forced_final_when_the_tape_never_catches_up(temp_db, venue) -> None:
    paper = resting.PaperRestingVenue(venue, force_final_after_s=600)
    placed = await paper.place(request(queue_ahead=0.0), now=START + 10)
    venue.tape_status[CID] = 503
    view = (await paper.fills([placed.order_id], now=STOP + 601))[placed.order_id]
    assert view.final and view.forced


async def test_unknown_ids_are_left_out(temp_db, venue) -> None:
    paper = resting.PaperRestingVenue(venue)
    assert await paper.fills(["paper-99", "0xlive", "paper-x"], now=START) == {}
    assert await paper.cancel(["paper-99"], reason="x", now=START) == 0


async def test_the_paper_venue_meets_the_protocol() -> None:
    assert isinstance(resting.PaperRestingVenue(FakeVenue()), resting.RestingVenue)
