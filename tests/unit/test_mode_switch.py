"""The PAPER/LIVE mode switch is never blocked out.

LIVE is always selectable from the dashboard. The live boot gate still guards
real orders — at Start and at executor build — so an unarmed LIVE selection
persists, explains why it isn't armed, and refuses to start.

Each test runs against its own throwaway SQLite so the real journal is untouched.
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import config as _config
import db as _db
from polymarket_bot import controller


@pytest.fixture
def unarmed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Live credentials absent — the boot gate refuses."""
    monkeypatch.setattr(_config, "POLYMARKET_PRIVATE_KEY", "")
    monkeypatch.setattr(_config, "LIVE_CONFIRM", "")
    monkeypatch.setattr(_config, "CONFIG_PARSE_ERRORS", [], raising=False)


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "test_mode_switch.db")
    from polymarket_exec.ops.dashboard.app import app

    with TestClient(app) as c:
        yield c


def _live_button(html: str) -> str:
    start = html.index('class="mode-opt live')
    return html[html.rfind("<button", 0, start): html.index("</button>", start)]


def test_live_button_never_disabled_when_unarmed(client: TestClient, unarmed: None) -> None:
    button = _live_button(client.get("/").text)
    assert "disabled" not in button
    assert "not armed" in button  # the tooltip still says why Start will refuse


def test_css_has_no_disabled_mode_style(client: TestClient) -> None:
    assert ".mode-opt:disabled" not in client.get("/static/style.css").text


def test_switch_to_live_succeeds_when_unarmed(client: TestClient, unarmed: None) -> None:
    r = client.post("/api/mode", json={"mode": "live"}).json()
    assert r["status"] != "error"
    assert r["mode"] == "live"
    assert "not armed" in r["detail"]
    assert 'class="mode-opt live active' in client.get("/").text


def test_unarmed_live_start_refuses_and_runs_nothing(
    client: TestClient, unarmed: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawned: list[bool] = []
    monkeypatch.setattr(controller, "_ensure_runner_started", lambda force=False: spawned.append(force))
    monkeypatch.setattr(controller, "_ensure_watchdog_started", lambda: None)

    client.post("/api/mode", json={"mode": "live"})
    r = client.post("/api/start").json()

    assert spawned == []
    assert r["status"] == "stopped"
    assert "REFUSED" in r["detail"]


def test_switch_back_to_paper(client: TestClient, unarmed: None) -> None:
    client.post("/api/mode", json={"mode": "live"})
    r = client.post("/api/mode", json={"mode": "paper"}).json()
    assert r["status"] != "error"
    assert 'class="mode-opt paper active' in client.get("/").text
