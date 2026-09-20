"""The paper record must survive the file it lives in being cleared."""

from __future__ import annotations

import sqlite3

import pytest

import config as _config
from polymarket_bot.copytrade import backup as _backup


@pytest.fixture
def live_db(tmp_path, monkeypatch):
    path = tmp_path / "live.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE copy_trades (id INTEGER PRIMARY KEY, pnl REAL)")
    con.execute("CREATE TABLE copy_decisions (id INTEGER PRIMARY KEY, reason TEXT)")
    con.execute("INSERT INTO copy_trades VALUES (1, 4.5)")
    con.execute("INSERT INTO copy_decisions VALUES (1, 'skipped')")
    con.commit()
    con.close()
    monkeypatch.setattr(_config, "DB_PATH", path)
    return path


def test_a_snapshot_stands_alone_without_the_app(live_db):
    out = _backup.write_snapshot(now=1_700_000_000)
    assert out is not None and out.exists()
    con = sqlite3.connect(out)
    assert con.execute("SELECT pnl FROM copy_trades").fetchone()[0] == 4.5
    assert con.execute("SELECT COUNT(*) FROM copy_decisions").fetchone()[0] == 1
    con.close()


def test_the_record_survives_the_live_table_being_cleared(live_db):
    """Exactly the failure that happened: a DELETE against the real database."""
    _backup.write_snapshot(now=1_700_000_000)
    con = sqlite3.connect(live_db)
    con.execute("DELETE FROM copy_trades")
    con.commit()
    assert con.execute("SELECT COUNT(*) FROM copy_trades").fetchone()[0] == 0
    con.close()

    recovered = sqlite3.connect(_backup.latest())
    assert recovered.execute("SELECT COUNT(*) FROM copy_trades").fetchone()[0] == 1
    recovered.close()


def test_snapshots_are_pruned(live_db):
    for i in range(6):
        _backup.write_snapshot(now=1_700_000_000 + i * 3600)
    _backup._prune(_backup.snapshot_dir(), keep=3)
    assert len(list(_backup.snapshot_dir().glob("copytrade-*.db"))) == 3


def test_an_empty_ledger_writes_no_snapshot(tmp_path, monkeypatch):
    path = tmp_path / "empty.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE copy_trades (id INTEGER PRIMARY KEY)")
    con.commit()
    con.close()
    monkeypatch.setattr(_config, "DB_PATH", path)
    assert _backup.write_snapshot(now=1_700_000_000) is None
