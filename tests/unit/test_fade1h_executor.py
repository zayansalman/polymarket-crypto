"""Fade 1h Momentum on 15m: the paper executor, its fill model, and settlement.

Runs against the real schema from ``db.init_db`` on a temp file, with a fake venue standing in
for the public trade tape (data-api) and the order-book service (CLOB). The fake serves real
``httpx.Response`` objects, newest record first, paged by offset, like the venue.
"""

from __future__ import annotations

import itertools
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

import config as _config
import db as _db
from polymarket_bot.fade_1h_momentum_15m import executor as ex
from polymarket_bot.fade_1h_momentum_15m import ledger
from polymarket_bot.fade_1h_momentum_15m.ledger import NewOrder
from polymarket_bot.maker import filler as maker_filler

HOUR = 1_789_934_400  # a UTC hour boundary
START = HOUR + 900  # the hour's second quarter
END = START + 900
LAG = 900  # the default tape lag allowance


# ---------------------------------------------------------------------------
# A fake venue
# ---------------------------------------------------------------------------


class FakeVenue:
    """The data-api trade tape and the CLOB market lookup, in memory."""

    def __init__(self) -> None:
        self.tape: dict[str, list[dict]] = {}
        self.markets: dict[str, dict] = {}
        self.tape_status: dict[str, int] = {}
        self.calls: list[tuple[str, dict, dict]] = []
        self._seq = itertools.count()

    def add(self, cid: str, *records: dict) -> None:
        for rec in records:
            self.tape.setdefault(cid, []).append({**rec, "_seq": next(self._seq)})

    def resolve(self, cid: str, *, winner_token: str | None, up: str, down: str,
                closed: bool = True) -> None:
        self.markets[cid] = {
            "condition_id": cid, "closed": closed,
            "tokens": [
                {"token_id": up, "outcome": "Up", "winner": winner_token == up},
                {"token_id": down, "outcome": "Down", "winner": winner_token == down},
            ],
        }

    def tape_calls(self, cid: str | None = None) -> list[dict]:
        return [p for url, p, _ in self.calls if url == f"{ex.DATA_API}/trades"
                and (cid is None or p["market"] == cid)]

    def clob_calls(self) -> list[str]:
        return [url for url, _, _ in self.calls if url.startswith(f"{ex.CLOB}/markets/")]

    def _page(self, cid: str, offset: int, limit: int) -> list[dict]:
        records = sorted(self.tape.get(cid, []), key=lambda r: (-r["timestamp"], -r["_seq"]))
        return [{k: v for k, v in r.items() if k != "_seq"} for r in records[offset:offset + limit]]

    async def get(self, url, *, params=None, headers=None, timeout=None):
        params = dict(params or {})
        self.calls.append((url, params, dict(headers or {})))
        request = httpx.Request("GET", url)
        if url == f"{ex.DATA_API}/trades":
            assert params["takerOnly"] == "true"
            cid = params["market"]
            if cid in self.tape_status:
                return httpx.Response(self.tape_status[cid], json={}, request=request)
            page = self._page(cid, int(params["offset"]), int(params["limit"]))
            return httpx.Response(200, json=page, request=request)
        if url.startswith(f"{ex.CLOB}/markets/"):
            cid = url.rsplit("/", 1)[1]
            if cid not in self.markets:
                return httpx.Response(404, json={"error": "market not found"}, request=request)
            return httpx.Response(200, json=self.markets[cid], request=request)
        raise AssertionError(f"unexpected request: {url} {params}")


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def fade_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "fade.db")
    monkeypatch.setattr(_config, "KILL_SWITCH_PATH", tmp_path / "KILL")
    await _db.init_db()
    await _db.set_config(ex.MODE_KEY, "paper")
    return _db


@pytest.fixture
def venue() -> FakeVenue:
    return FakeVenue()


_TX = itertools.count()


def cid_of(asset: str = "btc", start: int = START) -> str:
    return f"0xcid-{asset}-{start}"


def up_of(asset: str = "btc", start: int = START) -> str:
    return f"UP-{asset}-{start}"


def down_of(asset: str = "btc", start: int = START) -> str:
    return f"DN-{asset}-{start}"


def trade(ts: int, outcome: str, side: str, size: float, price: float, *,
          asset: str = "btc", start: int = START) -> dict:
    """One taker record, shaped like the data-api's."""
    return {
        "timestamp": ts, "side": side, "size": size, "price": price,
        "asset": up_of(asset, start) if outcome == "Up" else down_of(asset, start),
        "outcome": outcome, "outcomeIndex": 0 if outcome == "Up" else 1,
        "conditionId": cid_of(asset, start), "proxyWallet": "0xtaker",
        "transactionHash": f"0xtx{next(_TX)}",
    }


def nudge(ts: int, *, asset: str = "btc", start: int = START) -> dict:
    """A trade that reaches none of our bids (a sale of Up at 0.99, far above them), only
    there to show the tape has got to ``ts``."""
    return trade(ts, "Up", "SELL", 1.0, 0.99, asset=asset, start=start)


async def window(asset: str = "btc", start: int = START, *, cid: bool = True) -> str:
    slug = f"{asset}-updown-15m-{start}"
    await ledger.upsert_window(
        window_slug=slug, asset=asset, window_start=start, window_end=start + 900, ts=start,
        condition_id=cid_of(asset, start) if cid else None, up_token=up_of(asset, start),
        down_token=down_of(asset, start),
    )
    return slug


def bid(slug: str, side: str = "Up", *, price: float = 0.40, shares: float = 10.0,
        depth: float = 0.0, rung: int = 0, kind: str = "entry") -> NewOrder:
    asset, _, _, start = slug.split("-")
    token = up_of(asset, int(start)) if side == "Up" else down_of(asset, int(start))
    return NewOrder(window_slug=slug, token_id=token, side=side, kind=kind, price=price,
                    shares=shares, rung=rung, depth_ahead=depth)


async def rest(executor: ex.PaperExecutor, *orders: NewOrder, at: int) -> list[int]:
    ids = await executor.requote(orders, best_asks={o.token_id: 0.99 for o in orders}, now=at)
    assert all(i is not None for i in ids)
    return [int(i) for i in ids if i is not None]


async def order_row(order_id: int) -> dict:
    async with _db.connect() as conn:
        async with conn.execute("SELECT * FROM fade_orders WHERE id = ?", (order_id,)) as cur:
            row = await cur.fetchone()
    assert row is not None
    return dict(row)


async def order_count() -> int:
    async with _db.connect() as conn:
        async with conn.execute("SELECT COUNT(*) AS n FROM fade_orders") as cur:
            row = await cur.fetchone()
    return int(row["n"])


# ---------------------------------------------------------------------------
# Pure queue maths
# ---------------------------------------------------------------------------


def test_queue_ahead_is_everything_at_our_price_or_better() -> None:
    levels = [(0.45, 10.0), (0.42, 5.0), (0.40, 7.0), (0.39, 100.0)]
    assert ex.queue_ahead(levels, 0.40) == pytest.approx(22.0)
    assert ex.queue_ahead(levels, 0.46) == 0.0
    # Prices parsed from text land a hair off the grid; they still count at the level.
    assert ex.queue_ahead([(0.1 + 0.2, 3.0)], 0.30) == pytest.approx(3.0)


def test_each_record_sells_into_exactly_one_outcome_like_the_maker_rule() -> None:
    records = [
        ex.TapePrint(1, "Up", "SELL", 5.0, 0.40), ex.TapePrint(2, "Up", "SELL", 3.0, 0.45),
        ex.TapePrint(3, "Down", "BUY", 7.0, 0.62), ex.TapePrint(4, "Down", "BUY", 2.0, 0.55),
        ex.TapePrint(5, "Up", "BUY", 4.0, 0.47), ex.TapePrint(6, "Down", "SELL", 6.0, 0.50),
        ex.TapePrint(7, "Up", "BUY", 1.0, 0.52), ex.TapePrint(8, "Down", "SELL", 9.0, 0.61),
    ]
    flow = [(p.ts, 0 if p.outcome == "Up" else 1, p.side, p.size, p.price) for p in records]
    for side, index in (("Up", 0), ("Down", 1)):
        for price in (0.38, 0.40, 0.45, 0.50, 0.55, 0.60):
            ours = sum(p.size for p in records
                       if p.hits()[0] == side and p.hits()[1] <= price + 1e-9)
            assert ours == pytest.approx(
                maker_filler.crossed_volume(flow, our_index=index, our_price=price)
            ), (side, price)


def _queued(order_id: int, price: float, *, shares: float = 20.0, depth: float = 0.0,
            side: str = "Up", flow_from: int = 0, flow_to: int = 100, **kw) -> ex.QueuedBid:
    return ex.QueuedBid(order_id=order_id, side=side, price=price, shares=shares,
                        depth_ahead=depth, flow_from=flow_from, flow_to=flow_to, **kw)


def test_one_record_fills_our_rungs_top_down_and_never_beyond_its_size() -> None:
    high, low = _queued(1, 0.45, depth=10.0), _queued(2, 0.40, depth=25.0)
    flows = ex.allocate_fills([ex.TapePrint(10, "Up", "SELL", 50.0, 0.39)], [low, high])
    # The high rung works through its 10-share queue, then takes its 20.
    assert (flows[1].filled, flows[1].crossed, flows[1].fill_ts) == (20.0, 50.0, 10)
    # The low rung sees only the 30 left: 25 of queue, then 5 for us.
    assert (flows[2].filled, flows[2].crossed, flows[2].fill_ts) == (5.0, 30.0, 10)
    assert flows[1].filled + flows[2].filled <= 50.0

    flows = ex.allocate_fills([ex.TapePrint(10, "Up", "SELL", 25.0, 0.39)],
                              [_queued(1, 0.45), _queued(2, 0.40)])
    assert (flows[1].filled, flows[2].filled) == (20.0, 5.0)


def test_a_record_above_a_rung_reaches_only_the_rungs_it_crossed() -> None:
    flows = ex.allocate_fills([ex.TapePrint(10, "Up", "SELL", 50.0, 0.43)],
                              [_queued(1, 0.45), _queued(2, 0.40)])
    assert (flows[1].filled, flows[2].filled, flows[2].crossed) == (20.0, 0.0, 0.0)


def test_allocation_carries_on_from_what_was_read_before() -> None:
    queued = _queued(1, 0.40, shares=10.0, depth=30.0, crossed=20.0)
    flows = ex.allocate_fills([ex.TapePrint(50, "Up", "SELL", 25.0, 0.40)], [queued])
    assert (flows[1].crossed, flows[1].filled, flows[1].added, flows[1].fill_ts) == (
        45.0, 10.0, 10.0, 50)


# ---------------------------------------------------------------------------
# Fills through the ledger
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rungs_fill_top_down_through_the_ledger(fade_db, venue) -> None:
    slug = await window()
    paper = ex.PaperExecutor(venue)
    high, low = await rest(paper, bid(slug, price=0.45, shares=20, depth=10, rung=0),
                           bid(slug, price=0.40, shares=20, depth=25, rung=1), at=START + 10)
    venue.add(cid_of(), trade(START + 30, "Up", "SELL", 50.0, 0.39), nudge(START + 60))

    report = await paper.sync_fills(now=START + 120)

    assert report.errors == []
    assert (report.markets_read, report.orders_updated) == (1, 2)
    assert sorted((f.order_id, f.shares, f.ts) for f in report.fills) == [
        (high, 20.0, START + 30), (low, 5.0, START + 30)]
    top, bottom = await order_row(high), await order_row(low)
    assert (top["state"], top["filled_shares"], top["filled_ts"]) == ("filled", 20.0, START + 30)
    assert (bottom["state"], bottom["filled_shares"], bottom["crossed"]) == ("partial", 5.0, 30.0)
    assert bottom["flow_cursor_ts"] == START + 60  # the newest record's second is read next time
    # The tape is read with the browser user agent the venue needs.
    assert venue.calls[0][2]["User-Agent"].startswith("Mozilla/5.0")


@pytest.mark.asyncio
async def test_flow_counts_only_while_the_bid_rests(fade_db, venue) -> None:
    slug = await window()
    paper = ex.PaperExecutor(venue)
    (order_id,) = await rest(paper, bid(slug, price=0.40, shares=10), at=START + 10)
    assert await paper.cancel([order_id], reason="requote", now=START + 60) == 1
    venue.add(
        cid_of(),
        trade(START + 5, "Up", "SELL", 50.0, 0.30),  # before it was placed
        trade(START + 30, "Up", "SELL", 4.0, 0.40),  # while resting
        trade(START + 90, "Up", "SELL", 100.0, 0.30),  # after the cancel
        nudge(START + 200),
    )

    report = await paper.sync_fills(now=START + 300)

    row = await order_row(order_id)
    assert (row["state"], row["filled_shares"], row["filled_ts"]) == ("cancelled", 4.0, START + 30)
    assert (row["crossed"], row["flow_cursor_ts"]) == (4.0, START + 60)
    assert [f.shares for f in report.fills] == [4.0]
    assert await ledger.orders_needing_flow() == []


@pytest.mark.asyncio
async def test_flow_stops_at_the_window_end(fade_db, venue) -> None:
    slug = await window()
    paper = ex.PaperExecutor(venue)
    (order_id,) = await rest(paper, bid(slug, price=0.40, shares=10), at=END - 60)
    venue.add(cid_of(), trade(END - 30, "Up", "SELL", 3.0, 0.40),
              trade(END + 10, "Up", "SELL", 50.0, 0.20), nudge(END + 40))

    report = await paper.sync_fills(now=END + 60)

    assert report.expired == 1
    row = await order_row(order_id)
    assert (row["state"], row["filled_shares"], row["cancel_reason"]) == ("expired", 3.0,
                                                                          "window_end")
    assert (row["cancelled_ts"], row["flow_cursor_ts"]) == (END, END)


@pytest.mark.asyncio
async def test_the_other_outcomes_buyers_fill_our_bids_without_double_counting(
    fade_db, venue
) -> None:
    slug = await window()
    paper = ex.PaperExecutor(venue)
    entry, hedge = await rest(paper, bid(slug, "Up", price=0.40, shares=10),
                              bid(slug, "Down", price=0.55, shares=10, kind="hedge"),
                              at=START + 10)
    venue.add(
        cid_of(),
        trade(START + 20, "Down", "BUY", 6.0, 0.58),  # a sale of Up at 0.42: above our bid
        trade(START + 21, "Down", "BUY", 7.0, 0.62),  # a sale of Up at 0.38: fills the entry
        trade(START + 22, "Down", "SELL", 3.0, 0.55),  # a sale of Down at 0.55: the hedge
        trade(START + 23, "Up", "BUY", 4.0, 0.44),  # a sale of Down at 0.56: above the hedge
        trade(START + 24, "Up", "BUY", 5.0, 0.46),  # a sale of Down at 0.54: the hedge
        trade(START + 25, "Up", "SELL", 2.0, 0.41),  # a sale of Up at 0.41: above the entry
        nudge(START + 40),
    )

    await paper.sync_fills(now=START + 100)

    up, down = await order_row(entry), await order_row(hedge)
    assert (up["filled_shares"], up["crossed"], up["filled_ts"]) == (7.0, 7.0, START + 21)
    assert (down["filled_shares"], down["crossed"], down["filled_ts"]) == (8.0, 8.0, START + 22)
    positions = {p["side"]: p for p in await ledger.open_positions()}
    assert positions["Down"]["hedge_shares"] == 8.0


@pytest.mark.asyncio
async def test_the_queue_ahead_trades_first_across_passes(fade_db, venue) -> None:
    slug = await window()
    paper = ex.PaperExecutor(venue)
    (order_id,) = await rest(paper, bid(slug, price=0.40, shares=10, depth=30), at=START + 10)
    venue.add(cid_of(), trade(START + 20, "Up", "SELL", 20.0, 0.40), nudge(START + 40))

    first = await paper.sync_fills(now=START + 60)
    row = await order_row(order_id)
    assert (first.fills, row["crossed"], row["filled_shares"], row["state"]) == (
        [], 20.0, 0.0, "resting")

    venue.add(cid_of(), trade(START + 70, "Up", "SELL", 25.0, 0.39), nudge(START + 90))
    second = await paper.sync_fills(now=START + 100)
    row = await order_row(order_id)
    assert [(f.shares, f.ts) for f in second.fills] == [(10.0, START + 70)]
    assert (row["crossed"], row["filled_shares"], row["state"]) == (45.0, 10.0, "filled")

    # Reading again finds nothing new and changes nothing.
    third = await paper.sync_fills(now=START + 110)
    assert (third.fills, (await order_row(order_id))["filled_shares"]) == ([], 10.0)


@pytest.mark.asyncio
async def test_the_newest_second_waits_and_a_quiet_tape_moves_on_after_the_lag(
    fade_db, venue
) -> None:
    slug = await window()
    paper = ex.PaperExecutor(venue)
    (order_id,) = await rest(paper, bid(slug, price=0.40, shares=10), at=START + 10)
    venue.add(cid_of(), trade(START + 30, "Up", "SELL", 5.0, 0.40))

    await paper.sync_fills(now=START + 100)
    row = await order_row(order_id)
    # More records from that second may still be on their way, so it is not read yet.
    assert (row["filled_shares"], row["flow_cursor_ts"]) == (0.0, START + 30)

    venue.add(cid_of(), nudge(START + 50))
    await paper.sync_fills(now=START + 110)
    row = await order_row(order_id)
    assert (row["filled_shares"], row["filled_ts"], row["flow_cursor_ts"]) == (
        5.0, START + 30, START + 50)

    # With no newer record, the tape is trusted only once the lag allowance has passed.
    await paper.sync_fills(now=START + 50 + LAG - 1)
    assert (await order_row(order_id))["flow_cursor_ts"] == START + 50
    await paper.sync_fills(now=START + 400 + LAG)
    assert (await order_row(order_id))["flow_cursor_ts"] == START + 400


@pytest.mark.asyncio
async def test_a_failed_read_moves_nothing_and_says_so(fade_db, venue) -> None:
    slug = await window()
    paper = ex.PaperExecutor(venue)
    (order_id,) = await rest(paper, bid(slug, price=0.40, shares=10), at=START + 10)
    venue.add(cid_of(), trade(START + 30, "Up", "SELL", 5.0, 0.40), nudge(START + 50))
    venue.tape_status[cid_of()] = 503

    report = await paper.sync_fills(now=START + 100)

    assert report.fills == [] and report.markets_read == 0
    assert report.errors == [
        f"{slug}: could not read the trade tape (HTTP 503). Its fills are checked again next pass."
    ]
    assert (await order_row(order_id))["flow_cursor_ts"] is None

    del venue.tape_status[cid_of()]
    report = await paper.sync_fills(now=START + 100)
    assert [f.shares for f in report.fills] == [5.0] and report.errors == []


@pytest.mark.asyncio
async def test_one_market_failing_does_not_stop_the_others(fade_db, venue) -> None:
    btc, eth = await window("btc"), await window("eth")
    paper = ex.PaperExecutor(venue)
    await rest(paper, bid(btc, price=0.40), bid(eth, price=0.40), at=START + 10)
    venue.add(cid_of("eth"), trade(START + 30, "Up", "SELL", 5.0, 0.40, asset="eth"),
              nudge(START + 50, asset="eth"))
    venue.tape_status[cid_of("btc")] = 500

    report = await paper.sync_fills(now=START + 100)

    assert [f.window_slug for f in report.fills] == [eth]
    assert len(report.errors) == 1 and report.errors[0].startswith(btc)


@pytest.mark.asyncio
async def test_long_tapes_are_paged_with_overlap_and_counted_once(fade_db, venue) -> None:
    slug = await window()
    paper = ex.PaperExecutor(venue)
    (order_id,) = await rest(paper, bid(slug, price=0.40, shares=5000), at=START + 10)
    venue.add(cid_of(), *(trade(START + 10 + i // 2, "Up", "SELL", 1.0, 0.40)
                          for i in range(1200)))

    await paper.sync_fills(now=START + 700)

    offsets = [int(p["offset"]) for p in venue.tape_calls()]
    assert offsets == [0, ex.TAPE_PAGE - ex.TAPE_PAGE_OVERLAP,
                       2 * (ex.TAPE_PAGE - ex.TAPE_PAGE_OVERLAP)]
    # Every record once, except the newest second's two, which wait for the next read.
    assert (await order_row(order_id))["filled_shares"] == 1198.0


@pytest.mark.asyncio
async def test_a_tape_that_shifts_while_being_paged_is_thrown_away(fade_db, venue) -> None:
    slug = await window()
    paper = ex.PaperExecutor(venue)
    (order_id,) = await rest(paper, bid(slug, price=0.40, shares=5000), at=START + 10)
    venue.add(cid_of(), *(trade(START + 10 + i // 2, "Up", "SELL", 1.0, 0.40)
                          for i in range(700)))
    real_page = venue._page

    def stale_second_page(cid: str, offset: int, limit: int) -> list[dict]:
        # A reply from an older copy of the tape: page two skips past the overlap.
        return real_page(cid, offset + 200 if offset else 0, limit)

    venue._page = stale_second_page  # type: ignore[method-assign]
    report = await paper.sync_fills(now=START + 700)

    assert report.errors == [
        f"{slug}: the trade tape shifted while it was being read. "
        "Its fills are checked again next pass."
    ]
    row = await order_row(order_id)
    assert (row["filled_shares"], row["flow_cursor_ts"]) == (0.0, None)


@pytest.mark.asyncio
async def test_a_restart_keeps_fills_made_before_it_and_none_after(fade_db, venue) -> None:
    slug = await window()
    before = ex.PaperExecutor(venue)
    (order_id,) = await rest(before, bid(slug, price=0.40, shares=10), at=START + 10)
    venue.add(cid_of(), trade(START + 20, "Up", "SELL", 3.0, 0.40), nudge(START + 50))
    await before.sync_fills(now=START + 60)
    assert (await order_row(order_id))["filled_shares"] == 3.0

    # The app restarts at START + 100: nothing in memory survives.
    after = ex.PaperBookkeeper(venue)
    assert await after.recover_after_restart(now=START + 100) == 1
    row = await order_row(order_id)
    assert (row["state"], row["cancel_reason"], row["cancelled_ts"]) == (
        "cancelled", ex.RESTART_REASON, START + 100)

    venue.add(cid_of(), trade(START + 70, "Up", "SELL", 4.0, 0.40),  # before the restart
              trade(START + 150, "Up", "SELL", 50.0, 0.40),  # after it: the bid was gone
              nudge(START + 200))
    report = await after.sync_fills(now=START + 300)

    row = await order_row(order_id)
    assert [(f.shares, f.ts) for f in report.fills] == [(4.0, START + 70)]
    assert (row["filled_shares"], row["flow_cursor_ts"]) == (7.0, START + 100)
    assert await ledger.orders_needing_flow() == []


@pytest.mark.asyncio
async def test_orders_without_a_market_id_are_reported(fade_db, venue) -> None:
    slug = await window(cid=False)
    paper = ex.PaperExecutor(venue)
    # The executor will not rest such a bid, but a row written straight to the ledger is
    # still reported rather than silently skipped.
    await ledger.place_orders([bid(slug, price=0.40)], ts=START + 10)

    report = await paper.sync_fills(now=START + 100)

    assert report.errors == [
        f"{slug}: no market id recorded, so its trade tape cannot be read."]
    assert venue.tape_calls() == []


@pytest.mark.asyncio
async def test_a_reply_holding_another_markets_trades_is_refused(fade_db, venue) -> None:
    slug = await window()
    paper = ex.PaperExecutor(venue)
    (order_id,) = await rest(paper, bid(slug, price=0.40), at=START + 10)
    venue.add(cid_of(), trade(START + 30, "Up", "SELL", 5.0, 0.40),
              trade(START + 50, "Up", "SELL", 5.0, 0.40, asset="eth"))

    report = await paper.sync_fills(now=START + 100)

    assert report.errors == [
        f"{slug}: the trade tape answered with another market's trades. "
        "Its fills are checked again next pass."
    ]
    assert (await order_row(order_id))["flow_cursor_ts"] is None


@pytest.mark.asyncio
async def test_unreadable_records_are_left_out_and_said_so(fade_db, venue) -> None:
    slug = await window()
    paper = ex.PaperExecutor(venue)
    (order_id,) = await rest(paper, bid(slug, price=0.40, shares=10), at=START + 10)
    venue.add(cid_of(), trade(START + 20, "Up", "SELL", 3.0, 0.40),
              {**trade(START + 25, "Up", "SELL", 4.0, 0.40), "side": "SPLIT"},
              {**trade(START + 26, "Up", "SELL", 4.0, 0.40), "size": "lots"},
              nudge(START + 40))

    report = await paper.sync_fills(now=START + 100)

    assert (await order_row(order_id))["filled_shares"] == 3.0
    assert report.errors == [
        f"{slug}: 2 trade record(s) could not be read and were left out, so fills there may "
        "be under-counted."
    ]


# ---------------------------------------------------------------------------
# Placing: resting bids only
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_bid_at_or_above_the_ask_is_refused_and_nothing_is_written(
    fade_db, venue
) -> None:
    slug = await window()
    paper = ex.PaperExecutor(venue)
    order = bid(slug, price=0.40)
    for ask in (0.40, 0.39):
        with pytest.raises(ex.PlacementRefused) as refused:
            await paper.place_bid(order, best_ask=ask, now=START + 10)
        assert refused.value.reason == "would_cross"

    # One crossing bid in a batch refuses the whole batch, cancels included.
    (resting,) = await rest(paper, bid(slug, price=0.30), at=START + 5)
    with pytest.raises(ex.PlacementRefused):
        await paper.requote(
            [bid(slug, price=0.35, rung=1), bid(slug, "Down", price=0.61, kind="hedge")],
            best_asks={up_of(): 0.41, down_of(): 0.60}, now=START + 10, cancel_ids=[resting],
        )
    assert await order_count() == 1
    assert (await order_row(resting))["state"] == "resting"

    with pytest.raises(ex.PlacementRefused) as refused:
        await paper.requote([order], best_asks={}, now=START + 10)
    assert refused.value.reason == "no_ask"
    assert await order_count() == 1


@pytest.mark.asyncio
async def test_a_bid_below_the_ask_rests_and_an_empty_ask_side_cannot_be_crossed(
    fade_db, venue
) -> None:
    slug = await window()
    paper = ex.PaperExecutor(venue)
    first = await paper.place_bid(bid(slug, price=0.40), best_ask=0.41, now=START + 10)
    second = await paper.place_bid(bid(slug, "Down", price=0.55, kind="hedge"), best_ask=None,
                                   now=START + 10)
    assert first is not None and second is not None
    rows = [await order_row(first), await order_row(second)]
    assert [(r["state"], r["mode"], r["placed_ts"]) for r in rows] == [
        ("resting", "paper", START + 10)] * 2


@pytest.mark.asyncio
async def test_a_bid_whose_fills_could_never_be_read_is_refused(fade_db, venue) -> None:
    slug = await window(cid=False)
    paper = ex.PaperExecutor(venue)
    with pytest.raises(ex.PlacementRefused) as refused:
        await paper.place_bid(bid(slug, price=0.40), best_ask=0.5, now=START + 10)
    assert refused.value.reason == "no_market_id"
    assert await order_count() == 0


@pytest.mark.asyncio
async def test_the_ledgers_refusals_come_back_as_placement_refused(fade_db, venue) -> None:
    slug = await window()
    paper = ex.PaperExecutor(venue)
    (resting,) = await rest(paper, bid(slug, price=0.30), at=START + 5)
    unknown = NewOrder(window_slug="btc-updown-15m-1", token_id="UP-x", side="Up",
                       kind="entry", price=0.40, shares=10)
    wrong_token = NewOrder(window_slug=slug, token_id=down_of(), side="Up", kind="entry",
                           price=0.40, shares=10)
    cases = [([unknown], START + 10), ([bid(slug, price=0.40)], END),
             ([wrong_token], START + 10)]
    for orders, at in cases:
        with pytest.raises(ex.PlacementRefused) as refused:
            await paper.requote(orders, best_asks={o.token_id: 0.99 for o in orders}, now=at,
                                cancel_ids=[resting])
        assert refused.value.reason == "ledger_refused"
    # Nothing was written, and the cancel that came with each batch was rolled back too.
    assert await order_count() == 1
    assert (await order_row(resting))["state"] == "resting"


@pytest.mark.asyncio
async def test_the_kill_switch_stops_new_bids_at_the_executor_too(
    fade_db, venue, tmp_path: Path
) -> None:
    slug = await window()
    paper = ex.PaperExecutor(venue)
    (tmp_path / "KILL").touch()
    with pytest.raises(ex.PlacementRefused) as refused:
        await paper.place_bid(bid(slug, price=0.40), best_ask=0.5, now=START + 10)
    assert refused.value.reason == ex.KILL_STATE
    assert await order_count() == 0


# ---------------------------------------------------------------------------
# Settlement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_traded_and_untraded_windows_both_settle_fee_free(fade_db, venue) -> None:
    btc, eth = await window("btc"), await window("eth")
    paper = ex.PaperExecutor(venue)
    entry, hedge = await rest(paper, bid(btc, "Up", price=0.40, shares=10),
                              bid(btc, "Down", price=0.55, shares=5, kind="hedge"),
                              at=START + 10)
    venue.add(cid_of(), trade(START + 20, "Up", "SELL", 10.0, 0.40),
              trade(START + 30, "Down", "SELL", 5.0, 0.55), nudge(END + 30))
    venue.resolve(cid_of(), winner_token=up_of(), up=up_of(), down=down_of())
    venue.resolve(cid_of("eth"), winner_token=down_of("eth"), up=up_of("eth"),
                  down=down_of("eth"))

    await paper.sync_fills(now=END + 60)
    report = await paper.settle(now=END + 60)

    assert report.errors == [] and report.forced == []
    by_slug = {s.window_slug: s for s in report.settled}
    assert set(by_slug) == {btc, eth}
    # Resting fills pay no fee: 10 x (1 - 0.40) won, 5 x 0.55 lost.
    assert by_slug[btc].net_pnl == pytest.approx(10 * 0.60 - 5 * 0.55)
    assert (by_slug[btc].outcome, by_slug[btc].orders) == ("Up", 2)
    assert (by_slug[eth].outcome, by_slug[eth].orders, by_slug[eth].net_pnl) == ("Down", 0, 0.0)
    assert ((await ledger.get_window(eth))["outcome"], (await order_row(entry))["won"],
            (await order_row(hedge))["won"]) == ("Down", 1, 0)
    assert await ledger.settlement_due(END + 60) == []
    assert (await ledger.summary())["net_pnl_usd"] == pytest.approx(3.25)

    # Settling again does nothing.
    assert (await paper.settle(now=END + 120)).settled == []


@pytest.mark.asyncio
async def test_settlement_waits_for_the_venue_to_call_a_winner(fade_db, venue) -> None:
    btc, eth, sol, xrp = [await window(a) for a in ("btc", "eth", "sol", "xrp")]
    keeper = ex.PaperBookkeeper(venue)
    venue.resolve(cid_of("btc"), winner_token=None, up=up_of("btc"), down=down_of("btc"),
                  closed=False)
    venue.resolve(cid_of("eth"), winner_token=None, up=up_of("eth"), down=down_of("eth"))
    venue.resolve(cid_of("sol"), winner_token=up_of("sol"), up=up_of("sol"),
                  down=down_of("sol"))
    # xrp: the order-book service does not know the market.

    early = await keeper.settle(now=END - 1)
    assert (early.settled, venue.clob_calls()) == ([], [])  # nothing has ended yet

    report = await keeper.settle(now=END + 10)

    assert [s.window_slug for s in report.settled] == [sol]
    assert report.waiting_resolution == 2
    assert report.errors == [
        f"{xrp}: the order-book service does not know this market. Tried again in 1 min."]
    assert (await ledger.get_window(btc))["outcome"] is None
    assert (await ledger.get_window(eth))["outcome"] is None

    # Windows the venue has not resolved yet are asked again on the very next pass.
    venue.resolve(cid_of("btc"), winner_token=down_of("btc"), up=up_of("btc"),
                  down=down_of("btc"))
    again = await keeper.settle(now=END + 20)
    assert [s.window_slug for s in again.settled] == [btc]
    assert (again.waiting_resolution, again.retry_later) == (1, 1)  # eth waits; xrp paused


@pytest.mark.asyncio
async def test_a_settlement_backlog_is_worked_through_a_capped_number_at_a_time(
    fade_db, venue
) -> None:
    slugs = [await window(a) for a in ("btc", "eth", "sol")]
    for asset in ("btc", "eth", "sol"):
        venue.resolve(cid_of(asset), winner_token=up_of(asset), up=up_of(asset),
                      down=down_of(asset))
    keeper = ex.PaperBookkeeper(venue, max_settle_per_pass=2)

    first = await keeper.settle(now=END + 10)
    second = await keeper.settle(now=END + 70)

    assert (len(first.settled), first.backlog) == (2, 1)
    assert (len(second.settled), second.backlog) == (1, 0)
    assert sorted(s.window_slug for s in first.settled + second.settled) == sorted(slugs)


@pytest.mark.asyncio
async def test_windows_that_never_resolve_cannot_hold_up_newer_ones(fade_db, venue) -> None:
    # Three old windows the order-book service does not know, and one with no market id.
    old = [await window(a) for a in ("btc", "eth", "sol")]
    await window("xrp", cid=False)
    keeper = ex.PaperBookkeeper(venue, max_settle_per_pass=2)

    first = await keeper.settle(now=END + 10)
    assert (first.settled, first.backlog, first.no_market_id) == ([], 1, 1)
    assert len(venue.clob_calls()) == 2  # the cap counts lookups only

    # The two that failed pause before their next try, so the third gets its turn.
    second = await keeper.settle(now=END + 20)
    assert (second.retry_later, second.backlog) == (2, 0)
    assert venue.clob_calls()[-1].endswith(cid_of("sol"))
    assert (f"2 windows could not be looked up last time ({old[0]}, {old[1]}) and are tried "
            "again after a pause.") in second.errors

    # A newer window ends and resolves. It is looked up ahead of the old failures.
    new_start = START + 900
    newer = await window("btc", new_start)
    venue.resolve(cid_of("btc", new_start), winner_token=up_of("btc", new_start),
                  up=up_of("btc", new_start), down=down_of("btc", new_start))
    third = await keeper.settle(now=new_start + 910)
    assert [s.window_slug for s in third.settled] == [newer]
    assert (third.backlog, third.no_market_id) == (2, 1)
    assert venue.clob_calls()[-1].endswith(cid_of("btc"))  # the least recently tried
    assert f"{old[0]}: the order-book service does not know this market. " \
           "Tried again in 2 min." in third.errors  # its second failure: the pause doubles


@pytest.mark.asyncio
async def test_windows_without_a_market_id_are_reported_in_one_line(fade_db, venue) -> None:
    starts = [START + 900 * k for k in range(4)]
    slugs = [await window("btc", s, cid=False) for s in starts]
    keeper = ex.PaperBookkeeper(venue)

    one = await keeper.settle(now=starts[0] + 910)
    assert one.errors == [
        f"1 window ended with no market id recorded ({slugs[0]}), so the result cannot be "
        "looked up and it stays unsettled."
    ]

    four = await keeper.settle(now=starts[-1] + 910)
    assert four.errors == [
        f"4 windows ended with no market id recorded ({slugs[0]}, {slugs[1]}, {slugs[2]} and "
        "1 more), so the result cannot be looked up and they stay unsettled."
    ]
    assert (four.no_market_id, venue.clob_calls()) == (4, [])


@pytest.mark.asyncio
async def test_settlement_waits_for_unread_tape_then_settles_with_what_was_read(
    fade_db, venue
) -> None:
    slug = await window()
    keeper = ex.PaperBookkeeper(venue, force_settle_after_s=1800)
    paper = ex.PaperExecutor(venue, bookkeeper=keeper)
    (order_id,) = await rest(paper, bid(slug, price=0.40, shares=10), at=START + 10)
    venue.add(cid_of(), trade(START + 20, "Up", "SELL", 4.0, 0.40), nudge(START + 30))
    venue.resolve(cid_of(), winner_token=up_of(), up=up_of(), down=down_of())
    await paper.sync_fills(now=START + 40)
    venue.tape_status[cid_of()] = 503  # the tape goes dark

    await paper.sync_fills(now=END + 60)
    waiting = await paper.settle(now=END + 60)
    assert (waiting.settled, waiting.waiting_tape, venue.clob_calls()) == ([], 1, [])

    forced = await paper.settle(now=END + 1800)
    assert forced.forced == [slug]
    assert "could not be read in full" in forced.errors[0]
    (settled,) = forced.settled
    assert (settled.forced_pending, settled.net_pnl) == (1, pytest.approx(4 * 0.60))
    assert (await order_row(order_id))["flow_cursor_ts"] == START + 30


# ---------------------------------------------------------------------------
# Choosing the executor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_paper_mode_gets_the_paper_executor(fade_db, venue) -> None:
    choice = await ex.choose_executor(venue)
    assert (choice.requested_mode, choice.state, choice.can_place) == ("paper", "paper", True)
    assert isinstance(choice.executor, ex.PaperExecutor)
    assert isinstance(choice.executor, ex.Executor)
    assert choice.executor.bookkeeper is choice.bookkeeper
    assert await ex.stand_down(choice, now=START) == 0


@pytest.mark.asyncio
async def test_live_mode_places_nothing_but_keeps_settling_paper(fade_db, venue) -> None:
    slug = await window()
    paper = ex.PaperExecutor(venue)
    entry, spare = await rest(paper, bid(slug, price=0.40, shares=10),
                              bid(slug, price=0.35, shares=10, rung=1), at=START + 10)
    venue.add(cid_of(), trade(START + 20, "Up", "SELL", 10.0, 0.40), nudge(START + 30))
    await paper.sync_fills(now=START + 40)
    await _db.set_config(ex.MODE_KEY, "live")

    choice = await ex.choose_executor(venue)

    assert (choice.state, choice.executor, choice.can_place) == (ex.LIVE_STATE, None, False)
    assert "not authorised" in choice.message and "not built" in choice.message
    assert await ex.stand_down(choice, now=START + 50) == 1
    row = await order_row(spare)
    assert (row["state"], row["cancel_reason"]) == ("cancelled", ex.LIVE_STATE)
    assert (await order_row(entry))["state"] == "filled"

    venue.add(cid_of(), nudge(END + 30))
    venue.resolve(cid_of(), winner_token=up_of(), up=up_of(), down=down_of())
    await choice.bookkeeper.sync_fills(now=END + 60)
    report = await choice.bookkeeper.settle(now=END + 60)
    assert [s.net_pnl for s in report.settled] == [pytest.approx(10 * 0.60)]


@pytest.mark.asyncio
async def test_the_kill_switch_file_closes_the_executor(fade_db, venue, tmp_path: Path) -> None:
    (tmp_path / "KILL").touch()
    choice = await ex.choose_executor(venue)
    assert (choice.state, choice.executor) == (ex.KILL_STATE, None)
    assert str(tmp_path / "KILL") in choice.message

    elsewhere = tmp_path / "elsewhere"
    choice = await ex.choose_executor(venue, kill_switch_path=elsewhere)
    assert choice.state == ex.PAPER_STATE


@pytest.mark.asyncio
async def test_an_unknown_or_unreadable_mode_fails_closed(
    fade_db, venue, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _db.set_config(ex.MODE_KEY, "turbo")
    choice = await ex.choose_executor(venue)
    assert (choice.requested_mode, choice.state, choice.executor) == (
        "turbo", ex.UNKNOWN_MODE_STATE, None)

    async def broken(*_args, **_kwargs):
        raise OSError("disk gone")

    monkeypatch.setattr(_db, "get_config", broken)
    choice = await ex.choose_executor(venue)
    assert (choice.state, choice.executor) == (ex.UNKNOWN_MODE_STATE, None)
    assert "OSError" in choice.message
