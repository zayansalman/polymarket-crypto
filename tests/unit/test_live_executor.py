"""Unit tests for the live Polymarket CLOB executor (issue #20).

Every test mocks ClobClient — nothing here ever touches the network.
Covers: boot refusal gates, order construction (tick/size rounding, minimum
order size), per-trade cap, bankroll cap, daily loss halt, kill switch,
cancel-on-roll, and paper-mode defaults.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from py_clob_client_v2 import OrderPayload

import config as _config
import db as _db
from polymarket_exec.execution.live import (
    BUY,
    SELL,
    LiveBootRefused,
    LiveExecutor,
    LiveOrderResult,
    _avg_fill_price,
    _round_price_to_tick,
    _round_size_down,
    _row_window_resolved,
    assert_live_boot_allowed,
    build_live_executor,
)

UP_TOKEN = "1234567890"


def test_avg_fill_price_buy_uses_making_over_taking() -> None:
    """BUY: makingAmount (USDC) / takingAmount (tokens) = realised price."""
    resp = {"status": "matched", "makingAmount": "2.893", "takingAmount": "5.26"}
    assert _avg_fill_price(resp, BUY, 0.57) == pytest.approx(0.550, abs=1e-3)


def test_avg_fill_price_sell_uses_taking_over_making() -> None:
    """SELL: takingAmount (USDC) / makingAmount (tokens) = realised price."""
    resp = {"status": "matched", "makingAmount": "5.0", "takingAmount": "2.9"}
    assert _avg_fill_price(resp, SELL, 0.55) == pytest.approx(0.58, abs=1e-3)


def test_avg_fill_price_falls_back_to_limit() -> None:
    """Resting / missing amounts / out-of-range price -> the posted limit."""
    assert _avg_fill_price({"status": "live"}, BUY, 0.57) == 0.57  # not matched
    assert _avg_fill_price({"status": "matched", "takingAmount": "5"}, BUY, 0.57) == 0.57
    # making/taking implying price > 1 is rejected as nonsense.
    bad = {"status": "matched", "makingAmount": "10", "takingAmount": "5"}
    assert _avg_fill_price(bad, BUY, 0.57) == 0.57


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def journal_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the SQLite journal at a throwaway DB."""
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "test_live.db")
    await _db.init_db()
    return _db


def _level(price: str, size: str = "100") -> SimpleNamespace:
    return SimpleNamespace(price=price, size=size)


def _mock_book(
    best_ask: str = "0.57",
    best_bid: str = "0.55",
    tick_size: str = "0.01",
    min_order_size: str = "5",
) -> SimpleNamespace:
    # py-clob-client books list levels worst -> best (best is LAST).
    return SimpleNamespace(
        asks=[_level("0.99"), _level(best_ask)],
        bids=[_level("0.01"), _level(best_bid)],
        tick_size=tick_size,
        min_order_size=min_order_size,
    )


def _mock_client(book: SimpleNamespace | None = None) -> MagicMock:
    client = MagicMock()
    client.get_order_book.return_value = book or _mock_book()
    client.create_and_post_order.return_value = {
        "success": True,
        "errorMsg": "",
        "orderID": "0xORDER1",
        "status": "live",
    }
    # Cancel responses confirm whichever order id was asked for.
    client.cancel_order.side_effect = lambda payload: {"canceled": [payload.orderID], "not_canceled": {}}
    client.cancel_all.return_value = {"canceled": [], "not_canceled": {}}
    client.get_order.return_value = {"size_matched": "5.26", "price": "0.57"}
    client.create_or_derive_api_key.return_value = SimpleNamespace(
        api_key="k", api_secret="s", api_passphrase="p"
    )
    client.get_ok.return_value = "OK"
    return client


def _executor(
    client: MagicMock,
    tmp_path: Path,
    *,
    max_trade: float = 3.0,
    daily_halt: float = 10.0,
    bankroll: float | None = 30.0,
    slippage: float = 0.5,
    exit_timeout: float = 5.0,
) -> LiveExecutor:
    return LiveExecutor(
        private_key="0x" + "1" * 64,
        funder="0xFUNDER",
        signature_type=2,
        max_trade_usd=max_trade,
        daily_loss_halt_usd=daily_halt,
        bankroll_cap_usd=bankroll,
        max_entry_slippage=slippage,
        exit_fill_timeout_seconds=exit_timeout,
        kill_switch_path=tmp_path / "KILL",
        client=client,
    )


async def _journal_rows(journal_db) -> list[dict]:
    async with journal_db.connect() as conn:
        async with conn.execute(
            "SELECT * FROM live_orders ORDER BY id"
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


# ---------------------------------------------------------------------------
# Boot refusal gates
# ---------------------------------------------------------------------------


def test_boot_refused_without_private_key() -> None:
    with pytest.raises(LiveBootRefused, match="POLYMARKET_PRIVATE_KEY"):
        assert_live_boot_allowed(private_key="", funder="0xF")


def test_boot_allowed_with_key_and_no_env_confirm_phrase(monkeypatch: pytest.MonkeyPatch) -> None:
    # Consent is the dashboard LIVE click, not an env phrase (see controller).
    monkeypatch.delenv("LIVE_CONFIRM", raising=False)
    assert not hasattr(_config, "LIVE_CONFIRM")
    assert_live_boot_allowed(private_key="0xabc", funder="0xF")


def test_boot_refused_without_funder() -> None:
    # MetaMask trades from the Polymarket Safe; without it as funder the order
    # maker falls back to the EOA and the CLOB rejects every order.
    with pytest.raises(LiveBootRefused, match="POLYMARKET_FUNDER"):
        assert_live_boot_allowed(private_key="0xabc", funder="", signature_type=2)


def test_boot_allowed_for_metamask_safe() -> None:
    assert_live_boot_allowed(private_key="0xabc", funder="0xSAFE", signature_type=2)


@pytest.mark.parametrize("sig", [0, 1, 3, 7])
def test_boot_refused_for_non_metamask_signature_types(sig: int) -> None:
    with pytest.raises(LiveBootRefused, match="SIGNATURE_TYPE"):
        assert_live_boot_allowed(private_key="0xabc", funder="0xF", signature_type=sig)


def test_boot_refused_on_malformed_risk_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # A typo'd risk limit must refuse live boot, never trade on the silent
    # looser default.
    monkeypatch.setattr(
        _config, "CONFIG_PARSE_ERRORS",
        ["TRADE_MAX_USD='O.50' is not a valid number"],
    )
    with pytest.raises(LiveBootRefused, match="invalid env value"):
        assert_live_boot_allowed(
            private_key="0xabc", funder="0xF"
        )


def test_boot_gate_reads_config_at_call_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_config, "POLYMARKET_PRIVATE_KEY", "")
    monkeypatch.setattr(_config, "POLYMARKET_FUNDER", "")
    with pytest.raises(LiveBootRefused):
        build_live_executor()
    monkeypatch.setattr(_config, "POLYMARKET_PRIVATE_KEY", "0xabc")
    monkeypatch.setattr(_config, "POLYMARKET_FUNDER", "0xFUNDER")
    executor = build_live_executor()
    assert isinstance(executor, LiveExecutor)


def test_paper_mode_is_default_in_config(monkeypatch: pytest.MonkeyPatch) -> None:
    # Paper is the default when BOT_MODE is unset — asserted on the
    # resolution logic, independent of any operator .env that opts into live.
    monkeypatch.delenv("BOT_MODE", raising=False)
    assert _config._env_choice("BOT_MODE", "paper", {"paper", "live"}) == "paper"


# ---------------------------------------------------------------------------
# Rounding helpers
# ---------------------------------------------------------------------------


def test_price_rounds_to_cent_tick() -> None:
    assert _round_price_to_tick(0.5678, 0.01) == 0.57
    assert _round_price_to_tick(0.554, 0.01) == 0.55


def test_price_clamped_inside_tick_bounds() -> None:
    assert _round_price_to_tick(0.0001, 0.01) == 0.01
    assert _round_price_to_tick(0.9999, 0.01) == 0.99


def test_size_rounds_down_to_two_decimals() -> None:
    assert _round_size_down(5.2631578) == 5.26
    assert _round_size_down(5.999999) == 5.99


# ---------------------------------------------------------------------------
# Order construction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_entry_places_gtc_buy_at_best_ask(journal_db, tmp_path: Path) -> None:
    client = _mock_client()
    executor = _executor(client, tmp_path)

    result = await executor.submit_entry(UP_TOKEN, 0.58, 3.0, window_slug="w1")

    assert result.ok and result.status == "SUBMITTED"
    assert result.order_id == "0xORDER1"
    args = client.create_and_post_order.call_args.args[0]
    assert args.token_id == UP_TOKEN
    assert args.side == "BUY"
    assert args.price == 0.57  # best ask from the book, not the gamma price
    assert args.size == 5.26  # floor(3.0 / 0.57, 2dp)
    rows = await _journal_rows(journal_db)
    assert len(rows) == 1
    assert rows[0]["status"] == "SUBMITTED"
    assert rows[0]["intent"] == "ENTRY"
    assert rows[0]["order_type"] == "GTC"
    assert rows[0]["clob_order_id"] == "0xORDER1"


@pytest.mark.asyncio
async def test_entry_falls_back_to_signal_price_without_book(
    journal_db, tmp_path: Path
) -> None:
    client = _mock_client()
    client.get_order_book.side_effect = RuntimeError("book down")
    executor = _executor(client, tmp_path)

    result = await executor.submit_entry(UP_TOKEN, 0.555, 3.0)

    assert result.ok
    args = client.create_and_post_order.call_args.args[0]
    assert args.price == 0.56  # side_price rounded to default 0.01 tick


@pytest.mark.asyncio
async def test_entry_handles_dict_shaped_order_book(
    journal_db, tmp_path: Path
) -> None:
    # py-clob-client-v2 returns the raw JSON dict, not an OrderBookSummary
    # object. _book_context must accept both shapes — otherwise submit_entry
    # crashes with AttributeError and live entries silently never reach CLOB
    # (regression observed in production: 5 positions opened against a dict-
    # shaped book, zero BUY orders journaled).
    client = _mock_client()
    client.get_order_book.return_value = {
        "asks": [{"price": "0.99", "size": "100"}, {"price": "0.60", "size": "50"}],
        "bids": [{"price": "0.01", "size": "100"}, {"price": "0.55", "size": "50"}],
        "tick_size": "0.01",
        "min_order_size": "5",
    }
    executor = _executor(client, tmp_path)

    result = await executor.submit_entry(UP_TOKEN, 0.60, 3.0)

    assert result.ok, result.reason
    args = client.create_and_post_order.call_args.args[0]
    assert args.price == 0.60


@pytest.mark.asyncio
async def test_entry_below_min_order_size_bumps_to_minimum(
    journal_db, tmp_path: Path
) -> None:
    # $3 at 0.70 = 4.28 shares < Polymarket's 5-share minimum. The operator opted
    # into auto-bump (#87): round the order UP to exactly the venue minimum so a
    # small configured clip still places, rather than blocking the window.
    client = _mock_client(_mock_book(best_ask="0.70"))
    executor = _executor(client, tmp_path)

    result = await executor.submit_entry(UP_TOKEN, 0.70, 3.0)

    assert result.ok, result.reason
    args = client.create_and_post_order.call_args.args[0]
    assert args.size == 5.0  # bumped up to the venue minimum
    assert args.price == 0.70


@pytest.mark.asyncio
async def test_entry_blocked_when_venue_minimum_too_large_to_bump(
    journal_db, tmp_path: Path
) -> None:
    # A market demanding more than 2x the normal 5-share minimum is too large to
    # auto-bump on this bankroll — block rather than silently overspend.
    client = _mock_client(_mock_book(best_ask="0.70", min_order_size="20"))
    executor = _executor(client, tmp_path)

    result = await executor.submit_entry(UP_TOKEN, 0.70, 3.0)

    assert not result.ok and result.status == "BLOCKED"
    assert "minimum" in result.reason
    client.create_and_post_order.assert_not_called()
    rows = await _journal_rows(journal_db)
    assert rows[0]["status"] == "BLOCKED"


@pytest.mark.asyncio
async def test_entry_blocked_without_token_id(journal_db, tmp_path: Path) -> None:
    executor = _executor(_mock_client(), tmp_path)
    result = await executor.submit_entry("", 0.57, 3.0)
    assert not result.ok and result.status == "BLOCKED"
    assert "token id" in result.reason


@pytest.mark.asyncio
async def test_exit_places_gtc_sell_at_best_bid(journal_db, tmp_path: Path) -> None:
    client = _mock_client()
    executor = _executor(client, tmp_path)
    await executor.submit_entry(UP_TOKEN, 0.57, 3.0, window_slug="w1")
    client.create_and_post_order.return_value = {
        "success": True,
        "orderID": "0xORDER2",
    }

    result = await executor.submit_exit(side_price=0.56, size=5.26, window_slug="w1")

    assert result.ok
    args = client.create_and_post_order.call_args.args[0]
    assert args.side == "SELL"
    assert args.token_id == UP_TOKEN  # remembered from the entry
    assert args.price == 0.55  # best bid
    assert args.size == 5.26  # matched size from get_order
    # Entry fully flattened: a new entry is allowed again.
    assert executor.entry_block_reason(3.0) is None


@pytest.mark.asyncio
async def test_exit_skipped_when_entry_never_filled(journal_db, tmp_path: Path) -> None:
    client = _mock_client()
    client.get_order.return_value = {"size_matched": "0"}
    executor = _executor(client, tmp_path)
    await executor.submit_entry(UP_TOKEN, 0.57, 3.0)
    client.create_and_post_order.reset_mock()

    result = await executor.submit_exit(side_price=0.55)

    assert not result.ok and result.status == "SKIPPED"
    client.create_and_post_order.assert_not_called()
    # Resting unfilled entry was cancelled as part of the flatten.
    client.cancel_order.assert_called_once_with(OrderPayload(orderID="0xORDER1"))


# ---------------------------------------------------------------------------
# Risk gates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_per_trade_cap_blocks_entry(journal_db, tmp_path: Path) -> None:
    client = _mock_client()
    executor = _executor(client, tmp_path, max_trade=3.0)

    result = await executor.submit_entry(UP_TOKEN, 0.57, 3.01)

    assert not result.ok and result.status == "BLOCKED"
    assert "per-trade cap" in result.reason
    client.create_and_post_order.assert_not_called()


@pytest.mark.asyncio
async def test_one_open_position_max(journal_db, tmp_path: Path) -> None:
    client = _mock_client()
    executor = _executor(client, tmp_path)
    first = await executor.submit_entry(UP_TOKEN, 0.57, 3.0)
    assert first.ok

    second = await executor.submit_entry(UP_TOKEN, 0.57, 3.0)

    assert not second.ok and second.status == "BLOCKED"
    assert "max 1" in second.reason
    assert client.create_and_post_order.call_count == 1


@pytest.mark.asyncio
async def test_fully_matched_entry_is_not_tracked_as_resting(
    journal_db, tmp_path: Path
) -> None:
    """A fully-matched entry has no resting remainder.

    Polymarket reports a market-matched BUY with status='matched' and the
    filled shares in takingAmount. Such an order is NOT resting in the book —
    keeping its id would make the singleton gate's `entry_order_resting` lie,
    and the next flatten would waste a doomed "matched orders can't be
    canceled" round trip. The position (not a phantom order) is what holds the
    max-1 slot.
    """
    client = _mock_client()
    client.create_and_post_order.return_value = {
        "success": True,
        "errorMsg": "",
        "orderID": "0xORDER1",
        "status": "matched",
        "takingAmount": "5.26",  # floor(3.0 / 0.57, 2dp) — fully filled
    }
    executor = _executor(client, tmp_path)

    result = await executor.submit_entry(UP_TOKEN, 0.57, 3.0, window_slug="w1")

    assert result.ok and result.status == "SUBMITTED"
    # Filled, so nothing rests: the order id is not tracked as resting.
    assert executor._entry_order_id is None
    assert executor._entry_matched_size == 5.26
    # The slot is still held by the open position (singleton honoured).
    assert "max 1" in (executor.entry_block_reason(3.0) or "")


@pytest.mark.asyncio
async def test_entry_records_real_avg_fill_price(
    journal_db, tmp_path: Path
) -> None:
    """A matched BUY records the REAL average fill price, not the limit (#103).

    Polymarket reports makingAmount (USDC paid) / takingAmount (tokens received);
    the realised price is their ratio. Booking the limit instead overstates PnL
    when the order fills better than the posted ask.
    """
    client = _mock_client()
    client.create_and_post_order.return_value = {
        "success": True,
        "errorMsg": "",
        "orderID": "0xORDER1",
        "status": "matched",
        "makingAmount": "2.893",  # USDC actually paid
        "takingAmount": "5.26",   # tokens received -> avg 2.893/5.26 = 0.550
    }
    executor = _executor(client, tmp_path)

    result = await executor.submit_entry(UP_TOKEN, 0.57, 3.0, window_slug="w1")

    assert result.ok
    assert executor._entry_price == pytest.approx(0.550, abs=1e-3)  # NOT the 0.57 limit


@pytest.mark.asyncio
async def test_entry_fill_price_falls_back_to_limit(
    journal_db, tmp_path: Path
) -> None:
    """No makingAmount (resting / unparseable) -> entry price stays the limit."""
    client = _mock_client()
    client.create_and_post_order.return_value = {
        "success": True,
        "orderID": "0xORDER1",
        "status": "matched",
        "takingAmount": "5.26",  # no makingAmount -> avg price not computable
    }
    executor = _executor(client, tmp_path)

    await executor.submit_entry(UP_TOKEN, 0.57, 3.0, window_slug="w1")

    assert executor._entry_price == pytest.approx(0.57)  # safe fallback to limit


@pytest.mark.asyncio
async def test_resync_flat_heals_phantom_open_state(
    journal_db, tmp_path: Path
) -> None:
    """resync_flat() clears a stale open-state the flat ledger contradicts.

    A live ledger row is closed only after a confirmed venue flatten, so when
    the entry path sees zero open rows the venue is genuinely flat. Any
    lingering in-memory position/order (left by an interrupted stop/restart)
    is a phantom that blocks every entry with "max 1" — resync_flat heals it.
    """
    client = _mock_client()
    executor = _executor(client, tmp_path)
    # Simulate the stranded state: the gate would block "max 1".
    executor._position_open = True
    executor._entry_order_id = "0xSTALE"
    executor._entry_token_id = UP_TOKEN
    assert "max 1" in (executor.entry_block_reason(3.0) or "")

    healed = await executor.resync_flat()

    assert healed is True
    assert executor._position_open is False
    assert executor._entry_order_id is None
    # The doomed/abandoned order is cancelled on the venue, not forgotten.
    client.cancel_order.assert_called_once_with(OrderPayload(orderID="0xSTALE"))
    # Entry is no longer blocked by the phantom.
    assert executor.entry_block_reason(3.0) is None


@pytest.mark.asyncio
async def test_tracks_position_follows_the_entry_lifecycle(journal_db, tmp_path: Path) -> None:
    """Claude, 2026-09-15, branch-review finding crash-after-entry-marks-missed."""
    client = _mock_client()
    executor = _executor(client, tmp_path)
    assert executor.tracks_position is False

    assert (await executor.submit_entry(UP_TOKEN, 0.57, 3.0)).ok  # rests unfilled
    assert executor.tracks_position is True

    await executor.resync_flat()
    assert executor.tracks_position is False


@pytest.mark.asyncio
async def test_resync_flat_noop_when_already_flat(
    journal_db, tmp_path: Path
) -> None:
    client = _mock_client()
    executor = _executor(client, tmp_path)

    healed = await executor.resync_flat()

    assert healed is False
    client.cancel_order.assert_not_called()


@pytest.mark.asyncio
async def test_bankroll_cap_blocks_session_overspend(journal_db, tmp_path: Path) -> None:
    client = _mock_client()
    executor = _executor(client, tmp_path, bankroll=5.0)
    # First buy consumes ~$3 of the $5 session bankroll.
    first = await executor.submit_entry(UP_TOKEN, 0.57, 3.0)
    assert first.ok
    await executor.submit_exit(side_price=0.55)  # flatten so position gate passes

    second = await executor.submit_entry(UP_TOKEN, 0.57, 3.0)

    assert not second.ok and second.status == "BLOCKED"
    assert "bankroll cap" in second.reason


@pytest.mark.asyncio
async def test_bankroll_cap_none_does_not_block(
    journal_db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With TRADE_BANKROLL_CAP_USD unset (None), the cap gate is disabled
    and no amount of cumulative spend triggers a 'bankroll cap' BLOCKED."""
    # Monkeypatch the config fallback used by the constructor — the test must
    # not depend on whatever value the developer has in their local .env.
    monkeypatch.setattr(_config, "TRADE_BANKROLL_CAP_USD", None)
    client = _mock_client()
    executor = _executor(client, tmp_path, bankroll=None)
    assert executor.bankroll_cap_usd is None

    # Spike the persisted spend counter way above any reasonable cap, then
    # confirm the gate bypasses it (not just "not yet hit").
    executor._daily_buy_notional = 9_999.99
    assert executor.entry_block_reason(3.0) is None

    result = await executor.submit_entry(UP_TOKEN, 0.57, 3.0)
    assert result.ok, f"cap should be disabled, got: {result.reason}"


@pytest.mark.asyncio
async def test_daily_loss_halt_blocks_entries(journal_db, tmp_path: Path) -> None:
    client = _mock_client()
    executor = _executor(client, tmp_path, daily_halt=10.0)
    await executor.record_realized_pnl(-4.0)
    assert executor.entry_block_reason(3.0) is None  # not yet at the halt

    await executor.record_realized_pnl(-6.5)  # cumulative -10.50

    result = await executor.submit_entry(UP_TOKEN, 0.57, 3.0)
    assert not result.ok and result.status == "BLOCKED"
    assert "daily loss halt" in result.reason
    client.create_and_post_order.assert_not_called()


@pytest.mark.asyncio
async def test_daily_risk_counters_survive_restart(journal_db, tmp_path: Path) -> None:
    """Stop/Start (a brand-new executor) must NOT reset the daily loss halt
    or grant a fresh bankroll — the counters are persisted in SQLite."""
    first = _executor(_mock_client(), tmp_path, daily_halt=10.0)
    await first.start()
    await first.record_realized_pnl(-10.5)  # trips the halt
    entry = await first.submit_entry(UP_TOKEN, 0.57, 3.0)
    assert entry.status == "BLOCKED" and "daily loss halt" in entry.reason

    # "It stopped, restart it": a fresh executor over the same DB.
    second = _executor(_mock_client(), tmp_path, daily_halt=10.0)
    await second.start()

    assert second.daily_realized_pnl == pytest.approx(-10.5)
    result = await second.submit_entry(UP_TOKEN, 0.57, 3.0)
    assert not result.ok and "daily loss halt" in result.reason


@pytest.mark.asyncio
async def test_daily_buy_notional_survives_restart(journal_db, tmp_path: Path) -> None:
    client = _mock_client()
    first = _executor(client, tmp_path, bankroll=5.0)
    await first.start()
    assert (await first.submit_entry(UP_TOKEN, 0.57, 3.0)).ok
    await first.submit_exit(side_price=0.55)  # flatten

    second = _executor(_mock_client(), tmp_path, bankroll=5.0)
    await second.start()

    assert second.daily_buy_notional == pytest.approx(0.57 * 5.26)
    result = await second.submit_entry(UP_TOKEN, 0.57, 3.0)
    assert not result.ok and "bankroll cap" in result.reason


@pytest.mark.asyncio
async def test_exit_fill_feeds_daily_loss_tracker(journal_db, tmp_path: Path) -> None:
    """Realized PnL reaches the halt tracker from CONFIRMED exit fills inside
    the executor — not from paper-price guesses at submission time."""
    client = _mock_client()
    executor = _executor(client, tmp_path)
    await executor.submit_entry(UP_TOKEN, 0.57, 3.0)

    result = await executor.submit_exit(side_price=0.55)

    assert result.ok
    assert executor.daily_realized_pnl == pytest.approx(5.26 * (0.55 - 0.57))


# ---------------------------------------------------------------------------
# Fee-true booking (#133): the venue charges a taker fee of 0.07·p·(1−p) per
# share, in USDC, on the portion of an order that crosses at placement
# (status='matched'); resting (maker) fills and redemptions are fee-free.
# The live books ignored this and overstated PnL (booked −$8.01 vs venue-true
# −$17.24 over the bot era). Oracle numbers below are position 1768's real
# venue economics: BUY 5.09 @ 0.51 cost $2.68493 all-in, redeemed at $5.09.
# ---------------------------------------------------------------------------

_TAKER_ENTRY_1768 = {
    "success": True,
    "errorMsg": "",
    "orderID": "0xTAKER",
    "status": "matched",
    "makingAmount": "2.5959",  # USDC paid at price×size (fee charged on top)
    "takingAmount": "5.09",  # outcome tokens received
}
_FEE_1768 = 0.07 * 0.51 * (1 - 0.51) * 5.09  # 0.089049 USDC


async def _taker_entry_executor(tmp_path: Path) -> LiveExecutor:
    client = _mock_client(_mock_book(best_ask="0.51"))
    client.create_and_post_order.return_value = dict(_TAKER_ENTRY_1768)
    executor = _executor(client, tmp_path)
    entry = await executor.submit_entry(UP_TOKEN, 0.51, 2.5959)
    assert entry.ok
    return executor


@pytest.mark.asyncio
async def test_settlement_books_entry_taker_fee_on_win(
    journal_db, tmp_path: Path
) -> None:
    executor = await _taker_entry_executor(tmp_path)

    result = await executor.record_settlement(True, "btc-updown-5m-1782332700")

    expected = 5.09 * (1.0 - 0.51) - _FEE_1768  # +2.4051 — the real redemption net
    assert result.ok and result.status == "SETTLED"
    assert result.notional_usd == pytest.approx(expected, abs=1e-4)
    assert executor.daily_realized_pnl == pytest.approx(expected, abs=1e-4)
    rows = await _journal_rows(journal_db)
    settle = [r for r in rows if r["intent"] == "SETTLEMENT"][-1]
    assert settle["notional_usd"] == pytest.approx(expected, abs=1e-4)


@pytest.mark.asyncio
async def test_settlement_books_entry_taker_fee_on_loss(
    journal_db, tmp_path: Path
) -> None:
    """A lost taker position costs exactly the USDC paid all-in at entry."""
    executor = await _taker_entry_executor(tmp_path)

    result = await executor.record_settlement(False, "btc-updown-5m-1782332700")

    assert result.ok
    assert executor.daily_realized_pnl == pytest.approx(-2.68493, abs=1e-3)


@pytest.mark.asyncio
async def test_settlement_books_no_fee_for_resting_maker_fill(
    journal_db, tmp_path: Path
) -> None:
    """An entry that rested (status='live') and filled later is a maker fill:
    no fee — settlement books gross PnL."""
    client = _mock_client()  # placement rests; get_order later says 5.26 matched
    executor = _executor(client, tmp_path)
    entry = await executor.submit_entry(UP_TOKEN, 0.57, 3.0)
    assert entry.ok

    result = await executor.record_settlement(True, "btc-updown-5m-1782332700")

    assert result.ok
    assert executor.daily_realized_pnl == pytest.approx(5.26 * (1.0 - 0.57))


@pytest.mark.asyncio
async def test_exit_immediate_fill_books_sell_taker_fee(
    journal_db, tmp_path: Path
) -> None:
    """A SELL that crosses at placement pays the taker fee on its proceeds;
    a maker (resting) entry contributes no entry fee."""
    client = _mock_client()
    executor = _executor(client, tmp_path)
    entry = await executor.submit_entry(UP_TOKEN, 0.57, 3.0)
    assert entry.ok
    client.create_and_post_order.return_value = {
        "success": True,
        "errorMsg": "",
        "orderID": "0xSELL",
        "status": "matched",
        "makingAmount": "5.26",  # tokens sold
        "takingAmount": "2.893",  # USDC received (0.55/share)
    }

    result = await executor.submit_exit(side_price=0.55)

    assert result.ok
    sell_fee = 0.07 * 0.55 * (1 - 0.55) * 5.26
    assert executor.daily_realized_pnl == pytest.approx(
        5.26 * (0.55 - 0.57) - sell_fee, abs=1e-4
    )


# ---------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kill_switch_blocks_entries_but_allows_exits(
    journal_db, tmp_path: Path
) -> None:
    """Kill = no NEW risk. Flattening an open position only reduces exposure,
    so exits stay allowed while the kill file exists."""
    client = _mock_client()
    executor = _executor(client, tmp_path)
    first = await executor.submit_entry(UP_TOKEN, 0.57, 3.0)
    assert first.ok
    (tmp_path / "KILL").touch()

    entry = await executor.submit_entry(UP_TOKEN, 0.57, 3.0)
    exit_ = await executor.submit_exit(side_price=0.55)

    assert entry.status == "BLOCKED" and "KILL" in entry.reason
    assert exit_.ok  # the flatten went through under kill
    sell = client.create_and_post_order.call_args.args[0]
    assert sell.side == "SELL"


@pytest.mark.asyncio
async def test_kill_switch_blocks_entry_appearing_after_gate(
    journal_db, tmp_path: Path
) -> None:
    """TOCTOU guard: a kill file created between the risk gate and the order
    POST must still stop the entry."""
    client = _mock_client()

    def _touch_kill_then_book(token_id):
        (tmp_path / "KILL").touch()
        return _mock_book()

    client.get_order_book.side_effect = _touch_kill_then_book
    executor = _executor(client, tmp_path)

    result = await executor.submit_entry(UP_TOKEN, 0.57, 3.0)

    assert not result.ok and result.status == "BLOCKED"
    client.create_and_post_order.assert_not_called()


@pytest.mark.asyncio
async def test_kill_switch_cancels_open_orders_once(journal_db, tmp_path: Path) -> None:
    client = _mock_client()
    executor = _executor(client, tmp_path)
    await executor.submit_entry(UP_TOKEN, 0.57, 3.0)
    (tmp_path / "KILL").touch()

    assert await executor.enforce_kill_switch() is True
    assert await executor.enforce_kill_switch() is True  # still active

    client.cancel_order.assert_called_once_with(OrderPayload(orderID="0xORDER1"))  # cancelled exactly once
    rows = await _journal_rows(journal_db)
    assert any(r["intent"] == "CANCEL" and r["status"] == "CANCELLED" for r in rows)


@pytest.mark.asyncio
async def test_kill_switch_rearms_after_file_removed(journal_db, tmp_path: Path) -> None:
    """Delete-to-re-arm must really re-arm: a SECOND kill must cancel the
    orders resting at that moment, not just block entries."""
    client = _mock_client()
    executor = _executor(client, tmp_path)
    await executor.submit_entry(UP_TOKEN, 0.57, 3.0)
    (tmp_path / "KILL").touch()
    assert await executor.enforce_kill_switch() is True  # first kill cancels

    (tmp_path / "KILL").unlink()
    assert await executor.enforce_kill_switch() is False  # re-armed

    # New position with a resting entry order, then kill again.
    await executor.submit_exit(side_price=0.55)
    second = await executor.submit_entry(UP_TOKEN, 0.57, 3.0)
    assert second.ok
    (tmp_path / "KILL").touch()
    assert await executor.enforce_kill_switch() is True

    cancelled_ids = [c.args[0].orderID for c in client.cancel_order.call_args_list]
    assert cancelled_ids.count("0xORDER1") >= 2  # second kill cancelled again


@pytest.mark.asyncio
async def test_kill_switch_inactive_without_file(journal_db, tmp_path: Path) -> None:
    executor = _executor(_mock_client(), tmp_path)
    assert executor.kill_switch_active() is False
    assert await executor.enforce_kill_switch() is False


# ---------------------------------------------------------------------------
# Cancel on roll
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_open_cancels_and_journal(journal_db, tmp_path: Path) -> None:
    client = _mock_client()
    executor = _executor(client, tmp_path)
    await executor.submit_entry(UP_TOKEN, 0.57, 3.0, window_slug="w1")

    cancelled = await executor.cancel_open(reason="WINDOW_ROLL")

    assert cancelled == ["0xORDER1"]
    client.cancel_order.assert_called_once_with(OrderPayload(orderID="0xORDER1"))
    rows = await _journal_rows(journal_db)
    cancel_rows = [r for r in rows if r["intent"] == "CANCEL"]
    assert cancel_rows and cancel_rows[0]["status"] == "CANCELLED"
    assert "WINDOW_ROLL" in cancel_rows[0]["details_json"]


@pytest.mark.asyncio
async def test_cancel_open_noop_without_order(journal_db, tmp_path: Path) -> None:
    client = _mock_client()
    executor = _executor(client, tmp_path)
    assert await executor.cancel_open() == []
    client.cancel_order.assert_not_called()


@pytest.mark.asyncio
async def test_exit_after_cancel_sells_filled_portion(journal_db, tmp_path: Path) -> None:
    """Partial fill then roll: cancel remembers the matched size for the exit."""
    client = _mock_client()
    client.get_order.return_value = {"size_matched": "2.5"}
    executor = _executor(client, tmp_path)
    await executor.submit_entry(UP_TOKEN, 0.57, 3.0)
    await executor.cancel_open(reason="WINDOW_ROLL")
    client.create_and_post_order.reset_mock()
    client.create_and_post_order.return_value = {"success": True, "orderID": "0xORDER2"}

    result = await executor.submit_exit(side_price=0.55)

    assert result.ok
    args = client.create_and_post_order.call_args.args[0]
    assert args.side == "SELL"
    assert args.size == 2.5


# ---------------------------------------------------------------------------
# Exit lifecycle: no stale SELL may ever rest into resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exit_timeout_cancels_resting_sell(journal_db, tmp_path: Path) -> None:
    """An exit SELL that does not fill within the bound is cancelled — it may
    not rest in the book of a 5-minute market — and the position stays
    tracked so the next tick retries."""
    client = _mock_client()
    orders = {
        "0xENTRY": {"size_matched": "5.26", "price": "0.57"},
        "0xSELL": {"size_matched": "0", "price": "0.55", "status": "live"},
    }
    client.get_order.side_effect = lambda oid: orders[oid]
    client.create_and_post_order.return_value = {"success": True, "orderID": "0xENTRY"}
    executor = _executor(client, tmp_path, exit_timeout=0.0)
    await executor.submit_entry(UP_TOKEN, 0.57, 3.0)
    client.create_and_post_order.return_value = {"success": True, "orderID": "0xSELL"}

    result = await executor.submit_exit(side_price=0.55)

    assert not result.ok and result.status == "UNFILLED"
    cancelled = [c.args[0].orderID for c in client.cancel_order.call_args_list]
    assert "0xSELL" in cancelled  # the unfilled SELL was cancelled
    # Position must still be tracked (max-1 gate holds, retry possible).
    assert executor.entry_block_reason(3.0) is not None
    # Nothing was recorded as realized — the sell never filled.
    assert executor.daily_realized_pnl == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_exit_partial_fill_then_timeout_records_only_fill(
    journal_db, tmp_path: Path
) -> None:
    client = _mock_client()
    orders = {
        "0xENTRY": {"size_matched": "5.26", "price": "0.57"},
        "0xSELL": {"size_matched": "2.0", "price": "0.55", "status": "live"},
    }
    client.get_order.side_effect = lambda oid: orders[oid]
    client.create_and_post_order.return_value = {"success": True, "orderID": "0xENTRY"}
    executor = _executor(client, tmp_path, exit_timeout=0.0)
    await executor.submit_entry(UP_TOKEN, 0.57, 3.0)
    client.create_and_post_order.return_value = {"success": True, "orderID": "0xSELL"}

    result = await executor.submit_exit(side_price=0.55)

    assert not result.ok and result.status == "UNFILLED"
    assert result.size == pytest.approx(2.0)  # the confirmed partial fill
    assert executor.daily_realized_pnl == pytest.approx(2.0 * (0.55 - 0.57))
    # Retry sells only the remainder, never the already-sold shares.
    orders["0xSELL2"] = {"size_matched": "3.26", "price": "0.55"}
    client.create_and_post_order.return_value = {"success": True, "orderID": "0xSELL2"}
    retry = await executor.submit_exit(side_price=0.55)
    assert retry.ok
    args = client.create_and_post_order.call_args.args[0]
    assert args.size == pytest.approx(3.26)
    assert executor.entry_block_reason(3.0) is None  # flat again


# ---------------------------------------------------------------------------
# Entry slippage guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_entry_blocked_when_ask_gaps_above_signal(
    journal_db, tmp_path: Path
) -> None:
    client = _mock_client(_mock_book(best_ask="0.90"))
    executor = _executor(client, tmp_path, slippage=0.02)

    result = await executor.submit_entry(UP_TOKEN, 0.57, 3.0)

    assert not result.ok and result.status == "BLOCKED"
    assert "slippage" in result.reason
    client.create_and_post_order.assert_not_called()


# ---------------------------------------------------------------------------
# Boot reconciliation
# ---------------------------------------------------------------------------


async def _seed_open_position(journal_db, window_slug: str = "w-prev") -> int:
    async with journal_db.connect() as conn:
        cur = await conn.execute(
            "INSERT INTO paper_positions("
            "opened_at, window_slug, side, state, entry_price, notional_usd, shares"
            ") VALUES (?, ?, 'Up', 'open', 0.57, 3.0, 5.26)",
            ("2026-06-10T11:00:00+00:00", window_slug),
        )
        position_id = int(cur.lastrowid)
        await conn.commit()
    return position_id


@pytest.mark.asyncio
async def test_boot_cancels_all_resting_orders(journal_db, tmp_path: Path) -> None:
    client = _mock_client()
    executor = _executor(client, tmp_path)

    await executor.start()

    client.cancel_all.assert_called_once()
    rows = await _journal_rows(journal_db)
    assert any(r["intent"] == "CANCEL_ALL" for r in rows)


@pytest.mark.asyncio
async def test_boot_refused_when_cancel_all_fails(journal_db, tmp_path: Path) -> None:
    client = _mock_client()
    client.cancel_all.side_effect = RuntimeError("CLOB down")
    executor = _executor(client, tmp_path)

    with pytest.raises(LiveBootRefused, match="cancel resting orders"):
        await executor.start()


@pytest.mark.asyncio
async def test_boot_adopts_open_position_from_journal(
    journal_db, tmp_path: Path
) -> None:
    """A restart with an open live position must re-adopt it (so the normal
    exit path flattens it) instead of paper-closing it with fictional PnL."""
    await _seed_open_position(journal_db, window_slug="w-prev")
    await journal_db.journal_live_order(
        intent="ENTRY", side="BUY", status="SUBMITTED", window_slug="w-prev",
        token_id=UP_TOKEN, price=0.57, size=5.26, clob_order_id="0xOLD",
    )
    client = _mock_client()
    client.get_order.return_value = {"size_matched": "5.26", "price": "0.57"}
    executor = _executor(client, tmp_path)

    await executor.start()

    # The adopted position holds the max-1 gate...
    assert executor.entry_block_reason(3.0) == (
        "an open position/order already exists (max 1)"
    )
    # ...and the normal exit path can flatten it.
    client.create_and_post_order.return_value = {"success": True, "orderID": "0xSELL"}
    result = await executor.submit_exit(side_price=0.55, window_slug="w-prev")
    assert result.ok
    args = client.create_and_post_order.call_args.args[0]
    assert args.side == "SELL" and args.token_id == UP_TOKEN
    assert args.size == pytest.approx(5.26)


@pytest.mark.asyncio
async def test_boot_closes_open_row_without_live_trace(
    journal_db, tmp_path: Path
) -> None:
    """An open ledger row with NO live order journaled behind it is a paper
    artifact — closed harmlessly at boot, never adopted or traded against."""
    position_id = await _seed_open_position(journal_db, window_slug="w-paper")
    executor = _executor(_mock_client(), tmp_path)

    await executor.start()

    async with journal_db.connect() as conn:
        async with conn.execute(
            "SELECT state, exit_reason FROM paper_positions WHERE position_id = ?",
            (position_id,),
        ) as cur:
            row = dict(await cur.fetchone())
    assert row["state"] == "closed"
    assert row["exit_reason"] == "RECONCILED_NO_LIVE_TRACE"
    assert executor.entry_block_reason(3.0) is None


@pytest.mark.asyncio
async def test_boot_closes_open_row_when_entry_never_filled(
    journal_db, tmp_path: Path
) -> None:
    position_id = await _seed_open_position(journal_db, window_slug="w-prev")
    await journal_db.journal_live_order(
        intent="ENTRY", side="BUY", status="SUBMITTED", window_slug="w-prev",
        token_id=UP_TOKEN, price=0.57, size=5.26, clob_order_id="0xOLD",
    )
    client = _mock_client()
    client.get_order.return_value = {"size_matched": "0"}
    executor = _executor(client, tmp_path)

    await executor.start()

    async with journal_db.connect() as conn:
        async with conn.execute(
            "SELECT state, exit_reason FROM paper_positions WHERE position_id = ?",
            (position_id,),
        ) as cur:
            row = dict(await cur.fetchone())
    assert row["state"] == "closed"
    assert row["exit_reason"] == "RECONCILED_UNFILLED"
    assert executor.entry_block_reason(3.0) is None


# ---------------------------------------------------------------------------
# Boot reconciliation when the CLOB no longer knows the entry order (#132).
# A window that has already RESOLVED holds no executable risk: boot must heal
# (close the stale row; the reconcile tool trues-up its PnL), never refuse.
# An UNRESOLVED window adopts from the journal's placement response when the
# lookup fails; only an unknowable fill state on live risk may refuse boot.
# ---------------------------------------------------------------------------

_RESOLVED_SLUG = "btc-updown-5m-1700000000"  # window start long in the past


def _unresolved_slug() -> str:
    return f"btc-updown-5m-{int(time.time()) + 3600}"


@pytest.mark.asyncio
@pytest.mark.parametrize("lookup", ["returns_none", "raises"])
async def test_boot_closes_stale_row_when_order_pruned_and_window_resolved(
    journal_db, tmp_path: Path, lookup: str
) -> None:
    """The 2026-06-25 outage: get_order returned None for a resolved window's
    order and boot hard-refused (then crashed on NoneType). Zero live risk
    remained — boot must close the row for reconciliation and proceed."""
    position_id = await _seed_open_position(journal_db, window_slug=_RESOLVED_SLUG)
    await journal_db.journal_live_order(
        intent="ENTRY", side="BUY", status="SUBMITTED", window_slug=_RESOLVED_SLUG,
        token_id=UP_TOKEN, price=0.51, size=5.09, clob_order_id="0xPRUNED",
        details={"response": {"status": "matched", "takingAmount": "5.09"}},
    )
    client = _mock_client()
    if lookup == "returns_none":
        client.get_order.return_value = None
    else:
        client.get_order.side_effect = RuntimeError("order not found")
    executor = _executor(client, tmp_path)

    await executor.start()  # must NOT raise LiveBootRefused

    async with journal_db.connect() as conn:
        async with conn.execute(
            "SELECT state, exit_reason FROM paper_positions WHERE position_id = ?",
            (position_id,),
        ) as cur:
            row = dict(await cur.fetchone())
    assert row["state"] == "closed"
    assert row["exit_reason"] == "RECONCILED_STALE_RESOLVED"
    assert executor.entry_block_reason(3.0) is None


@pytest.mark.asyncio
async def test_boot_adopts_unresolved_row_from_journal_when_lookup_fails(
    journal_db, tmp_path: Path
) -> None:
    """Live risk in a still-open window with a pruned/unreachable order lookup:
    the journal's own placement response recorded the match — adopt from it."""
    slug = _unresolved_slug()
    await _seed_open_position(journal_db, window_slug=slug)
    await journal_db.journal_live_order(
        intent="ENTRY", side="BUY", status="SUBMITTED", window_slug=slug,
        token_id=UP_TOKEN, price=0.57, size=5.26, clob_order_id="0xPRUNED",
        details={"response": {"status": "matched", "takingAmount": "5.26"}},
    )
    client = _mock_client()
    client.get_order.return_value = None
    executor = _executor(client, tmp_path)

    await executor.start()

    assert executor.entry_block_reason(3.0) == (
        "an open position/order already exists (max 1)"
    )


@pytest.mark.asyncio
async def test_boot_refuses_unresolved_row_when_fill_state_unknowable(
    journal_db, tmp_path: Path
) -> None:
    """No venue answer AND no journal fill record on an unresolved window is
    genuinely blind live risk — the refusal safety must be preserved, with a
    truthful message (not the NoneType AttributeError artifact)."""
    slug = _unresolved_slug()
    await _seed_open_position(journal_db, window_slug=slug)
    await journal_db.journal_live_order(
        intent="ENTRY", side="BUY", status="SUBMITTED", window_slug=slug,
        token_id=UP_TOKEN, price=0.57, size=5.26, clob_order_id="0xPRUNED",
    )
    client = _mock_client()
    client.get_order.return_value = None
    executor = _executor(client, tmp_path)

    with pytest.raises(LiveBootRefused, match="not resolved"):
        await executor.start()


# ---------------------------------------------------------------------------
# Error paths / journaling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_order_rejection_is_journaled_as_error(journal_db, tmp_path: Path) -> None:
    client = _mock_client()
    client.create_and_post_order.return_value = {
        "success": False,
        "errorMsg": "not enough balance",
    }
    executor = _executor(client, tmp_path)

    result = await executor.submit_entry(UP_TOKEN, 0.57, 3.0)

    assert not result.ok and result.status == "ERROR"
    assert "not enough balance" in result.reason
    rows = await _journal_rows(journal_db)
    assert rows[0]["status"] == "ERROR"
    # A failed entry must not lock the one-position gate.
    assert executor.entry_block_reason(3.0) is None


@pytest.mark.asyncio
async def test_order_exception_is_journaled_as_error(journal_db, tmp_path: Path) -> None:
    client = _mock_client()
    client.create_and_post_order.side_effect = RuntimeError("CLOB 503")
    executor = _executor(client, tmp_path)

    result = await executor.submit_entry(UP_TOKEN, 0.57, 3.0)

    assert not result.ok and result.status == "ERROR"
    rows = await _journal_rows(journal_db)
    assert rows[0]["status"] == "ERROR" and "CLOB 503" in rows[0]["error"]


@pytest.mark.asyncio
async def test_start_derives_and_sets_api_creds(journal_db, tmp_path: Path) -> None:
    client = _mock_client()
    executor = _executor(client, tmp_path)

    await executor.start()

    client.create_or_derive_api_key.assert_called_once()
    client.set_api_creds.assert_called_once()
    client.get_ok.assert_called_once()


def test_executor_requires_key_without_injected_client() -> None:
    with pytest.raises(LiveBootRefused):
        LiveExecutor(private_key="", client=None)


def test_private_key_never_in_result(tmp_path: Path) -> None:
    executor = _executor(_mock_client(), tmp_path)
    result = LiveOrderResult(ok=True, status="SUBMITTED")
    assert "1111" not in repr(result)
    assert executor._private_key.startswith("0x")  # held privately only


@pytest.mark.asyncio
async def test_start_refreshes_balance_allowance(journal_db, tmp_path: Path) -> None:
    client = _mock_client()
    ex = _executor(client, tmp_path)
    await ex.start()
    client.update_balance_allowance.assert_called_once()
    params = client.update_balance_allowance.call_args.args[0]
    assert params.signature_type == 2  # the executor's configured type


@pytest.mark.asyncio
async def test_boot_cancel_retries_transient_then_succeeds(
    journal_db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient 425 'order manager not ready' on cancel_all is retried,
    not treated as fatal — reconciliation succeeds once the cancel does."""
    monkeypatch.setattr("polymarket_exec.execution.live._BOOT_CANCEL_BACKOFF_SECONDS", 0.0)
    client = _mock_client()
    client.cancel_all.side_effect = [
        Exception("PolyApiException[status_code=425, order manager not ready]"),
        Exception("PolyApiException[status_code=425, order manager not ready]"),
        {"canceled": [], "not_canceled": {}},
    ]
    ex = _executor(client, tmp_path)
    await ex._reconcile_account()  # must NOT raise
    assert client.cancel_all.call_count == 3
    rows = await _journal_rows(journal_db)
    assert any(r["intent"] == "CANCEL_ALL" and r["status"] == "CANCELLED" for r in rows)


@pytest.mark.asyncio
async def test_boot_cancel_refuses_after_exhausting_retries(
    journal_db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the cancel keeps failing, boot is STILL refused — the never-trade-on-
    unknown-resting-orders safety is preserved, just no longer tripped by a blip."""
    monkeypatch.setattr("polymarket_exec.execution.live._BOOT_CANCEL_BACKOFF_SECONDS", 0.0)
    client = _mock_client()
    client.cancel_all.side_effect = Exception("PolyApiException[status_code=425]")
    ex = _executor(client, tmp_path)
    with pytest.raises(LiveBootRefused, match="after 5 attempts"):
        await ex._reconcile_account()
    assert client.cancel_all.call_count == 5


# ---------------------------------------------------------------------------
# Settlement must not book a phantom fill on a venue lookup failure (#103/#109).
# A resting entry that never matched on-venue must settle to held=0 when the
# get_order lookup fails — the "assume filled" default is for the EXIT/SELL path
# (under-selling strands tokens), NOT for settle (over-counting books fiction).
# ---------------------------------------------------------------------------


def _resting_unfilled_entry(executor: LiveExecutor) -> None:
    """Put the executor in the state of a submitted entry that never matched."""
    executor._position_open = True
    executor._entry_order_id = "0xRESTING"
    executor._entry_token_id = UP_TOKEN
    executor._entry_matched_size = None  # never matched on venue
    executor._entry_size = 5.0
    executor._entry_price = 0.55


@pytest.mark.asyncio
async def test_settlement_no_phantom_win_when_fill_lookup_fails(
    journal_db, tmp_path: Path
) -> None:
    """won=True + failed fill lookup on a never-matched entry -> held 0, PnL 0."""
    client = _mock_client()
    client.get_order.side_effect = RuntimeError("get_order down")  # lookup failure
    executor = _executor(client, tmp_path)
    _resting_unfilled_entry(executor)

    result = await executor.record_settlement(
        won=True, window_slug="btc-updown-5m-1781827800"
    )

    assert result.status == "SETTLED"
    assert result.size == 0.0  # no phantom held size
    assert executor.daily_realized_pnl == 0.0  # no phantom WIN booked


@pytest.mark.asyncio
async def test_settlement_no_phantom_loss_when_fill_lookup_fails(
    journal_db, tmp_path: Path
) -> None:
    """won=False + failed fill lookup on a never-matched entry -> held 0, PnL 0."""
    client = _mock_client()
    client.get_order.side_effect = RuntimeError("get_order down")
    executor = _executor(client, tmp_path)
    _resting_unfilled_entry(executor)

    result = await executor.record_settlement(
        won=False, window_slug="btc-updown-5m-1781827800"
    )

    assert result.size == 0.0
    assert executor.daily_realized_pnl == 0.0  # no phantom LOSS booked


@pytest.mark.asyncio
async def test_matched_entry_size_asymmetry_on_lookup_failure(
    journal_db, tmp_path: Path
) -> None:
    """Exit path stays optimistic (assume filled); settle path is conservative."""
    client = _mock_client()
    client.get_order.side_effect = RuntimeError("get_order down")
    executor = _executor(client, tmp_path)
    executor._entry_order_id = "0xRESTING"
    executor._entry_size = 5.0

    # EXIT default: assume filled so the follow-up SELL never strands tokens.
    assert await executor._matched_entry_size() == 5.0
    # SETTLE: no SELL, so a failed lookup must not manufacture held size.
    assert await executor._matched_entry_size(assume_filled_on_error=False) == 0.0


# ---------------------------------------------------------------------------
# Strategy slots: one open position per strategy (operator decision 2026-09-14)
# ---------------------------------------------------------------------------

SPOT_PUSH_ID = "btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal"
KRONOS_ID = "kronos_lc2004_btcusdt_1h_finetune_up_chance_vs_polymarket_price"
HSLUG = "bitcoin-up-or-down-september-13-2026-3pm-et"


def _distinct_order_ids(client: MagicMock) -> None:
    ids = iter(f"0xORDER{i}" for i in range(1, 100))
    client.create_and_post_order.side_effect = lambda args: {
        "success": True, "errorMsg": "", "orderID": next(ids), "status": "live",
    }


async def _seed_hourly_row(journal_db, strategy_id: str, *, mode: str = "live",
                           start: int | None = None) -> int:
    start = start if start is not None else int(time.time()) // 3600 * 3600
    async with journal_db.connect() as conn:
        cur = await conn.execute(
            "INSERT INTO paper_positions(opened_at, window_slug, side, state, entry_price,"
            " notional_usd, shares, mode, strategy_id, market_timeframe, window_start_ts)"
            " VALUES ('2026-09-13T19:00:30+00:00', ?, 'Down', 'open', 0.57, 3.0, 5.26, ?, ?,"
            " '1h', ?)",
            (HSLUG, mode, strategy_id, start),
        )
        await conn.commit()
        return int(cur.lastrowid)


async def _row(journal_db, position_id: int) -> dict:
    async with journal_db.connect() as conn:
        async with conn.execute(
            "SELECT * FROM paper_positions WHERE position_id = ?", (position_id,)
        ) as cur:
            return dict(await cur.fetchone())


@pytest.mark.asyncio
async def test_slots_share_client_and_gate_but_hold_their_own_position(
    journal_db, tmp_path: Path
) -> None:
    client = _mock_client()
    account = _executor(client, tmp_path)
    spot_push_slot, kronos_slot = account.slot_executor(SPOT_PUSH_ID), account.slot_executor(KRONOS_ID)
    assert account.slot_executor(SPOT_PUSH_ID) is spot_push_slot
    assert spot_push_slot.gate is account.gate and spot_push_slot._client is client

    assert (await spot_push_slot.submit_entry(UP_TOKEN, 0.57, 3.0, window_slug=HSLUG)).ok

    assert "max 1" in (spot_push_slot.entry_block_reason(3.0) or "")
    assert kronos_slot.entry_block_reason(3.0) is None
    assert account.entry_block_reason(3.0) is None
    entry = [r for r in await _journal_rows(journal_db) if r["intent"] == "ENTRY"][-1]
    assert entry["strategy_id"] == SPOT_PUSH_ID


def test_slot_executor_needs_a_client_and_the_account_executor(tmp_path: Path) -> None:
    unbuilt = LiveExecutor(private_key="0x" + "1" * 64, kill_switch_path=tmp_path / "KILL")
    with pytest.raises(RuntimeError):
        unbuilt.slot_executor(SPOT_PUSH_ID)
    slot = _executor(_mock_client(), tmp_path).slot_executor(SPOT_PUSH_ID)
    with pytest.raises(RuntimeError):
        slot.slot_executor(KRONOS_ID)


@pytest.mark.asyncio
async def test_kill_switch_and_stop_cancel_resting_orders_in_every_slot(
    journal_db, tmp_path: Path
) -> None:
    client = _mock_client()
    _distinct_order_ids(client)
    account = _executor(client, tmp_path)
    await account.slot_executor(SPOT_PUSH_ID).submit_entry(UP_TOKEN, 0.57, 3.0, window_slug=HSLUG)
    await account.slot_executor(KRONOS_ID).submit_entry(UP_TOKEN, 0.57, 3.0, window_slug=HSLUG)
    (tmp_path / "KILL").touch()

    assert await account.enforce_kill_switch() is True

    cancelled = {c.args[0].orderID for c in client.cancel_order.call_args_list}
    assert cancelled == {"0xORDER1", "0xORDER2"}
    assert account.slot_executor(SPOT_PUSH_ID)._entry_order_id is None
    assert account.slot_executor(KRONOS_ID)._entry_order_id is None


@pytest.mark.asyncio
async def test_cancel_open_all_covers_the_account_and_every_slot(
    journal_db, tmp_path: Path
) -> None:
    client = _mock_client()
    _distinct_order_ids(client)
    account = _executor(client, tmp_path)
    await account.submit_entry(UP_TOKEN, 0.57, 3.0)
    await account.slot_executor(SPOT_PUSH_ID).submit_entry(UP_TOKEN, 0.57, 3.0, window_slug=HSLUG)

    assert sorted(await account.cancel_open_all(reason="LOOP_STOP")) == ["0xORDER1", "0xORDER2"]


@pytest.mark.asyncio
async def test_boot_adopts_one_row_per_strategy_using_that_strategys_order(
    journal_db, tmp_path: Path
) -> None:
    spot_push_row_id = await _seed_hourly_row(journal_db, SPOT_PUSH_ID)
    kronos_row_id = await _seed_hourly_row(journal_db, KRONOS_ID)
    # Same hour, same token: only strategy_id tells the two entries apart.
    await journal_db.journal_live_order(
        intent="ENTRY", side="BUY", status="SUBMITTED", window_slug=HSLUG,
        token_id=UP_TOKEN, price=0.57, size=5.26, clob_order_id="0xSPOTPUSH", strategy_id=SPOT_PUSH_ID,
    )
    await journal_db.journal_live_order(
        intent="ENTRY", side="BUY", status="SUBMITTED", window_slug=HSLUG,
        token_id=UP_TOKEN, price=0.57, size=5.26, clob_order_id="0xKRONOS", strategy_id=KRONOS_ID,
    )
    client = _mock_client()
    client.get_order.side_effect = lambda oid: {
        "size_matched": "5.26" if oid == "0xSPOTPUSH" else "0", "price": "0.57",
    }
    account = _executor(client, tmp_path)

    await account.start()

    assert "max 1" in (account.slot_executor(SPOT_PUSH_ID).entry_block_reason(3.0) or "")
    assert (await _row(journal_db, spot_push_row_id))["state"] == "open"
    assert (await _row(journal_db, kronos_row_id))["exit_reason"] == "RECONCILED_UNFILLED"
    assert account.slot_executor(KRONOS_ID).entry_block_reason(3.0) is None


@pytest.mark.asyncio
async def test_boot_refuses_two_open_rows_for_one_strategy(journal_db, tmp_path: Path) -> None:
    await _seed_hourly_row(journal_db, SPOT_PUSH_ID)
    await _seed_hourly_row(journal_db, SPOT_PUSH_ID)
    with pytest.raises(LiveBootRefused, match="max 1 per strategy"):
        await _executor(_mock_client(), tmp_path).start()


@pytest.mark.asyncio
async def test_boot_leaves_paper_rows_of_strategy_slots_for_paper_settlement(
    journal_db, tmp_path: Path
) -> None:
    paper_id = await _seed_hourly_row(journal_db, SPOT_PUSH_ID, mode="paper")
    account = _executor(_mock_client(), tmp_path)

    await account.start()

    row = await _row(journal_db, paper_id)
    assert row["state"] == "open" and row["exit_reason"] is None
    assert account.slot_executor(SPOT_PUSH_ID).entry_block_reason(3.0) is None


@pytest.mark.asyncio
async def test_boot_adopts_resolved_hourly_row_from_journal_so_it_settles(
    journal_db, tmp_path: Path
) -> None:
    """An hourly row settles from the Binance candle, which stays readable after
    resolution, so a pruned order with a journal-recorded fill is adopted, not zeroed."""
    past = int(time.time()) // 3600 * 3600 - 3 * 3600
    position_id = await _seed_hourly_row(journal_db, SPOT_PUSH_ID, start=past)
    await journal_db.journal_live_order(
        intent="ENTRY", side="BUY", status="SUBMITTED", window_slug=HSLUG,
        token_id=UP_TOKEN, price=0.57, size=5.26, clob_order_id="0xPRUNED",
        details={"response": {"status": "matched", "takingAmount": "5.26"}},
        strategy_id=SPOT_PUSH_ID,
    )
    client = _mock_client()
    client.get_order.return_value = None
    account = _executor(client, tmp_path)

    await account.start()

    assert (await _row(journal_db, position_id))["state"] == "open"
    settled = await account.slot_executor(SPOT_PUSH_ID).record_settlement(True, HSLUG)
    assert settled.ok and settled.size == pytest.approx(5.26)


def test_hourly_rows_resolve_an_hour_after_their_own_start() -> None:
    row = {"window_slug": HSLUG, "market_timeframe": "1h", "window_start_ts": 1_000_000}
    assert _row_window_resolved(row, now=1_000_000 + 3600 + 59) is False
    assert _row_window_resolved(row, now=1_000_000 + 3600 + 60) is True
    legacy = {"window_slug": "btc-updown-5m-1000000"}
    assert _row_window_resolved(legacy, now=1_000_000 + 359) is False
    assert _row_window_resolved(legacy, now=1_000_000 + 360) is True


# ---------------------------------------------------------------------------
# Claude, 2026-09-15, branch-review finding reconcile-resets-sold-size-double-books:
# adoption restores the shares a previous session already sold (its PnL is already
# in the reloaded gate and the row), so a later exit or settlement uses only the
# shares still held and never books the sold ones twice.
# ---------------------------------------------------------------------------

SPOT_PUSH_ID = "btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal"
KRONOS_ID = "kronos_lc2004_btcusdt_1h_finetune_up_chance_vs_polymarket_price"


def _orders_client(orders: dict[str, object]) -> MagicMock:
    """A mock client whose get_order answers per order id (an Exception raises)."""
    client = _mock_client()

    def get_order(oid: str) -> object:
        answer = orders[oid]
        if isinstance(answer, Exception):
            raise answer
        return answer

    client.get_order.side_effect = get_order
    return client


@pytest.mark.asyncio
async def test_boot_restores_sold_shares_on_the_legacy_slot_so_the_exit_sells_the_rest(
    journal_db, tmp_path: Path
) -> None:
    slug = _unresolved_slug()
    await _seed_open_position(journal_db, window_slug=slug)
    await journal_db.journal_live_order(
        intent="ENTRY", side="BUY", status="SUBMITTED", window_slug=slug,
        token_id=UP_TOKEN, price=0.57, size=5.26, clob_order_id="0xOLD",
    )
    await journal_db.journal_live_order(
        intent="EXIT", side="SELL", status="SUBMITTED", window_slug=slug,
        token_id=UP_TOKEN, price=0.55, size=5.26, clob_order_id="0xSELL1",
        details={"response": {"status": "live"}},
    )
    await journal_db.journal_live_order(
        intent="EXIT", side="SELL", status="UNFILLED", window_slug=slug,
        token_id=UP_TOKEN, price=0.55, size=2.0, clob_order_id="0xSELL1",
    )
    orders: dict[str, object] = {
        "0xOLD": {"size_matched": "5.26", "price": "0.57"},
        "0xSELL1": {"size_matched": "2", "price": "0.55"},
        "0xSELL2": {"size_matched": "3.26", "price": "0.55"},
    }
    client = _orders_client(orders)
    executor = _executor(client, tmp_path)

    await executor.start()
    pnl_at_boot = executor.gate.live_pnl

    client.create_and_post_order.return_value = {"success": True, "orderID": "0xSELL2"}
    result = await executor.submit_exit(side_price=0.55, window_slug=slug)
    assert result.ok
    assert client.create_and_post_order.call_args.args[0].size == pytest.approx(3.26)
    assert executor.gate.live_pnl == pytest.approx(pnl_at_boot + 3.26 * (0.55 - 0.57))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("evidence", "sold"),
    [("venue", 2.0), ("unfilled_row", 2.0), ("placement_match", 2.0), ("none", 0.0)],
)
async def test_boot_restores_sold_shares_on_a_strategy_slot_from_venue_then_journal(
    journal_db, tmp_path: Path, evidence: str, sold: float
) -> None:
    await _seed_hourly_row(journal_db, SPOT_PUSH_ID)
    await journal_db.journal_live_order(
        intent="ENTRY", side="BUY", status="SUBMITTED", window_slug=HSLUG,
        token_id=UP_TOKEN, price=0.57, size=5.26, clob_order_id="0xE",
        strategy_id=SPOT_PUSH_ID,
    )
    placement = (
        {"status": "matched", "makingAmount": "2", "takingAmount": "1.1"}
        if evidence == "placement_match" else {"status": "live"}
    )
    await journal_db.journal_live_order(
        intent="EXIT", side="SELL", status="SUBMITTED", window_slug=HSLUG,
        token_id=UP_TOKEN, price=0.55, size=5.26, clob_order_id="0xS",
        details={"response": placement}, strategy_id=SPOT_PUSH_ID,
    )
    if evidence == "unfilled_row":
        await journal_db.journal_live_order(
            intent="EXIT", side="SELL", status="UNFILLED", window_slug=HSLUG,
            token_id=UP_TOKEN, price=0.55, size=2.0, clob_order_id="0xS",
            strategy_id=SPOT_PUSH_ID,
        )
    exit_answer: object = (
        {"size_matched": "2", "price": "0.55"} if evidence == "venue"
        else RuntimeError("order lookup down") if evidence == "placement_match"
        else None
    )
    client = _orders_client({"0xE": {"size_matched": "5.26", "price": "0.57"},
                             "0xS": exit_answer})
    account = _executor(client, tmp_path)

    await account.start()

    slot = account.slot_executor(SPOT_PUSH_ID)
    # Never the SELL's requested size: only venue or journal evidence of a fill counts.
    assert slot._entry_sold_size == pytest.approx(sold)
    settled = await slot.record_settlement(True, HSLUG)
    assert settled.size == pytest.approx(5.26 - sold)


@pytest.mark.asyncio
async def test_boot_counts_only_this_positions_exits_in_this_slot(
    journal_db, tmp_path: Path
) -> None:
    await _seed_hourly_row(journal_db, SPOT_PUSH_ID)
    # An exit of an earlier position in the same hour, journalled before the entry.
    await journal_db.journal_live_order(
        intent="EXIT", side="SELL", status="SUBMITTED", window_slug=HSLUG,
        token_id=UP_TOKEN, price=0.55, size=5.0, clob_order_id="0xEARLIER",
        strategy_id=SPOT_PUSH_ID,
    )
    await journal_db.journal_live_order(
        intent="ENTRY", side="BUY", status="SUBMITTED", window_slug=HSLUG,
        token_id=UP_TOKEN, price=0.57, size=5.26, clob_order_id="0xE",
        strategy_id=SPOT_PUSH_ID,
    )
    # Another strategy's exit in the same hour.
    await journal_db.journal_live_order(
        intent="EXIT", side="SELL", status="SUBMITTED", window_slug=HSLUG,
        token_id=UP_TOKEN, price=0.55, size=5.0, clob_order_id="0xOTHER",
        strategy_id=KRONOS_ID,
    )
    client = _orders_client({
        "0xE": {"size_matched": "5.26", "price": "0.57"},
        "0xEARLIER": {"size_matched": "5", "price": "0.55"},
        "0xOTHER": {"size_matched": "5", "price": "0.55"},
    })
    account = _executor(client, tmp_path)

    await account.start()

    slot = account.slot_executor(SPOT_PUSH_ID)
    assert slot._entry_sold_size == 0.0
    assert (await slot.record_settlement(True, HSLUG)).size == pytest.approx(5.26)


@pytest.mark.asyncio
async def test_boot_closes_a_row_whose_exits_already_sold_every_share(
    journal_db, tmp_path: Path
) -> None:
    position_id = await _seed_hourly_row(journal_db, SPOT_PUSH_ID)
    async with journal_db.connect() as conn:
        await conn.execute(
            "UPDATE paper_positions SET realized_pnl_usd = -0.1 WHERE position_id = ?",
            (position_id,),
        )
        await conn.commit()
    await journal_db.journal_live_order(
        intent="ENTRY", side="BUY", status="SUBMITTED", window_slug=HSLUG,
        token_id=UP_TOKEN, price=0.57, size=5.26, clob_order_id="0xE",
        strategy_id=SPOT_PUSH_ID,
    )
    for oid in ("0xS1", "0xS2"):
        await journal_db.journal_live_order(
            intent="EXIT", side="SELL", status="SUBMITTED", window_slug=HSLUG,
            token_id=UP_TOKEN, price=0.55, size=5.26, clob_order_id=oid,
            strategy_id=SPOT_PUSH_ID,
        )
    client = _orders_client({
        "0xE": {"size_matched": "5.26", "price": "0.57"},
        "0xS1": {"size_matched": "2", "price": "0.55"},
        "0xS2": {"size_matched": "3.26", "price": "0.55"},
    })
    account = _executor(client, tmp_path)

    await account.start()

    row = await _row(journal_db, position_id)
    assert (row["state"], row["exit_reason"]) == ("closed", "RECONCILED_FLAT")
    assert row["realized_pnl_usd"] == pytest.approx(-0.1)  # kept, never re-booked
    assert account.gate.live_pnl == 0.0
    assert account.slot_executor(SPOT_PUSH_ID).entry_block_reason(3.0) is None
