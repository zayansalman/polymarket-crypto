"""EMS ribbon wallet size (cash + open positions) — no network in tests."""
from __future__ import annotations

import pytest

import config as _config
from polymarket_exec.ops.dashboard.panels import _wallet, ribbon

_FUNDER = "0x" + "c1" * 20


def _ribbon(
    wallet: dict | None = None,
    *,
    paper_wallet: dict | None = None,
    mode: str = "live",
) -> str:
    return ribbon.render(
        mode=mode, state="stopped", session_start=None,
        live_pnl=0.0, paper_pnl=0.0, day_pnl=0.0,
        open_pos=[], closed_session=[], tick=None, last_live_at=None,
        wallet=wallet,
        paper_wallet=paper_wallet,
    )


def test_ribbon_shows_wallet_total_cash_and_positions() -> None:
    html = _ribbon({"cash": 8.57, "positions": 1.5, "total": 10.07, "stale": False})
    assert "Wallet" in html and "$10.07" in html
    assert "cash $8.57 · pos $1.50" in html
    assert "stale" not in html


def test_ribbon_wallet_placeholder_without_wallet() -> None:
    assert "no wallet" in _ribbon(None)


def test_paper_ribbon_shows_virtual_equity_and_cash() -> None:
    html = _ribbon(
        paper_wallet={
            "initial": 100.0,
            "cash": 96.0,
            "positions": 5.0,
            "equity": 101.0,
            "mark_source": "cost_basis",
        },
        mode="paper",
    )
    assert "Paper Equity" in html and "$101.00" in html
    assert "cash $96.00 · pos $5.00 · virtual · cost-basis mark" in html
    assert "Wallet" not in html


@pytest.fixture
def fresh_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_wallet, "_cache", {"funder": None, "at": 0.0, "snapshot": None})


@pytest.mark.asyncio
async def test_snapshot_none_without_funder(fresh_cache: None) -> None:
    assert await _wallet.wallet_snapshot() is None


@pytest.mark.asyncio
async def test_snapshot_sums_cash_and_positions_and_caches(
    monkeypatch: pytest.MonkeyPatch, fresh_cache: None
) -> None:
    monkeypatch.setattr(_config, "POLYMARKET_FUNDER", _FUNDER)
    calls: list[str] = []

    async def _cash(_client: object, funder: str) -> float:
        calls.append(funder)
        return 8.571918

    async def _positions(_client: object, funder: str) -> float:
        return 2.0

    monkeypatch.setattr(_wallet, "_cash", _cash)
    monkeypatch.setattr(_wallet, "_positions", _positions)

    snap = await _wallet.wallet_snapshot()
    assert snap == {"cash": 8.571918, "positions": 2.0, "total": 10.571918, "stale": False}
    await _wallet.wallet_snapshot()
    assert calls == [_FUNDER]  # second read served from cache


@pytest.mark.asyncio
async def test_failed_refresh_keeps_last_value_marked_stale(
    monkeypatch: pytest.MonkeyPatch, fresh_cache: None
) -> None:
    monkeypatch.setattr(_config, "POLYMARKET_FUNDER", _FUNDER)
    monkeypatch.setattr(_wallet, "_cache", {
        "funder": _FUNDER, "at": -1e9,
        "snapshot": {"cash": 5.0, "positions": 0.0, "total": 5.0, "stale": False},
    })

    async def _boom(*_a: object) -> float:
        raise RuntimeError("rpc down")

    monkeypatch.setattr(_wallet, "_cash", _boom)
    snap = await _wallet.wallet_snapshot()
    assert snap is not None and snap["total"] == 5.0 and snap["stale"] is True
