"""Fade 1h Momentum on 15m: tables (db.py) and the paper ledger.

Runs against the real schema from ``db.init_db`` on a temp file, the same way the ledger
will run in the app.
"""

from __future__ import annotations

import math
from pathlib import Path

import aiosqlite
import pytest
import pytest_asyncio

from ems import db as _db
from ems.fade_1h_momentum_15m import ledger
from ems.fade_1h_momentum_15m.ledger import FlowUpdate, NewOrder, PendingFlow

pytestmark = pytest.mark.asyncio

HOUR = 1_789_934_400  # a UTC hour boundary
Q2 = HOUR + 900  # the hour's second quarter
Q2_END = HOUR + 1800


@pytest_asyncio.fixture
async def fade_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "fade.db")
    await _db.init_db()
    return _db


def _slug(asset: str, start: int) -> str:
    return f"{asset}-updown-15m-{start}"


async def _window(asset: str = "btc", start: int = Q2, **extra) -> str:
    slug = _slug(asset, start)
    await ledger.upsert_window(
        window_slug=slug, asset=asset, window_start=start, window_end=start + 900,
        ts=start, condition_id=f"cid-{asset}-{start}", up_token=f"UP-{asset}-{start}",
        down_token=f"DN-{asset}-{start}", **extra,
    )
    return slug


def _order(slug: str, side: str = "Up", *, kind: str = "entry", price: float = 0.40,
           shares: float = 10.0, level: int = 0, depth_ahead: float = 0.0,
           levels_ahead: tuple = ()) -> NewOrder:
    asset, _, _, start = slug.split("-")
    token = f"{'UP' if side == 'Up' else 'DN'}-{asset}-{start}"
    return NewOrder(window_slug=slug, token_id=token, side=side, kind=kind, price=price,
                    shares=shares, level=level, depth_ahead=depth_ahead,
                    levels_ahead=levels_ahead)


def _sale(slug: str, side: str = "Up", **kw) -> NewOrder:
    return _order(slug, side, kind="hedge", **kw)


async def _place(orders, *, ts, **kw) -> list:
    """Place orders and return the new ids (None where nothing was placed)."""
    return (await ledger.place_orders(orders, ts=ts, **kw)).ids


async def _orders(**where) -> list[dict]:
    clause = " AND ".join(f"{k} = ?" for k in where) or "1"
    async with _db.connect() as conn:
        async with conn.execute(
            f"SELECT * FROM fade_orders WHERE {clause} ORDER BY id", tuple(where.values())
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def _order_row(order_id: int) -> dict:
    (row,) = await _orders(id=order_id)
    return row


async def _fill(order_id: int, shares: float, *, at: int, cursor: int) -> ledger.FlowResult:
    return await ledger.record_flow([FlowUpdate(
        order_id=order_id, cursor_ts=cursor, crossed=shares, add_shares=shares, fill_ts=at,
    )])


# ---------------------------------------------------------------------------
# Tables and migrations
# ---------------------------------------------------------------------------

# Columns present since each table was created. Everything else must be in the table's
# migration dict, so an older copy of the table can gain it.
CORE = {
    "fade_windows": {"window_slug", "asset", "window_start", "window_end", "hour_start",
                     "outcome", "created_ts"},
    "fade_decisions": {"id", "ts", "asset", "window_slug", "inputs_json", "action"},
    "fade_orders": {"id", "window_slug", "token_id", "side", "kind", "level", "price",
                    "shares", "state", "placed_ts"},
    "fade_dials": {"version", "ts", "source", "params_json"},
}
MIGRATIONS = {
    "fade_windows": _db.FADE_WINDOW_COLUMN_MIGRATIONS,
    "fade_decisions": _db.FADE_DECISION_COLUMN_MIGRATIONS,
    "fade_orders": _db.FADE_ORDER_COLUMN_MIGRATIONS,
    "fade_dials": _db.FADE_DIALS_COLUMN_MIGRATIONS,
}


async def _columns(table: str) -> set[str]:
    async with _db.connect() as conn:
        async with conn.execute(f"PRAGMA table_info({table})") as cur:
            return {r["name"] for r in await cur.fetchall()}


async def _indexed_columns(table: str) -> set[str]:
    out: set[str] = set()
    async with _db.connect() as conn:
        async with conn.execute(f"PRAGMA index_list({table})") as cur:
            names = [r["name"] for r in await cur.fetchall()]
        for name in names:
            async with conn.execute(f"PRAGMA index_info({name})") as cur:
                out |= {r["name"] for r in await cur.fetchall()}
    return out


@pytest.mark.parametrize("table", sorted(CORE))
async def test_every_column_is_core_or_migratable(fade_db, table: str) -> None:
    migratable = set(MIGRATIONS[table])
    assert await _columns(table) == CORE[table] | migratable
    assert not CORE[table] & migratable
    # SCHEMA's CREATE INDEX runs before the migrations, so an indexed column must be core.
    assert not await _indexed_columns(table) & migratable


COINED = ("lad" + "der", "ru" + "ng")  # words the operator's standard terms rule out


async def test_no_coined_words_in_the_tables(fade_db) -> None:
    for table in CORE:
        for column in await _columns(table):
            assert not any(word in column for word in COINED), (table, column)


async def test_old_tables_gain_new_columns(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "old.db"
    # A table made by an earlier build: the core columns only, without the later ones.
    async with aiosqlite.connect(path) as conn:
        await conn.executescript(
            """
            CREATE TABLE fade_windows (
              window_slug TEXT PRIMARY KEY, asset TEXT NOT NULL, window_start INTEGER NOT NULL,
              window_end INTEGER NOT NULL, hour_start INTEGER NOT NULL, outcome TEXT,
              created_ts INTEGER NOT NULL);
            CREATE TABLE fade_orders (
              id INTEGER PRIMARY KEY AUTOINCREMENT, window_slug TEXT NOT NULL,
              token_id TEXT NOT NULL, side TEXT NOT NULL, kind TEXT NOT NULL,
              level INTEGER NOT NULL DEFAULT 0, price REAL NOT NULL, shares REAL NOT NULL,
              state TEXT NOT NULL DEFAULT 'resting', placed_ts INTEGER NOT NULL);
            CREATE UNIQUE INDEX idx_fade_orders_key
              ON fade_orders(window_slug, token_id, kind, level, placed_ts);
            CREATE TABLE fade_decisions (
              id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, asset TEXT NOT NULL,
              window_slug TEXT, inputs_json TEXT NOT NULL, action TEXT NOT NULL,
              child_orders_json TEXT);
            INSERT INTO fade_windows VALUES ('w', 'btc', 900, 1800, 0, NULL, 1);
            INSERT INTO fade_orders (window_slug, token_id, side, kind, level, price,
                                     shares, placed_ts)
              VALUES ('w', 't', 'Up', 'entry', 2, 0.4, 10, 1000);
            INSERT INTO fade_decisions (ts, asset, inputs_json, action, child_orders_json)
              VALUES (1, 'btc', '{}', 'bid', '[{"price": 0.4}]');
            """
        )
        await conn.commit()
    monkeypatch.setattr(_db, "DB_PATH", path)
    await _db.init_db()

    orders = await _columns("fade_orders")
    assert set(_db.FADE_ORDER_COLUMN_MIGRATIONS) <= orders
    assert "level" in orders
    assert set(_db.FADE_WINDOW_COLUMN_MIGRATIONS) <= await _columns("fade_windows")
    (row,) = await _orders()
    assert (row["filled_shares"], row["crossed"], row["depth_ahead"], row["mode"]) == (
        0, 0, 0, "paper"
    )
    assert (row["level"], row["order_side"], row["levels_ahead_json"]) == (2, "BUY", None)
    (decision,) = await ledger.recent_decisions()
    assert decision["child_orders"] == [{"price": 0.4}]


async def test_init_db_twice_is_harmless(fade_db) -> None:
    await ledger.seed_dials(ts=1)
    await _db.init_db()
    assert (await ledger.dials())["version"] == 0


# ---------------------------------------------------------------------------
# Dials
# ---------------------------------------------------------------------------


async def test_seed_writes_the_prior_once(fade_db) -> None:
    assert await ledger.dials() is None
    seeded = await ledger.seed_dials(ts=HOUR)
    assert seeded["version"] == 0
    assert seeded["source"] == "prior"
    assert seeded["params"] == dict(ledger.PRIOR_DIALS)
    # The market anchor starts on the market: p = m until the learner earns weight.
    assert (seeded["params"]["w_M"], seeded["params"]["w_S"]) == (1.0, 0.0)
    assert (seeded["params"]["theta"], seeded["params"]["kappa0"]) == (0.0, 0.0)
    assert seeded["note"]

    await ledger.seed_dials(ts=HOUR + 60)
    assert len(await ledger.dial_history()) == 1


async def test_learned_dials_are_new_versions_and_seed_never_resets(fade_db) -> None:
    await ledger.seed_dials(ts=HOUR)
    v1 = await ledger.save_dials({**ledger.PRIOR_DIALS, "w_S": 0.2}, source="rml", ts=HOUR + 900,
                                 n_windows=4, loglik=-2.7, note="first step")
    assert v1 == 1
    await ledger.seed_dials(ts=HOUR + 1000)
    latest = await ledger.dials()
    assert (latest["version"], latest["source"], latest["params"]["w_S"]) == (1, "rml", 0.2)
    assert (latest["n_windows"], latest["loglik"]) == (4, -2.7)
    assert [d["version"] for d in await ledger.dial_history()] == [1, 0]


async def test_save_dials_refuses_empty_params(fade_db) -> None:
    with pytest.raises(ValueError):
        await ledger.save_dials({}, source="rml", ts=1)


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------


async def test_window_first_known_value_wins(fade_db) -> None:
    slug = _slug("eth", Q2)
    await ledger.upsert_window(window_slug=slug, asset="eth", window_start=Q2,
                               window_end=Q2_END, ts=Q2)
    await ledger.upsert_window(window_slug=slug, asset="eth", window_start=Q2,
                               window_end=Q2_END, ts=Q2 + 60, start_ref_price=2500.5,
                               start_ref_source="chainlink_twap60", condition_id="cid")
    await ledger.upsert_window(window_slug=slug, asset="eth", window_start=Q2,
                               window_end=Q2_END, ts=Q2 + 120, start_ref_price=9999.0,
                               hour_slug="eth-updown-1h", hour_condition_id="hcid")
    w = await ledger.get_window(slug)
    assert w["hour_start"] == HOUR
    assert (w["start_ref_price"], w["start_ref_source"]) == (2500.5, "chainlink_twap60")
    assert (w["condition_id"], w["hour_slug"], w["hour_condition_id"]) == (
        "cid", "eth-updown-1h", "hcid"
    )
    assert w["created_ts"] == Q2
    assert w["outcome"] is None


async def test_window_bounds_are_checked(fade_db) -> None:
    with pytest.raises(ValueError):
        await ledger.upsert_window(window_slug="x", asset="btc", window_start=Q2,
                                   window_end=Q2, ts=Q2)
    with pytest.raises(ValueError):
        await ledger.upsert_window(window_slug="x", asset="btc", window_start=Q2,
                                   window_end=Q2_END, ts=Q2, hour_start=HOUR + 3600)


async def test_unreadable_start_print_is_stored_as_unknown(fade_db) -> None:
    slug = await _window(start_ref_price=math.nan)
    assert (await ledger.get_window(slug))["start_ref_price"] is None


async def test_latest_market_is_the_newest_started_window_with_a_market_id(fade_db) -> None:
    await _window("btc", Q2)
    await _window("eth", Q2_END)
    await _window("sol", Q2_END + 900)  # not started yet
    await ledger.upsert_window(window_slug=_slug("xrp", Q2_END), asset="xrp",
                               window_start=Q2_END, window_end=Q2_END + 900, ts=Q2_END)
    newest = await ledger.latest_market(Q2_END + 10)
    assert newest["condition_id"] == f"cid-eth-{Q2_END}"
    other = await ledger.latest_market(Q2_END + 10, exclude=[f"cid-eth-{Q2_END}"])
    assert other["condition_id"] == f"cid-btc-{Q2}"
    assert await ledger.latest_market(Q2 - 1) is None


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------


async def test_latest_decision_per_coin_with_inputs_round_trip(fade_db) -> None:
    for i, asset in enumerate(["sol", "btc", "sol", "btc", "eth"]):
        await ledger.record_decision(
            ts=Q2 + i, asset=asset, window_slug=_slug(asset, Q2), mode="paper",
            dials_version=0, inputs={"x_t": i / 4, "sigma": math.nan, "returns": [0.1, -0.2]},
            p_model=0.55, p=0.52, side="Up", kelly_f=0.04, stake_usd=3.2,
            child_orders=[{"level": 0, "price": 0.49, "shares": 4.0}],
            hedge={"side": "Up", "price": 0.55, "shares": 0.0},
            factors={"momentum": 0.1, "reversion": -0.05}, action="none",
            reason=f"pass {i}",
        )
    latest = await ledger.latest_decisions()
    assert [(d["asset"], d["reason"]) for d in latest] == [
        ("btc", "pass 3"), ("eth", "pass 4"), ("sol", "pass 2")
    ]
    btc = latest[0]
    assert btc["inputs"] == {"x_t": 0.75, "sigma": None, "returns": [0.1, -0.2]}
    assert btc["child_orders"] == [{"level": 0, "price": 0.49, "shares": 4.0}]
    assert btc["hedge"]["side"] == "Up"
    assert btc["factors"]["momentum"] == 0.1
    assert "inputs_json" not in btc and "child_orders_json" not in btc


async def test_decision_with_nothing_placed(fade_db) -> None:
    await ledger.record_decision(ts=Q2, asset="xrp", inputs={}, action="none",
                                 reason="model not plugged in")
    (d,) = await ledger.latest_decisions()
    assert (d["side"], d["p"], d["child_orders"], d["hedge"]) == (None, None, [], None)
    assert d["reason"] == "model not plugged in"


async def test_recent_decisions_newest_first_and_capped(fade_db) -> None:
    for i in range(5):
        await ledger.record_decision(ts=Q2 + i, asset="btc" if i % 2 else "eth", inputs={"i": i},
                                     action="none")
    assert [d["inputs"]["i"] for d in await ledger.recent_decisions(3)] == [4, 3, 2]
    assert [d["inputs"]["i"] for d in await ledger.recent_decisions(10, asset="btc")] == [3, 1]
    assert len(await ledger.recent_decisions(10_000)) == 5
    assert await ledger.latest_decisions() != []


async def test_decisions_for_one_window_oldest_first(fade_db) -> None:
    here, there = _slug("btc", Q2), _slug("btc", Q2_END)
    for i, slug in enumerate([here, there, here, None]):
        await ledger.record_decision(ts=Q2 + i, asset="btc", window_slug=slug, inputs={"i": i},
                                     action="none")
    assert [d["inputs"]["i"] for d in await ledger.decisions_for_window(here)] == [0, 2]
    assert await ledger.decisions_for_window("nope") == []


async def test_decision_input_checks(fade_db) -> None:
    with pytest.raises(ValueError):
        await ledger.record_decision(ts=1, asset="btc", inputs={}, action="none", side="up")
    with pytest.raises(TypeError):
        await ledger.record_decision(ts=1, asset="btc", inputs=[1, 2], action="none")
    assert await ledger.latest_decisions() == []


# ---------------------------------------------------------------------------
# Child orders: the order itself
# ---------------------------------------------------------------------------


async def test_new_order_checks() -> None:
    for kwargs in ({"price": 0.0}, {"price": 1.0}, {"price": math.nan}, {"shares": 0.0},
                   {"side": "up"}, {"kind": "taker"}, {"depth_ahead": -1.0}, {"level": -1},
                   {"level": 1.5}, {"levels_ahead": ((0.5, -1.0),)},
                   {"levels_ahead": ((0.5, 3.0),), "depth_ahead": 7.0}):
        base = dict(window_slug="w", token_id="t", side="Up", kind="entry", price=0.4,
                    shares=5.0)
        with pytest.raises(ValueError):
            NewOrder(**{**base, **kwargs})


async def test_the_depth_ahead_keeps_only_levels_at_our_price_or_better_best_first() -> None:
    buy = NewOrder(window_slug="w", token_id="t", side="Up", kind="entry", price=0.40,
                   shares=5.0, levels_ahead=((0.38, 50.0), (0.40, 7.0), (0.45, 10.0),
                                             (0.42, 0.0), (0.43, 5.0)))
    assert buy.order_side == "BUY"
    assert buy.levels_ahead == ((0.45, 10.0), (0.43, 5.0), (0.40, 7.0))
    assert buy.depth_ahead == 22.0
    sale = NewOrder(window_slug="w", token_id="t", side="Up", kind="hedge", price=0.60,
                    shares=5.0, levels_ahead=((0.62, 50.0), (0.60, 2.0), (0.58, 3.0)))
    assert sale.order_side == "SELL"
    assert sale.levels_ahead == ((0.58, 3.0), (0.60, 2.0))
    assert sale.depth_ahead == 5.0
    # Only a total: all of it is taken to sit at our own price.
    plain = NewOrder(window_slug="w", token_id="t", side="Up", kind="entry", price=0.40,
                     shares=5.0, depth_ahead=9.0)
    assert plain.queue() == ((0.40, 9.0),)


# ---------------------------------------------------------------------------
# Placing, cancelling, expiring
# ---------------------------------------------------------------------------


async def test_place_and_list_open_orders(fade_db) -> None:
    slug = await _window()
    result = await ledger.place_orders(
        [_order(slug, price=0.45, level=0, levels_ahead=((0.46, 20.0), (0.45, 100.0))),
         _order(slug, price=0.40, level=1)],
        ts=Q2 + 60,
    )
    assert all(isinstance(i, int) for i in result.ids)
    assert (result.cancelled, result.held_back, result.placed_ts) == (0, {}, Q2 + 61)
    opened = await ledger.open_orders()
    assert [o["price"] for o in opened] == [0.45, 0.40]
    assert opened[0]["asset"] == "btc"
    assert opened[0]["condition_id"] == f"cid-btc-{Q2}"
    assert (opened[0]["state"], opened[0]["mode"], opened[0]["depth_ahead"]) == (
        "resting", "paper", 120.0
    )
    assert (opened[0]["order_side"], opened[0]["level"]) == ("BUY", 0)
    assert ledger.load_levels(opened[0]) == ((0.46, 20.0), (0.45, 100.0))
    assert ledger.load_levels(opened[1]) == ((0.40, 0.0),)
    # The same placement again in the same second is recognised, not doubled.
    assert await _place([_order(slug, price=0.45, level=0)], ts=Q2 + 60) == [None]
    assert len(await _orders()) == 2


async def test_orders_rest_from_the_second_after_the_write(fade_db) -> None:
    slug = await _window()
    (a,) = await _place([_order(slug)], ts=Q2 + 60.2)
    assert (await _order_row(a))["placed_ts"] == Q2 + 61
    # The clock is read inside the write, not before it.
    reads: list[float] = []

    def clock() -> float:
        reads.append(Q2 + 99.9)
        return reads[-1]

    (b,) = await _place([_order(slug, price=0.41)], ts=clock, cancel_ids=[a])
    assert len(reads) == 1
    assert (await _order_row(b))["placed_ts"] == Q2 + 100
    row_a = await _order_row(a)
    # The cancel stops at the second of the write: a trade in it is credited to neither.
    assert (row_a["state"], row_a["cancelled_ts"]) == ("cancelled", Q2 + 99)


async def test_an_order_cancelled_in_its_own_second_rested_for_no_time(fade_db) -> None:
    slug = await _window()
    (a,) = await _place([_order(slug)], ts=Q2 + 60.1)
    assert await ledger.cancel_orders([a], ts=Q2 + 60.9, reason="requote") == 1
    row = await _order_row(a)
    assert (row["placed_ts"], row["cancelled_ts"]) == (Q2 + 61, Q2 + 61)
    assert await ledger.orders_needing_flow() == []


@pytest.mark.parametrize("bad", ["unknown_window", "window_over", "wrong_token"])
async def test_a_bad_order_writes_nothing(fade_db, bad: str) -> None:
    slug = await _window()
    first = (await _place([_order(slug)], ts=Q2 + 10))[0]
    good = _order(slug, price=0.41)
    ts = Q2 + 60
    if bad == "unknown_window":
        wrong = _order(_slug("sol", Q2))
    elif bad == "window_over":
        wrong, ts = _order(slug, price=0.39), Q2_END - 1  # it would rest from the end
    else:
        wrong = NewOrder(window_slug=slug, token_id=f"DN-btc-{Q2}", side="Up", kind="entry",
                         price=0.39, shares=5.0)
    with pytest.raises(ValueError):
        await ledger.place_orders([good, wrong], ts=ts, cancel_ids=[first])
    rows = await _orders()
    assert [(r["id"], r["state"]) for r in rows] == [(first, "resting")]


async def test_requote_cancels_and_places_in_one_step(fade_db) -> None:
    slug = await _window()
    (old,) = await _place([_order(slug, price=0.45)], ts=Q2 + 60)
    result = await ledger.place_orders([_order(slug, price=0.43)], ts=Q2 + 120,
                                       cancel_ids=[old])
    (new,) = result.ids
    assert result.cancelled == 1
    old_row, new_row = await _order_row(old), await _order_row(new)
    assert (old_row["state"], old_row["cancelled_ts"], old_row["cancel_reason"]) == (
        "cancelled", Q2 + 120, "requote"
    )
    assert (new_row["state"], new_row["placed_ts"]) == ("resting", Q2 + 121)
    assert [o["id"] for o in await ledger.open_orders()] == [new]


async def test_cancel_never_rests_past_the_window_end(fade_db) -> None:
    slug = await _window()
    a, b = await _place([_order(slug), _order(slug, price=0.3, level=1)], ts=Q2 + 60)
    assert await ledger.cancel_orders([a], ts=Q2 + 300, reason="stop") == 1
    assert await ledger.cancel_orders([a], ts=Q2 + 400, reason="stop") == 0  # already stopped
    assert await ledger.cancel_all_open(ts=Q2_END + 30, reason="restart") == 1
    row_a, row_b = await _order_row(a), await _order_row(b)
    assert (row_a["state"], row_a["cancelled_ts"], row_a["cancel_reason"]) == (
        "cancelled", Q2 + 300, "stop"
    )
    assert (row_b["state"], row_b["cancelled_ts"], row_b["cancel_reason"]) == (
        "expired", Q2_END, "window_end"
    )


async def test_expire_due_uses_the_ledgers_own_window_end(fade_db) -> None:
    ended = await _window(start=Q2)
    live = await _window(start=Q2_END)
    (a,) = await _place([_order(ended)], ts=Q2 + 60)
    (b,) = await _place([_order(live)], ts=Q2 + 60)
    assert await ledger.expire_due(Q2_END) == 1
    row_a, row_b = await _order_row(a), await _order_row(b)
    assert (row_a["state"], row_a["cancelled_ts"]) == ("expired", Q2_END)
    assert row_b["state"] == "resting"


# ---------------------------------------------------------------------------
# One side only
# ---------------------------------------------------------------------------


async def _held(slug: str, side: str = "Up", shares: float = 10.0, *, at: int = Q2 + 60,
                price: float = 0.40) -> int:
    """A filled buy of ``shares`` on ``side``, tape read to ``at + 60``."""
    (oid,) = await _place([_order(slug, side, price=price, shares=shares)], ts=at)
    await _fill(oid, shares, at=at + 10, cursor=at + 60)
    return oid


async def test_a_sale_is_never_bigger_than_the_shares_held_and_free(fade_db) -> None:
    slug = await _window()
    # Nothing held: nothing to sell.
    result = await ledger.place_orders([_sale(slug, price=0.60, shares=5.0)], ts=Q2 + 30)
    assert result.ids == [None] and result.held_back[0][0] == ledger.NOT_HELD
    assert "none are held" in result.held_back[0][1]

    await _held(slug, shares=10.0)
    # 6 offered, then 8 more asked: cut to the 4 still free.
    result = await ledger.place_orders(
        [_sale(slug, price=0.60, shares=6.0), _sale(slug, price=0.62, shares=8.0, level=1)],
        ts=Q2 + 200, min_shares=0.0, share_step=0.01)
    first, second = result.ids
    assert first is not None and second is not None
    assert result.placed_shares == {1: 4.0} and result.held_back[1][0] == ledger.NOT_HELD
    assert (await _order_row(second))["shares"] == 4.0

    # A cancelled sale whose tape is not read yet may still have sold: its shares stay
    # offered, so a new sale waits (below the venue minimum it is held back, not placed).
    result = await ledger.place_orders([_sale(slug, price=0.58, shares=6.0, level=2)],
                                       ts=Q2 + 300, cancel_ids=[first], min_shares=5.0,
                                       share_step=0.01)
    assert result.cancelled == 1 and result.ids == [None]
    assert result.held_back[0][0] == ledger.NOT_HELD
    # Once its tape is read (nothing sold), its shares are free again.
    await ledger.record_flow([FlowUpdate(order_id=first, cursor_ts=Q2 + 300)])
    assert (await _place([_sale(slug, price=0.58, shares=6.0, level=2)], ts=Q2 + 310))[0]

    (pos,) = await ledger.open_positions()
    assert (pos["shares"], pos["bought_shares"], pos["sold_shares"]) == (10.0, 10.0, 0.0)
    assert pos["offered_shares"] == 10.0  # the 4 cut to fit, and the 6 just placed


async def test_a_buy_waits_while_the_other_side_is_held_or_might_be(fade_db) -> None:
    slug = await _window()
    (down,) = await _place([_order(slug, "Down", price=0.55)], ts=Q2 + 30)
    # A Down buy rests: an Up buy is held back, but the cancel in the same write goes through.
    result = await ledger.place_orders([_order(slug, "Up", price=0.40)], ts=Q2 + 60,
                                       cancel_ids=[down])
    assert result.ids == [None] and result.cancelled == 1
    assert result.held_back[0][0] == ledger.OTHER_SIDE
    assert "Down buy orders rest, or their trade tape is not read yet" in result.held_back[0][1]
    assert (await _order_row(down))["state"] == "cancelled"
    # The cancelled Down buy's tape is read: it filled 5 before the cancel. Down is held now.
    await _fill(down, 5.0, at=Q2 + 45, cursor=Q2 + 60)
    result = await ledger.place_orders([_order(slug, "Up", price=0.40)], ts=Q2 + 90)
    assert result.ids == [None] and "5 Down shares are held" in result.held_back[0][1]
    # Selling the Down shares is allowed; once they are all sold, Up may be bought.
    (sale,) = await _place([_sale(slug, "Down", price=0.60, shares=5.0)], ts=Q2 + 100)
    await _fill(sale, 5.0, at=Q2 + 120, cursor=Q2 + 150)
    assert (await _place([_order(slug, "Up", price=0.40)], ts=Q2 + 160))[0] is not None
    # And one batch never buys both sides.
    other = await _window("eth")
    result = await ledger.place_orders([_order(other, "Up"), _order(other, "Down", price=0.5)],
                                       ts=Q2 + 170)
    assert result.ids[0] is not None and result.ids[1] is None
    assert result.held_back[1][0] == ledger.OTHER_SIDE


# ---------------------------------------------------------------------------
# Fills from the tape
# ---------------------------------------------------------------------------


async def test_partial_then_full_fill(fade_db) -> None:
    slug = await _window()
    (oid,) = await _place([_order(slug, shares=10.0)], ts=Q2 + 60)
    result = await _fill(oid, 4.0, at=Q2 + 70, cursor=Q2 + 120)
    assert (result.updated, result.gained, result.stale) == (1, 1, 0)
    row = await _order_row(oid)
    assert (row["state"], row["filled_shares"], row["filled_ts"], row["fill_price"]) == (
        "partial", 4.0, Q2 + 70, 0.40
    )
    await _fill(oid, 8.0, at=Q2 + 150, cursor=Q2 + 180)  # more than is left: capped
    row = await _order_row(oid)
    assert (row["state"], row["filled_shares"], row["filled_ts"]) == ("filled", 10.0, Q2 + 70)
    assert row["flow_cursor_ts"] == Q2 + 180
    assert await ledger.open_orders() == []


async def test_replaying_the_same_tape_changes_nothing(fade_db) -> None:
    slug = await _window()
    (oid,) = await _place([_order(slug, shares=10.0, depth_ahead=9.0)], ts=Q2 + 60)
    batch = [FlowUpdate(order_id=oid, cursor_ts=Q2 + 120, crossed=6.0, add_shares=3.0,
                        fill_ts=Q2 + 90, levels_ahead=((0.40, 0.0),))]
    await ledger.record_flow(batch)
    again = await ledger.record_flow(batch)
    assert (again.updated, again.stale) == (0, 1)
    row = await _order_row(oid)
    assert (row["filled_shares"], row["crossed"]) == (3.0, 6.0)
    assert ledger.load_levels(row) == ((0.40, 0.0),)
    # crossed never goes backwards, and levels stay when an update leaves them out
    await ledger.record_flow([FlowUpdate(order_id=oid, cursor_ts=Q2 + 180, crossed=2.0)])
    row = await _order_row(oid)
    assert row["crossed"] == 6.0 and ledger.load_levels(row) == ((0.40, 0.0),)


async def test_a_fill_found_after_cancel_counts_if_it_traded_while_resting(fade_db) -> None:
    slug = await _window()
    (oid,) = await _place([_order(slug, shares=10.0)], ts=Q2 + 60)
    await ledger.cancel_orders([oid], ts=Q2 + 120, reason="requote")
    await _fill(oid, 2.0, at=Q2 + 100, cursor=Q2 + 200)
    row = await _order_row(oid)
    assert (row["state"], row["filled_shares"]) == ("cancelled", 2.0)
    assert row["flow_cursor_ts"] == Q2 + 120  # read to the moment it stopped resting


async def test_bad_fills_write_nothing(fade_db) -> None:
    slug = await _window()
    a, b = await _place([_order(slug), _order(slug, price=0.3, level=1)], ts=Q2 + 60)
    await ledger.cancel_orders([b], ts=Q2 + 120, reason="requote")
    good = FlowUpdate(order_id=a, cursor_ts=Q2 + 200, crossed=1.0, add_shares=1.0,
                      fill_ts=Q2 + 90)
    for bad in (
        FlowUpdate(order_id=b, cursor_ts=Q2 + 200, add_shares=1.0, fill_ts=Q2 + 150),  # after
        FlowUpdate(order_id=b, cursor_ts=Q2 + 200, add_shares=1.0, fill_ts=Q2 + 60),  # before
        FlowUpdate(order_id=b, cursor_ts=Q2 + 200, add_shares=1.0),  # no trade time
        FlowUpdate(order_id=b, cursor_ts=Q2 + 200, add_shares=-1.0, fill_ts=Q2 + 90),
        FlowUpdate(order_id=999, cursor_ts=Q2 + 200),
    ):
        with pytest.raises(ValueError):
            await ledger.record_flow([good, bad])
    assert [r["filled_shares"] for r in await _orders()] == [0.0, 0.0]


async def test_tape_from_before_an_order_was_placed_is_never_its(fade_db) -> None:
    slug = await _window()
    (oid,) = await _place([_order(slug, shares=10.0)], ts=Q2 + 60.4)  # rests from Q2 + 61
    for bad in (
        FlowUpdate(order_id=oid, cursor_ts=Q2 + 30),  # a read that ends before it rested
        FlowUpdate(order_id=oid, cursor_ts=Q2 + 60),  # ... or in the write's own second
        FlowUpdate(order_id=oid, cursor_ts=Q2 + 120, crossed=5.0, add_shares=5.0,
                   fill_ts=Q2 + 60),  # a trade in the write's own second
        FlowUpdate(order_id=oid, cursor_ts=Q2 + 120, crossed=5.0, add_shares=5.0,
                   fill_ts=Q2 + 10),  # a trade before the write
    ):
        with pytest.raises(ValueError):
            await ledger.record_flow([bad])
    row = await _order_row(oid)
    assert (row["state"], row["filled_shares"], row["flow_cursor_ts"], row["crossed"]) == (
        "resting", 0.0, None, 0.0)
    # From the second after the write, the tape is its own.
    await _fill(oid, 5.0, at=Q2 + 61, cursor=Q2 + 120)
    row = await _order_row(oid)
    assert (row["state"], row["filled_shares"], row["filled_ts"]) == ("partial", 5.0, Q2 + 61)


async def test_orders_needing_flow(fade_db) -> None:
    slug = await _window()
    resting, cancelled, filled = await _place(
        [_order(slug, price=0.45), _order(slug, price=0.40, level=1),
         _order(slug, price=0.35, level=2, shares=2.0)], ts=Q2 + 60)
    await ledger.cancel_orders([cancelled], ts=Q2 + 120, reason="requote")
    await _fill(filled, 2.0, at=Q2 + 65, cursor=Q2 + 90)

    need = {o["id"]: o for o in await ledger.orders_needing_flow()}
    assert set(need) == {resting, cancelled}
    assert (need[resting]["flow_from"], need[resting]["flow_until"]) == (Q2 + 61, Q2_END)
    assert (need[cancelled]["flow_from"], need[cancelled]["flow_until"]) == (Q2 + 61, Q2 + 120)
    assert need[resting]["condition_id"] == f"cid-btc-{Q2}"

    await ledger.record_flow([FlowUpdate(order_id=cancelled, cursor_ts=Q2 + 300)])
    assert {o["id"] for o in await ledger.orders_needing_flow()} == {resting}


# ---------------------------------------------------------------------------
# Settlement
# ---------------------------------------------------------------------------


async def _hedged_window(start: int = Q2) -> tuple[str, int, int, int]:
    """An entry (10 Up at 0.40, filled), a hedge (a sale of 5 of them at 0.60, filled) and a
    bid that never filled (4 Up at 0.30), with the tape read to the window end."""
    slug = await _window(start=start)
    entry, unfilled = await _place(
        [_order(slug, "Up", price=0.40, shares=10.0),
         _order(slug, "Up", price=0.30, shares=4.0, level=1)], ts=start + 60)
    await ledger.record_flow([FlowUpdate(order_id=entry, cursor_ts=start + 200, crossed=10.0,
                                         add_shares=10.0, fill_ts=start + 100)])
    (hedge,) = await _place([_sale(slug, "Up", price=0.60, shares=5.0)], ts=start + 300)
    end = start + 900
    await ledger.record_flow([
        FlowUpdate(order_id=hedge, cursor_ts=end, crossed=5.0, add_shares=5.0,
                   fill_ts=start + 500),
        FlowUpdate(order_id=unfilled, cursor_ts=end, crossed=0.0),
    ])
    return slug, entry, hedge, unfilled


@pytest.mark.parametrize("outcome, net", [("Up", 10 * 0.60 + 5 * (0.60 - 1.0)),
                                          ("Down", 10 * -0.40 + 5 * 0.60)])
async def test_settle_a_window_with_a_sale(fade_db, outcome: str, net: float) -> None:
    slug, entry, hedge, unfilled = await _hedged_window()
    assert [(w["window_slug"], w["pending_flow"]) for w in await ledger.settlement_due(Q2_END)] \
        == [(slug, 0)]

    result = await ledger.settle_window(slug, outcome=outcome, ts=Q2_END + 120)
    # The 5 held shares pay out; the 5 sold brought in 0.60 each; the 10 cost 0.40 each.
    held_payout = 5 * (1.0 if outcome == "Up" else 0.0)
    assert net == pytest.approx(held_payout + 5 * 0.60 - 10 * 0.40)
    assert result.net_pnl == pytest.approx(net)
    assert (result.orders, result.filled_shares, result.staked_usd) == (3, 10.0, 4.0)
    assert (result.sold_shares, result.sale_proceeds_usd) == (5.0, pytest.approx(3.0))
    assert result.forced_pending == 0

    e, h, u = [await _order_row(i) for i in (entry, hedge, unfilled)]
    up_won = outcome == "Up"
    assert (e["won"], e["pnl"]) == (int(up_won), pytest.approx(10 * ((1.0 if up_won else 0) - 0.4)))
    assert (h["won"], h["pnl"]) == (int(up_won), pytest.approx(5 * (0.6 - (1.0 if up_won else 0))))
    assert (u["state"], u["pnl"], u["cancelled_ts"]) == ("expired", 0.0, Q2_END)
    w = await ledger.get_window(slug)
    assert (w["outcome"], w["settled_ts"], w["net_pnl"]) == (outcome, Q2_END + 120,
                                                             pytest.approx(net))
    assert await ledger.settlement_due(Q2_END + 200) == []
    assert await ledger.settle_window(slug, outcome="Down", ts=Q2_END + 300) is None


@pytest.mark.parametrize("outcome, sale_pnl", [
    ("Up", 5 * (0.60 - 1.0)),  # the 5 sold would have paid $1: the sale lost 40c a share
    ("Down", 5 * 0.60),  # the 5 sold would have paid nothing: the sale made 60c a share
])
async def test_a_partly_filled_sale_counts_only_the_shares_it_sold(
    fade_db, outcome: str, sale_pnl: float
) -> None:
    # 10 Up bought at 0.40. A sale of 8 at 0.60 sells 5, then rests unfilled to the close:
    # 5 are still held at settlement, and 3 were only ever offered.
    slug = await _window()
    entry = await _held(slug, shares=10.0, price=0.40)
    (sale,) = await _place([_sale(slug, price=0.60, shares=8.0)], ts=Q2 + 200)
    await _fill(sale, 5.0, at=Q2 + 300, cursor=Q2 + 400)

    (pos,) = await ledger.open_positions()
    assert (pos["shares"], pos["bought_shares"], pos["sold_shares"], pos["offered_shares"]) == (
        5.0, 10.0, 5.0, 3.0)
    assert (pos["cost_usd"], pos["proceeds_usd"]) == (pytest.approx(4.0), pytest.approx(3.0))
    open_now = await ledger.summary()
    assert open_now["open_exposure_usd"] == pytest.approx(4.0 - 3.0)
    assert open_now["resting_sell_shares"] == 3.0
    # The sale commits shares, never cash.
    assert await ledger.free_cash_usd(100.0) == pytest.approx(100.0 - 1.0)

    await ledger.record_flow([FlowUpdate(order_id=sale, cursor_ts=Q2_END)])
    result = await ledger.settle_window(slug, outcome=outcome, ts=Q2_END + 60)

    up_won = outcome == "Up"
    entry_pnl = 10 * ((1.0 if up_won else 0.0) - 0.40)
    # The 5 held pay out, the 5 sold brought in 0.60 each, the 10 cost 0.40 each.
    assert entry_pnl + sale_pnl == pytest.approx(5 * (1.0 if up_won else 0.0) + 3.0 - 4.0)
    assert result.net_pnl == pytest.approx(entry_pnl + sale_pnl)
    assert (result.filled_shares, result.staked_usd) == (10.0, pytest.approx(4.0))
    assert (result.sold_shares, result.sale_proceeds_usd) == (5.0, pytest.approx(3.0))
    e, s = await _order_row(entry), await _order_row(sale)
    assert (s["state"], s["filled_shares"], s["cancelled_ts"]) == ("expired", 5.0, Q2_END)
    assert (e["pnl"], s["pnl"]) == (pytest.approx(entry_pnl), pytest.approx(sale_pnl))
    assert (s["pnl"] > 0.0) == (not up_won)  # a sale pays exactly when the side sold loses
    after = await ledger.summary()
    assert after["net_pnl_usd"] == pytest.approx(entry_pnl + sale_pnl)
    assert (after["sold_shares"], after["sale_proceeds_usd"]) == (5.0, pytest.approx(3.0))
    assert (after["open_exposure_usd"], after["resting_sell_shares"]) == (0.0, 0.0)
    assert await ledger.open_positions() == []


async def test_settlement_waits_for_the_tape(fade_db) -> None:
    slug = await _window()
    (oid,) = await _place([_order(slug)], ts=Q2 + 60)
    await ledger.record_flow([FlowUpdate(order_id=oid, cursor_ts=Q2 + 600)])
    (due,) = await ledger.settlement_due(Q2_END)
    assert due["pending_flow"] == 1
    with pytest.raises(PendingFlow) as err:
        await ledger.settle_window(slug, outcome="Down", ts=Q2_END + 60)
    assert err.value.pending == 1
    assert (await _order_row(oid))["state"] == "resting"  # nothing written

    forced = await ledger.settle_window(slug, outcome="Down", ts=Q2_END + 60, force=True)
    assert (forced.forced_pending, forced.net_pnl) == (1, 0.0)
    row = await _order_row(oid)
    assert (row["state"], row["flow_cursor_ts"], row["settled_ts"]) == (
        "expired", Q2 + 600, Q2_END + 60
    )
    assert await ledger.orders_needing_flow() == []


async def test_untraded_windows_settle_too(fade_db) -> None:
    slug = await _window(asset="xrp")
    result = await ledger.settle_window(slug, outcome="Down", ts=Q2_END)
    assert (result.orders, result.net_pnl) == (0, 0.0)
    assert (await ledger.summary())["windows_observed"] == 1


async def test_settle_checks(fade_db) -> None:
    slug = await _window()
    with pytest.raises(ValueError):
        await ledger.settle_window(slug, outcome="Up", ts=Q2_END - 1)
    with pytest.raises(ValueError):
        await ledger.settle_window(slug, outcome="up", ts=Q2_END)
    with pytest.raises(ValueError):
        await ledger.settle_window("nope", outcome="Up", ts=Q2_END)
    (oid,) = await _place([_order(slug)], ts=Q2 + 1)
    with pytest.raises(PendingFlow):
        await ledger.settle_window(slug, outcome="Up", ts=Q2_END)
    assert await ledger.record_flow(
        [FlowUpdate(order_id=oid, cursor_ts=Q2_END)]
    ) == ledger.FlowResult(updated=1, gained=0, stale=0)
    await ledger.settle_window(slug, outcome="Up", ts=Q2_END)
    stale = await ledger.record_flow([FlowUpdate(order_id=oid, cursor_ts=Q2_END + 60)])
    assert stale.stale == 1  # settled orders are final


# ---------------------------------------------------------------------------
# Record, positions, bankroll
# ---------------------------------------------------------------------------


async def test_empty_summary_has_no_made_up_ratios(fade_db) -> None:
    s = await ledger.summary()
    assert s["net_pnl_usd"] == 0.0
    assert s["max_drawdown_usd"] == 0.0
    assert (s["cents_per_share"], s["return_on_staked"]) == (None, None)
    assert s["settled_windows"] == s["open_windows"] == s["orders_placed"] == 0
    assert (s["sold_shares"], s["sale_proceeds_usd"], s["resting_sell_shares"]) == (0, 0, 0)


async def test_summary_profit_first(fade_db) -> None:
    # Window 1: +4.0 (10 Up at 0.40, 5 sold at 0.60, Up won). Window 2: 10 Up at 0.40 lose:
    # -4.0. Window 3: 5 Down at 0.80 win: +1.0. Equity 4, 0, 1: peak 4, drawdown 4.
    w1, *_ = await _hedged_window(start=Q2)
    await ledger.settle_window(w1, outcome="Up", ts=Q2_END)
    w2 = await _window(asset="eth", start=Q2_END)
    (o2,) = await _place([_order(w2, "Up", price=0.40, shares=10.0)], ts=Q2_END + 10)
    await _fill(o2, 10.0, at=Q2_END + 20, cursor=Q2_END + 900)
    await ledger.settle_window(w2, outcome="Down", ts=Q2_END + 900)
    w3 = await _window(asset="sol", start=Q2_END + 900)
    (o3,) = await _place([_order(w3, "Down", price=0.80, shares=5.0)], ts=Q2_END + 910)
    await _fill(o3, 5.0, at=Q2_END + 920, cursor=Q2_END + 1800)
    await ledger.settle_window(w3, outcome="Down", ts=Q2_END + 1800)
    # Open: 6 bought at 0.50 on a live window, 2 of them sold at 0.70, a sale of 1 more
    # resting, and a buy of 2 at 0.45 still resting.
    w4 = await _window(asset="btc", start=Q2_END + 1800)
    t4 = Q2_END + 1810
    (o4, o5) = await _place(
        [_order(w4, "Up", price=0.50, shares=6.0),
         _order(w4, "Up", price=0.45, shares=2.0, level=1)], ts=t4)
    await _fill(o4, 6.0, at=t4 + 10, cursor=t4 + 20)
    (s4,) = await _place([_sale(w4, "Up", price=0.70, shares=2.0)], ts=t4 + 30)
    await _fill(s4, 2.0, at=t4 + 40, cursor=t4 + 50)
    (s5,) = await _place([_sale(w4, "Up", price=0.75, shares=1.0, level=1)], ts=t4 + 60)
    # An untraded window settles too: it is a label for the learner, not a bet.
    w5 = await _window(asset="xrp", start=Q2)
    await ledger.settle_window(w5, outcome="Up", ts=Q2_END)

    s = await ledger.summary()
    assert s["net_pnl_usd"] == pytest.approx(1.0)
    assert s["settled_windows"] == 3
    assert s["settled_shares"] == 25.0  # bought: 10 + 10 + 5
    assert s["staked_usd"] == pytest.approx(4.0 + 4.0 + 4.0)
    assert (s["sold_shares"], s["sale_proceeds_usd"]) == (5.0, pytest.approx(3.0))
    assert s["cents_per_share"] == pytest.approx(100 * 1.0 / 25)
    assert s["return_on_staked"] == pytest.approx(1.0 / 12.0)
    assert s["max_drawdown_usd"] == pytest.approx(4.0)
    # Cash in the open window: 6 x 0.50 paid, 2 x 0.70 back.
    assert (s["open_exposure_usd"], s["open_windows"]) == (pytest.approx(3.0 - 1.4), 1)
    assert (s["resting_usd"], s["resting_orders"]) == (pytest.approx(2 * 0.45), 2)
    assert s["resting_sell_shares"] == 1.0
    assert (s["orders_placed"], s["orders_filled"]) == (9, 6)
    assert s["windows_observed"] == 4

    assert await ledger.free_cash_usd(100.0) == pytest.approx(100 + 1.0 - 1.6 - 0.9)
    (pos,) = await ledger.open_positions()
    assert (pos["window_slug"], pos["asset"], pos["side"]) == (w4, "btc", "Up")
    assert (pos["shares"], pos["bought_shares"], pos["sold_shares"]) == (4.0, 6.0, 2.0)
    assert (pos["cost_usd"], pos["proceeds_usd"], pos["avg_price"]) == (
        pytest.approx(3.0), pytest.approx(1.4), pytest.approx(0.5))
    assert pos["offered_shares"] == 1.0
    assert o5 is not None and s5 is not None


async def test_free_cash_counts_every_buy_that_could_still_fill_and_no_sale(fade_db) -> None:
    here, there = await _window("btc"), await _window("eth")
    await _held(here, shares=10.0, price=0.40)  # 4.00 of cash in the open window
    (sale,) = await _place([_sale(here, price=0.60, shares=5.0)], ts=Q2 + 200)
    (resting,) = await _place([_order(there, price=0.50, shares=6.0)], ts=Q2 + 200)
    (gone,) = await _place([_order(there, price=0.45, shares=4.0, level=1)], ts=Q2 + 200)
    await ledger.cancel_orders([gone], ts=Q2 + 250, reason="requote")
    # The sale commits shares, not cash. The resting buy (3.00) and the cancelled one whose
    # tape is unread (1.80) both might still fill.
    assert await ledger.free_cash_usd(100.0) == pytest.approx(100 - 4.0 - 3.0 - 1.8)
    # The caller re-plans eth: its resting buy is left out, the cancelled one still counts.
    assert await ledger.free_cash_usd(100.0, replanned=[there]) == pytest.approx(100 - 4.0 - 1.8)
    # Once the cancelled buy's tape is read, it no longer counts.
    await ledger.record_flow([FlowUpdate(order_id=gone, cursor_ts=Q2 + 300)])
    assert await ledger.free_cash_usd(100.0, replanned=[there]) == pytest.approx(100 - 4.0)
    assert sale is not None and resting is not None
