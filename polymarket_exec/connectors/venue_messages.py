"""Parsers for venue feed frames: Kraken spot/futures trades, Binance liquidations, perp state."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from polymarket_exec.connectors.venue_flow import Side, VenueSnapshot

Trade = tuple[int, float, float, Side]  # (ts_ms, price, qty, taker side)

_SIDES: dict[str, Side] = {"buy": "buy", "sell": "sell"}


def _iso_ms(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


def _f(value: Any) -> float | None:
    return None if value is None else float(value)


def kraken_spot_trades(msg: dict[str, Any], symbol: str = "BTC/USD") -> list[Trade]:
    """Trades from a Kraken WS v2 ``trade`` update; ``side`` is the taker side.

    Snapshot frames are skipped: they replay trades from before the connection and
    would double count after a reconnect.
    """
    if msg.get("channel") != "trade" or msg.get("type") != "update":
        return []
    trades: list[Trade] = []
    for t in msg.get("data") or []:
        side = _SIDES.get(t.get("side"))
        if t.get("symbol") != symbol or side is None:
            continue
        trades.append((_iso_ms(t["timestamp"]), float(t["price"]), float(t["qty"]), side))
    return trades


def kraken_futures_trades(msg: dict[str, Any], product_id: str = "PF_XBTUSD") -> list[Trade]:
    """A live fill from Kraken Futures WS v1; the ``trade_snapshot`` backlog is skipped."""
    if "event" in msg or msg.get("feed") != "trade" or msg.get("product_id") != product_id:
        return []
    side = _SIDES.get(msg.get("side"))
    if side is None or msg.get("type") not in ("fill", "liquidation"):
        return []
    return [(int(msg["time"]), float(msg["price"]), float(msg["qty"]), side)]


def binance_liquidation(msg: dict[str, Any], symbol: str = "BTCUSDT") -> Trade | None:
    """A forced-liquidation order from ``!forceOrder@arr``; SELL means a long was liquidated."""
    order = msg.get("o") if msg.get("e") == "forceOrder" else None
    if not isinstance(order, dict) or order.get("s") != symbol:
        return None
    side = _SIDES.get(str(order.get("S", "")).lower())
    qty = float(order.get("z") or order.get("q") or 0.0)
    if side is None or qty <= 0:
        return None
    price = float(order.get("ap") or order.get("p"))
    return (int(order["T"]), price, qty, side)


def binance_perp_snapshot(
    premium: dict[str, Any], open_interest: dict[str, Any], *, symbol: str
) -> VenueSnapshot:
    return VenueSnapshot(
        venue="binance_perp",
        symbol=symbol,
        taken_at_ms=int(premium["time"]),
        mark_price=float(premium["markPrice"]),
        index_price=float(premium["indexPrice"]),
        funding_rate=float(premium["lastFundingRate"]),
        next_funding_ms=int(premium["nextFundingTime"]),
        open_interest=float(open_interest["openInterest"]),
        source="fapi_premiumIndex_openInterest",
    )


def kraken_futures_snapshot(
    tickers: dict[str, Any], *, symbol: str, now_ms: int
) -> VenueSnapshot | None:
    """PF ticker. ``fundingRate`` is absolute (USD per contract per hour); stored relative to index."""
    row = next((t for t in tickers.get("tickers") or [] if t.get("symbol") == symbol), None)
    if row is None:
        return None
    index = _f(row.get("indexPrice"))
    rate = _f(row.get("fundingRate"))
    return VenueSnapshot(
        venue="kraken_futures",
        symbol=symbol,
        taken_at_ms=now_ms,
        mark_price=_f(row.get("markPrice")),
        index_price=index,
        funding_rate=rate / index if rate is not None and index else None,
        next_funding_ms=None,
        open_interest=_f(row.get("openInterest")),
        source="futures_tickers",
    )
