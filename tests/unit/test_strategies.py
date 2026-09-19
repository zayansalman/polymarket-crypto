"""Operator strategy switches: a strategy opens new positions only while it is ON.

The switches are the STRATEGIES card's model. Each strategy reads its own
switch at the top of its tick, so turning one off stops NEW entries without
touching anything already open — settlement always keeps running.
"""

from __future__ import annotations


from pathlib import Path

import pytest
import pytest_asyncio

import db as _db
from polymarket_bot import strategies as _strategies


@pytest_asyncio.fixture(autouse=True)
async def _isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "test_strategies.db")
    await _db.init_db()
    yield


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_strategy_is_on_before_the_operator_touches_anything() -> None:
    for name in _strategies.STRATEGIES:
        assert await _strategies.enabled(name) is True, name


@pytest.mark.asyncio
async def test_turning_a_strategy_off_persists() -> None:
    await _strategies.set_enabled("daily_altcoin", False)
    assert await _strategies.enabled("daily_altcoin") is False


@pytest.mark.asyncio
async def test_turning_one_off_leaves_the_others_on() -> None:
    await _strategies.set_enabled("daily_altcoin", False)
    assert await _strategies.enabled("btc_updown") is True


@pytest.mark.asyncio
async def test_a_strategy_can_be_turned_back_on() -> None:
    await _strategies.set_enabled("daily_altcoin", False)
    await _strategies.set_enabled("daily_altcoin", True)
    assert await _strategies.enabled("daily_altcoin") is True


@pytest.mark.asyncio
async def test_an_unknown_strategy_is_rejected() -> None:
    with pytest.raises(ValueError):
        await _strategies.set_enabled("no_such_strategy", False)


@pytest.mark.asyncio
async def test_enabled_map_reports_one_entry_per_strategy() -> None:
    await _strategies.set_enabled("daily_altcoin", False)
    assert await _strategies.enabled_map() == {
        "btc_updown": True,
        "daily_altcoin": False,
    }


def test_the_shadow_forward_tester_is_gone() -> None:
    # Removed outright: it recorded hypothetical trades no panel ever showed.
    assert "shadow" not in _strategies.STRATEGIES


def test_every_strategy_stores_under_its_own_runtime_key() -> None:
    keys = {s.key for s in _strategies.STRATEGIES.values()}
    assert len(keys) == len(_strategies.STRATEGIES)
    assert all(k.startswith("runtime.strategy.") for k in keys)


# ---------------------------------------------------------------------------
# The switches actually gate each strategy
# ---------------------------------------------------------------------------


def _stub_scanner(monkeypatch: pytest.MonkeyPatch, calls: list[str]):
    from polymarket_bot.daily import scanner

    async def _discover(*_a, **_kw):
        calls.append("scan")
        return {"sol": {}}

    async def _scored_view(*_a, **_kw):
        return None  # nothing qualifies — keeps the test off the network

    async def _settle(*_a, **_kw):
        calls.append("settle")

    monkeypatch.setattr(scanner._market, "discover_daily_markets", _discover)
    monkeypatch.setattr(scanner, "_scored_view", _scored_view)
    monkeypatch.setattr(scanner, "_settle_due", _settle)
    return scanner


@pytest.mark.asyncio
async def test_daily_scanner_off_skips_the_scan_but_still_settles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    scanner = _stub_scanner(monkeypatch, calls)
    await _strategies.set_enabled("daily_altcoin", False)

    await scanner.scan_once(None)

    assert calls == ["settle"]


@pytest.mark.asyncio
async def test_daily_scanner_on_still_scans(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    scanner = _stub_scanner(monkeypatch, calls)

    await scanner.scan_once(None)

    assert calls == ["scan", "settle"]


def _snapshot():
    from polymarket_bot import paper

    return paper.PaperSnapshot(
        created_at="2026-09-19T12:00:00+00:00",
        window_slug="btc-updown-1h-1770000000",
        market_question="Bitcoin Up or Down?",
        remaining_seconds=1200,
        spot_price=100000.0,
        reference_price=99950.0,
        sigma_per_second=0.5,
        market_up_price=0.57,
        market_down_price=0.43,
        fair_up_prob=0.65,
        edge=0.08,
        signal_side="Up",
        confidence=0.8,
        notional_usd=3.0,
        reason="edge above minimum",
        feed_source="test",
        up_token_id="UP_TOKEN",
        down_token_id="DOWN_TOKEN",
    )


async def _open_position_count() -> int:
    async with _db.connect() as conn:
        async with conn.execute(
            "SELECT COUNT(*) AS n FROM paper_positions"
        ) as cur:
            return (await cur.fetchone())["n"]


@pytest.mark.asyncio
async def test_btc_loop_off_opens_no_position(monkeypatch: pytest.MonkeyPatch) -> None:
    from polymarket_bot import paper

    monkeypatch.setattr(paper, "_live_executor", None)
    monkeypatch.setattr(paper, "_risk_gate", None)
    await _strategies.set_enabled("btc_updown", False)

    await paper._maybe_open_position(_snapshot())

    assert await _open_position_count() == 0


@pytest.mark.asyncio
async def test_btc_loop_on_opens_a_position(monkeypatch: pytest.MonkeyPatch) -> None:
    from polymarket_bot import paper

    monkeypatch.setattr(paper, "_live_executor", None)
    monkeypatch.setattr(paper, "_risk_gate", None)

    await paper._maybe_open_position(_snapshot())

    assert await _open_position_count() == 1
