"""Configuration for the local Polymarket crypto trading lab."""
from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


# Env vars that failed to parse. Paper mode tolerates the fallback defaults,
# but live mode REFUSES to boot while this is non-empty (see
# polymarket_exec.execution.live.assert_live_boot_allowed): a typo in a risk limit
# must never silently degrade to looser defaults with real funds.
CONFIG_PARSE_ERRORS: list[str] = []


# Every env name config.py reads, recorded by _env_str. The legacy-name check
# at the bottom compares against this real read set, not a hand-kept list.
_ENV_NAMES_READ: set[str] = set()


def _env_str(name: str, default: str = "") -> str:
    _ENV_NAMES_READ.add(name)
    return os.getenv(name, default)


def _env_float(name: str, default: float) -> float:
    value = _env_str(name)
    if value == "":
        return default
    try:
        return float(value)
    except ValueError:
        CONFIG_PARSE_ERRORS.append(f"{name}={value!r} is not a valid number")
        return default


def _env_int(name: str, default: int) -> int:
    value = _env_str(name)
    if value == "":
        return default
    try:
        return int(value)
    except ValueError:
        CONFIG_PARSE_ERRORS.append(f"{name}={value!r} is not a valid integer")
        return default


def _env_optional_float(name: str) -> float | None:
    """Risk-limit-style env var: blank / unset / ≤0 → None (gate disabled)."""
    value = _env_str(name)
    if value.strip() == "":
        return None
    try:
        v = float(value)
    except ValueError:
        CONFIG_PARSE_ERRORS.append(f"{name}={value!r} is not a valid number")
        return None
    return v if v > 0 else None


def _env_choice(name: str, default: str, allowed: set[str]) -> str:
    value = _env_str(name, default).strip().lower()
    return value if value in allowed else default


REPO_ROOT = Path(__file__).parent.resolve()

DATA_DIR = Path(_env_str("DATA_DIR", "./data")).expanduser().resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = Path(
    _env_str("DB_PATH", str(DATA_DIR / "btc_5m_binary_fair_value.db"))
).expanduser().resolve()
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

DASHBOARD_SERVER_NAME = _env_str("DASHBOARD_SERVER_NAME", "127.0.0.1")
DASHBOARD_SERVER_PORT = int(_env_str("DASHBOARD_SERVER_PORT", "7860"))

# Market-data mirror of the spot API: api.binance.com is unreachable from some
# networks, while data-api.binance.vision serves the same /api/v3 endpoints.
BINANCE_API_BASE = _env_str("BINANCE_API_BASE", "https://data-api.binance.vision")
POLYMARKET_GAMMA_API = "https://gamma-api.polymarket.com"
CHAINLINK_STREAM_URL = "https://data.chain.link/streams/btc-usd-cexprice-streams"
MARKET_TIMEFRAME_MINUTES = 5

# --- Settlement-aligned Chainlink feed (issue #21) --------------------------
# Polymarket resolves BTC 5m markets on its Chainlink BTC/USD stream, NOT on
# Binance (measured basis: Chainlink ~ $50.7 BELOW Binance, std $3.8). The
# reference open, live spot, and sigma all come from these two endpoints;
# Binance remains only as a volatility-shape fallback and for backtest tooling.
POLYMARKET_CRYPTO_PRICE_API = _env_str(
    "POLYMARKET_CRYPTO_PRICE_API", "https://polymarket.com/api/crypto/crypto-price"
)
POLYMARKET_LIVE_DATA_WS = _env_str(
    "POLYMARKET_LIVE_DATA_WS", "wss://ws-live-data.polymarket.com"
)
# Seconds after which the latest Chainlink WS print is considered stale; a
# stale/absent settlement feed blocks NEW entries (exits still run).
CHAINLINK_STALE_SECONDS = _env_float("CHAINLINK_STALE_SECONDS", 15.0)
# Observed Chainlink print granularity (~2 decimal places at $61k). Used to
# estimate the discrete tie mass P(close == open), which resolves Up.
PRINT_GRANULARITY_USD = _env_float("PRINT_GRANULARITY_USD", 0.01)

# Execution target. Paper is the default; live places REAL orders on the
# Polymarket CLOB and only boots when POLYMARKET_PRIVATE_KEY is set AND
# LIVE_CONFIRM=YES_I_UNDERSTAND. The private key is never logged.
BOT_MODE = _env_choice("BOT_MODE", "paper", {"paper", "live"})
# Trade shape. 'settle' (default): max one entry per window, hold to
# resolution — the shape the April backtest validated (+31% ROI); the spread
# is paid once at entry. 'scalp': legacy intra-window TARGET/STOP/BAND exits —
# soaked -$7.87 in 70 minutes under honest fills (median hold 8s, paying the
# spread every round trip); kept only for experiments.
EXIT_STYLE = _env_choice("EXIT_STYLE", "settle", {"settle", "scalp"})
# Shadow forward-tester: log candidate strategies' would-be trades in parallel
# with the live strategy (no real orders placed). "off" disables it entirely.
SHADOW_ENABLED = _env_choice("SHADOW_ENABLED", "on", {"on", "off"})

# Adaptive risk controller (issue #36): pause NEW entries when the strategy's
# rolling expectancy decays, before losses pile up. Complements the hard halt.
AUTO_PAUSE_ENABLED = _env_choice(
    "AUTO_PAUSE_ENABLED", "true", {"true", "false"}
) == "true"
AUTO_PAUSE_WINDOW = _env_int("AUTO_PAUSE_WINDOW", 20)
AUTO_PAUSE_MIN_TRADES = _env_int("AUTO_PAUSE_MIN_TRADES", 10)
AUTO_PAUSE_MIN_ROI = _env_float("AUTO_PAUSE_MIN_ROI", -0.15)

# --- Live trading (Polymarket CLOB) ---------------------------------------
POLYMARKET_CLOB_API = _env_str("POLYMARKET_CLOB_API", "https://clob.polymarket.com")
POLYMARKET_CHAIN_ID = _env_int("POLYMARKET_CHAIN_ID", 137)  # Polygon mainnet
POLYMARKET_PRIVATE_KEY = _env_str("POLYMARKET_PRIVATE_KEY", "")
POLYMARKET_FUNDER = _env_str("POLYMARKET_FUNDER", "")
# 0 = EOA, 1 = email/Magic proxy (POLY_PROXY), 2 = Gnosis Safe (browser wallet
# e.g. MetaMask), 3 = deposit wallet / ERC-1271 (tools/live_setup.py).
POLYMARKET_SIGNATURE_TYPE = _env_int("POLYMARKET_SIGNATURE_TYPE", 1)
# Hard risk limits enforced by the unified RiskGate (issue #64) before every
# paper or live entry. Same gate, same values, both modes — paper is a
# faithful preview of live. The per-trade and daily-loss-halt limits are
# always on; the daily bankroll cap is OPT-IN and disabled when
# TRADE_BANKROLL_CAP_USD is blank / unset / ≤0. The persisted daily
# counters in SQLite keep tracking spend regardless, so the dashboard can
# still display daily throughput when the cap is off.
TRADE_MAX_USD = _env_float("TRADE_MAX_USD", 3.0)
TRADE_DAILY_LOSS_HALT_USD = _env_float("TRADE_DAILY_LOSS_HALT_USD", 10.0)
TRADE_BANKROLL_CAP_USD: float | None = _env_optional_float("TRADE_BANKROLL_CAP_USD")
TRADE_MAX_ENTRY_SLIPPAGE = _env_float("TRADE_MAX_ENTRY_SLIPPAGE", 0.02)
# How long an exit SELL may rest before it is cancelled and retried at the
# new best bid. Exits never rest beyond this bound.
LIVE_EXIT_FILL_TIMEOUT_SECONDS = _env_float("LIVE_EXIT_FILL_TIMEOUT_SECONDS", 10.0)
# Must be the literal string YES_I_UNDERSTAND for live mode to boot.
LIVE_CONFIRM = _env_str("LIVE_CONFIRM", "")
# Touch this file to halt all live trading and cancel open orders.
KILL_SWITCH_PATH = Path(
    _env_str("KILL_SWITCH_PATH", str(DATA_DIR / "KILL"))
).expanduser().resolve()
PAPER_MIN_TRADE_USD = _env_float("PAPER_MIN_TRADE_USD", 1.0)
PAPER_MAX_TRADE_USD = _env_float("PAPER_MAX_TRADE_USD", 5.0)
PAPER_TICK_SECONDS = _env_float("PAPER_TICK_SECONDS", 5.0)
PAPER_ENTRY_EDGE_MIN = _env_float("PAPER_ENTRY_EDGE_MIN", 0.045)
# Stale-model guard + favorites filter (issue #29, from the 26h settle soak,
# n=225): claimed edges above ~7% and entries below ~50c were where adverse
# selection lived; the joint surviving slice ran +22.8% ROI (n=48, in-sample).
PAPER_ENTRY_EDGE_MAX = _env_float("PAPER_ENTRY_EDGE_MAX", 0.07)
PAPER_MIN_ENTRY_PRICE = _env_float("PAPER_MIN_ENTRY_PRICE", 0.50)
PAPER_MIN_CONFIDENCE = _env_float("PAPER_MIN_CONFIDENCE", 0.50)
PAPER_ENTRY_MIN_REMAINING_SECONDS = int(
    _env_str("PAPER_ENTRY_MIN_REMAINING_SECONDS", "60")
)
PAPER_TARGET_RETURN = _env_float("PAPER_TARGET_RETURN", 0.10)
PAPER_STOP_RETURN = _env_float("PAPER_STOP_RETURN", -0.08)
PAPER_TIME_EXIT_SECONDS = int(_env_str("PAPER_TIME_EXIT_SECONDS", "45"))

HISTORY_CSV_PATH = Path(
    _env_str(
        "HISTORY_CSV_PATH",
        str(DATA_DIR / "polymarket_history.csv"),
    )
).expanduser()

# --- Daily altcoin Up/Down shadow strategy (issue #185) ---------------------
# Paper-only, no live gate. Scans the daily (24h-window) Up/Down family across
# several thinner altcoin markets and shadow-trades a fixed-size position on
# whichever asset currently shows the strongest signal.
DAILY_ASSETS = [
    a.strip().lower()
    for a in _env_str("DAILY_ASSETS", "doge,sol,xrp,bnb,eth").split(",")
    if a.strip()
]
DAILY_TRADE_USD = _env_float("DAILY_TRADE_USD", 10.0)
DAILY_SCAN_INTERVAL_SECONDS = _env_float("DAILY_SCAN_INTERVAL_SECONDS", 60.0)
DAILY_ENTRY_EDGE_MIN = _env_float("DAILY_ENTRY_EDGE_MIN", 0.045)
DAILY_ENTRY_MIN_REMAINING_SECONDS = int(
    _env_str("DAILY_ENTRY_MIN_REMAINING_SECONDS", "3600")
)
DAILY_VOL_LOOKBACK_DAYS = _env_int("DAILY_VOL_LOOKBACK_DAYS", 30)


# --- Legacy BTC_-prefixed env names ------------------------------------------
# The knobs above used to be read as BTC_<NAME> (and the four risk limits as
# BTC_LIVE_*). An operator still carrying an old name silently gets the
# default — e.g. BTC_LIVE_MAX_TRADE_USD=1 still trades at the $3 default.
# Collect a non-fatal warning per stale key (main.py logs them once at boot;
# values are never echoed). Deliberately NO aliasing: a risk limit or wallet
# setting must be set consciously under its current name.
_LEGACY_ENV_RENAMES = {
    "BTC_LIVE_MAX_TRADE_USD": "TRADE_MAX_USD",
    "BTC_LIVE_DAILY_LOSS_HALT_USD": "TRADE_DAILY_LOSS_HALT_USD",
    "BTC_LIVE_BANKROLL_CAP_USD": "TRADE_BANKROLL_CAP_USD",
    "BTC_LIVE_MAX_ENTRY_SLIPPAGE": "TRADE_MAX_ENTRY_SLIPPAGE",
}


def _legacy_env_warnings(environ: Mapping[str, str], read_names: set[str]) -> list[str]:
    warnings = []
    for key in sorted(environ):
        if not key.startswith("BTC_"):
            continue
        new_name = _LEGACY_ENV_RENAMES.get(key, key.removeprefix("BTC_"))
        if new_name in read_names:
            warnings.append(
                f"{key} is set but IGNORED; the app reads {new_name}. Rename it "
                "in .env / the environment (the value is not carried over)."
            )
    return warnings


CONFIG_WARNINGS: list[str] = _legacy_env_warnings(os.environ, _ENV_NAMES_READ)
