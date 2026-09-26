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

from ems import config as _config
from ems import db as _db
from ems.fade_1h_momentum_15m import executor as ex
from ems.fade_1h_momentum_15m import ledger
from ems.fade_1h_momentum_15m.ledger import NewOrder

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
        depth: float = 0.0, level: int = 0, levels: tuple = ()) -> NewOrder:
    """A buy (an entry) of ``side``'s token."""
    asset, _, _, start = slug.split("-")
    token = up_of(asset, int(start)) if side == "Up" else down_of(asset, int(start))
    return NewOrder(window_slug=slug, token_id=token, side=side, kind="entry", price=price,
                    shares=shares, level=level, depth_ahead=depth, levels_ahead=levels)


def sale(slug: str, side: str = "Up", *, price: float = 0.60, shares: float = 5.0,
         depth: float = 0.0, level: int = 0, levels: tuple = ()) -> NewOrder:
    """A sale (a hedge) of shares of ``side``'s token already held."""
    asset, _, _, start = slug.split("-")
    token = up_of(asset, int(start)) if side == "Up" else down_of(asset, int(start))
    return NewOrder(window_slug=slug, token_id=token, side=side, kind="hedge", price=price,
                    shares=shares, level=level, depth_ahead=depth, levels_ahead=levels)


async def rest(executor: ex.PaperExecutor, *orders: NewOrder, at: float) -> list[int]:
    """Rest orders written at ``at``: they rest from the second after it."""
    asks = {o.token_id: 0.99 for o in orders if o.order_side == "BUY"}
    bids = {o.token_id: 0.01 for o in orders if o.order_side == "SELL"}
    result = await executor.requote(orders, best_asks=asks, best_bids=bids, now=at)
    assert all(i is not None for i in result.ids), result.held_back
    return [int(i) for i in result.ids if i is not None]


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


def _queued(order_id: int, price: float, *, shares: float = 20.0, depth: float = 0.0,
            side: str = "Up", flow_from: int = 0, flow_to: int = 100, levels=None,
            **kw) -> ex.QueuedOrder:
    return ex.QueuedOrder(order_id=order_id, side=side, price=price, shares=shares,
                          flow_from=flow_from, flow_to=flow_to,
                          levels=levels if levels is not None else ((price, depth),), **kw)


# ---------------------------------------------------------------------------
# Pure queue maths
# ---------------------------------------------------------------------------


def test_queue_ahead_is_everything_at_our_price_or_better() -> None:
    levels = [(0.45, 10.0), (0.42, 5.0), (0.40, 7.0), (0.39, 100.0)]
    assert ex.queue_ahead(levels, 0.40) == pytest.approx(22.0)
    assert ex.queue_ahead(levels, 0.46) == 0.0
    # Prices parsed from text land a hair off the grid; they still count at the level.
    assert ex.queue_ahead([(0.1 + 0.2, 3.0)], 0.30) == pytest.approx(3.0)
    # For a sale, the asks at our price or lower are ahead of us.
    asks = [(0.58, 3.0), (0.60, 2.0), (0.62, 50.0)]
    assert ex.queue_ahead(asks, 0.60, "SELL") == pytest.approx(5.0)


def _crossed_volume(flow, our_index: int, our_price: float) -> float:
    """The maker rule: a taker SELL on our outcome at or under our price, or a taker BUY
    of the other outcome at or above 1 - our price (the same sale, mirrored), is flow
    that reached a bid resting at our price. The two outcomes share one book."""
    total = 0.0
    for _ts, index, side, size, price in flow:
        if side == "SELL" and index == our_index and price <= our_price + 1e-9:
            total += size
        elif side == "BUY" and index != our_index and 1.0 - price <= our_price + 1e-9:
            total += size
    return total


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
                _crossed_volume(flow, our_index=index, our_price=price)
            ), (side, price)


def test_one_record_fills_our_child_orders_top_down_and_never_beyond_its_size() -> None:
    high, low = _queued(1, 0.45, depth=10.0), _queued(2, 0.40, depth=25.0)
    flows = ex.allocate_fills([ex.TapePrint(10, "Up", "SELL", 50.0, 0.39)], [low, high])
    # The higher price level works through its 10-share queue, then takes its 20.
    assert (flows[1].filled, flows[1].crossed, flows[1].fill_ts) == (20.0, 50.0, 10)
    # The lower one sees only the 30 left: 25 of queue, then 5 for us.
    assert (flows[2].filled, flows[2].crossed, flows[2].fill_ts) == (5.0, 30.0, 10)
    assert flows[1].filled + flows[2].filled <= 50.0
    assert flows[2].levels == ((0.40, 0.0),)

    flows = ex.allocate_fills([ex.TapePrint(10, "Up", "SELL", 25.0, 0.39)],
                              [_queued(1, 0.45), _queued(2, 0.40)])
    assert (flows[1].filled, flows[2].filled) == (20.0, 5.0)


def test_a_record_above_a_price_level_reaches_only_the_orders_it_crossed() -> None:
    flows = ex.allocate_fills([ex.TapePrint(10, "Up", "SELL", 50.0, 0.43)],
                              [_queued(1, 0.45), _queued(2, 0.40)])
    assert (flows[1].filled, flows[2].filled, flows[2].crossed) == (20.0, 0.0, 0.0)


def test_trades_above_our_price_use_up_the_levels_above_us_level_by_level() -> None:
    # Book: 500 bid at 0.45, 300 at 0.44, 100 at 0.40. Our bid: 10 at 0.40.
    order = _queued(1, 0.40, shares=10.0,
                    levels=((0.45, 500.0), (0.44, 300.0), (0.40, 100.0)))
    tape = [ex.TapePrint(10, "Up", "SELL", 500.0, 0.45),
            ex.TapePrint(11, "Up", "SELL", 300.0, 0.44),
            ex.TapePrint(12, "Up", "SELL", 250.0, 0.40)]
    flows = ex.allocate_fills(tape, [order])
    # The first two use up the levels above us; the third works through the 100 at our
    # price, and 10 of the 150 left are ours.
    assert (flows[1].filled, flows[1].fill_ts, flows[1].crossed) == (10.0, 12, 250.0)
    assert flows[1].levels == ((0.45, 0.0), (0.44, 0.0), (0.40, 0.0))
    # A record between two levels uses up only the levels at its price or better.
    flows = ex.allocate_fills([ex.TapePrint(10, "Up", "SELL", 700.0, 0.443)], [order])
    assert (flows[1].filled, flows[1].levels) == (
        0.0, ((0.45, 0.0), (0.44, 300.0), (0.40, 100.0)))


def test_allocation_carries_on_from_what_was_read_before() -> None:
    queued = _queued(1, 0.40, shares=10.0, depth=10.0, crossed=20.0)  # 10 of 30 left ahead
    flows = ex.allocate_fills([ex.TapePrint(50, "Up", "SELL", 25.0, 0.40)], [queued])
    assert (flows[1].crossed, flows[1].filled, flows[1].added, flows[1].fill_ts) == (
        45.0, 10.0, 10.0, 50)


# ---------------------------------------------------------------------------
# Fills through the ledger
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_child_orders_fill_top_down_through_the_ledger(fade_db, venue) -> None:
    slug = await window()
    paper = ex.PaperExecutor(venue)
    high, low = await rest(paper, bid(slug, price=0.45, shares=20, depth=10, level=0),
                           bid(slug, price=0.40, shares=20, depth=25, level=1), at=START + 10)
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
async def test_the_depth_ahead_is_kept_level_by_level_across_reads(fade_db, venue) -> None:
    slug = await window()
    paper = ex.PaperExecutor(venue)
    (order_id,) = await rest(paper, bid(slug, price=0.40, shares=10,
                                        levels=((0.45, 500.0), (0.44, 300.0), (0.40, 100.0),
                                                (0.39, 1000.0))), at=START + 10)
    row = await order_row(order_id)
    assert row["depth_ahead"] == 900.0  # the 0.39 level is behind us
    venue.add(cid_of(), trade(START + 20, "Up", "SELL", 500.0, 0.45),
              trade(START + 30, "Up", "SELL", 300.0, 0.44), nudge(START + 40))
    first = await paper.sync_fills(now=START + 50)
    assert first.fills == []
    assert ledger.load_levels(await order_row(order_id)) == (
        (0.45, 0.0), (0.44, 0.0), (0.40, 100.0))

    venue.add(cid_of(), trade(START + 60, "Up", "SELL", 250.0, 0.40), nudge(START + 70))
    second = await paper.sync_fills(now=START + 80)
    assert [(f.shares, f.ts) for f in second.fills] == [(10.0, START + 60)]


@pytest.mark.asyncio
async def test_no_trade_from_before_an_order_was_written_can_fill_it(fade_db, venue) -> None:
    slug = await window()
    paper = ex.PaperExecutor(venue)
    (old,) = await rest(paper, bid(slug, price=0.54, shares=10), at=START + 9.5)
    assert (await order_row(old))["placed_ts"] == START + 10
    venue.add(cid_of(),
              trade(START + 7, "Up", "SELL", 50.0, 0.45),  # before the write
              trade(START + 9, "Up", "SELL", 50.0, 0.45),  # in the write's own second
              nudge(START + 30))
    await paper.sync_fills(now=START + 40)
    assert (await order_row(old))["filled_shares"] == 0.0

    # A cancel and its replacement written at START + 50: a trade in that second belongs to
    # neither, one before it to the old order, one after it to the new one.
    result = await paper.requote([bid(slug, price=0.53, shares=10)],
                                 best_asks={up_of(): 0.99}, now=START + 50.4, cancel_ids=[old])
    (new,) = result.ids
    venue.add(cid_of(), trade(START + 49, "Up", "SELL", 3.0, 0.50),
              trade(START + 50, "Up", "SELL", 4.0, 0.50),
              trade(START + 51, "Up", "SELL", 5.0, 0.50), nudge(START + 60))
    await paper.sync_fills(now=START + 70)
    assert (await order_row(old))["filled_shares"] == 3.0
    assert (await order_row(new))["filled_shares"] == 5.0


@pytest.mark.asyncio
async def test_the_executor_reads_its_clock_inside_the_write(fade_db, venue) -> None:
    slug = await window()
    now = [START + 5.0]
    paper = ex.PaperExecutor(venue, clock=lambda: now[0])
    real_place = ledger.place_orders

    async def slow_place(*args, **kwargs):
        now[0] = START + 42.6  # time passes before the write gets its lock
        return await real_place(*args, **kwargs)

    ledger.place_orders = slow_place  # type: ignore[assignment]
    try:
        result = await paper.requote([bid(slug)], best_asks={up_of(): 0.99})
    finally:
        ledger.place_orders = real_place  # type: ignore[assignment]
    assert result.placed_ts == START + 43
    assert await paper.cancel(result.ids, reason="stop") == 1
    assert (await order_row(result.ids[0]))["cancelled_ts"] == START + 43  # rested no time


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


# ---------------------------------------------------------------------------
# Sales of shares held (the hedge)
# ---------------------------------------------------------------------------


async def _holding(paper: ex.PaperExecutor, slug: str, *, shares: float = 10.0,
                   price: float = 0.40) -> int:
    """A buy of ``shares`` Up at ``price``, filled at START + 20, tape read to START + 30."""
    (order_id,) = await rest(paper, bid(slug, price=price, shares=shares), at=START + 10)
    venue_of = paper.bookkeeper._client  # the fake venue
    venue_of.add(cid_of(), trade(START + 20, "Up", "SELL", shares, price), nudge(START + 30))
    await paper.sync_fills(now=START + 40)
    assert (await order_row(order_id))["state"] == "filled"
    return order_id


@pytest.mark.asyncio
async def test_a_sale_fills_from_buyers_of_our_token_or_sellers_of_the_other(
    fade_db, venue
) -> None:
    slug = await window()
    paper = ex.PaperExecutor(venue)
    await _holding(paper, slug)
    # Sell 6 of the 10 Up at 0.60. Asks ahead of us: 3 at 0.58, 2 at 0.60 (0.62 is behind).
    sale_id = await paper.place_sell(
        sale(slug, price=0.60, shares=6.0, levels=((0.58, 3.0), (0.60, 2.0), (0.62, 50.0))),
        best_bid=0.55, now=START + 50)
    assert (await order_row(sale_id))["order_side"] == "SELL"
    venue.add(
        cid_of(),
        trade(START + 60, "Up", "BUY", 2.0, 0.59),  # takes 2 of the 3 at 0.58, never us
        trade(START + 61, "Down", "SELL", 4.0, 0.40),  # the mirror: Up bought at 0.60
        trade(START + 62, "Up", "BUY", 10.0, 0.61),  # an Up buy through our price
        trade(START + 63, "Up", "SELL", 5.0, 0.40),  # a sale of Up: never fills a sale
        nudge(START + 80),
    )

    report = await paper.sync_fills(now=START + 100)

    # At START + 61: 1 left at 0.58, 2 at 0.60, then 1 for us. At START + 62: the other 5.
    assert [(f.order_side, f.side, f.price, f.shares, f.ts) for f in report.fills] == [
        ("SELL", "Up", 0.60, 6.0, START + 61)]
    row = await order_row(sale_id)
    assert (row["state"], row["filled_shares"], row["crossed"]) == ("filled", 6.0, 13.0)
    (pos,) = await ledger.open_positions()
    assert (pos["shares"], pos["sold_shares"], pos["proceeds_usd"]) == (
        4.0, 6.0, pytest.approx(3.6))

    venue.add(cid_of(), nudge(END + 30))
    venue.resolve(cid_of(), winner_token=up_of(), up=up_of(), down=down_of())
    await paper.sync_fills(now=END + 60)
    (settled,) = (await paper.settle(now=END + 60)).settled
    # 10 bought at 0.40, 6 sold at 0.60, the 4 kept pay $1.
    assert settled.net_pnl == pytest.approx(4 * 1.0 + 6 * 0.60 - 10 * 0.40)
    assert (settled.sold_shares, settled.sale_proceeds_usd) == (6.0, pytest.approx(3.6))


@pytest.mark.asyncio
@pytest.mark.parametrize("winner", ["Up", "Down"])
async def test_a_partly_filled_sale_settles_on_the_shares_it_sold(
    fade_db, venue, winner: str
) -> None:
    # 10 Up bought at 0.40. A sale of 8 at 0.60 meets takers for 4 (2 buying Up at 0.60, 2
    # selling Down at 0.40, the mirror) and rests unfilled to the close.
    slug = await window()
    paper = ex.PaperExecutor(venue)
    entry = await _holding(paper, slug, shares=10.0, price=0.40)
    (sale_id,) = await rest(paper, sale(slug, price=0.60, shares=8.0), at=START + 50)
    venue.add(cid_of(), trade(START + 60, "Up", "BUY", 2.0, 0.60),
              trade(START + 70, "Down", "SELL", 2.0, 0.40), nudge(END + 30))
    venue.resolve(cid_of(), winner_token=up_of() if winner == "Up" else down_of(),
                  up=up_of(), down=down_of())

    await paper.sync_fills(now=END + 60)
    row = await order_row(sale_id)
    assert (row["state"], row["filled_shares"]) == ("expired", 4.0)
    (settled,) = (await paper.settle(now=END + 60)).settled

    payout = 1.0 if winner == "Up" else 0.0
    # The 6 kept pay out; the 4 sold brought in 0.60 each and no longer pay; the 10 cost 0.40.
    assert settled.net_pnl == pytest.approx(6 * payout + 4 * 0.60 - 10 * 0.40)
    assert (settled.filled_shares, settled.sold_shares) == (10.0, 4.0)
    assert settled.sale_proceeds_usd == pytest.approx(2.4)
    assert (await order_row(entry))["pnl"] == pytest.approx(10 * (payout - 0.40))
    assert (await order_row(sale_id))["pnl"] == pytest.approx(4 * (0.60 - payout))
    assert (await ledger.summary())["net_pnl_usd"] == pytest.approx(settled.net_pnl)


@pytest.mark.asyncio
async def test_a_sale_never_crosses_and_never_sells_more_than_is_held(fade_db, venue) -> None:
    slug = await window()
    paper = ex.PaperExecutor(venue)
    with pytest.raises(ex.PlacementRefused) as refused:
        await paper.place_sell(sale(slug, price=0.60), best_bid=0.50, now=START + 5)
    assert refused.value.reason == "not_held"
    await _holding(paper, slug, shares=10.0)
    for best_bid in (0.60, 0.61):
        with pytest.raises(ex.PlacementRefused) as refused:
            await paper.place_sell(sale(slug, price=0.60), best_bid=best_bid, now=START + 50)
        assert refused.value.reason == "would_cross"
    with pytest.raises(ex.PlacementRefused) as refused:
        await paper.requote([sale(slug, price=0.60)], best_asks={up_of(): 0.99}, now=START + 50)
    assert refused.value.reason == "no_bid"
    with pytest.raises(ex.PlacementRefused) as refused:
        await paper.place_bid(sale(slug), best_ask=0.99, now=START + 50)
    assert refused.value.reason == "bad_order"
    # An empty bid side cannot be crossed. Asking for 15 of the 10 held sells the 10.
    sale_id = await paper.place_sell(sale(slug, price=0.60, shares=15.0), best_bid=None,
                                     now=START + 50)
    assert (await order_row(sale_id))["shares"] == 10.0
    # Nothing left to offer.
    with pytest.raises(ex.PlacementRefused) as refused:
        await paper.place_sell(sale(slug, price=0.65, shares=5.0, level=1), best_bid=None,
                               now=START + 55)
    assert refused.value.reason == "not_held"


@pytest.mark.asyncio
async def test_buying_the_other_side_waits_but_the_cancels_go_through(fade_db, venue) -> None:
    slug = await window()
    paper = ex.PaperExecutor(venue)
    (up,) = await rest(paper, bid(slug, "Up", price=0.40), at=START + 10)
    result = await paper.requote([bid(slug, "Down", price=0.55)],
                                 best_asks={down_of(): 0.99}, now=START + 20, cancel_ids=[up])
    assert result.ids == [None] and result.cancelled == 1
    assert result.held_back[0][0] == ledger.OTHER_SIDE
    assert (await order_row(up))["state"] == "cancelled"
    # Once the cancelled Up bid's tape is read (it filled nothing), Down may be bought.
    venue.add(cid_of(), nudge(START + 30))
    await paper.sync_fills(now=START + 40)
    assert (await paper.place_bid(bid(slug, "Down", price=0.55), best_ask=0.99,
                                  now=START + 45)) is not None


# ---------------------------------------------------------------------------
# Bringing resting orders in line with the plan
# ---------------------------------------------------------------------------


def _row(order_id: int, price: float, shares: float, *, filled: float = 0.0,
         placed: int = START, kind: str = "entry", slug: str = f"btc-updown-15m-{START}",
         token: str = up_of()) -> dict:
    return {"id": order_id, "window_slug": slug, "token_id": token, "kind": kind,
            "price": price, "shares": shares, "filled_shares": filled, "placed_ts": placed}


def test_an_unchanged_plan_keeps_every_order_and_places_nothing() -> None:
    slug = f"btc-updown-15m-{START}"
    wanted = [bid(slug, price=0.41, shares=45.0), bid(slug, price=0.39, shares=20.0, level=1)]
    resting = [_row(1, 0.41, 45.0), _row(2, 0.39, 20.0)]
    changes = ex.reconcile_orders(wanted, resting)
    assert (changes.keep, changes.cancel, changes.place) == ((1, 2), (), ())
    # A partly filled order is judged by what is left of it.
    changes = ex.reconcile_orders([bid(slug, price=0.41, shares=30.0)],
                                  [_row(1, 0.41, 45.0, filled=15.0)])
    assert (changes.keep, changes.cancel, changes.place) == ((1,), (), ())


def test_more_shares_add_a_child_order_for_the_difference_only() -> None:
    slug = f"btc-updown-15m-{START}"
    changes = ex.reconcile_orders([bid(slug, price=0.41, shares=52.37)], [_row(1, 0.41, 45.0)])
    assert (changes.keep, changes.cancel) == ((1,), ())
    (added,) = changes.place
    assert (added.price, added.shares) == (0.41, 7.37)
    # A difference under the venue's minimum order is not placed; the order keeps its place.
    changes = ex.reconcile_orders([bid(slug, price=0.41, shares=45.07)], [_row(1, 0.41, 45.0)])
    assert (changes.keep, changes.cancel, changes.place) == ((1,), (), ())


def test_fewer_shares_cancel_the_newest_orders_and_add_back_what_is_still_wanted() -> None:
    slug = f"btc-updown-15m-{START}"
    resting = [_row(3, 0.41, 5.0, placed=START + 120), _row(1, 0.41, 30.0, placed=START),
               _row(2, 0.41, 15.0, placed=START + 60)]
    changes = ex.reconcile_orders([bid(slug, price=0.41, shares=36.0)], resting)
    # Oldest first while they fit: 30, then 15 does not fit, then 5 does.
    assert (changes.keep, changes.cancel, changes.place) == ((1, 3), (2,), ())
    changes = ex.reconcile_orders([bid(slug, price=0.41, shares=44.0)], resting[1:])
    assert (changes.keep, changes.cancel) == ((1,), (2,))
    (added,) = changes.place
    assert added.shares == 14.0
    # The venue cannot shrink an order: a cut inside the only order cancels it and adds back
    # the part still wanted, at the back of the queue.
    changes = ex.reconcile_orders([bid(slug, price=0.41, shares=44.93)], [_row(1, 0.41, 45.0)])
    assert (changes.keep, changes.cancel) == ((), (1,))
    assert [o.shares for o in changes.place] == [44.93]


def test_prices_the_plan_no_longer_wants_are_cancelled() -> None:
    slug = f"btc-updown-15m-{START}"
    resting = [_row(1, 0.41, 45.0), _row(2, 0.40, 10.0),
               _row(3, 0.60, 5.0, kind="hedge")]
    wanted = [bid(slug, price=0.41, shares=45.0), bid(slug, price=0.39, shares=10.0, level=1)]
    changes = ex.reconcile_orders(wanted, resting)
    assert (changes.keep, sorted(changes.cancel)) == ((1,), [2, 3])
    assert [(o.price, o.shares) for o in changes.place] == [(0.39, 10.0)]
    # Same price, different kind: a sale at 0.41 is not a buy at 0.41.
    changes = ex.reconcile_orders([sale(slug, price=0.41, shares=45.0)], [_row(1, 0.41, 45.0)])
    assert changes.cancel == (1,) and changes.place[0].kind == "hedge"


@pytest.mark.asyncio
async def test_reconcile_keeps_an_orders_place_in_the_queue(fade_db, venue) -> None:
    slug = await window()
    paper = ex.PaperExecutor(venue)
    (first,) = await rest(paper, bid(slug, price=0.40, shares=10, depth=30), at=START + 10)
    venue.add(cid_of(), trade(START + 20, "Up", "SELL", 25.0, 0.40), nudge(START + 30))
    await paper.sync_fills(now=START + 40)

    # The plan grows from 10 to 17: the first order stays (5 still ahead of it), and a new
    # child order of 7 joins the back of the queue.
    wanted = [bid(slug, price=0.40, shares=17, depth=40)]
    done = await paper.reconcile(wanted, await ledger.open_orders(),
                                 best_asks={up_of(): 0.45}, now=START + 50)
    assert (done.changes.keep, done.changes.cancel) == ((first,), ())
    (added,) = done.result.ids
    assert (await order_row(added))["shares"] == 7.0
    # The same plan again changes nothing and writes nothing.
    again = await paper.reconcile(wanted, await ledger.open_orders(),
                                  best_asks={up_of(): 0.45}, now=START + 60)
    assert again.result.ids == [] and set(again.changes.keep) == {first, added}
    assert await order_count() == 2

    venue.add(cid_of(), trade(START + 70, "Up", "SELL", 20.0, 0.40), nudge(START + 80))
    report = await paper.sync_fills(now=START + 90)
    # The kept order fills first from its place: 5 of queue, then its 10; the new one's
    # queue of 40 is still ahead of it.
    assert [(f.order_id, f.shares) for f in report.fills] == [(first, 10.0)]


# ---------------------------------------------------------------------------
# How far the tape is trusted
# ---------------------------------------------------------------------------


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
async def test_the_newest_second_waits_for_a_newer_record(fade_db, venue) -> None:
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


@pytest.mark.asyncio
async def test_the_clock_alone_never_vouches_for_a_quiet_or_stale_tape(fade_db, venue) -> None:
    slug = await window()
    keeper = ex.PaperBookkeeper(venue)
    paper = ex.PaperExecutor(venue, bookkeeper=keeper)
    (order_id,) = await rest(paper, bid(slug, price=0.40, shares=10), at=START + 10)
    # The copy of the tape stops at START + 200 (it may be quiet, or the indexer may be
    # running behind: nothing tells the two apart).
    venue.add(cid_of(), nudge(START + 200))
    venue.resolve(cid_of(), winner_token=up_of(), up=up_of(), down=down_of())

    await paper.sync_fills(now=END + LAG)
    assert (await order_row(order_id))["flow_cursor_ts"] == START + 200
    waiting = await paper.settle(now=END + LAG)
    assert (waiting.settled, waiting.waiting_tape, waiting.forced) == ([], 1, [])

    # The delayed record turns up: it is still read.
    venue.add(cid_of(), trade(END - 60, "Up", "SELL", 50.0, 0.38), nudge(END - 1))
    report = await paper.sync_fills(now=END + LAG + 100)
    assert [(f.shares, f.ts) for f in report.fills] == [(10.0, END - 60)]

    # With no newer record at all, the window settles after the force delay, and says so.
    other = await window("eth")
    (quiet,) = await rest(paper, bid(other, price=0.40, shares=10), at=START + 10)
    venue.add(cid_of("eth"), nudge(START + 200, asset="eth"))
    venue.resolve(cid_of("eth"), winner_token=up_of("eth"), up=up_of("eth"),
                  down=down_of("eth"))
    await paper.sync_fills(now=END + keeper.force_settle_after_s)
    forced = await paper.settle(now=END + keeper.force_settle_after_s)
    assert forced.forced == [other]
    assert "could not be read in full" in " ".join(forced.errors)
    # btc's record at END - 1 vouched for eth's tape up to it; its last second never was.
    assert (await order_row(quiet))["flow_cursor_ts"] == END - 1


@pytest.mark.asyncio
async def test_a_quiet_tape_is_vouched_for_by_a_newer_record_elsewhere(fade_db, venue) -> None:
    btc = await window("btc")
    nxt = START + 900
    eth = await window("eth", nxt)
    paper = ex.PaperExecutor(venue)
    (quiet,) = await rest(paper, bid(btc, price=0.40, shares=10), at=START + 10)
    (busy,) = await rest(paper, bid(eth, price=0.40, shares=10), at=nxt + 10)
    venue.add(cid_of("btc"), nudge(START + 200))
    venue.add(cid_of("eth", nxt), nudge(nxt + 800, asset="eth", start=nxt))

    # Before the eth record is read, nothing vouches for btc's quiet stretch.
    await paper.sync_fills(now=START + 300)
    assert (await order_row(quiet))["flow_cursor_ts"] == START + 200
    # END + LAG - 1: the eth tape is newer, but the last second is not yet LAG old.
    await paper.sync_fills(now=END + LAG - 1)
    assert (await order_row(quiet))["flow_cursor_ts"] == END - 1
    # END + LAG: old enough, and the eth tape holds a record newer than it.
    await paper.sync_fills(now=END + LAG)
    assert (await order_row(quiet))["flow_cursor_ts"] == END
    assert (await order_row(busy))["flow_cursor_ts"] == nxt + 800


@pytest.mark.asyncio
async def test_the_newest_windows_tape_is_read_when_nothing_else_can_vouch(
    fade_db, venue
) -> None:
    btc = await window("btc")
    nxt = START + 900
    await window("eth", nxt)  # no orders there, but it is trading
    paper = ex.PaperExecutor(venue)
    (quiet,) = await rest(paper, bid(btc, price=0.40, shares=10), at=START + 10)
    venue.add(cid_of("btc"), nudge(START + 200))
    venue.add(cid_of("eth", nxt), nudge(END + 500, asset="eth", start=nxt))

    await paper.sync_fills(now=END + LAG + 10)

    assert (await order_row(quiet))["flow_cursor_ts"] == END
    (probe,) = venue.tape_calls(cid_of("eth", nxt))
    assert int(probe["limit"]) == ex.FRESHNESS_PAGE
    # Nothing to vouch for: no extra read.
    await paper.sync_fills(now=END + LAG + 20)
    assert len(venue.tape_calls(cid_of("eth", nxt))) == 1


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
    # Every record once, except the two from the write's own second (before the order rested)
    # and the newest second's two, which wait for the next read.
    assert (await order_row(order_id))["filled_shares"] == 1196.0


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
# Placing: passive orders only
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
            [bid(slug, price=0.35, level=1), bid(slug, price=0.42, level=2)],
            best_asks={up_of(): 0.41}, now=START + 10, cancel_ids=[resting],
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
    second = await paper.place_bid(bid(slug, price=0.35, level=1), best_ask=None,
                                   now=START + 10)
    assert first is not None and second is not None
    rows = [await order_row(first), await order_row(second)]
    assert [(r["state"], r["mode"], r["placed_ts"], r["order_side"]) for r in rows] == [
        ("resting", "paper", START + 11, "BUY")] * 2


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
    cases = [([unknown], START + 10), ([bid(slug, price=0.40)], END - 1),
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
async def test_the_kill_switch_stops_new_orders_at_the_executor_too(
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
    entry = await _holding(paper, btc, shares=10.0, price=0.40)
    (hedge,) = await rest(paper, sale(btc, price=0.55, shares=5), at=START + 50)
    venue.add(cid_of(), trade(START + 60, "Up", "BUY", 5.0, 0.55), nudge(END + 30))
    venue.resolve(cid_of(), winner_token=up_of(), up=up_of(), down=down_of())
    venue.resolve(cid_of("eth"), winner_token=down_of("eth"), up=up_of("eth"),
                  down=down_of("eth"))

    await paper.sync_fills(now=END + 60)
    report = await paper.settle(now=END + 60)

    assert report.errors == [] and report.forced == []
    by_slug = {s.window_slug: s for s in report.settled}
    assert set(by_slug) == {btc, eth}
    # Passive fills pay no fee: 10 bought at 0.40, 5 sold at 0.55, the 5 kept won $1 each.
    assert by_slug[btc].net_pnl == pytest.approx(5 * 1.0 + 5 * 0.55 - 10 * 0.40)
    assert (by_slug[btc].outcome, by_slug[btc].orders) == ("Up", 2)
    assert (by_slug[eth].outcome, by_slug[eth].orders, by_slug[eth].net_pnl) == ("Down", 0, 0.0)
    assert ((await ledger.get_window(eth))["outcome"], (await order_row(entry))["pnl"],
            (await order_row(hedge))["pnl"]) == (
        "Down", pytest.approx(6.0), pytest.approx(5 * (0.55 - 1.0)))
    assert await ledger.settlement_due(END + 60) == []
    assert (await ledger.summary())["net_pnl_usd"] == pytest.approx(3.75)

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
                              bid(slug, price=0.35, shares=10, level=1), at=START + 10)
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
