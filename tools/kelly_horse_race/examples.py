"""Worked examples of Kelly horse-race from real BTC 15m windows, through the real maths.

For each of the last N resolved windows it rebuilds the decision the strategy makes at the
open: ``K`` is the window's ``priceToBeat`` (Gamma), ``X = K`` at the open, ``r60`` and
``sigma_h`` come from the 61 one-minute Binance BTCUSDT candles closing at or before the open,
and ``tau = 0.25`` h. It runs them through ``ems.kelly_horse_race.maths`` and sets the chance of
Up against how the window settled, then scores the chances (Brier score, against always 0.5)
and writes the cards and the scores as markdown.

It reads only (Gamma and Binance public endpoints) and writes only the markdown file. The
book at the open is not in any public history, so the price and size part of a decision is
not rebuilt here; ``docs/strategies/kelly_horse_race.md`` works it on hand-picked numbers.

    python tools/kelly_horse_race/examples.py --windows 96 --cards 5
    python tools/kelly_horse_race/examples.py --out docs/strategies/examples/kelly_horse_race.md
"""

from __future__ import annotations

import argparse
import json
import math
import time
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ems.kelly_horse_race import maths

GAMMA = "https://gamma-api.polymarket.com"
BINANCE = "https://data-api.binance.vision"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "Chrome/128.0 Safari/537.36", "Accept": "application/json"}
STEP = 900
OUT = Path("data/kelly_horse_race/worked_examples.md")


def _get(url: str, params: Mapping[str, Any]) -> Any:
    for attempt in range(3):
        try:
            req = urllib.request.Request(f"{url}?{urllib.parse.urlencode(params)}", headers=UA)
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.load(resp)
        except Exception:  # retry transient HTTP errors
            if attempt == 2:
                raise
            time.sleep(1 + attempt)
    return None


def gamma_window(start: int) -> tuple[float | None, str | None]:
    """(priceToBeat, winner) of ``btc-updown-15m-<start>``; winner None until it resolves."""
    rows = _get(f"{GAMMA}/events", {"slug": f"btc-updown-15m-{start}"}) or []
    if not rows:
        return None, None
    event = rows[0]
    ptb = (event.get("eventMetadata") or {}).get("priceToBeat")
    market = (event.get("markets") or [{}])[0]
    prices = [float(x) for x in json.loads(market.get("outcomePrices") or "[]")]
    outcomes = json.loads(market.get("outcomes") or "[]")
    winner = (outcomes[prices.index(1.0)]
              if market.get("closed") and 1.0 in prices and outcomes else None)
    return (float(ptb) if ptb is not None else None), winner


def minute_closes(start: int) -> dict[int, float]:
    """Binance BTCUSDT 1-minute closes of the 61 candles closing at or before ``start``."""
    first = start - 61 * 60
    rows = _get(f"{BINANCE}/api/v3/klines", {"symbol": "BTCUSDT", "interval": "1m",
                                             "startTime": first * 1000, "limit": 61}) or []
    return {int(r[0]) // 1000: float(r[4]) for r in rows}


def window_row(start: int, k: float, winner: str, closes: Mapping[int, float]) -> dict:
    """The decision at the open of one window, and how it settled."""
    opens = [start - 61 * 60 + 60 * i for i in range(61)]
    series = [closes[o] for o in opens]
    returns = [math.log(series[i] / series[i - 1]) for i in range(1, 61)]
    r60, sigma_h = maths.hour_moves(returns)
    chance = maths.chance_of_up(k, k, r60, sigma_h, 0.25)
    return {"start": start, "k": k, "r60": r60, "sigma_h": sigma_h, "z": chance.z,
            "p_up": chance.p_up, "winner": winner}


def brier(rows: Sequence[Mapping[str, Any]], key: str = "p_up") -> float:
    return sum((float(r[key]) - (1.0 if r["winner"] == "Up" else 0.0)) ** 2
               for r in rows) / len(rows)


def _clock(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M UTC")


def card(row: Mapping[str, Any]) -> str:
    p = row["p_up"]
    z = row["z"]
    lean = "Up" if p > 0.5 else "Down" if p < 0.5 else "neither side"
    return "\n".join([
        f"### btc-updown-15m-{row['start']} ({_clock(row['start'])})",
        "",
        "| factor | value |",
        "|---|---|",
        f"| Price to beat $K$ (Gamma priceToBeat) | {row['k']:,.2f} |",
        "| Price now $X$ | $= K$ at the open |",
        f"| Hour drift $r_{{60}}$ | {row['r60']:+.5f} |",
        f"| Hour volatility $\\sigma_h$ | {row['sigma_h']:.5f} |",
        "| Time left $\\tau$ | 0.25 h |",
        f"| $z = r_{{60}}\\sqrt{{\\tau}}/\\sigma_h$ | {z:+.3f} |" if z is not None else
        "| $z$ | no volatility |",
        f"| $P(\\text{{Up}})$ | {p:.3f} |",
        f"| Settled | {row['winner']} |",
        "",
        f"The last hour {'rose' if row['r60'] > 0 else 'fell' if row['r60'] < 0 else 'was flat'}"
        f" {abs(row['r60']) * 100:.3f}% against {row['sigma_h'] * 100:.3f}% of volatility, so the "
        f"maths leans {lean}: the die picks Up with chance {p:.1%} and Down with "
        f"{1 - p:.1%}. The window settled {row['winner']}.",
        "",
    ])


def report(rows: Sequence[Mapping[str, Any]], cards: int) -> str:
    n = len(rows)
    ups = sum(1 for r in rows if r["winner"] == "Up")
    b = brier(rows)
    side_right = sum(1 for r in rows if (r["p_up"] > 0.5) == (r["winner"] == "Up")
                     and r["p_up"] != 0.5)
    expected_die = sum(r["p_up"] if r["winner"] == "Up" else 1 - r["p_up"] for r in rows)
    buckets = [(0.0, 0.4), (0.4, 0.45), (0.45, 0.5), (0.5, 0.55), (0.55, 0.6), (0.6, 1.01)]
    lines = [
        "# Kelly horse-race: worked examples from real windows",
        "",
        f"Generated {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')} by "
        "`tools/kelly_horse_race/examples.py` from Gamma priceToBeat and results and Binance "
        f"BTCUSDT 1-minute candles: {n} resolved BTC 15m windows, "
        f"{_clock(rows[0]['start'])} to {_clock(rows[-1]['start'])}.",
        "",
        "Each is the decision at the open ($X = K$, $\\tau = 0.25$ h). The book at the open is "
        "not in any public history, so the price and size are not rebuilt.",
        "",
        "## Scores",
        "",
        "| measure | value |",
        "|---|---|",
        f"| Windows settled Up | {ups} of {n} ({ups / n:.1%}) |",
        f"| Brier score of $P(\\text{{Up}})$ | {b:.4f} |",
        f"| Brier score of always 0.5 | {brier([{**r, 'c': 0.5} for r in rows], 'c'):.4f} |",
        f"| The likelier side won | {side_right} of {n} ({side_right / n:.1%}) |",
        f"| The die's side, expected wins | {expected_die:.1f} of {n} "
        f"({expected_die / n:.1%}) |",
        "",
        "| $P(\\text{Up})$ | windows | settled Up |",
        "|---|---|---|",
    ]
    for low, high in buckets:
        inside = [r for r in rows if low <= r["p_up"] < high]
        if inside:
            up = sum(1 for r in inside if r["winner"] == "Up")
            lines.append(f"| {low:.2f} to {min(high, 1.0):.2f} | {len(inside)} | "
                         f"{up} ({up / len(inside):.0%}) |")
    lines += ["", "## Cards", ""]
    picks = sorted(rows, key=lambda r: -abs(r["p_up"] - 0.5))[:cards]
    lines += [card(r) for r in sorted(picks, key=lambda r: r["start"])]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--windows", type=int, default=96, help="resolved windows to read")
    parser.add_argument("--cards", type=int, default=5, help="worked cards to write")
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args(argv)
    end = (int(time.time()) // STEP) * STEP - 2 * STEP
    rows = []
    for start in range(end - (args.windows - 1) * STEP, end + STEP, STEP):
        k, winner = gamma_window(start)
        if k is None or winner is None:
            continue
        closes = minute_closes(start)
        if len(closes) < 61:
            continue
        rows.append(window_row(start, k, winner, closes))
        time.sleep(0.2)
    if not rows:
        print("no resolved windows with a priceToBeat and 61 candles were found")
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(report(rows, args.cards), encoding="utf-8")
    print(f"{len(rows)} windows -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
