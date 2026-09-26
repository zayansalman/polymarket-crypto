"""Configuration for the local Polymarket crypto trading lab."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _env_choice(name: str, default: str, allowed: set[str]) -> str:
    value = os.getenv(name, default).strip().lower()
    return value if value in allowed else default


DATA_DIR = Path(os.getenv("DATA_DIR", "./data")).expanduser().resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = Path(
    os.getenv("DB_PATH", str(DATA_DIR / "btc_5m_binary_fair_value.db"))
).expanduser().resolve()
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

DASHBOARD_SERVER_NAME = os.getenv("DASHBOARD_SERVER_NAME", "127.0.0.1")
DASHBOARD_SERVER_PORT = int(os.getenv("DASHBOARD_SERVER_PORT", "7860"))

# Market-data mirror of the spot API: api.binance.com is unreachable from some
# networks, while data-api.binance.vision serves the same /api/v3 endpoints.
BINANCE_API_BASE = os.getenv("BINANCE_API_BASE", "https://data-api.binance.vision")
POLYMARKET_GAMMA_API = "https://gamma-api.polymarket.com"
POLYMARKET_CLOB_API = os.getenv("POLYMARKET_CLOB_API", "https://clob.polymarket.com")

# Execution target. Paper is the only mode built: a strategy reads the requested
# mode each pass and places nothing new while it is anything but paper.
BOT_MODE = _env_choice("BOT_MODE", "paper", {"paper", "live"})
# Touch this file to stop every strategy placing new orders and cancel resting ones.
KILL_SWITCH_PATH = Path(
    os.getenv("KILL_SWITCH_PATH", str(DATA_DIR / "KILL"))
).expanduser().resolve()
