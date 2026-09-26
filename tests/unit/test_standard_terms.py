"""The operator's standard terms, across the whole tree.

An order split across several prices is a "scaled passive limit order": a parent order split
into child orders resting at price levels, with the depth ahead for its place in the queue. The
coined words for these are ruled out everywhere: code, tables, settings, the dashboard and docs.
This test reads every file git tracks (and new files not yet added) and fails on any of them.
The words are spelt in pieces here so this file does not match itself.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
COINED = re.compile(
    r"(?<![a-z])(" + "|".join(("lad" + "der", "ru" + "ng", "layer" + "ing")) + ")",
    re.IGNORECASE,
)
MAX_BYTES = 5_000_000


def _files() -> list[Path]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=ROOT, capture_output=True, check=True, timeout=60,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        pytest.skip("git is not available to list the tree's files")
    return [ROOT / name for name in out.decode().split("\0") if name]


def test_no_coined_order_words_anywhere_in_the_tree() -> None:
    found = []
    for path in _files():
        rel = path.relative_to(ROOT).as_posix()
        if COINED.search(rel):
            found.append(f"{rel}: in the file name")
        if not path.is_file() or path.stat().st_size > MAX_BYTES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # not a text file
        for number, line in enumerate(text.splitlines(), 1):
            if COINED.search(line):
                found.append(f"{rel}:{number}: {line.strip()[:120]}")
    assert not found, "coined words (use standard terms):\n" + "\n".join(found)
