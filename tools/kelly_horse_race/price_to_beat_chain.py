"""Check how Polymarket's BTC 15m Up/Down markets settle (research, 2026-09-22).

The rules say Up wins if the Chainlink BTC/USD TWAP "of the time range" is >= the price at
the start. Two readings: (a) the 60 s TWAP print at the close vs the print at the open, or
(b) an average over the whole 15 minutes. Under (a) the next window's ``priceToBeat`` is this
window's settle price, so every outcome equals ``priceToBeat(N+1) >= priceToBeat(N)``.
This walks back over consecutive resolved windows on the Gamma API and counts matches.

    python tools/kelly_horse_race/price_to_beat_chain.py 900 120 15m
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "Chrome/128.0 Safari/537.36"}


def event(slug: str) -> dict | None:
    for _ in range(3):
        try:
            req = urllib.request.Request(
                f"https://gamma-api.polymarket.com/events?slug={slug}", headers=UA)
            data = json.load(urllib.request.urlopen(req, timeout=15))
            return data[0] if data else None
        except Exception:  # retry transient HTTP errors
            time.sleep(1)
    return None


def main(step: int, n: int, family: str) -> None:
    end = (int(time.time()) // step) * step - 2 * step
    rows = []
    for start in range(end - n * step, end + step, step):
        e = event(f"btc-updown-{family}-{start}")
        if not e:
            rows.append((start, None, None))
            continue
        ptb = (e.get("eventMetadata") or {}).get("priceToBeat")
        m = e["markets"][0]
        prices = [float(x) for x in json.loads(m.get("outcomePrices") or "[]")]
        outcomes = json.loads(m.get("outcomes") or "[]")
        winner = outcomes[prices.index(1.0)] if m.get("closed") and 1.0 in prices else None
        rows.append((start, ptb, winner))
        time.sleep(0.25)
    match = mismatch = 0
    for (start, ptb, winner), (_, ptb_next, _) in zip(rows, rows[1:]):
        if None in (ptb, ptb_next, winner):
            continue
        if ("Up" if ptb_next >= ptb else "Down") == winner:
            match += 1
        else:
            mismatch += 1
            print("mismatch", start, ptb, ptb_next, winner)
    print(f"{family}: {len(rows)} windows, {match} match, {mismatch} mismatch")


if __name__ == "__main__":
    main(int(sys.argv[1]), int(sys.argv[2]), sys.argv[3])
