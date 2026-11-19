"""The app restarts itself after an update, only in paper mode with no open trade."""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from polymarket_exec.ops.dashboard import self_restart as sr


def test_only_the_windows_launcher_turns_it_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PAPER_SELF_RESTART", raising=False)
    assert sr.enabled() is False
    monkeypatch.setenv("PAPER_SELF_RESTART", "1")
    assert sr.enabled() is (os.name == "nt")


def test_changes_are_found_and_broken_files_are_held_back(tmp_path: Path) -> None:
    pkg = tmp_path / "polymarket_bot"
    pkg.mkdir()
    good = pkg / "good.py"
    good.write_text("x = 1\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("y = 1\n")
    before = sr.snapshot(tmp_path)
    assert list(before) == [str(good)]  # tests are not watched

    later = time.time() + 5
    good.write_text("x = 2\n")
    os.utime(good, (later, later))
    bad = pkg / "bad.py"
    bad.write_text("def broken(:\n")
    now = sr.snapshot(tmp_path)
    changed = sr.changed_files(before, now)
    assert changed == sorted([str(good), str(bad)])
    errors = sr.compile_errors(changed)
    assert len(errors) == 1 and errors[0].startswith("bad.py")


def test_launcher_turns_on_self_restart_and_keeps_paper_only() -> None:
    import importlib.util

    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "startup_for_test", root / "tools" / "windows_paper_startup.py")
    startup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(startup)
    env = startup._sanitized_environment()
    assert env["PAPER_SELF_RESTART"] == "1" and env["BOT_MODE"] == "paper"
    assert env["POLYMARKET_PRIVATE_KEY"] == ""
