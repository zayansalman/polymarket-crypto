"""Editable paper trading rules: knobs, fee-aware entries, and the Settings API.

No network. DB-backed tests use a temporary SQLite file.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

import config as _config
import db as _db
from polymarket_bot import runtime_knobs as _knobs
from polymarket_bot.shadow.fees import DEFAULT_TAKER_FEE_RATE, taker_fee_per_share
from polymarket_bot.strategy import StrategyParams, signal_from_executable_edges

RULE_KNOBS = (
    "paper_entry_edge_min",
    "paper_entry_edge_max",
    "paper_min_entry_price",
    "paper_max_entry_price",
    "paper_entry_min_remaining_seconds",
    "paper_fee_aware",
)


def _params(**over) -> StrategyParams:
    base = dict(
        min_trade_usd=1.0, max_trade_usd=5.0, entry_edge_min=0.045, min_confidence=0.5,
        entry_min_remaining_seconds=60, entry_edge_max=0.07, min_entry_price=0.5,
    )
    base.update(over)
    return StrategyParams(**base)


# ------------------------------------------------------------------ strategy


def test_fee_rate_zero_keeps_the_old_decisions() -> None:
    old = _params()
    for edge_up, edge_down, up_ask, down_ask in (
        (0.05, -0.02, 0.55, 0.47),
        (0.03, 0.06, 0.52, 0.80),
        (0.044, 0.01, 0.60, 0.42),
    ):
        a = signal_from_executable_edges(edge_up, edge_down, 120, up_ask, down_ask, old)
        b = signal_from_executable_edges(
            edge_up, edge_down, 120, up_ask, down_ask, _params(fee_rate=0.0)
        )
        assert a == b


def test_fee_aware_skips_an_edge_the_fee_would_eat() -> None:
    # 5c edge at 50c: fee 1.75c leaves 3.25c, below the 4.5c minimum.
    side, _, notional, reason = signal_from_executable_edges(
        0.05, -0.05, 120, 0.50, 0.52, _params(fee_rate=DEFAULT_TAKER_FEE_RATE)
    )
    assert side is None and notional == 0.0
    assert reason == "skip: edge/confidence below threshold (after fees)"
    # Without fee-awareness the same tick trades.
    side, _, _, _ = signal_from_executable_edges(0.05, -0.05, 120, 0.50, 0.52, _params())
    assert side == "Up"


def test_fee_aware_enters_when_the_edge_survives_the_fee() -> None:
    side, _, notional, reason = signal_from_executable_edges(
        0.065, -0.05, 120, 0.50, 0.52, _params(fee_rate=DEFAULT_TAKER_FEE_RATE)
    )
    assert side == "Up" and notional > 0
    assert "after fees" in reason


def test_stale_model_cap_still_uses_the_raw_edge() -> None:
    # 8c raw edge is above the 7c cap even though only ~6.3c remains after fees.
    side, _, _, reason = signal_from_executable_edges(
        0.08, -0.05, 120, 0.50, 0.52, _params(fee_rate=DEFAULT_TAKER_FEE_RATE)
    )
    assert side is None and reason.startswith("skip: edge above cap")


def test_fee_aware_picks_the_side_with_more_edge_after_fees() -> None:
    # Up: 6c at 50c -> 4.25c after fees. Down: 5.5c at 90c -> ~4.87c after fees.
    assert taker_fee_per_share(0.9) == pytest.approx(0.0063)
    side, _, _, _ = signal_from_executable_edges(
        0.06, 0.055, 120, 0.50, 0.90, _params(fee_rate=DEFAULT_TAKER_FEE_RATE)
    )
    assert side == "Down"
    side, _, _, _ = signal_from_executable_edges(0.06, 0.055, 120, 0.50, 0.90, _params())
    assert side == "Up"


# ------------------------------------------------------------------ knobs


def test_rule_knob_defaults_match_the_old_constants() -> None:
    assert _knobs.KNOBS["paper_entry_edge_min"].default == _config.PAPER_ENTRY_EDGE_MIN
    assert _knobs.KNOBS["paper_entry_edge_max"].default == _config.PAPER_ENTRY_EDGE_MAX
    assert _knobs.KNOBS["paper_min_entry_price"].default == _config.PAPER_MIN_ENTRY_PRICE
    assert _knobs.KNOBS["paper_max_entry_price"].default == pytest.approx(0.95)
    assert (
        _knobs.KNOBS["paper_entry_min_remaining_seconds"].default
        == _config.PAPER_ENTRY_MIN_REMAINING_SECONDS
    )
    assert _knobs.KNOBS["paper_fee_aware"].default is False


def test_validate_rejects_out_of_range_without_writing() -> None:
    with pytest.raises(ValueError):
        _knobs.validate("paper_entry_edge_min", 0.5)
    with pytest.raises(ValueError):
        _knobs.validate("paper_max_entry_price", 1.2)
    assert _knobs.validate("paper_entry_min_remaining_seconds", "45") == 45


@pytest_asyncio.fixture
async def ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "paper_settings.db")
    await _db.init_db()
    for name, knob in _knobs.KNOBS.items():
        _knobs._cache[name] = knob.default
    yield _db
    for name, knob in _knobs.KNOBS.items():
        _knobs._cache[name] = knob.default


@pytest.mark.asyncio
async def test_strategy_params_follow_the_saved_rules(ledger) -> None:
    from polymarket_bot import paper

    await _knobs.set("paper_entry_edge_min", 0.05)
    await _knobs.set("paper_entry_edge_max", 0.09)
    await _knobs.set("paper_min_entry_price", 0.55)
    await _knobs.set("paper_max_entry_price", 0.85)
    await _knobs.set("paper_entry_min_remaining_seconds", 30)
    await _knobs.set("paper_fee_aware", True)
    await _knobs.refresh_cache()
    params = paper._strategy_params()
    assert params.entry_edge_min == pytest.approx(0.05)
    assert params.entry_edge_max == pytest.approx(0.09)
    assert params.min_entry_price == pytest.approx(0.55)
    assert params.max_entry_price == pytest.approx(0.85)
    assert params.entry_min_remaining_seconds == 30
    assert params.fee_rate == pytest.approx(DEFAULT_TAKER_FEE_RATE)

    for name in RULE_KNOBS:
        await _knobs.reset(name)
    await _knobs.refresh_cache()
    params = paper._strategy_params()
    assert params.entry_edge_min == pytest.approx(_config.PAPER_ENTRY_EDGE_MIN)
    assert params.fee_rate == 0.0


# ------------------------------------------------------------------ API


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient

    from polymarket_exec.ops.dashboard import app as app_module

    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "settings_api.db")
    with TestClient(app_module.app) as c:
        yield c
    for name, knob in _knobs.KNOBS.items():
        _knobs._cache[name] = knob.default


def test_settings_save_validates_and_persists(client) -> None:
    ok = client.post("/api/simple/settings", json={"changes": {
        "paper_entry_edge_min": 0.05, "paper_max_entry_price": 0.85,
        "paper_fee_aware": True, "trade_shares": 6,
    }})
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["status"] == "ok"
    values = body["settings"]["values"]
    assert values["paper_entry_edge_min"] == pytest.approx(0.05)
    assert values["paper_max_entry_price"] == pytest.approx(0.85)
    assert values["paper_fee_aware"] is True
    assert values["trade_shares"] == pytest.approx(6.0)

    data = client.get("/api/simple").json()
    assert data["rules"]["edge_min"] == pytest.approx(0.05)
    assert data["rules"]["fee_aware"] is True
    assert data["rules"]["trade_shares"] == pytest.approx(6.0)


def test_settings_rejects_crossed_pairs_and_writes_nothing(client) -> None:
    bad = client.post("/api/simple/settings", json={"changes": {
        "paper_entry_edge_min": 0.08, "paper_entry_edge_max": 0.06,
    }})
    assert bad.status_code == 400
    assert "minimum edge" in bad.json()["detail"]
    values = client.get("/api/simple").json()["settings"]["values"]
    assert values["paper_entry_edge_min"] == pytest.approx(_config.PAPER_ENTRY_EDGE_MIN)

    assert client.post("/api/simple/settings", json={"changes": {
        "paper_min_entry_price": 0.9, "paper_max_entry_price": 0.8,
    }}).status_code == 400
    assert client.post("/api/simple/settings", json={"changes": {
        "trade_shares": 2,
    }}).status_code == 400
    # Dollar sizing is refused on the simple screen: the RiskGate's $3 per-trade
    # cap would block every such trade (2026-11-18: $20-$100 blocked 238 entries).
    refused = client.post("/api/simple/settings", json={"changes": {"trade_shares": None}})
    assert refused.status_code == 400 and "shares" in refused.json()["detail"]
    assert client.post("/api/simple/settings", json={"changes": {
        "live_daily_loss_halt_usd": 5,
    }}).status_code == 400


def test_settings_refused_while_real_money_mode_is_selected(client) -> None:
    import asyncio

    asyncio.run(_db.set_config("polymarket_bot.requested_mode", "live"))
    try:
        refused = client.post("/api/simple/settings", json={"changes": {
            "paper_entry_edge_min": 0.05,
        }})
        assert refused.status_code == 409
        assert client.post("/api/simple/settings/reset").status_code == 409
    finally:
        asyncio.run(_db.set_config("polymarket_bot.requested_mode", "paper"))


def test_reset_restores_rule_defaults_but_keeps_trade_size(client) -> None:
    client.post("/api/simple/settings", json={"changes": {
        "paper_entry_edge_min": 0.06, "trade_shares": 7,
    }})
    reset = client.post("/api/simple/settings/reset")
    assert reset.status_code == 200
    values = reset.json()["settings"]["values"]
    assert values["paper_entry_edge_min"] == pytest.approx(_config.PAPER_ENTRY_EDGE_MIN)
    assert values["trade_shares"] == pytest.approx(7.0)
