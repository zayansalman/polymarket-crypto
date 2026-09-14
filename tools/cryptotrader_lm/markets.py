"""Resolved daily Up/Down markets for BTC and ETH from Gamma series listings.

Slugs are NOT built from dates: older events drop the year and reuse slugs
across years, so everything is keyed off the series listing and `endDate`.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime

import httpx

GAMMA_API = "https://gamma-api.polymarket.com"
SERIES_ID = {"btc": 41, "eth": 40}
# Resolved ETH events that the series listing omits.
EXTRA_EVENT_SLUGS = {
    "btc": [],
    "eth": [f"ethereum-up-or-down-on-april-{d}" for d in (6, 7, 8, 9)],
}
_OUTCOME = {("1", "0"): "up", ("0", "1"): "down", ("0.5", "0.5"): "tie"}


@dataclass(frozen=True)
class Market:
    asset: str
    market_date: str
    slug: str
    condition_id: str
    up_token: str
    down_token: str
    created_at: int
    outcome: str | None
    fee_rate: float
    fee_exponent: float


def _epoch(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def parse_event(asset: str, event: dict) -> Market | None:
    markets = event.get("markets") or []
    if len(markets) != 1:
        return None
    m = markets[0]
    if json.loads(m["outcomes"]) != ["Up", "Down"]:
        return None
    up_token, down_token = json.loads(m["clobTokenIds"])
    prices = tuple(json.loads(m["outcomePrices"])) if m.get("outcomePrices") else ()
    outcome = _OUTCOME.get(prices) if m.get("closed") else None
    rate, exponent = 0.0, 1.0
    if m.get("feesEnabled"):
        schedule = m.get("feeSchedule")
        if not schedule:
            raise ValueError(f"{m['slug']}: fees enabled but no feeSchedule")
        rate, exponent = float(schedule["rate"]), float(schedule["exponent"])
    return Market(
        asset=asset,
        market_date=m["endDate"][:10],
        slug=m["slug"],
        condition_id=m["conditionId"],
        up_token=up_token,
        down_token=down_token,
        created_at=_epoch(m["createdAt"]),
        outcome=outcome,
        fee_rate=rate,
        fee_exponent=exponent,
    )


def fetch_events(client: httpx.Client, asset: str) -> list[dict]:
    events: list[dict] = []
    offset = 0
    while True:
        resp = client.get(f"{GAMMA_API}/events", params={
            "series_id": SERIES_ID[asset], "order": "endDate", "ascending": "true",
            "limit": 100, "offset": offset,
        })
        resp.raise_for_status()
        page = resp.json()
        if not page:
            break
        events.extend(page)
        offset += len(page)
    for slug in EXTRA_EVENT_SLUGS[asset]:
        resp = client.get(f"{GAMMA_API}/events", params={"slug": slug})
        resp.raise_for_status()
        events.extend(resp.json())
    return events


def fetch_markets(client: httpx.Client, asset: str) -> list[Market]:
    by_date: dict[str, Market] = {}
    for event in fetch_events(client, asset):
        market = parse_event(asset, event)
        if market is not None:
            by_date.setdefault(market.market_date, market)
    return [by_date[d] for d in sorted(by_date)]


def to_dict(market: Market) -> dict:
    return asdict(market)
