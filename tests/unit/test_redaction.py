"""Secret-redaction at output sinks (issue #35, key-leak audit).

The private key must never reach a log line, the SQLite journal/feed, or the
dashboard detail — even via an exception message. Redaction is by EXACT value
so it can never mangle legitimate 0x+64hex order ids / condition ids / hashes.
"""

from __future__ import annotations

import pytest

from logging_setup import _redact_processor, redact_secrets

FAKE_KEY = "0x" + "a" * 64
OTHER_HASH = "0x" + "b" * 64  # a legit order id / tx hash — must survive


@pytest.fixture
def key_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", FAKE_KEY)
    monkeypatch.delenv("POLYMARKET_SECRET", raising=False)
    monkeypatch.delenv("POLYMARKET_PASSPHRASE", raising=False)


def test_redacts_exact_key(key_env):
    assert FAKE_KEY not in redact_secrets(f"boom with key {FAKE_KEY} inside")
    assert "redacted" in redact_secrets(FAKE_KEY)


def test_redacts_key_without_0x_prefix(key_env):
    # eth libraries sometimes echo the key without the 0x prefix.
    bare = "a" * 64
    assert bare not in redact_secrets(f"ValueError: invalid key {bare}")


def test_preserves_legitimate_hashes(key_env):
    # An order id / condition id / tx hash is 0x+64hex too — must NOT be touched.
    msg = f"order {OTHER_HASH} posted"
    assert redact_secrets(msg) == msg
    assert OTHER_HASH in redact_secrets(msg)


def test_noop_without_secret(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("POLYMARKET_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("POLYMARKET_SECRET", raising=False)
    monkeypatch.delenv("POLYMARKET_PASSPHRASE", raising=False)
    s = f"nothing secret here {OTHER_HASH}"
    assert redact_secrets(s) == s


def test_redacts_api_secret_and_passphrase(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("POLYMARKET_SECRET", "t-8_yG53kcWg55_bCyM1IlAdHZVJjEGiDpHBRh6h8Us=")
    monkeypatch.setenv("POLYMARKET_PASSPHRASE", "e203be163cd9d7e55b30a9fc953141ef8e53027674d5")
    out = redact_secrets("creds secret=t-8_yG53kcWg55_bCyM1IlAdHZVJjEGiDpHBRh6h8Us= done")
    assert "t-8_yG53" not in out


def test_processor_scrubs_event_fields(key_env):
    event = {"event": "boot_failed", "error": f"key={FAKE_KEY}", "module": "live"}
    out = _redact_processor(None, "error", event)
    assert FAKE_KEY not in out["error"]
    assert out["event"] == "boot_failed"


def test_non_string_values_pass_through(key_env):
    assert redact_secrets(42) == 42
    assert redact_secrets(None) is None
    assert redact_secrets(0.5) == 0.5
