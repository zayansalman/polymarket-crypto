"""Cross-platform singleton-lock behavior for the dashboard process."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import main


def test_singleton_lock_refuses_a_second_process(tmp_path: Path, monkeypatch) -> None:
    lock_db = tmp_path / "paper.db"
    monkeypatch.setattr(main, "DB_PATH", lock_db)
    lock = main._acquire_singleton_lock()
    child_script = (
        "import sys; from pathlib import Path; import main; "
        "main.DB_PATH = Path(sys.argv[1]); main._acquire_singleton_lock()"
    )

    result = subprocess.run(
        [sys.executable, "-c", child_script, str(lock_db)],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        check=False,
    )
    lock.close()

    assert result.returncode == 1
    assert "another bot instance already holds" in result.stdout
