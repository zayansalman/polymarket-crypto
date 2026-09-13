"""Legacy BTC_-prefixed env names are flagged, never silently honoured.

The env knobs lost their ``BTC_`` prefix; an operator still carrying e.g.
``BTC_BOT_MODE`` or ``BTC_LIVE_MAX_TRADE_USD`` gets the default instead.
config.py collects a non-fatal CONFIG_WARNINGS entry for each such key (logged
once at boot by main.py) and deliberately does NOT carry the value over.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import config as _config

REPO = Path(_config.__file__).resolve().parent


def test_flags_prefixed_keys_with_a_read_equivalent() -> None:
    environ = {
        "BTC_BOT_MODE": "live",
        "BTC_EXIT_STYLE": "scalp",
        "BTC_PAPER_MAX_TRADE_USD": "5",
    }
    read = {"BOT_MODE", "EXIT_STYLE", "PAPER_MAX_TRADE_USD"}

    warnings = _config._legacy_env_warnings(environ, read)

    assert len(warnings) == 3
    joined = "\n".join(warnings)
    for old, new in [
        ("BTC_BOT_MODE", "BOT_MODE"),
        ("BTC_EXIT_STYLE", "EXIT_STYLE"),
        ("BTC_PAPER_MAX_TRADE_USD", "PAPER_MAX_TRADE_USD"),
    ]:
        assert any(w.startswith(f"{old} ") and f" {new}" in w for w in warnings), joined


def test_flags_renamed_live_risk_limits() -> None:
    # BTC_LIVE_* risk limits became TRADE_* — not a plain prefix strip.
    environ = {
        "BTC_LIVE_MAX_TRADE_USD": "1",
        "BTC_LIVE_DAILY_LOSS_HALT_USD": "2",
        "BTC_LIVE_BANKROLL_CAP_USD": "3",
        "BTC_LIVE_MAX_ENTRY_SLIPPAGE": "0.01",
    }
    read = {
        "TRADE_MAX_USD",
        "TRADE_DAILY_LOSS_HALT_USD",
        "TRADE_BANKROLL_CAP_USD",
        "TRADE_MAX_ENTRY_SLIPPAGE",
    }

    warnings = _config._legacy_env_warnings(environ, read)

    assert len(warnings) == 4
    assert any("BTC_LIVE_MAX_TRADE_USD" in w and "TRADE_MAX_USD" in w for w in warnings)


def test_ignores_unrelated_keys() -> None:
    environ = {
        "BTC_NOT_A_KNOB": "x",  # no unprefixed equivalent is read
        "BOT_MODE": "paper",  # already the right name
        "PATH": "/usr/bin",
    }
    assert _config._legacy_env_warnings(environ, {"BOT_MODE"}) == []


def test_warning_never_echoes_the_value() -> None:
    secret = "0xdeadbeefdeadbeefdeadbeefdeadbeef"
    warnings = _config._legacy_env_warnings(
        {"BTC_POLYMARKET_PRIVATE_KEY": secret}, {"POLYMARKET_PRIVATE_KEY"}
    )
    assert len(warnings) == 1
    assert secret not in warnings[0]


def test_real_read_set_covers_live_settings() -> None:
    for name in ("BOT_MODE", "TRADE_MAX_USD", "POLYMARKET_PRIVATE_KEY"):
        assert name in _config._ENV_NAMES_READ


def test_import_warns_and_does_not_alias_risk_limit(tmp_path: Path) -> None:
    """End to end: a stale BTC_LIVE_MAX_TRADE_USD must not change the limit."""
    env = {
        k: v for k, v in os.environ.items() if not k.startswith("BTC_")
    }
    env.update(
        {
            "BTC_LIVE_MAX_TRADE_USD": "999",
            # Keep the operator's real .env out of the child (python-dotenv
            # >=1.1); the explicit TRADE_MAX_USD wins on older versions anyway,
            # since load_dotenv never overrides a set variable.
            "PYTHON_DOTENV_DISABLED": "1",
            "TRADE_MAX_USD": "",
            "DATA_DIR": str(tmp_path),
            "DB_PATH": str(tmp_path / "t.db"),
        }
    )
    code = (
        "import config\n"
        "print(repr(config.TRADE_MAX_USD))\n"
        "print('\\n'.join(config.CONFIG_WARNINGS))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=60, check=True,
    ).stdout

    lines = out.splitlines()
    assert lines[0] == "3.0"
    assert any(
        line.startswith("BTC_LIVE_MAX_TRADE_USD ") and " TRADE_MAX_USD" in line
        for line in lines[1:]
    ), out


def test_env_example_only_documents_names_config_reads() -> None:
    text = (REPO / ".env.example").read_text()
    documented = set(re.findall(r"^#?([A-Z][A-Z0-9_]*)=", text, flags=re.MULTILINE))

    assert documented, "no KEY= lines parsed from .env.example"
    assert not {k for k in documented if k.startswith("BTC_")}
    assert documented <= _config._ENV_NAMES_READ, documented - _config._ENV_NAMES_READ
