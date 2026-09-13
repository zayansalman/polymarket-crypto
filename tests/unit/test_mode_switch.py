"""The PAPER/LIVE mode switch is never blocked out.

LIVE is always selectable from the dashboard. The live boot gate still guards
real orders — at Start and at executor build — so an unarmed LIVE selection
persists, reports that it isn't armed, and refuses to start. Stopping or
switching never paper-closes a LIVE ledger row.

Each test runs against its own throwaway SQLite so the real journal is untouched.
"""
from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import config as _config
import db as _db
from polymarket_bot import controller, paper
from polymarket_exec.execution.live import CONFIRM_PHRASE


@pytest.fixture
def unarmed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Live credentials absent — the boot gate refuses."""
    monkeypatch.setattr(_config, "POLYMARKET_PRIVATE_KEY", "")
    monkeypatch.setattr(_config, "LIVE_CONFIRM", "")
    monkeypatch.setattr(_config, "CONFIG_PARSE_ERRORS", [], raising=False)


@pytest.fixture
def armed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every boot-gate input satisfied (dummy key — nothing here signs)."""
    monkeypatch.setattr(_config, "POLYMARKET_PRIVATE_KEY", "0x" + "11" * 32)
    monkeypatch.setattr(_config, "LIVE_CONFIRM", CONFIRM_PHRASE)
    monkeypatch.setattr(_config, "POLYMARKET_SIGNATURE_TYPE", 0)
    monkeypatch.setattr(_config, "CONFIG_PARSE_ERRORS", [], raising=False)


@pytest.fixture
def isolated_controller(monkeypatch: pytest.MonkeyPatch) -> None:
    """Restore controller/paper globals the stop/start paths mutate."""
    monkeypatch.setattr(controller, "_runner_thread", None)
    monkeypatch.setattr(controller, "_stop_event", None)
    monkeypatch.setattr(controller, "_desired_running", False)
    monkeypatch.setattr(controller, "_mode_cache", "paper")
    monkeypatch.setattr(paper, "_live_executor", None)


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
    assert 'data-armed="0"' in button
    assert "not armed" in button  # the tooltip still says why Start will refuse


def test_live_button_marked_armed_when_gate_passes(client: TestClient, armed: None) -> None:
    button = _live_button(client.get("/").text)
    assert "disabled" not in button
    assert 'data-armed="1"' in button
    assert "not armed" not in button


def test_css_has_no_disabled_mode_style(client: TestClient) -> None:
    assert ".mode-opt:disabled" not in client.get("/static/style.css").text


def test_switch_to_live_succeeds_when_unarmed(client: TestClient, unarmed: None) -> None:
    r = client.post("/api/mode", json={"mode": "live"}).json()
    assert r["status"] != "error"
    assert r["mode"] == "live"
    assert r["live_armed"] is False
    assert "not armed" in r["live_hint"]
    assert "not armed" not in r["detail"]  # armed-ness is never persisted (can go stale)
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
    assert client.post("/api/mode", json={"mode": "live"}).json()["mode"] == "live"
    assert 'class="mode-opt live active' in client.get("/").text
    r = client.post("/api/mode", json={"mode": "paper"}).json()
    assert r["status"] != "error"
    assert r["mode"] == "paper"
    assert 'class="mode-opt paper active' in client.get("/").text


@pytest.mark.asyncio
async def test_run_paper_loop_uses_mode_passed_by_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, unarmed: None, isolated_controller: None
) -> None:
    """The runner uses the mode Start decided on, not a re-read of the selector."""
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "loop_mode.db")
    await _db.init_db()
    await _db.set_config("polymarket_bot.requested_mode", "paper")

    await paper.run_paper_loop(threading.Event(), mode="live")

    assert paper._live_executor is None
    assert "LIVE mode refused" in (await _db.get_config("polymarket_bot.detail"))


async def _db_with_rows(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, modes: tuple[str, ...]) -> None:
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "rows.db")
    await _db.init_db()
    async with _db.connect() as conn:
        for mode in modes:
            await conn.execute(
                "INSERT INTO paper_positions (opened_at, window_slug, side, state, "
                "entry_price, notional_usd, shares, mode) VALUES (?, ?, ?, 'open', 0.5, 1, 2, ?)",
                ("2026-09-13T00:00:00+00:00", "btc-updown-5m-1", "Up", mode),
            )
        await conn.commit()


def _stub_closes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    @asynccontextmanager
    async def _client():
        yield None

    async def _snapshot(_client: object) -> None:
        return None

    closed: list[str] = []

    async def _close(pos: dict, *_a: object, **_k: object) -> bool:
        closed.append(pos["mode"])
        return True

    monkeypatch.setattr(paper, "_make_settlement_client", _client)
    monkeypatch.setattr(paper, "_build_snapshot", _snapshot)
    monkeypatch.setattr(paper, "_current_price_for_side", lambda *_a: None)
    monkeypatch.setattr(paper, "_close_position", _close)
    return closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("executor", "expected"),
    [(None, ["paper"]), (object(), ["paper", "live"])],
    ids=["no-executor-skips-live", "executor-flattens-live"],
)
async def test_force_close_touches_live_rows_only_with_executor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, isolated_controller: None,
    executor: object | None, expected: list[str],
) -> None:
    await _db_with_rows(monkeypatch, tmp_path, ("paper", "live"))
    closed = _stub_closes(monkeypatch)
    monkeypatch.setattr(paper, "_live_executor", executor)

    assert await paper.force_close_open_positions() == len(expected)
    assert closed == expected


@pytest.mark.asyncio
async def test_stop_reports_live_rows_left_open(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, isolated_controller: None
) -> None:
    """No runner (e.g. after a restart) → paper rows close, LIVE rows stay open and are flagged."""
    await _db_with_rows(monkeypatch, tmp_path, ("paper", "live"))
    closed = _stub_closes(monkeypatch)
    notes: list[str] = []

    async def _notify(kind: str, *_a: object, **_k: object) -> None:
        notes.append(kind)

    monkeypatch.setattr(controller, "notify", _notify)
    status = await controller.request_stop()

    assert closed == ["paper"]
    assert "1 LIVE position(s) remain OPEN" in status.detail
    assert "live_positions_left_open" in notes


class _StuckThread:
    """A runner that ignores the stop event (e.g. a hung live flatten)."""

    name = "stuck"

    def is_alive(self) -> bool:
        return True

    def join(self, _timeout: float | None = None) -> None:
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize("env_mode", ["paper", "live"])
async def test_stop_never_touches_ledger_while_runner_alive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, isolated_controller: None, env_mode: str
) -> None:
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "stuck.db")
    await _db.init_db()
    monkeypatch.setattr(_config, "BOT_MODE", env_mode)
    monkeypatch.setattr(controller, "_runner_thread", _StuckThread())
    calls: list[bool] = []

    async def _fake_close() -> tuple[int, None]:
        calls.append(True)
        return 0, None

    monkeypatch.setattr(controller, "_safe_force_close", _fake_close)
    status = await controller.request_stop()

    assert calls == []
    assert "Do NOT restart" in status.detail


@pytest.mark.asyncio
async def test_start_refused_while_previous_runner_shutting_down(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, isolated_controller: None
) -> None:
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "shutting.db")
    await _db.init_db()
    stop = threading.Event()
    stop.set()
    monkeypatch.setattr(controller, "_runner_thread", _StuckThread())
    monkeypatch.setattr(controller, "_stop_event", stop)
    monkeypatch.setattr(controller, "_mode_cache", "live")
    spawned: list[bool] = []
    monkeypatch.setattr(controller, "_ensure_runner_started", lambda force=False: spawned.append(force))

    status = await controller.request_start()

    assert spawned == []
    assert status.state == "stopped"
    assert "still shutting down" in status.detail
    assert controller._mode_cache == "live"  # untouched under the old runner
    assert controller._desired_running is False


@pytest.mark.asyncio
async def test_mode_switch_keeps_do_not_restart_warning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, isolated_controller: None
) -> None:
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "switch_stuck.db")
    await _db.init_db()
    monkeypatch.setattr(controller, "_runner_thread", _StuckThread())

    status = await controller.set_mode("paper")

    assert "Do NOT restart" in status.detail
