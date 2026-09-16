"""Pure parsers for the Polymarket CLOB market channel: one text frame in, typed events out.

Wire facts (live-checked 2026-09-16):

* JSON objects are tagged by ``event_type`` and every number is a string. The snapshot
  sent after a subscribe is ONE frame holding a JSON array of ``book`` objects (one per
  new token); only those snapshot books carry ``tick_size`` and ``last_trade_price``.
* Book levels come worst-to-best: bids ascending, asks descending (best = last element).
* ``price_change`` carries the order and its mirror on the other outcome. ``size`` is the
  new absolute size at that price ("0" removes the level); ``best_bid``/``best_ask`` are
  that token's top after the change ("0"/"1" when a side is empty).
* ``best_bid_ask``, ``new_market`` and ``market_resolved`` flow only while the
  connection's ``custom_feature_enabled`` flag is on.
* Plain text frames: ``PONG``, ``NO NEW ASSETS``, ``INVALID MESSAGE``,
  ``INVALID OPERATION``, and ``[]`` (a subscribe that named an unknown id).
* ``fee_rate_bps`` on trades is always "0" and is not kept.

Nothing here raises on bad input: unknown or malformed objects become :class:`Unknown`,
so the stream can count them.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Union

PONG = "PONG"
NO_NEW_ASSETS = "NO NEW ASSETS"
INVALID_MESSAGE = "INVALID MESSAGE"
INVALID_OPERATION = "INVALID OPERATION"
EMPTY_SNAPSHOT = "[]"
OTHER_TEXT = "other"
_KNOWN_TEXT = frozenset({PONG, NO_NEW_ASSETS, INVALID_MESSAGE, INVALID_OPERATION})

BUY = "BUY"
SELL = "SELL"

Level = tuple[float, float]  # (price, size)


@dataclass(frozen=True, slots=True)
class BookEvent:
    """A full book for one token. Every book replaces that token's levels."""

    market: str
    asset_id: str
    bids: tuple[Level, ...]  # wire order: ascending, best bid last
    asks: tuple[Level, ...]  # wire order: descending, best ask last
    ts_ms: int
    hash: str
    tick_size: float | None = None  # snapshot books only
    last_trade_price: float | None = None  # snapshot books only
    snapshot: bool = False  # came in the array sent after a subscribe


@dataclass(frozen=True, slots=True)
class LevelChange:
    asset_id: str
    price: float
    size: float  # new absolute size at this price; 0 removes the level
    side: str  # BUY (bids) | SELL (asks)
    best_bid: float | None  # the token's top after the change, as sent ("0" = empty)
    best_ask: float | None  # "1" = empty
    hash: str


@dataclass(frozen=True, slots=True)
class PriceChangeEvent:
    market: str
    changes: tuple[LevelChange, ...]
    ts_ms: int


@dataclass(frozen=True, slots=True)
class BestBidAskEvent:
    market: str
    asset_id: str
    best_bid: float | None
    best_ask: float | None
    spread: float | None
    ts_ms: int


@dataclass(frozen=True, slots=True)
class LastTradeEvent:
    market: str
    asset_id: str
    price: float
    size: float
    side: str  # the taker's side
    ts_ms: int
    transaction_hash: str


@dataclass(frozen=True, slots=True)
class TickSizeEvent:
    market: str
    asset_id: str
    old_tick_size: float | None
    new_tick_size: float
    ts_ms: int


@dataclass(frozen=True, slots=True)
class NewMarketEvent:
    """Broadcast for every new market platform-wide (Up/Down ~24 h ahead)."""

    market: str
    slug: str
    question: str
    outcomes: tuple[str, ...]
    token_ids: tuple[str, ...]  # same order as ``outcomes``
    condition_id: str
    active: bool
    tick_size: float | None
    fee_rate: float | None
    ts_ms: int


@dataclass(frozen=True, slots=True)
class MarketResolvedEvent:
    """Sent for subscribed tokens only, ~2 min after an Up/Down window ends."""

    id: str
    market: str
    token_ids: tuple[str, ...]
    winning_asset_id: str
    winning_outcome: str
    ts_ms: int
    tags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TextFrame:
    kind: str  # PONG | NO_NEW_ASSETS | INVALID_MESSAGE | INVALID_OPERATION | EMPTY_SNAPSHOT | other
    text: str


@dataclass(frozen=True, slots=True)
class Unknown:
    """An object we do not use (a new event type) or could not read. Counted, never raised."""

    event_type: str
    detail: str = ""


ClobEvent = Union[
    BookEvent,
    PriceChangeEvent,
    BestBidAskEvent,
    LastTradeEvent,
    TickSizeEvent,
    NewMarketEvent,
    MarketResolvedEvent,
    TextFrame,
    Unknown,
]


def parse_frame(text: str | bytes) -> list[ClobEvent]:
    """Every event in one received frame (a snapshot array holds one book per token)."""
    if isinstance(text, (bytes, bytearray)):
        text = text.decode("utf-8", "replace")
    body = text.strip()
    if not body.startswith(("{", "[")):
        return [TextFrame(body if body in _KNOWN_TEXT else OTHER_TEXT, body)]
    try:
        data = json.loads(body)
    except ValueError as exc:
        return [Unknown("unparsable", str(exc)[:120])]
    if isinstance(data, list):
        if not data:
            return [TextFrame(EMPTY_SNAPSHOT, body)]
        return [_parse_object(item, snapshot=True) for item in data]
    return [_parse_object(data, snapshot=False)]


def _parse_object(obj: Any, *, snapshot: bool) -> ClobEvent:
    if not isinstance(obj, dict):
        return Unknown("not_an_object")
    event_type = obj.get("event_type")
    parser = _PARSERS.get(event_type) if isinstance(event_type, str) else None
    if parser is None:
        return Unknown(str(event_type) if event_type is not None else "missing")
    try:
        return parser(obj, snapshot)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        return Unknown(event_type, f"{type(exc).__name__}: {exc}"[:120])


def _ms(value: Any) -> int:
    try:
        return int(value)
    except ValueError:
        return int(float(value))


def _opt_float(value: Any) -> float | None:
    """None for an absent value; anything else must read as a number."""
    if value is None or value == "":
        return None
    return float(value)


def _lenient_float(value: Any) -> float | None:
    try:
        return _opt_float(value)
    except (TypeError, ValueError):
        return None


def _levels(raw: Any) -> tuple[Level, ...]:
    if not isinstance(raw, list):
        raise TypeError("levels are not a list")
    return tuple((float(level["price"]), float(level["size"])) for level in raw)


def _side(value: Any) -> str:
    side = str(value).upper()
    if side not in (BUY, SELL):
        raise ValueError(f"unknown side {value!r}")
    return side


def _str_list(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, list):
        raise TypeError("expected a list")
    return tuple(str(v) for v in value)


def _book(obj: dict[str, Any], snapshot: bool) -> BookEvent:
    return BookEvent(
        market=str(obj["market"]),
        asset_id=str(obj["asset_id"]),
        bids=_levels(obj["bids"]),
        asks=_levels(obj["asks"]),
        ts_ms=_ms(obj["timestamp"]),
        hash=str(obj.get("hash", "")),
        tick_size=_opt_float(obj.get("tick_size")),
        last_trade_price=_opt_float(obj.get("last_trade_price")),
        snapshot=snapshot,
    )


def _change(raw: dict[str, Any]) -> LevelChange:
    return LevelChange(
        asset_id=str(raw["asset_id"]),
        price=float(raw["price"]),
        size=float(raw["size"]),
        side=_side(raw["side"]),
        best_bid=_opt_float(raw.get("best_bid")),
        best_ask=_opt_float(raw.get("best_ask")),
        hash=str(raw.get("hash", "")),
    )


def _price_change(obj: dict[str, Any], snapshot: bool) -> PriceChangeEvent:
    changes = tuple(_change(raw) for raw in obj["price_changes"])
    if not changes:
        raise ValueError("price_change without changes")
    return PriceChangeEvent(str(obj["market"]), changes, _ms(obj["timestamp"]))


def _best_bid_ask(obj: dict[str, Any], snapshot: bool) -> BestBidAskEvent:
    return BestBidAskEvent(
        market=str(obj["market"]),
        asset_id=str(obj["asset_id"]),
        best_bid=_opt_float(obj.get("best_bid")),
        best_ask=_opt_float(obj.get("best_ask")),
        spread=_opt_float(obj.get("spread")),
        ts_ms=_ms(obj["timestamp"]),
    )


def _last_trade(obj: dict[str, Any], snapshot: bool) -> LastTradeEvent:
    return LastTradeEvent(
        market=str(obj["market"]),
        asset_id=str(obj["asset_id"]),
        price=float(obj["price"]),
        size=float(obj["size"]),
        side=str(obj.get("side", "")).upper(),
        ts_ms=_ms(obj["timestamp"]),
        transaction_hash=str(obj.get("transaction_hash", "")),
    )


def _tick_size(obj: dict[str, Any], snapshot: bool) -> TickSizeEvent:
    return TickSizeEvent(
        market=str(obj["market"]),
        asset_id=str(obj["asset_id"]),
        old_tick_size=_opt_float(obj.get("old_tick_size")),
        new_tick_size=float(obj["new_tick_size"]),
        ts_ms=_ms(obj["timestamp"]),
    )


def _new_market(obj: dict[str, Any], snapshot: bool) -> NewMarketEvent:
    tokens = obj.get("clob_token_ids") or obj.get("assets_ids")
    fees = obj.get("fee_schedule")
    market = str(obj.get("market") or obj.get("condition_id") or "")
    return NewMarketEvent(
        market=market,
        slug=str(obj["slug"]),
        question=str(obj.get("question", "")),
        outcomes=_str_list(obj.get("outcomes", [])),
        token_ids=_str_list(tokens),
        condition_id=str(obj.get("condition_id") or market),
        active=obj.get("active") is True,
        tick_size=_lenient_float(obj.get("order_price_min_tick_size")),
        fee_rate=_lenient_float(fees.get("rate")) if isinstance(fees, dict) else None,
        ts_ms=_ms(obj["timestamp"]),
    )


def _market_resolved(obj: dict[str, Any], snapshot: bool) -> MarketResolvedEvent:
    return MarketResolvedEvent(
        id=str(obj.get("id", "")),
        market=str(obj["market"]),
        token_ids=_str_list(obj["assets_ids"]),
        winning_asset_id=str(obj["winning_asset_id"]),
        winning_outcome=str(obj.get("winning_outcome", "")),
        ts_ms=_ms(obj["timestamp"]),
        tags=_str_list(obj.get("tags") or []),
    )


_PARSERS: dict[str, Callable[[dict[str, Any], bool], ClobEvent]] = {
    "book": _book,
    "price_change": _price_change,
    "best_bid_ask": _best_bid_ask,
    "last_trade_price": _last_trade,
    "tick_size_change": _tick_size,
    "new_market": _new_market,
    "market_resolved": _market_resolved,
}
