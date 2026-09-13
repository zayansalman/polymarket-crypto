"""Pull this account's Polymarket trade history via the CLOB API → CSV.

Replaces the manual "export from Polymarket → drop CSV in data/" flow. Uses
the same auth the live executor uses (POLYMARKET_PRIVATE_KEY etc.) so no
extra credentials are needed.

The output CSV matches the schema ``polymarket_bot.backtest.build_opportunities``
expects, so the existing backtest tool runs against it unchanged:

    timestamp, action, marketName, tokenName, usdcAmount, tokenAmount, hash

Usage:
    .venv/bin/python tools/fetch_polymarket_trades.py \\
        --since 30d \\
        --output data/polymarket_history_$(date +%F).csv

    .venv/bin/python tools/backtest_btc_strategy.py \\
        --history data/polymarket_history_2026-06-15.csv \\
        --output data/backtests/refresh_2026-06-15.json
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config as _config  # noqa: E402
from polymarket_exec.execution.live import assert_live_boot_allowed  # noqa: E402


# ---------------------------------------------------------------------------
# Time-range parsing
# ---------------------------------------------------------------------------


def _parse_since(spec: str) -> int:
    """Accept '30d', '7d', '24h', or an ISO date; return unix-seconds boundary."""
    spec = spec.strip().lower()
    now = datetime.now(UTC)
    if spec.endswith("d"):
        return int((now - timedelta(days=int(spec[:-1]))).timestamp())
    if spec.endswith("h"):
        return int((now - timedelta(hours=int(spec[:-1]))).timestamp())
    if spec.endswith("w"):
        return int((now - timedelta(weeks=int(spec[:-1]))).timestamp())
    # ISO date fallback (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ).
    dt = datetime.fromisoformat(spec.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp())


# ---------------------------------------------------------------------------
# Gamma resolver — condition_id / asset_id → market metadata
# ---------------------------------------------------------------------------


try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:  # noqa: BLE001
    _ET = None


def synthesize_btc_market_name(trade_ts: int) -> str:
    """Return the canonical ``Bitcoin Up or Down - <Month> <D>, H:MMAM-H:MMAM ET``
    name for the 5-minute BTC market that contains ``trade_ts``.

    BTC 5m markets are aligned to wall-clock 5-minute boundaries, so the
    window the trade belongs to is the floor-aligned 5-minute slot. Gamma
    drops resolved markets from its index — we can't look the name up after
    the fact — but we can reconstruct it from the trade timestamp alone.
    """
    if _ET is None:
        return ""
    dt = datetime.fromtimestamp(trade_ts, UTC).astimezone(_ET)
    floor_min = (dt.minute // 5) * 5
    start = dt.replace(minute=floor_min, second=0, microsecond=0)
    end = start + timedelta(minutes=5)

    def _fmt(t: datetime) -> str:
        # 5:25AM, 12:00PM — drop the leading zero from the hour.
        return t.strftime("%I:%M%p").lstrip("0")

    return (
        f"Bitcoin Up or Down - {start.strftime('%B')} "
        f"{start.day}, {_fmt(start)}-{_fmt(end)} ET"
    )


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------


def fetch_trades(after_ts: int, before_ts: int | None) -> list[dict[str, Any]]:
    """Pull every fill for this account between *after_ts* and *before_ts*."""
    assert_live_boot_allowed()  # same gates as live boot
    from py_clob_client_v2 import ClobClient
    from py_clob_client_v2.clob_types import TradeParams

    client = ClobClient(
        _config.POLYMARKET_CLOB_API,
        chain_id=_config.POLYMARKET_CHAIN_ID,
        key=_config.POLYMARKET_PRIVATE_KEY,
        signature_type=_config.POLYMARKET_SIGNATURE_TYPE,
        funder=_config.POLYMARKET_FUNDER or None,
    )
    creds = client.create_or_derive_api_key()
    if creds is None:
        raise RuntimeError("Could not derive CLOB API credentials.")
    client.set_api_creds(creds)
    params = TradeParams(after=after_ts, before=before_ts)
    print(
        f"Pulling trades after={datetime.fromtimestamp(after_ts, UTC).isoformat()}"
        + (
            f" before={datetime.fromtimestamp(before_ts, UTC).isoformat()}"
            if before_ts
            else ""
        ),
        file=sys.stderr,
    )
    trades = client.get_trades(params=params)
    print(f"  fetched {len(trades)} trades", file=sys.stderr)
    return trades


# ---------------------------------------------------------------------------
# Map CLOB trade → backtest CSV row
# ---------------------------------------------------------------------------


def trade_to_row(t: dict[str, Any]) -> dict[str, Any] | None:
    """Convert one CLOB trade record to a CSV row compatible with build_opportunities.

    Returns ``None`` when the trade isn't a BTC 5m buy we can backtest.
    The CLOB ``side`` field is the user's perspective regardless of taker/maker
    role, so a BUY here is always a buy of the named outcome.

    Market discrimination is by outcome label: BTC 5m markets only have
    "Up" / "Down" outcomes; sports markets use team names ("Knicks", etc.)
    and other binaries use "Yes" / "No". So the outcome alone is enough to
    filter to BTC 5m without a gamma round-trip per trade. Market name is
    synthesized from the trade timestamp (gamma drops resolved markets).
    """
    if (t.get("side") or "").upper() != "BUY":
        return None
    side_label = (t.get("outcome") or "").strip()
    if side_label not in ("Up", "Down"):
        return None
    try:
        price = float(t.get("price") or 0.0)
        size = float(t.get("size") or 0.0)
    except (TypeError, ValueError):
        return None
    if price <= 0 or size <= 0:
        return None
    notional = price * size
    match_time = t.get("match_time") or t.get("matched_time") or t.get("timestamp")
    try:
        ts = int(match_time)
    except (TypeError, ValueError):
        return None
    market_name = synthesize_btc_market_name(ts)
    if not market_name or "Bitcoin Up or Down" not in market_name:
        return None
    return {
        "timestamp": ts,
        "action": "BUY",
        "marketName": market_name,
        "tokenName": side_label,
        "usdcAmount": round(notional, 6),
        "tokenAmount": round(size, 6),
        "hash": t.get("transaction_hash") or t.get("tx_hash") or "",
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


CSV_FIELDS = [
    "marketName",
    "action",
    "usdcAmount",
    "tokenAmount",
    "tokenName",
    "timestamp",
    "hash",
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--since",
        default="30d",
        help="Pull trades since this point: '30d', '24h', '2w', or ISO date.",
    )
    parser.add_argument(
        "--until",
        default=None,
        help="Optional upper bound (same format as --since).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data") / f"polymarket_history_{datetime.now(UTC).date()}.csv",
    )
    args = parser.parse_args()

    after_ts = _parse_since(args.since)
    before_ts = _parse_since(args.until) if args.until else None

    trades = fetch_trades(after_ts, before_ts)
    if not trades:
        print("No trades returned. Nothing to write.", file=sys.stderr)
        sys.exit(0)

    rows: list[dict[str, Any]] = []
    for i, t in enumerate(trades):
        row = trade_to_row(t)
        if row is not None:
            rows.append(row)
        if (i + 1) % 100 == 0:
            print(f"  processed {i + 1}/{len(trades)}", file=sys.stderr)

    rows.sort(key=lambda r: r["timestamp"])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    print(
        f"Wrote {len(rows)} BTC-5m buy rows to {args.output} "
        f"(filtered from {len(trades)} total trades).",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
