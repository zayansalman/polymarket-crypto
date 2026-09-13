"""Unit tests for tools/live_detect_wallet.py .env merge (issues #33, #34).

The merge writes a real private key, so its behavior is safety-critical:
update keys in place, preserve everything else, and never write keys it
doesn't manage.
"""

from __future__ import annotations

import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[2] / "tools"
sys.path.insert(0, str(TOOLS))

from live_detect_wallet import _merge_env  # noqa: E402

_UPDATES = {
    "POLYMARKET_PRIVATE_KEY": "0xSECRET",
    "POLYMARKET_FUNDER": "0xFUND",
    "POLYMARKET_SIGNATURE_TYPE": "2",
    "TRADE_MAX_USD": "5",
    "PAPER_MIN_TRADE_USD": "5",
    "PAPER_MAX_TRADE_USD": "5",
}


def test_updates_existing_keys_in_place():
    out = _merge_env("POLYMARKET_SIGNATURE_TYPE=1\nPAPER_MIN_TRADE_USD=1\n", _UPDATES)
    assert "POLYMARKET_SIGNATURE_TYPE=2" in out
    assert "POLYMARKET_SIGNATURE_TYPE=1" not in out
    assert out.count("PAPER_MIN_TRADE_USD=") == 1
    assert "PAPER_MIN_TRADE_USD=5" in out


def test_preserves_comments_and_unmanaged_keys():
    out = _merge_env("# header\nDB_PATH=./data/x.db\n", _UPDATES)
    assert "# header" in out
    assert "DB_PATH=./data/x.db" in out


def test_appends_new_managed_keys():
    out = _merge_env("DB_PATH=./x\n", _UPDATES)
    assert "POLYMARKET_PRIVATE_KEY=0xSECRET" in out
    assert "POLYMARKET_FUNDER=0xFUND" in out


def test_only_writes_managed_keys():
    # Unmanaged keys in updates are ignored — the script never writes
    # arbitrary settings into .env.
    out = _merge_env("", {**_UPDATES, "SOME_OTHER_KEY": "x"})
    assert "SOME_OTHER_KEY" not in out


def test_idempotent_on_already_live_env():
    once = _merge_env("BOT_MODE=paper\n", _UPDATES)
    twice = _merge_env(once, _UPDATES)
    assert once == twice
