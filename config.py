"""Configuration for the local Polymarket crypto trading lab."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


# Env vars that failed to parse. Paper mode tolerates the fallback defaults,
# but live mode REFUSES to boot while this is non-empty (see
# polymarket_exec.execution.live.assert_live_boot_allowed): a typo in a risk limit
# must never silently degrade to looser defaults with real funds.
CONFIG_PARSE_ERRORS: list[str] = []


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        return float(value)
    except ValueError:
        CONFIG_PARSE_ERRORS.append(f"{name}={value!r} is not a valid number")
        return default


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError:
        CONFIG_PARSE_ERRORS.append(f"{name}={value!r} is not a valid integer")
        return default


def _env_optional_float(name: str) -> float | None:
    """Risk-limit-style env var: blank / unset / ≤0 → None (gate disabled)."""
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return None
    try:
        v = float(value)
    except ValueError:
        CONFIG_PARSE_ERRORS.append(f"{name}={value!r} is not a valid number")
        return None
    return v if v > 0 else None


def _env_choice(name: str, default: str, allowed: set[str]) -> str:
    value = os.getenv(name, default).strip().lower()
    return value if value in allowed else default


REPO_ROOT = Path(__file__).parent.resolve()

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
# Interpreter for the isolated Kronos forecast worker (Tsinghua-Kronos BTC 24h). Blank =
# the app's own interpreter; set it when torch lives in a different environment.
KRONOS_PYTHON = os.getenv("KRONOS_PYTHON", "")
CHAINLINK_STREAM_URL = "https://data.chain.link/streams/btc-usd-cexprice-streams"
MARKET_TIMEFRAME_MINUTES = 5

# --- Settlement-aligned Chainlink feed (issue #21) --------------------------
# Polymarket resolves BTC 5m markets on its Chainlink BTC/USD stream, NOT on
# Binance (measured basis: Chainlink ~ $50.7 BELOW Binance, std $3.8). The
# reference open, live spot, and sigma all come from these two endpoints;
# Binance remains only as a volatility-shape fallback and for backtest tooling.
POLYMARKET_CRYPTO_PRICE_API = os.getenv(
    "POLYMARKET_CRYPTO_PRICE_API", "https://polymarket.com/api/crypto/crypto-price"
)
POLYMARKET_LIVE_DATA_WS = os.getenv(
    "POLYMARKET_LIVE_DATA_WS", "wss://ws-live-data.polymarket.com"
)
# Seconds after which the latest Chainlink WS print is considered stale; a
# stale/absent settlement feed blocks NEW entries (exits still run).
CHAINLINK_STALE_SECONDS = _env_float("CHAINLINK_STALE_SECONDS", 15.0)
# Observed Chainlink print granularity (~2 decimal places at $61k). Used to
# estimate the discrete tie mass P(close == open), which resolves Up.
PRINT_GRANULARITY_USD = _env_float("PRINT_GRANULARITY_USD", 0.01)

# Execution target. Paper is the default; live places REAL orders on the
# Polymarket CLOB and only boots when POLYMARKET_PRIVATE_KEY is set and the
# operator clicked LIVE in the dashboard. The private key is never logged.
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

# --- Live trading (Polymarket CLOB) ---------------------------------------
POLYMARKET_CLOB_API = os.getenv("POLYMARKET_CLOB_API", "https://clob.polymarket.com")
POLYMARKET_CHAIN_ID = _env_int("POLYMARKET_CHAIN_ID", 137)  # Polygon mainnet
POLYMARKET_PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY", "")
POLYMARKET_FUNDER = os.getenv("POLYMARKET_FUNDER", "")
# 2 = MetaMask (Polymarket Safe). The only supported setup — see
# tools/live_detect_wallet.py.
POLYMARKET_SIGNATURE_TYPE = _env_int("POLYMARKET_SIGNATURE_TYPE", 2)
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
# Touch this file to halt all live trading and cancel open orders.
KILL_SWITCH_PATH = Path(
    os.getenv("KILL_SWITCH_PATH", str(DATA_DIR / "KILL"))
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
    os.getenv("PAPER_ENTRY_MIN_REMAINING_SECONDS", "60")
)
PAPER_TARGET_RETURN = _env_float("PAPER_TARGET_RETURN", 0.10)
PAPER_STOP_RETURN = _env_float("PAPER_STOP_RETURN", -0.08)
PAPER_TIME_EXIT_SECONDS = int(os.getenv("PAPER_TIME_EXIT_SECONDS", "45"))

HISTORY_CSV_PATH = Path(
    os.getenv(
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
    for a in os.getenv("DAILY_ASSETS", "doge,sol,xrp,bnb,eth").split(",")
    if a.strip()
]
DAILY_TRADE_USD = _env_float("DAILY_TRADE_USD", 10.0)
DAILY_SCAN_INTERVAL_SECONDS = _env_float("DAILY_SCAN_INTERVAL_SECONDS", 60.0)
DAILY_ENTRY_EDGE_MIN = _env_float("DAILY_ENTRY_EDGE_MIN", 0.045)
DAILY_ENTRY_MIN_REMAINING_SECONDS = int(
    os.getenv("DAILY_ENTRY_MIN_REMAINING_SECONDS", "3600")
)
DAILY_VOL_LOOKBACK_DAYS = _env_int("DAILY_VOL_LOOKBACK_DAYS", 30)
