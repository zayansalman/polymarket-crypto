"""Test doubles for the venue's public reads: the trade tape, the CLOB market lookup, the CLOB
``/book``, Gamma's events and markets, and Binance 1-minute candles. Replies are real
``httpx.Response`` objects shaped like the real ones, the tape newest record first and paged by
offset.

The tape has the venue's two lists: ``takerOnly=true`` (one record per taker order, at its
average price) and ``takerOnly=false`` (those records and, next to each, the maker records it
traded with, at each maker's own price). Nothing in a record says which kind it is."""

from __future__ import annotations

import itertools
from typing import Any

import httpx

from ems import config as _config
from ems.execution.tape import CLOB, DATA_API

GAMMA = "https://gamma-api.polymarket.com"

_TX = itertools.count()
_MAKER = itertools.count()

Leg = tuple  # (outcome, the maker's side, size, price) or (..., wallet)


def trade(ts: int, outcome: str, side: str, size: float, price: float, *, cid: str,
          up: str, down: str, legs: tuple[Leg, ...] | list[Leg] = ()) -> dict:
    """One taker record, shaped like the data-api's. ``legs`` are the maker orders it traded
    with, as (outcome, the maker's side, size, price[, wallet]); left empty, it traded with one
    maker at its own price."""
    rec = {
        "timestamp": ts, "side": side, "size": size, "price": price,
        "asset": up if outcome == "Up" else down, "outcome": outcome,
        "outcomeIndex": 0 if outcome == "Up" else 1, "conditionId": cid,
        "proxyWallet": "0xtaker", "transactionHash": f"0xtx{next(_TX)}",
    }
    if legs:
        rec["_legs"] = [maker(rec, *leg, asset=up if leg[0] == "Up" else down) for leg in legs]
    return rec


def maker(taker: dict, outcome: str, side: str, size: float, price: float,
          wallet: str | None = None, *, asset: str) -> dict:
    """One maker record behind ``taker``: the same trade and second, the maker's own side,
    size and price."""
    return {
        **{k: v for k, v in taker.items() if not k.startswith("_")},
        "side": side, "size": size, "price": price, "asset": asset, "outcome": outcome,
        "outcomeIndex": 0 if outcome == "Up" else 1,
        "proxyWallet": wallet or f"0xmaker{next(_MAKER)}",
    }


def legs_of(taker: dict) -> list[dict]:
    """The maker records a taker record traded with: its ``_legs``, else one maker on the other
    side of it at its own price and size."""
    if "_legs" in taker:
        return list(taker["_legs"])
    flipped = {"BUY": "SELL", "SELL": "BUY"}.get(str(taker.get("side")), taker.get("side"))
    return [{**{k: v for k, v in taker.items() if not k.startswith("_")},
             "side": flipped, "proxyWallet": "0xmaker"}]


def feed(records: list[dict], *, taker_only: bool) -> list[dict]:
    """What one list of the tape shows, newest first: the taker records, and unless
    ``taker_only`` each one's maker records next to it (the level a sweep reached last first,
    as the venue lists them)."""
    out: list[dict] = []
    for rec in sorted(records, key=lambda r: (-r["timestamp"], -r["_seq"])):
        out.append({k: v for k, v in rec.items() if not k.startswith("_")})
        if not taker_only:
            out.extend(reversed(legs_of(rec)))
    return out


class FakeVenue:
    """The data-api trade tape, the CLOB market lookup and CLOB ``/book``, in memory."""

    def __init__(self) -> None:
        self.tape: dict[str, list[dict]] = {}
        self.markets: dict[str, dict] = {}
        self.books: dict[str, dict] = {}
        self.tape_status: dict[str, int] = {}
        self.book_status: dict[str, int] = {}
        self.closes: dict[int, float] = {}  # Binance BTCUSDT 1m close, by candle open second
        self.klines_status: int = 200
        self.price_to_beat: dict[str, float] = {}  # Gamma eventMetadata.priceToBeat by slug
        self.condition_ids: dict[str, str] = {}  # Gamma markets conditionId by slug
        self.calls: list[tuple[str, dict]] = []
        self._seq = itertools.count()

    def add(self, cid: str, *records: dict) -> None:
        for rec in records:
            self.tape.setdefault(cid, []).append({**rec, "_seq": next(self._seq)})

    def resolve(self, cid: str, *, winner: str | None, up: str, down: str,
                closed: bool = True) -> None:
        self.markets[cid] = {
            "condition_id": cid, "closed": closed,
            "tokens": [
                {"token_id": up, "outcome": "Up", "winner": winner == "Up"},
                {"token_id": down, "outcome": "Down", "winner": winner == "Down"},
            ],
        }

    def book(self, token: str, *, bids: list[tuple[float, float]],
             asks: list[tuple[float, float]], tick: str = "0.01", min_size: str = "5") -> None:
        """A /book reply; levels are listed worst to best, as the venue lists them."""
        self.books[token] = {
            "asset_id": token, "tick_size": tick, "min_order_size": min_size,
            "bids": [{"price": str(p), "size": str(s)} for p, s in sorted(bids)],
            "asks": [{"price": str(p), "size": str(s)} for p, s in sorted(asks, reverse=True)],
        }

    def tape_calls(self, cid: str | None = None) -> list[dict]:
        return [p for url, p in self.calls if url == f"{DATA_API}/trades"
                and (cid is None or p["market"] == cid)]

    def _page(self, cid: str, offset: int, limit: int, taker_only: bool = True) -> list[dict]:
        return feed(self.tape.get(cid, []), taker_only=taker_only)[offset:offset + limit]

    async def get(self, url: str, *, params: Any = None, headers: Any = None,
                  timeout: Any = None) -> httpx.Response:
        params = dict(params or {})
        self.calls.append((url, params))
        request = httpx.Request("GET", url)
        if url == f"{DATA_API}/trades":
            cid = params["market"]
            if cid in self.tape_status:
                return httpx.Response(self.tape_status[cid], json={}, request=request)
            taker_only = str(params.get("takerOnly", "true")).lower() == "true"
            page = self._page(cid, int(params["offset"]), int(params["limit"]), taker_only)
            return httpx.Response(200, json=page, request=request)
        if url.startswith(f"{CLOB}/markets/"):
            cid = url.rsplit("/", 1)[1]
            if cid not in self.markets:
                return httpx.Response(404, json={"error": "not found"}, request=request)
            return httpx.Response(200, json=self.markets[cid], request=request)
        if url == f"{_config.BINANCE_API_BASE}/api/v3/klines":
            if self.klines_status != 200:
                return httpx.Response(self.klines_status, json={}, request=request)
            first = int(params["startTime"]) // 1000
            rows = [[o * 1000, "1", "1", "1", repr(self.closes[o]), "1", o * 1000 + 59_999]
                    for o in sorted(self.closes) if o >= first][: int(params["limit"])]
            return httpx.Response(200, json=rows, request=request)
        if url == f"{GAMMA}/events":
            slug = params["slug"]
            meta = ({"priceToBeat": self.price_to_beat[slug]}
                    if slug in self.price_to_beat else {})
            return httpx.Response(200, json=[{"slug": slug, "eventMetadata": meta}],
                                  request=request)
        if url == f"{GAMMA}/markets":
            slug = params["slug"]
            rows = ([{"slug": slug, "conditionId": self.condition_ids[slug]}]
                    if slug in self.condition_ids else [])
            return httpx.Response(200, json=rows, request=request)
        if url.endswith("/book"):
            token = params["token_id"]
            if token in self.book_status:
                return httpx.Response(self.book_status[token], json={}, request=request)
            if token not in self.books:
                return httpx.Response(404, json={"error": "no book"}, request=request)
            return httpx.Response(200, json=self.books[token], request=request)
        raise AssertionError(f"unexpected request: {url} {params}")
