"""Provision a local test environment and run deterministic BTC paper trials."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENV = ROOT / ".venv"
VENV_PYTHON = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
TEST_FILES = [
    "tests/unit/test_settle_style.py",
    "tests/unit/test_live_wiring.py",
    "tests/unit/test_data_integrity.py",
    "tests/unit/test_mode_switch.py",
    "tests/unit/test_runtime_config.py",
    "tests/unit/test_offline_replay.py",
    "tests/unit/test_main_singleton.py",
    "tests/unit/test_windows_paper_startup.py",
    "tests/unit/test_simple_view.py",
    "tests/unit/test_paper_settings.py",
    "tests/unit/test_learning.py",
    "tests/unit/test_strategy_lab.py",
    "tests/unit/test_paper_wallet.py",
    "tests/unit/test_paper_wallet_rescale.py",
]


def _test_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for name in list(environment):
        if (
            name in {"GH_TOKEN", "GH_HOST", "GITHUB_TOKEN"}
            or name.startswith("COPILOT_GH_ACCOUNT_")
            or name.startswith("POLYMARKET_")
        ):
            environment.pop(name)
    environment["BOT_MODE"] = "paper"
    return environment


def _run(command: list[str], environment: dict[str, str]) -> None:
    subprocess.run(command, cwd=ROOT, env=environment, check=True)


def main() -> None:
    environment = _test_environment()
    if not VENV_PYTHON.is_file():
        if sys.version_info < (3, 11):
            raise SystemExit("Python 3.11 or newer is required to create the test environment.")
        _run([sys.executable, "-m", "venv", str(VENV)], environment)

    _run(
        [
            str(VENV_PYTHON),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--quiet",
            "-e",
            ".[test]",
        ],
        environment,
    )
    _run(
        [str(VENV_PYTHON), "-m", "pytest", *TEST_FILES, "-q"],
        environment,
    )


if __name__ == "__main__":
    main()
