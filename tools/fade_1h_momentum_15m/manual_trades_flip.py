"""What a wallet's recent manual trades made, against taking the other side of each.

The starting point of Fade 1h Momentum on 15m (Zayan, 2026-09-21): his own
trades since 2026-09-13 were mostly wrong, so would the opposite side have
been right? Each market is settled from the CLOB (Gamma drops 15m markets).
The flip buys the other outcome with the same dollars at 1 minus the price
paid, which ignores the spread on the other side.

    python3 tools/fade_1h_momentum_15m/manual_trades_flip.py --wallet 0x... --since 2026-09-13
"""
from __future__ import annotations

import argparse
import json
import urllib.request
from collections import defaultdict
from datetime import UTC, datetime

UA = {"User-Agent": "Mozilla/5.0"}


def _get(url: str):
    return json.load(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wallet", required=True)
    ap.add_argument("--since", default="2026-09-13")
    args = ap.parse_args()

    since = datetime.fromisoformat(args.since).replace(tzinfo=UTC).timestamp()
    acts = _get(f"https://data-api.polymarket.com/activity?user={args.wallet}&limit=500&offset=0")
    trades = sorted((a for a in acts if a["type"] == "TRADE" and a["timestamp"] >= since),
                    key=lambda a: a["timestamp"])

    per = defaultdict(lambda: {"buy_usdc": 0.0, "buy_sh": 0.0, "sell_usdc": 0.0, "sell_sh": 0.0})
    meta: dict[str, dict] = {}
    for t in trades:
        m = per[t["conditionId"]]
        side = "buy" if t["side"] == "BUY" else "sell"
        m[f"{side}_usdc"] += t["usdcSize"]
        m[f"{side}_sh"] += t["size"]
        meta.setdefault(t["conditionId"], t)

    rows = []
    for cid, m in per.items():
        market = _get(f"https://clob.polymarket.com/markets/{cid}")
        winner = next((tok["outcome"] for tok in market.get("tokens", []) if tok.get("winner")), None)
        if winner is None or m["buy_sh"] == 0:
            continue
        t = meta[cid]
        p = m["buy_usdc"] / m["buy_sh"]
        won = winner == t["outcome"]
        held = m["buy_sh"] - m["sell_sh"]
        actual = m["sell_usdc"] + (held if won else 0.0) - m["buy_usdc"]
        c = m["buy_usdc"]
        flipped = -c if won else c / (1 - p) - c
        rows.append((t["title"], t["outcome"], p, c, won, actual, flipped, "15m" in t["slug"]))
        print(f"{t['outcome']:4} @{p:.2f} ${c:5.2f} {'right' if won else 'wrong'} "
              f"actual {actual:+.2f} flipped {flipped:+.2f} | {t['title']}")

    def summary(sel, label):
        print(f"{label}: n={len(sel)} right={sum(r[4] for r in sel)} staked=${sum(r[3] for r in sel):.2f} "
              f"actual=${sum(r[5] for r in sel):+.2f} flipped=${sum(r[6] for r in sel):+.2f}")

    print()
    summary(rows, "all settled")
    summary([r for r in rows if r[7]], "15m only")
    summary([r for r in rows if r[7] and r[2] >= 0.40], "15m at >=40c")


if __name__ == "__main__":
    main()
