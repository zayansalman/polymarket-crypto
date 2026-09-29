"""Kelly horse-race inputs (``ems/kelly_horse_race/inputs.py``) against fakes."""

from __future__ import annotations

import pytest

from ems.kelly_horse_race import inputs
from tests.unit.test_kelly_runner import (
    CID,
    DOWN,
    END,
    NOW,
    SLUG,
    START,
    UP,
    make_hub,
    point,
    seed_venue,
)
from tests.unit.venue_fakes import GAMMA, FakeVenue

pytestmark = pytest.mark.asyncio

WIN = inputs.Window(slug=SLUG, start=START, end=END, up_token=UP, down_token=DOWN,
                    condition_id=CID)


@pytest.fixture
def venue() -> FakeVenue:
    v = FakeVenue()
    seed_venue(v)
    return v


async def test_the_book_is_read_by_price_not_by_position(venue) -> None:
    venue.book(UP, bids=[(0.10, 1.0), (0.50, 20.0), (0.49, 100.0)],
               asks=[(0.99, 1.0), (0.52, 30.0)], tick="0.01", min_size="5")
    book = await inputs.read_book(venue, UP)
    assert (book.best_bid, book.bid_size, book.best_ask) == (0.50, 20.0, 0.52)
    assert (book.tick_size, book.min_order_size) == (0.01, 5.0)


async def test_an_empty_side_is_none(venue) -> None:
    venue.book(DOWN, bids=[], asks=[])
    book = await inputs.read_book(venue, DOWN)
    assert book.best_bid is None and book.bid_size is None and book.best_ask is None


async def test_a_failed_book_read_waits(venue) -> None:
    venue.book_status[UP] = 503
    with pytest.raises(inputs.NotReady) as missing:
        await inputs.read_book(venue, UP)
    assert missing.value.code == "book_failed" and not missing.value.final


async def test_a_stale_price_now_waits() -> None:
    hub = make_hub(until=int(NOW) - 10)
    with pytest.raises(inputs.NotReady) as missing:
        inputs.price_now(hub, NOW)
    assert missing.value.code == "x_stale"
    hub.prints.append(point(int(NOW) - 1, 100_010.0))
    assert inputs.price_now(hub, NOW).value == 100_010.0


async def test_missing_candles_wait(venue) -> None:
    venue.closes.pop(min(venue.closes))
    with pytest.raises(inputs.NotReady) as missing:
        await inputs.minute_returns(venue, NOW)
    assert missing.value.code == "klines_incomplete"


async def test_sixty_returns_oldest_first(venue) -> None:
    returns = await inputs.minute_returns(venue, NOW)
    assert len(returns) == 60 and returns[0] == pytest.approx(0.002)


async def test_the_market_id_comes_from_gamma_when_the_hub_lacks_it(venue) -> None:
    hub, memory = make_hub(cid=None), inputs.Memory()
    with pytest.raises(inputs.NotReady) as missing:
        await inputs.window(hub, venue, NOW, memory)
    assert missing.value.code == "condition_id_unknown"
    venue.condition_ids[SLUG] = "0xfromgamma"
    memory.gamma_retry_at.clear()
    win = await inputs.window(hub, venue, NOW, memory)
    assert win.condition_id == "0xfromgamma" and win.start == START
    assert hub.wants[-1] == ("btc", "15m", inputs.OWNER, False)


async def test_k_waits_while_a_reconnect_could_still_bring_the_open_print(venue) -> None:
    # A later print is held but the open's is not (a hole, or updates landing before a
    # reconnect's history): it can still arrive until OPEN_PRINT_BACKFILL_S after the open.
    # Gamma is never asked (it publishes priceToBeat only at the window end).
    venue.price_to_beat[SLUG] = 99_950.0
    hub = make_hub(skip_open=True, until=START + 4)
    with pytest.raises(inputs.NotReady) as missing:
        inputs.price_to_beat(hub, WIN, START + 5)
    assert missing.value.code == "k_missing" and not missing.value.final
    with pytest.raises(inputs.NotReady) as missing:
        inputs.price_to_beat(hub, WIN, START + inputs.OPEN_PRINT_BACKFILL_S)
    assert not missing.value.final
    # A reconnect's history brings it back, in time order.
    at = next(i for i, p in enumerate(hub.prints) if p.obs_ms // 1000 > START)
    hub.prints.insert(at, point(START, 100_000.0))
    assert inputs.price_to_beat(hub, WIN, START + 30) == (100_000.0, inputs.K_FROM_PRINT)
    assert not [url for url, _ in venue.calls if url.startswith(GAMMA)]


async def test_k_is_skipped_once_no_reconnect_can_bring_it(venue) -> None:
    hub = make_hub(skip_open=True, until=START + 4)
    with pytest.raises(inputs.NotReady) as missing:
        inputs.price_to_beat(hub, WIN, START + inputs.OPEN_PRINT_BACKFILL_S + 1)
    assert missing.value.code == "k_missing" and missing.value.final
    assert "did not see the TWAP-60s print at the window's open" in missing.value.message
    assert "Gamma" not in missing.value.message


async def test_k_is_pending_just_after_the_open(venue) -> None:
    hub = make_hub(until=START - 1)  # the feed has nothing from the open on
    with pytest.raises(inputs.NotReady) as missing:
        inputs.price_to_beat(hub, WIN, START + 3)
    assert missing.value.code == "k_pending" and not missing.value.final
    with pytest.raises(inputs.NotReady) as missing:
        inputs.price_to_beat(hub, WIN, START + inputs.OPEN_PRINT_WAIT_S + 1)
    assert missing.value.code == "k_missing" and not missing.value.final


async def test_a_rolling_window_waits(venue) -> None:
    hub = make_hub(start=START - 900)
    with pytest.raises(inputs.NotReady) as missing:
        await inputs.window(hub, venue, NOW, inputs.Memory())
    assert missing.value.code == "window_rolling"
