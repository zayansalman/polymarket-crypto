"""Measure what the Polymarket perpetuals venue actually charges and pays.

Read-only. Hits only public endpoints on api.perpetuals.polymarket.com, plus
Kraken's public AssetPairs list to see which perp legs can be hedged with spot.

Writes one JSON blob so the write-up can be regenerated without re-hitting the
API:  python -m tools.perps_research.survey_perps --out data/perps_research/survey.json
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

PERPS = "https://api.perpetuals.polymarket.com"
KRAKEN = "https://api.kraken.com/0/public"
UA = {"User-Agent": "polymarket-crypto-research/1.0"}

# The fixed interest leg is 0.01% per 8h, scaled 1.0 for crypto and 0.5 for the
# rest, so a perp trading exactly at index still pays this much per hour.
BASELINE_HOURLY = {"crypto": 0.0001 / 8, "other": 0.5 * 0.0001 / 8}

HOURS_PER_YEAR = 24 * 365


def _get(base: str, path: str, **params):
    url = base + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except Exception:
            if attempt == 3:
                raise
            time.sleep(1.5 * (attempt + 1))


def instruments() -> list[dict]:
    return _get(PERPS, "/v1/info/instruments")


def tickers() -> dict[int, dict]:
    return {t["instrument_id"]: t for t in _get(PERPS, "/v1/info/tickers")}


def funding_history(iid: int, max_pages: int = 40) -> list[dict]:
    """Page backwards from now until the venue stops returning rows."""
    rows: list[dict] = []
    end = None
    for _ in range(max_pages):
        params = {"instrument_id": iid}
        if end is not None:
            params["end_timestamp"] = end
        page = _get(PERPS, "/v1/info/funding", **params)
        data = page["data"] if isinstance(page, dict) else page
        if not data:
            break
        rows.extend(data)
        oldest = min(r["timestamp"] for r in data)
        if end is not None and oldest >= end:
            break
        end = oldest - 1
        if not (isinstance(page, dict) and page.get("more")):
            break
        time.sleep(0.15)
    seen: dict[int, dict] = {r["timestamp"]: r for r in rows}
    return sorted(seen.values(), key=lambda r: r["timestamp"])


def book_cost(iid: int, depth: int = 100) -> dict:
    """Top-of-book spread and how much notional sits inside 5 and 10 bps."""
    b = _get(PERPS, "/v1/info/book", instrument_id=iid, depth=depth)
    bids = [(float(p), float(q)) for p, q in b.get("bids", [])]
    asks = [(float(p), float(q)) for p, q in b.get("asks", [])]
    if not bids or not asks:
        return {"instrument_id": iid, "empty": True}
    best_bid, best_ask = bids[0][0], asks[0][0]
    mid = (best_bid + best_ask) / 2
    out = {
        "instrument_id": iid,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid": mid,
        "spread_bps": (best_ask - best_bid) / mid * 1e4,
        "levels_bid": len(bids),
        "levels_ask": len(asks),
    }
    for bps in (5, 10, 25):
        lo, hi = mid * (1 - bps / 1e4), mid * (1 + bps / 1e4)
        out[f"bid_notional_{bps}bps"] = sum(p * q for p, q in bids if p >= lo)
        out[f"ask_notional_{bps}bps"] = sum(p * q for p, q in asks if p <= hi)
    return out


def kraken_spot_bases() -> set[str]:
    """Base assets Kraken lists a USD spot pair for."""
    res = _get(KRAKEN, "/AssetPairs").get("result", {})
    bases = set()
    for pair in res.values():
        quote = pair.get("wsname", "/").split("/")[-1]
        if quote in ("USD", "USDT", "USDC", "ZUSD"):
            base = pair.get("wsname", "/").split("/")[0]
            bases.add(base.upper().lstrip("X").lstrip("Z") if len(base) > 4 else base.upper())
            bases.add(base.upper())
    return bases


def summarise_funding(rows: list[dict], category: str) -> dict:
    rates = [float(r["funding_rate"]) for r in rows]
    if not rates:
        return {"hours": 0}
    baseline = BASELINE_HOURLY["crypto" if category == "crypto" else "other"]
    mean = sum(rates) / len(rates)
    ordered = sorted(rates)
    median = ordered[len(ordered) // 2]
    at_floor = sum(1 for r in rates if abs(r - baseline) < 1e-9)
    return {
        "hours": len(rates),
        "first": datetime.fromtimestamp(min(r["timestamp"] for r in rows) / 1000, timezone.utc).isoformat(),
        "last": datetime.fromtimestamp(max(r["timestamp"] for r in rows) / 1000, timezone.utc).isoformat(),
        "baseline_hourly": baseline,
        "mean_hourly": mean,
        "median_hourly": median,
        "min_hourly": ordered[0],
        "max_hourly": ordered[-1],
        "mean_annualised_pct": mean * HOURS_PER_YEAR * 100,
        "baseline_annualised_pct": baseline * HOURS_PER_YEAR * 100,
        "pct_hours_at_zero_premium_floor": 100 * at_floor / len(rates),
        "pct_hours_above_floor": 100 * sum(1 for r in rates if r > baseline + 1e-9) / len(rates),
        "pct_hours_negative": 100 * sum(1 for r in rates if r < 0) / len(rates),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/perps_research/survey.json")
    ap.add_argument(
        "--books-for",
        default="BTC-USD,ETH-USD,SOL-USD,XRP-USD,DOGE-USD,GOLD-USD,SILVER-USD,"
        "WTIOIL-USD,SP500-USD,NAS100-USD,NVDA-USD,TSLA-USD",
        help="comma-separated symbols to pull order books for",
    )
    ap.add_argument("--funding-all", action="store_true", help="page funding for every instrument")
    args = ap.parse_args()

    insts = instruments()
    tick = tickers()
    by_symbol = {i["symbol"]: i for i in insts}
    wanted = [s.strip() for s in args.books_for.split(",") if s.strip()]

    out: dict = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "instrument_count": len(insts),
        "instruments": insts,
        "tickers": list(tick.values()),
        "books": {},
        "funding": {},
    }

    for sym in wanted:
        inst = by_symbol.get(sym)
        if not inst:
            continue
        out["books"][sym] = book_cost(inst["instrument_id"])
        time.sleep(0.2)

    funding_targets = insts if args.funding_all else [by_symbol[s] for s in wanted if s in by_symbol]
    for inst in funding_targets:
        rows = funding_history(inst["instrument_id"])
        out["funding"][inst["symbol"]] = summarise_funding(rows, inst["category"])
        time.sleep(0.2)

    try:
        out["kraken_usd_bases"] = sorted(kraken_spot_bases())
    except Exception as exc:  # Kraken outage must not lose the perps capture
        out["kraken_usd_bases_error"] = str(exc)

    path = Path(args.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=2))
    print(f"wrote {path} ({path.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
