"""Test doubles for the venue's public reads: the taker trade tape, the CLOB market lookup and
the CLOB ``/book``. Replies are real ``httpx.Response`` objects shaped like the venue's, the
tape newest record first and paged by offset."""

from __future__ import annotations

import itertools
from typing import Any

import httpx

from ems.execution.tape import CLOB, DATA_API

_TX = itertools.count()


def trade(ts: int, outcome: str, side: str, size: float, price: float, *, cid: str,
          up: str, down: str) -> dict:
    """One taker record, shaped like the data-api's."""
    return {
        "timestamp": ts, "side": side, "size": size, "price": price,
        "asset": up if outcome == "Up" else down, "outcome": outcome,
        "outcomeIndex": 0 if outcome == "Up" else 1, "conditionId": cid,
        "proxyWallet": "0xtaker", "transactionHash": f"0xtx{next(_TX)}",
    }


class FakeVenue:
    """The data-api trade tape, the CLOB market lookup and CLOB ``/book``, in memory."""

    def __init__(self) -> None:
        self.tape: dict[str, list[dict]] = {}
        self.markets: dict[str, dict] = {}
        self.books: dict[str, dict] = {}
        self.tape_status: dict[str, int] = {}
        self.book_status: dict[str, int] = {}
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

    def _page(self, cid: str, offset: int, limit: int) -> list[dict]:
        records = sorted(self.tape.get(cid, []), key=lambda r: (-r["timestamp"], -r["_seq"]))
        return [{k: v for k, v in r.items() if k != "_seq"}
                for r in records[offset:offset + limit]]

    async def get(self, url: str, *, params: Any = None, headers: Any = None,
                  timeout: Any = None) -> httpx.Response:
        params = dict(params or {})
        self.calls.append((url, params))
        request = httpx.Request("GET", url)
        if url == f"{DATA_API}/trades":
            cid = params["market"]
            if cid in self.tape_status:
                return httpx.Response(self.tape_status[cid], json={}, request=request)
            page = self._page(cid, int(params["offset"]), int(params["limit"]))
            return httpx.Response(200, json=page, request=request)
        if url.startswith(f"{CLOB}/markets/"):
            cid = url.rsplit("/", 1)[1]
            if cid not in self.markets:
                return httpx.Response(404, json={"error": "not found"}, request=request)
            return httpx.Response(200, json=self.markets[cid], request=request)
        if url.endswith("/book"):
            token = params["token_id"]
            if token in self.book_status:
                return httpx.Response(self.book_status[token], json={}, request=request)
            if token not in self.books:
                return httpx.Response(404, json={"error": "no book"}, request=request)
            return httpx.Response(200, json=self.books[token], request=request)
        raise AssertionError(f"unexpected request: {url} {params}")
