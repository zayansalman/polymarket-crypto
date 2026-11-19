"""Safety checks for the local Windows paper-startup flow."""

from __future__ import annotations

import importlib
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tools import windows_paper_startup as startup

dashboard = importlib.import_module("polymarket_exec.ops.dashboard.app")


@pytest.mark.asyncio
async def test_paper_autostart_is_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BTC_PAPER_AUTOSTART", raising=False)
    set_mode = AsyncMock()
    request_start = AsyncMock()
    monkeypatch.setattr(dashboard, "set_mode", set_mode)
    monkeypatch.setattr(dashboard, "request_start", request_start)

    await dashboard._paper_autostart_if_requested()

    set_mode.assert_not_awaited()
    request_start.assert_not_awaited()


@pytest.mark.asyncio
async def test_paper_autostart_forces_paper_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BTC_PAPER_AUTOSTART", "1")
    monkeypatch.setattr(dashboard, "BOT_MODE", "paper")
    monkeypatch.setattr(dashboard, "_BTC_BOT_AVAILABLE", True)
    set_mode = AsyncMock()
    request_start = AsyncMock(
        return_value=SimpleNamespace(state="running", mode="paper", detail="paper started")
    )
    monkeypatch.setattr(dashboard, "set_mode", set_mode)
    monkeypatch.setattr(dashboard, "request_start", request_start)

    await dashboard._paper_autostart_if_requested()

    set_mode.assert_awaited_once_with("paper")
    request_start.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_paper_autostart_rejects_live_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BTC_PAPER_AUTOSTART", "1")
    monkeypatch.setattr(dashboard, "BOT_MODE", "live")
    set_mode = AsyncMock()
    monkeypatch.setattr(dashboard, "set_mode", set_mode)

    with pytest.raises(RuntimeError, match="requires BOT_MODE=paper"):
        await dashboard._paper_autostart_if_requested()

    set_mode.assert_not_awaited()


def test_task_environment_is_paper_only_and_drops_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "test-token")
    monkeypatch.setenv("COPILOT_GH_ACCOUNT_example", "test-account-token")
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "test-private-key")

    env = startup._sanitized_environment()

    assert env["BOT_MODE"] == "paper"
    assert env["BTC_PAPER_AUTOSTART"] == "1"
    assert env["PYTHON_DOTENV_DISABLED"] == "1"
    assert env["POLYMARKET_PRIVATE_KEY"] == ""
    assert "GH_TOKEN" not in env
    assert "COPILOT_GH_ACCOUNT_example" not in env


def test_desktop_shortcut_is_idempotent_and_targets_paper_launcher(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(startup.os, "name", "nt")
    root = tmp_path / "Polymarket's repository"
    python = root / ".venv" / "Scripts" / "python.exe"
    monkeypatch.setattr(
        startup,
        "_python_paths",
        lambda _root: (python, root / ".venv" / "Scripts" / "pythonw.exe"),
    )
    calls: list[tuple[list[str], dict[str, str]]] = []

    def fake_run(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs["env"]))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=r"C:\Users\user\Desktop\Start Polymarket Paper Trading.lnk",
            stderr="",
        )

    monkeypatch.setattr(startup.subprocess, "run", fake_run)

    first = startup._create_desktop_shortcut(root)
    second = startup._create_desktop_shortcut(root)

    assert first == second
    assert len(calls) == 2
    script = calls[0][0][-1]
    assert "CreateShortcut($shortcutPath)" in script
    expected_python = str(python).replace("'", "''")
    expected_script = str(root / "tools" / "windows_paper_startup.py").replace(
        "'", "''"
    )
    assert f"$shortcut.TargetPath = '{expected_python}'" in script
    assert (
        f"$shortcut.Arguments = '\"{expected_script}\" run'" in script
    )
    assert "BOT_MODE" in calls[0][1]
    assert calls[0][1]["BOT_MODE"] == "paper"
    assert calls[0][1]["POLYMARKET_PRIVATE_KEY"] == ""


def test_starting_an_already_running_paper_dashboard_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(startup, "_is_our_dashboard", lambda: True)

    def unexpected_start(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("an already-running paper loop must not be started twice")

    monkeypatch.setattr(startup.urllib.request, "urlopen", unexpected_start)

    startup._start_existing_dashboard(
        {"runtime": {"mode": "paper", "state": "running"}}
    )


def test_copilot_worktree_is_not_a_durable_install() -> None:
    assert startup._is_copilot_worktree(
        Path(r"C:\Users\user\.copilot\repos\copilot-worktrees\repo\worktree")
    )
    assert not startup._is_copilot_worktree(
        Path(r"C:\Users\user\AppData\Local\PolymarketCrypto\repository")
    )


def test_current_btc_contract_requires_a_recent_window() -> None:
    now = 1_800_000_123
    current_start = now - now % 300

    assert startup._has_current_btc_contract(
        {"execution_view": f"btc-updown-5m-{current_start}"}, now
    )
    assert startup._has_current_btc_contract(
        {"execution_view": f"btc-updown-15m-{now - now % 900}"}, now
    )
    assert startup._has_current_btc_contract(
        {
            "execution_view": f"<span class='win'>#{current_start} · 120s</span>",
            "market_selector": (
                "<button class='mkt-btn active' data-timeframe='5m'>5m</button>"
            ),
        },
        now,
    )
    assert not startup._has_current_btc_contract(
        {"execution_view": f"#{current_start}"}, now
    )
    assert not startup._has_current_btc_contract(
        {"execution_view": "btc-updown-5m-1700000000"}, now
    )


def test_dashboard_log_matches_only_its_install_and_listener_pid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data_root = tmp_path / "data"
    log_root = tmp_path / "logs"
    log_root.mkdir()
    monkeypatch.setattr(startup, "DATA_ROOT", data_root)
    monkeypatch.setattr(startup, "LOG_ROOT", log_root)
    log_path = log_root / "dashboard.log"
    log_path.write_text(
        ("old log entry\n" * 10000)
        + f"app.boot db_path={data_root / 'btc_5m_binary_fair_value.db'}\n"
        + "INFO: Started server process [12345]\n",
        encoding="utf-8",
    )

    assert startup._dashboard_log_matches_process(12345)
    assert not startup._dashboard_log_matches_process(12346)


def test_published_branch_remote_resolves_pr_fork(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        startup,
        "_git",
        lambda *_args, **_kwargs: "https://github.com/zayansalman/polymarket-crypto.git",
    )
    monkeypatch.setattr(
        startup,
        "_public_pr_head_repository",
        lambda *_args, **_kwargs: "holdenluke-5/polymarket-crypto",
    )

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(startup, "_run", fake_run)

    assert startup._published_branch_remote(tmp_path, "feature") == (
        "https://github.com/holdenluke-5/polymarket-crypto.git"
    )
