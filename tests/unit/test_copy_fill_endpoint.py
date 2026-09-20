"""POST /api/copy-fill — the Copy button on the COPY TRADE WALLETS card.

Books ONE of a target's observed fills by hand, through exactly the path
autocopy takes. Each test runs against its own throwaway SQLite so the real
journal is untouched.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import db as _db
from polymarket_bot.copytrade import targets as _targets
from polymarket_bot.copytrade import watcher as _watcher


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "copy_fill.db")
    from polymarket_exec.ops.dashboard.app import app

    with TestClient(app) as c:
        yield c


def _fill(**kw):
    base = dict(
        tx="0xabc", ts=int(time.time()) - 30, wallet=_targets.DEFAULT_TARGET,
        side="BUY", outcome="Up", size=40.0, price=0.54, title="t",
        slug="bitcoin-up-or-down-september-21-3pm-et",
        condition_id="0xc", token_id="1", followed=True,
    )
    base.update(kw)
    return _watcher.ObservedFill(**base)


def _seed(monkeypatch: pytest.MonkeyPatch, fills: list) -> _watcher.CopyWatcher:
    w = _watcher.CopyWatcher()
    w.state.fills = fills
    monkeypatch.setattr(_watcher, "current", lambda: w)
    return w


def test_a_tx_we_are_watching_is_copied_through_the_autocopy_path(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list = []

    async def _consider(_client, fill, address):
        seen.append((fill.tx, address))
        return True

    _seed(monkeypatch, [_fill()])
    from polymarket_bot.copytrade import trader as _trader

    monkeypatch.setattr(_trader, "consider", _consider)

    r = client.post("/api/copy-fill", json={"tx": "0xabc"})
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    # Same function autocopy calls, and against the wallet that made the fill —
    # a hand-picked copy must be the same kind of ledger row as an automatic
    # one, or the two cannot be compared.
    assert seen == [("0xabc", _targets.DEFAULT_TARGET)]


def test_a_declined_copy_reports_the_reason_rather_than_just_failing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _consider(_client, _fill, _address):
        return False

    _seed(monkeypatch, [_fill()])
    from polymarket_bot.copytrade import ledger as _ledger
    from polymarket_bot.copytrade import trader as _trader

    monkeypatch.setattr(_trader, "consider", _consider)

    async def _decisions(limit: int = 100):
        return [{"tx": "0xabc", "reason": "book moved 9c against us"}]

    monkeypatch.setattr(_ledger, "decisions", _decisions)

    r = client.post("/api/copy-fill", json={"tx": "0xabc"})
    assert r.json() == {"status": "error", "detail": "book moved 9c against us"}


def test_an_unknown_tx_says_the_fill_has_aged_out(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(monkeypatch, [_fill()])
    r = client.post("/api/copy-fill", json={"tx": "0xnope"})
    body = r.json()
    assert body["status"] == "error"
    assert "no longer in the watch window" in body["detail"]


def test_their_exit_is_refused(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(monkeypatch, [_fill(side="SELL")])
    body = client.post("/api/copy-fill", json={"tx": "0xabc"}).json()
    assert body["status"] == "error"
    assert "exit, not an entry" in body["detail"]


def test_a_missing_tx_is_refused(client: TestClient) -> None:
    assert client.post("/api/copy-fill", json={}).json() == {
        "status": "error", "detail": "tx required"
    }


def test_no_watcher_running_is_said_plainly(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_watcher, "current", lambda: None)
    body = client.post("/api/copy-fill", json={"tx": "0xabc"}).json()
    assert body["status"] == "error"
    assert "watcher is not running" in body["detail"]


def test_a_raising_trader_surfaces_the_error_instead_of_500ing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _boom(_client, _fill, _address):
        raise RuntimeError("book unavailable")

    _seed(monkeypatch, [_fill()])
    from polymarket_bot.copytrade import trader as _trader

    monkeypatch.setattr(_trader, "consider", _boom)

    r = client.post("/api/copy-fill", json={"tx": "0xabc"})
    assert r.status_code == 200
    assert r.json()["detail"] == "RuntimeError: book unavailable"
