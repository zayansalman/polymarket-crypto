"""Score CryptoTrader-LM decisions against resolved daily markets, split by order flow.

    python3 -m tools.cryptotrader_lm.report --run full

buy -> $10 of Up, sell -> $10 of Down, hold/unparsed -> no position. Entry is the
taker price from the pre-decision CLOB mid, net of that market's fee schedule.
Order-flow splits compare the model's direction with the sign of aggressive flow
measured before the decision (Binance spot taker imbalance, Polymarket taker flow).
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from tools.cryptotrader_lm import pricing
from tools.cryptotrader_lm.collect import DEFAULT_OUT

SIDE = {"buy": "up", "sell": "down"}
FLOW_SIGNALS = {
    "binance 1h": lambda r: r["bn_imbalance_1h"],
    "binance 4h": lambda r: r["bn_imbalance_4h"],
    "binance 24h": lambda r: r["bn_imbalance_24h"],
    "polymarket 3h": lambda r: r["pm_flow_3h"]["imbalance"],
    "polymarket since open": lambda r: r["pm_flow_all"]["imbalance"],
}


def _sign_side(value: float | None) -> str | None:
    if value is None or value == 0:
        return None
    return "up" if value > 0 else "down"


def score(pairs: list[tuple[dict, str]]) -> dict:
    """pairs = (input row, side). Hit rate excludes ties; PnL includes fees."""
    wins = losses = ties = 0
    pnl = fees = 0.0
    for row, side in pairs:
        s = pricing.settle(side, row["up_mid"], row["outcome"], row["fee_rate"], row["fee_exponent"])
        pnl += s.pnl_usd
        fees += s.fee_usd
        if s.won is None:
            ties += 1
        elif s.won:
            wins += 1
        else:
            losses += 1
    decided = wins + losses
    return {"trades": len(pairs), "hit_rate": round(wins / decided, 4) if decided else None,
            "wins": wins, "losses": losses, "ties": ties, "pnl_usd": round(pnl, 2),
            "fees_usd": round(fees, 2)}


def fee_period(row: dict) -> str:
    if row["fee_rate"] == 0:
        return "no fees"
    return "fee 0.25*(p(1-p))^2" if row["fee_exponent"] == 2 else "fee 0.07*p(1-p)"


def summarize(inputs: dict, decisions: list[dict]) -> dict:
    joined = [(inputs[(d["asset"], d["market_date"])], d) for d in decisions]
    usable = [(r, d) for r, d in joined if r["skip_reason"] is None]
    trades = [(r, SIDE[d["decision"]]) for r, d in usable if d["decision"] in SIDE]
    out = {
        "rows": len(joined),
        "skipped_no_price": len(joined) - len(usable),
        "decisions": dict(Counter(str(d["decision"]) for _, d in usable)),
        "parse": dict(Counter(d["parse"] for _, d in usable)),
        "finish": dict(Counter(str(d["finish_reason"]) for _, d in usable)),
        "avg_output_tokens": round(sum(d["output_tokens"] for _, d in usable) / max(len(usable), 1)),
        "all": score(trades),
        "by_asset": {a: score([(r, s) for r, s in trades if r["asset"] == a]) for a in ("btc", "eth")},
        "by_side": {s: score([(r, x) for r, x in trades if x == s]) for s in ("up", "down")},
        "by_fee_period": {p: score([(r, s) for r, s in trades if fee_period(r) == p])
                          for p in sorted({fee_period(r) for r, _ in trades})},
        "young_markets": score([(r, s) for r, s in trades if r["young_market"]]),
        "order_flow": {},
    }
    for name, get in FLOW_SIGNALS.items():
        agree, disagree, missing = [], [], []
        for r, s in trades:
            flow_side = _sign_side(get(r))
            (missing if flow_side is None else agree if flow_side == s else disagree).append((r, s))
        out["order_flow"][name] = {"agrees": score(agree), "disagrees": score(disagree),
                                   "no_flow": score(missing)}
    return out


def reference_rules(rows: list[dict]) -> dict:
    """Simple rules on the same tradable days, for context next to the model."""
    usable = [r for r in rows if r["skip_reason"] is None]
    refs = {"always up": score([(r, "up") for r in usable]),
            "3-day momentum": score([(r, _sign_side(r["ret_3d"])) for r in usable
                                     if _sign_side(r["ret_3d"])])}
    for name, get in FLOW_SIGNALS.items():
        refs[f"follow {name} flow"] = score([(r, _sign_side(get(r))) for r in usable
                                             if _sign_side(get(r))])
    return refs


def _line(label: str, s: dict) -> str:
    hr = "-" if s["hit_rate"] is None else f"{100 * s['hit_rate']:.1f}%"
    return f"| {label} | {s['trades']} | {hr} | {s['pnl_usd']:+.2f} | {s['fees_usd']:.2f} |"


def render(name: str, summary: dict) -> str:
    head = "| slice | trades | hit rate | PnL $ | fees $ |\n|---|---|---|---|---|"
    lines = [f"## {name}",
             f"rows {summary['rows']}, no price {summary['skipped_no_price']}, "
             f"decisions {summary['decisions']}, parse {summary['parse']}, "
             f"finish {summary['finish']}, avg output tokens {summary['avg_output_tokens']}",
             head, _line("all", summary["all"])]
    lines += [_line(a, s) for a, s in summary["by_asset"].items()]
    lines += [_line(f"buy {s}", v) for s, v in summary["by_side"].items()]
    lines += [_line(p, s) for p, s in summary["by_fee_period"].items()]
    lines.append(_line("young markets (<3h old)", summary["young_markets"]))
    for flow, buckets in summary["order_flow"].items():
        lines += [_line(f"{flow}: {b.replace('_', ' ')}", s) for b, s in buckets.items()]
    return "\n".join(lines)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--run", default="full")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = p.parse_args()
    rows = [json.loads(x) for x in (args.out / "inputs.jsonl").read_text().splitlines()]
    inputs = {(r["asset"], r["market_date"]): r for r in rows}
    run_dir = args.out / "runs" / args.run
    report = {"reference": reference_rules(rows), "runs": {}}
    parts = ["| slice | trades | hit rate | PnL $ | fees $ |\n|---|---|---|---|---|"]
    parts += [_line(k, v) for k, v in report["reference"].items()]
    parts = ["## reference rules (same days)", "\n".join(parts)]
    for path in sorted(run_dir.glob("*.jsonl")):
        decisions = [json.loads(x) for x in path.read_text().splitlines()]
        report["runs"][path.stem] = summarize(inputs, decisions)
        parts.append(render(path.stem, report["runs"][path.stem]))
    (run_dir / "report.json").write_text(json.dumps(report, indent=2))
    print("\n\n".join(parts))


if __name__ == "__main__":
    main()
