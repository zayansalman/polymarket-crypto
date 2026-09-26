"""Configuration for the local Polymarket crypto trading lab."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


# Env values that failed to parse. The live venue refuses to arm while any is listed: a typo
# must never quietly become a default.
CONFIG_PARSE_ERRORS: list[str] = []


def _env_choice(name: str, default: str, allowed: set[str]) -> str:
    value = os.getenv(name, default).strip().lower()
    return value if value in allowed else default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError:
        CONFIG_PARSE_ERRORS.append(f"{name} is not a whole number")
        return default


DATA_DIR = Path(os.getenv("DATA_DIR", "./data")).expanduser().resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = Path(
    os.getenv("DB_PATH", str(DATA_DIR / "btc_5m_binary_fair_value.db"))
).expanduser().resolve()
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

DASHBOARD_SERVER_NAME = os.getenv("DASHBOARD_SERVER_NAME", "127.0.0.1")
DASHBOARD_SERVER_PORT = int(os.getenv("DASHBOARD_SERVER_PORT", "7860"))
# Extra host names the dashboard answers to (comma-separated), beyond 127.0.0.1 and localhost.
# Only for a deliberate LAN or container setup: any name here can serve the page's token.
DASHBOARD_ALLOWED_HOSTS = tuple(
    h.strip() for h in os.getenv("DASHBOARD_ALLOWED_HOSTS", "").split(",") if h.strip()
)

# Market-data mirror of the spot API: api.binance.com is unreachable from some
# networks, while data-api.binance.vision serves the same /api/v3 endpoints.
BINANCE_API_BASE = os.getenv("BINANCE_API_BASE", "https://data-api.binance.vision")
POLYMARKET_GAMMA_API = "https://gamma-api.polymarket.com"
POLYMARKET_CLOB_API = os.getenv("POLYMARKET_CLOB_API", "https://clob.polymarket.com")

# The PAPER/LIVE selection before the operator picks one in the dashboard. LIVE here
# is never consent: live orders need LIVE clicked in the dashboard in this process
# (AGENTS.md, "Live trading"). Fade places nothing while LIVE is selected.
BOT_MODE = _env_choice("BOT_MODE", "paper", {"paper", "live"})

# The live venue's wallet (ems/execution/clob.py). The private key is read here and
# passed only to the CLOB client: never logged, journaled or shown (logging_setup
# scrubs its value from every sink). Signature type 2 with the Polymarket Safe as
# funder is the only setup the live venue accepts.
POLYMARKET_CHAIN_ID = _env_int("POLYMARKET_CHAIN_ID", 137)
POLYMARKET_PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY", "").strip()
POLYMARKET_FUNDER = os.getenv("POLYMARKET_FUNDER", "").strip()
POLYMARKET_SIGNATURE_TYPE = _env_int("POLYMARKET_SIGNATURE_TYPE", 2)
# Touch this file to stop every strategy placing new orders and cancel resting ones.
KILL_SWITCH_PATH = Path(
    os.getenv("KILL_SWITCH_PATH", str(DATA_DIR / "KILL"))
).expanduser().resolve()
