"""Practice balance switch: a new starting balance that keeps the % gain or loss.

2026-11-18: Luke moved the practice wallet from $100 to $500 and asked that the
results so far carry over as percentages. The wallet is multiplied by the
factor once, on request, and the simple screen counts earlier trades at the
same factor, so the balance, the totals, the chart and the fee estimate stay
consistent with each other.
"""
from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

import db as _db
from polymarket_bot import paper_wallet
from polymarket_exec.ops.dashboard import simple_view as sv

RULES = {
    "edge_min": 0.02, "edge_max": 0.50, "price_min": 0.50, "price_max": 0.95,
    "min_seconds_left": 60, "tick_seconds": 3.0, "fee_aware": True, "fee_rate": 0.07,
}
BOT = {"state": "running", "mode": "paper", "detail": ""}
SELECTION = {"asset": "btc", "timeframe": "5m", "supported": True}


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds")


@pytest.fixture
def wallet_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "rescale.db")
    asyncio.run(_db.init_db())
    return _db


async def _buy(start: int, shares: float, entry: float) -> int:
    slug = f"btc-updown-5m-{start}"
    async with _db.connect() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        cur = await conn.execute(
            "INSERT INTO paper_positions(opened_at, window_slug, side, state, entry_price,"
            " notional_usd, shares, quote_source, strategy_style, mode, edge)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (_iso(start + 40), slug, "Up", "open", entry, shares * entry, shares,
             "clob", "settle", "paper", 0.05),
        )
        pid = cur.lastrowid
        await paper_wallet.apply_cash_flow(
            conn, position_id=pid, window_slug=slug, transaction_type="BUY",
            amount_usd=shares * entry,
        )
        await conn.commit()
    return pid


async def _settle(pid: int, start: int, payout: float) -> None:
    async with _db.connect() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        await conn.execute(
            "UPDATE paper_positions SET state = 'closed', closed_at = ? WHERE position_id = ?",
            (_iso(start + 305), pid),
        )
        await paper_wallet.apply_cash_flow(
            conn, position_id=pid, window_slug=f"btc-updown-5m-{start}",
            transaction_type="SETTLEMENT", amount_usd=payout,
        )
        await conn.commit()


async def _trade(start: int, shares: float, entry: float, payout: float) -> None:
    await _settle(await _buy(start, shares, entry), start, payout)


def _screen(now: float) -> dict:
    raw = asyncio.run(sv.load_raw())
    return sv.build_payload(raw, now=now, bot=BOT, selection=SELECTION, rules=RULES)


def test_switch_to_500_keeps_the_percentage_everywhere(wallet_db) -> None:
    now = time.time()
    start = int(now // 300) * 300 - 3600

    async def before() -> paper_wallet.Rescale:
        await _trade(start, 5, 0.60, 5.0)          # +2.00
        await _trade(start + 300, 5, 0.70, 0.0)    # -3.50
        return await paper_wallet.rescale(500)

    event = asyncio.run(before())
    assert event.factor == pytest.approx(5.0)
    assert (event.from_start_usd, event.to_start_usd) == (100.0, 500.0)
    assert event.balance_before_usd == pytest.approx(98.5)
    assert event.balance_after_usd == pytest.approx(492.5)
    assert event.change_pct == pytest.approx(-0.015)
    assert event.last_txn_id == 4
    assert asyncio.run(paper_wallet.cash_balance()) == pytest.approx(492.5)

    p = _screen(now)
    assert p["wallet"]["initial_usd"] == 500.0
    assert p["wallet"]["pnl_pct"] == pytest.approx(-0.015)
    assert p["stats"]["total_pnl_usd"] == pytest.approx(p["wallet"]["pnl_usd"])
    assert p["stats"]["fees_usd"] == 0.0  # these trades predate fee booking
    # The chart starts at $500 and has no jump at the switch.
    assert [round(pt["v"], 2) for pt in p["curve"]] == [500.0, 510.0, 492.5, 492.5]
    rescale = p["wallet"]["rescale"]
    assert (rescale["from_usd"], rescale["to_usd"], rescale["factor"]) == (100.0, 500.0, 5.0)
    assert rescale["first_start_usd"] == 100.0

    # A 10-share trade after the switch counts at its real size.
    asyncio.run(_trade(start + 600, 10, 0.60, 10.0))  # +4.00
    p = _screen(now)
    assert p["wallet"]["equity_usd"] == pytest.approx(496.5)
    assert p["wallet"]["pnl_usd"] == pytest.approx(-3.5)
    assert p["stats"]["total_pnl_usd"] == pytest.approx(-3.5)
    assert [t["scale"] for t in p["trades"]] == [1.0, 5.0, 5.0]
    assert [t["pnl_usd"] for t in p["trades"]] == [4.0, -3.5, 2.0]  # as they happened
    # Accuracy profits are on the same scale as the balance.
    assert p["accuracy"]["trades"] == 3
    assert sum(b["profit_usd"] for b in p["accuracy"]["buckets"]) == pytest.approx(-3.5)

    # Charging the fees these trades skipped counts the ones from before the
    # switch 5x too, so the balance and the totals still agree after fees.
    event = asyncio.run(paper_wallet.book_past_fees())
    fees = 5 * (sv.estimated_fee(5, 0.60) + sv.estimated_fee(5, 0.70)) + sv.estimated_fee(10, 0.60)
    assert event.amount_usd == pytest.approx(fees, abs=1e-3)
    p = _screen(now)
    assert p["wallet"]["equity_usd"] == pytest.approx(496.5 - fees, abs=1e-3)
    assert p["stats"]["total_pnl_usd"] == pytest.approx(p["wallet"]["pnl_usd"], abs=1e-3)
    assert p["stats"]["fees_usd"] == pytest.approx(fees, abs=1e-3)
    assert p["wallet"]["fees_paid_usd"] == pytest.approx(fees, abs=1e-3)
    assert p["curve"][-1]["v"] == pytest.approx(p["wallet"]["equity_usd"])


def test_switch_cannot_be_backdated_across_fee_booking(wallet_db) -> None:
    # Balances from before fees were charged don't include them, so a switch
    # can't count from back then.
    now = time.time()
    start = int(now // 300) * 300 - 3600
    asyncio.run(_trade(start, 5, 0.60, 5.0))
    assert asyncio.run(paper_wallet.book_past_fees()) is not None
    with pytest.raises(ValueError, match="before trading fees were added"):
        asyncio.run(paper_wallet.rescale(500, as_of=_iso(now - 600)))
    assert asyncio.run(paper_wallet.rescale(500)).factor == pytest.approx(5.0)


def test_open_trade_keeps_its_real_size_across_the_switch(wallet_db) -> None:
    now = time.time()
    start = int(now // 300) * 300 - 1200

    async def scenario() -> paper_wallet.Rescale:
        await _trade(start - 300, 5, 0.50, 5.0)    # +2.50 -> 102.50
        pid = await _buy(start, 10, 0.60)          # $6 open, cash 96.50
        event = await paper_wallet.rescale(500)
        await _settle(pid, start, 10.0)            # +4.00 at its real size
        return event

    event = asyncio.run(scenario())
    assert event.open_cost_usd == pytest.approx(6.0)
    assert event.balance_after_usd == pytest.approx(512.5)
    assert event.cash_after_usd == pytest.approx(506.5)
    p = _screen(now)
    assert p["wallet"]["equity_usd"] == pytest.approx(516.5)
    assert p["wallet"]["pnl_usd"] == pytest.approx(5 * 2.5 + 4.0)
    assert p["stats"]["total_pnl_usd"] == pytest.approx(p["wallet"]["pnl_usd"])
    assert [t["scale"] for t in p["trades"]] == [1.0, 5.0]


def test_switch_counts_from_when_it_was_asked_for(wallet_db, monkeypatch) -> None:
    """Luke asked at 8:12 PM; two 10-share trades lost before the app restarted.
    Counted from the request, those trades keep their real size instead of 5x."""
    now = datetime.now(UTC).replace(microsecond=0)
    clock = {"t": ""}
    monkeypatch.setattr(paper_wallet, "utc_now_iso", lambda: clock["t"])

    def at(seconds_ago: int) -> str:
        return _iso(now.timestamp() - seconds_ago)

    start = int(now.timestamp() // 300) * 300 - 4 * 3600

    async def scenario() -> paper_wallet.Rescale:
        clock["t"] = at(3 * 3600)
        await _trade(start, 5, 0.60, 5.0)               # +2.00 -> 102.00
        clock["t"] = at(2 * 3600 + 60)
        pid = await _buy(start + 300, 10, 0.52)         # open at the request
        clock["t"] = at(3600 + 3000)
        await _settle(pid, start + 300, 0.0)            # -5.20, real size
        clock["t"] = at(3600 + 2400)
        await _trade(start + 600, 10, 0.76, 0.0)        # -7.60, real size
        return await paper_wallet.rescale(500, as_of=at(2 * 3600))

    event = asyncio.run(scenario())
    assert event.last_txn_id == 3 and event.applied_txn_id == 6
    assert event.balance_before_usd == pytest.approx(102.0)  # cash 96.80 + $5.20 open
    assert event.open_cost_usd == pytest.approx(5.2)
    assert event.cash_after_usd == pytest.approx(89.2 + 4 * 102.0)
    assert "Counted from" in paper_wallet.describe_rescale(event)

    p = _screen(now.timestamp())
    assert p["wallet"]["equity_usd"] == pytest.approx(497.2)
    assert p["wallet"]["pnl_usd"] == pytest.approx(5 * 2.0 - 5.2 - 7.6)
    assert p["stats"]["total_pnl_usd"] == pytest.approx(p["wallet"]["pnl_usd"])
    # No jump at the switch, and no drop for the trades booked before it applied.
    assert [round(pt["v"], 2) for pt in p["curve"]] == [500.0, 510.0, 504.8, 497.2, 497.2]
    assert [t["scale"] for t in p["trades"]] == [1.0, 1.0, 5.0]


def test_backdated_switch_times_are_checked(wallet_db) -> None:
    now = datetime.now(UTC)
    for bad in ("soon", "2026-11-19T03:12:00", _iso(now.timestamp() + 600),
                _iso(now.timestamp() - 2 * 86_400)):
        with pytest.raises(ValueError):
            asyncio.run(paper_wallet.rescale(500, as_of=bad))
    assert asyncio.run(paper_wallet.cash_balance()) == pytest.approx(100.0)


def test_switches_stack_and_bad_amounts_are_refused(wallet_db) -> None:
    async def scenario() -> list[paper_wallet.Rescale]:
        await _trade(1_700_000_100, 5, 0.60, 5.0)
        first = await paper_wallet.rescale(500)
        second = await paper_wallet.rescale(250)
        return [first, second]

    first, second = asyncio.run(scenario())
    assert second.factor == pytest.approx(0.5)
    stored = asyncio.run(_db.get_config(paper_wallet.RESCALE_KEY))
    rescales = paper_wallet.load_rescales(stored)
    assert [r.to_start_usd for r in rescales] == [500.0, 250.0]
    assert paper_wallet.scale_for_txn(2, rescales) == pytest.approx(2.5)
    assert paper_wallet.scale_for_txn(3, rescales) == pytest.approx(1.0)
    assert paper_wallet.scale_for_txn(None, rescales) == 1.0
    assert asyncio.run(paper_wallet.cash_balance()) == pytest.approx(102.0 * 2.5)

    for bad in (5, 500_000, "lots", None, float("nan"), 250):
        with pytest.raises(ValueError):
            asyncio.run(paper_wallet.rescale(bad))
    assert asyncio.run(paper_wallet.cash_balance()) == pytest.approx(255.0)


def test_stored_switches_tolerate_bad_entries() -> None:
    good = {
        "at": "2026-11-19T03:30:00+00:00", "factor": 5.0, "from_start_usd": 100.0,
        "to_start_usd": 500.0, "balance_before_usd": 101.35, "balance_after_usd": 506.75,
        "cash_before_usd": 101.35, "cash_after_usd": 506.75, "open_cost_usd": 0.0,
        "last_txn_id": 340, "added_later": True,
    }
    import json

    parsed = paper_wallet.load_rescales(json.dumps([good, {"factor": 2}, "x"]))
    assert len(parsed) == 1 and parsed[0].last_txn_id == 340
    assert paper_wallet.load_rescales("not json") == []
    assert paper_wallet.load_rescales(None) == []
    text = paper_wallet.describe_rescale(parsed[0])
    assert text.startswith("Practice balance switched from $100.00 to $500.00")
    assert "+1.35% gain" in text and "count 5×" in text


def test_strategy_table_counts_this_bot_at_five_shares() -> None:
    now = 1_700_100_000.0
    fee5, fee10 = sv.estimated_fee(5, 0.60), sv.estimated_fee(10, 0.60)
    closed = [  # the wallet's results, after the fees each trade paid
        {"opened_at": _iso(now - 1_800), "shares": 10, "entry_price": 0.60,
         "pnl_usd": 4.0 - fee10, "fee_usd": fee10},
        {"opened_at": _iso(now - 900), "shares": 5, "entry_price": 0.60,
         "pnl_usd": -3.0 - fee5, "fee_usd": fee5},
    ]
    raw = {
        "tick": None, "wallet": None, "trades": [], "closed": closed, "open_positions": [],
        "any_open": 0, "curve": [], "activity": [], "window_trades": 0, "blocked": None,
        "learning_started": _iso(now - 3_600),
    }
    main = sv.build_payload(raw, now=now, bot=BOT, selection=SELECTION, rules=RULES)["strategies"][0]
    assert main["trades"] == 2
    assert main["profit_after_fees_usd"] == pytest.approx((2.0 - fee5) + (-3.0 - fee5), abs=1e-4)
