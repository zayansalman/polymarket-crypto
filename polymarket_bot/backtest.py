"""Backtest and optimize the BTC 5-minute binary strategy on local history."""
from __future__ import annotations

import csv
import json
import re
import time
import urllib.parse
import urllib.request
from bisect import bisect_right
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from config import (
    BINANCE_API_BASE,
    HISTORY_CSV_PATH,
    PAPER_ENTRY_EDGE_MIN,
    PAPER_ENTRY_MIN_REMAINING_SECONDS,
    PAPER_MIN_CONFIDENCE,
    DATA_DIR,
)
from polymarket_bot.strategy import (
    StrategyParams,
    confidence_from_edge,
    fair_up_probability,
    notional_from_confidence,
    sigma_per_second,
)

BINANCE_API = BINANCE_API_BASE
EASTERN = ZoneInfo("America/New_York")


def _fetch_klines(params: str, attempts: int = 3) -> list[Any]:
    """Fetch klines with bounded retry; one transient timeout must not kill a grid run."""
    last_error: OSError | None = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(
                f"{BINANCE_API}/api/v3/klines?{params}", timeout=15
            ) as r:
                return json.loads(r.read())
        except OSError as exc:
            last_error = exc
            time.sleep(2 * (attempt + 1))
    raise last_error

MARKET_RE = re.compile(
    r"Bitcoin Up or Down - (?P<month>[A-Za-z]+) (?P<day>\d{1,2}), "
    r"(?P<start>\d{1,2}:\d{2}(?:AM|PM))-(?P<end>\d{1,2}:\d{2}(?:AM|PM)) ET"
)


@dataclass(frozen=True)
class MarketWindow:
    market_name: str
    start_ts: int
    end_ts: int
    start_et: str
    end_et: str


@dataclass(frozen=True)
class BuyOpportunity:
    market_name: str
    side: str
    trade_ts: int
    window_start_ts: int
    window_end_ts: int
    remaining_seconds: int
    entry_price: float
    actual_notional_usd: float
    actual_shares: float
    reference_price: float
    trade_spot_price: float
    settlement_price: float
    outcome: str
    fair_side_prob: float
    edge: float
    confidence: float
    settlement_pnl_usd: float


@dataclass(frozen=True)
class BacktestParams:
    entry_edge_min: float
    min_confidence: float
    min_remaining_seconds: int
    max_entry_price: float
    min_trade_usd: float = 1.0
    max_trade_usd: float = 5.0
    min_entry_price: float = 0.05


@dataclass(frozen=True)
class BacktestMetrics:
    name: str
    params: dict[str, Any]
    opportunities: int
    trades: int
    wins: int
    losses: int
    skipped: int
    total_notional_usd: float
    total_pnl_usd: float
    roi: float
    win_rate: float
    avg_pnl_usd: float
    max_drawdown_usd: float
    score: float


def parse_market_window(market_name: str, trade_ts: int) -> MarketWindow | None:
    match = MARKET_RE.search(market_name)
    if not match:
        return None
    trade_dt = datetime.fromtimestamp(trade_ts, timezone.utc).astimezone(EASTERN)
    year = trade_dt.year
    month = match.group("month")
    day = int(match.group("day"))
    start_label = match.group("start")
    end_label = match.group("end")

    start_dt = datetime.strptime(
        f"{month} {day} {year} {start_label}", "%B %d %Y %I:%M%p"
    ).replace(tzinfo=EASTERN)
    end_dt = datetime.strptime(
        f"{month} {day} {year} {end_label}", "%B %d %Y %I:%M%p"
    ).replace(tzinfo=EASTERN)
    if end_dt <= start_dt:
        end_dt += timedelta(days=1)
    return MarketWindow(
        market_name=market_name,
        start_ts=int(start_dt.timestamp()),
        end_ts=int(end_dt.timestamp()),
        start_et=start_dt.isoformat(),
        end_et=end_dt.isoformat(),
    )


class BinanceWindowCache:
    """Fetch and cache 1-second BTCUSDT closes by market window."""

    def __init__(self, cache_dir: Path | None = None) -> None:
        self.cache_dir = cache_dir or DATA_DIR / "backtests" / "binance_1s"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._memory: dict[tuple[int, int], dict[int, float]] = {}

    def closes_for_window(self, start_ts: int, end_ts: int) -> dict[int, float]:
        key = (start_ts, end_ts)
        if key in self._memory:
            return self._memory[key]
        path = self.cache_dir / f"btcusdt_1s_{start_ts}_{end_ts}.json"
        if path.exists():
            closes = {int(k): float(v) for k, v in json.loads(path.read_text()).items()}
            self._memory[key] = closes
            return closes

        params = urllib.parse.urlencode(
            {
                "symbol": "BTCUSDT",
                "interval": "1s",
                "startTime": (start_ts - 120) * 1000,
                "endTime": (end_ts + 2) * 1000,
                "limit": 1000,
            }
        )
        rows = _fetch_klines(params)
        closes = {int(row[0] // 1000): float(row[4]) for row in rows if len(row) > 4}
        path.write_text(json.dumps(closes, sort_keys=True), encoding="utf-8")
        self._memory[key] = closes
        return closes


def build_opportunities(
    history_path: Path | None = None,
    cache: BinanceWindowCache | None = None,
) -> list[BuyOpportunity]:
    csv_path = history_path or HISTORY_CSV_PATH
    if not csv_path.exists():
        return []
    cache = cache or BinanceWindowCache()
    opportunities: list[BuyOpportunity] = []

    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        rows = sorted(csv.DictReader(f), key=lambda r: int(r.get("timestamp") or 0))

    for row in rows:
        if row.get("action", "").lower() != "buy":
            continue
        market_name = row.get("marketName") or ""
        if "Bitcoin Up or Down" not in market_name:
            continue
        side = (row.get("tokenName") or "").strip()
        if side not in {"Up", "Down"}:
            continue
        trade_ts = int(row.get("timestamp") or 0)
        window = parse_market_window(market_name, trade_ts)
        if window is None:
            continue
        actual_notional = _as_float(row.get("usdcAmount"))
        actual_shares = _as_float(row.get("tokenAmount"))
        if actual_notional <= 0 or actual_shares <= 0:
            continue

        closes = cache.closes_for_window(window.start_ts, window.end_ts)
        reference = _close_at(closes, window.start_ts)
        trade_spot = _close_at(closes, trade_ts)
        settlement = _close_at(closes, window.end_ts)
        recent = _recent_closes(closes, trade_ts, lookback_seconds=90)
        sigma = sigma_per_second(recent)
        remaining = max(0, window.end_ts - trade_ts)
        fair_up = fair_up_probability(trade_spot, reference, sigma, remaining)
        fair_side = fair_up if side == "Up" else 1 - fair_up
        entry_price = actual_notional / actual_shares
        edge = fair_side - entry_price
        confidence = confidence_from_edge(edge)
        outcome = "Up" if settlement >= reference else "Down"
        settle_value = 1.0 if side == outcome else 0.0
        settlement_pnl = actual_shares * (settle_value - entry_price)

        opportunities.append(
            BuyOpportunity(
                market_name=market_name,
                side=side,
                trade_ts=trade_ts,
                window_start_ts=window.start_ts,
                window_end_ts=window.end_ts,
                remaining_seconds=remaining,
                entry_price=entry_price,
                actual_notional_usd=actual_notional,
                actual_shares=actual_shares,
                reference_price=reference,
                trade_spot_price=trade_spot,
                settlement_price=settlement,
                outcome=outcome,
                fair_side_prob=fair_side,
                edge=edge,
                confidence=confidence,
                settlement_pnl_usd=settlement_pnl,
            )
        )
    return opportunities


def evaluate_all_buys(opportunities: list[BuyOpportunity]) -> BacktestMetrics:
    return _metrics_from_trades(
        name="all_historical_buys_hold_to_resolution",
        params={"source": "actual_buys", "sizing": "actual_notional"},
        opportunities=opportunities,
        trade_pnls=[
            (opp.actual_notional_usd, opp.settlement_pnl_usd, opp.settlement_pnl_usd > 0)
            for opp in opportunities
        ],
    )


def evaluate_params(
    opportunities: list[BuyOpportunity],
    params: BacktestParams,
    name: str = "strategy_filter",
) -> BacktestMetrics:
    strategy_params = StrategyParams(
        min_trade_usd=params.min_trade_usd,
        max_trade_usd=params.max_trade_usd,
        entry_edge_min=params.entry_edge_min,
        min_confidence=params.min_confidence,
        entry_min_remaining_seconds=params.min_remaining_seconds,
        max_entry_price=params.max_entry_price,
        min_entry_price=params.min_entry_price,
    )
    trade_pnls: list[tuple[float, float, bool]] = []
    for opp in opportunities:
        if not _accepts(opp, params):
            continue
        notional = notional_from_confidence(opp.confidence, strategy_params)
        if notional <= 0:
            continue
        shares = notional / opp.entry_price
        settle_value = 1.0 if opp.side == opp.outcome else 0.0
        pnl = shares * (settle_value - opp.entry_price)
        trade_pnls.append((notional, pnl, pnl > 0))
    return _metrics_from_trades(
        name=name,
        params=asdict(params),
        opportunities=opportunities,
        trade_pnls=trade_pnls,
    )


def optimize_params(
    opportunities: list[BuyOpportunity],
    min_trades: int | None = None,
) -> list[BacktestMetrics]:
    if min_trades is None:
        min_trades = max(8, min(25, int(len(opportunities) * 0.08)))
    results: list[BacktestMetrics] = []
    for edge in [0.00, 0.02, 0.04, 0.045, 0.06, 0.08, 0.10, 0.12]:
        for conf in [0.50, 0.55, 0.60, 0.62, 0.65, 0.70, 0.75]:
            for remaining in [60, 90, 120, 150, 180, 210]:
                for max_entry in [0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.90, 0.95]:
                    params = BacktestParams(
                        entry_edge_min=edge,
                        min_confidence=conf,
                        min_remaining_seconds=remaining,
                        max_entry_price=max_entry,
                    )
                    metrics = evaluate_params(opportunities, params)
                    if metrics.trades >= min_trades:
                        results.append(metrics)
    return sorted(results, key=lambda m: (m.score, m.total_pnl_usd, m.trades), reverse=True)


def build_report(history_path: Path | None = None) -> dict[str, Any]:
    opportunities = build_opportunities(history_path)
    baseline = evaluate_all_buys(opportunities)
    current = evaluate_params(
        opportunities,
        BacktestParams(
            entry_edge_min=PAPER_ENTRY_EDGE_MIN,
            min_confidence=PAPER_MIN_CONFIDENCE,
            min_remaining_seconds=PAPER_ENTRY_MIN_REMAINING_SECONDS,
            max_entry_price=0.95,
        ),
        name="current_default_filter",
    )
    optimized = optimize_params(opportunities)
    recommended = _select_recommended(optimized) or current
    return {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "history_path": str(history_path or HISTORY_CSV_PATH),
        "method": (
            "Trade-history conditional backtest. It replays only historical BTC buys "
            "from the exported Polymarket CSV, enriches each buy with Binance 1s "
            "spot/reference data, and evaluates hold-to-resolution settlement PnL. "
            "It cannot measure opportunities that were never traded or full CLOB fill quality."
        ),
        "opportunities": len(opportunities),
        "baseline": asdict(baseline),
        "current": asdict(current),
        "recommended": asdict(recommended),
        "best": asdict(recommended),
        "best_score_unconstrained": asdict(optimized[0]) if optimized else asdict(current),
        "top_results": [asdict(m) for m in optimized[:10]],
        "edge_distribution": _edge_distribution(opportunities),
    }


def save_report(report: dict[str, Any], path: Path | None = None) -> Path:
    out = path or DATA_DIR / "backtests" / "latest.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return out


def format_report(report: dict[str, Any]) -> str:
    baseline = report["baseline"]
    current = report["current"]
    best = report["best"]
    lines = [
        "# BTC 5m Binary Pricing Model Backtest",
        "",
        f"- Opportunities: {report['opportunities']}",
        f"- Method: {report['method']}",
        "",
        "## Results",
        _metric_line("All historical buys", baseline),
        _metric_line("Current defaults", current),
        _metric_line("Optimized filter", best),
        "",
        "## Optimized Parameters",
    ]
    for key, value in best["params"].items():
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## Edge Distribution"])
    for key, value in report["edge_distribution"].items():
        lines.append(f"- {key}: {value}")
    return "\n".join(lines)


def _accepts(opp: BuyOpportunity, params: BacktestParams) -> bool:
    return (
        opp.edge >= params.entry_edge_min
        and opp.confidence >= params.min_confidence
        and opp.remaining_seconds > params.min_remaining_seconds
        and params.min_entry_price <= opp.entry_price <= params.max_entry_price
    )


def _select_recommended(results: list[BacktestMetrics]) -> BacktestMetrics | None:
    """Prefer a disciplined edge floor over the unconstrained max-PnL corner."""
    eligible = [
        m
        for m in results
        if m.params.get("entry_edge_min", 0) >= PAPER_ENTRY_EDGE_MIN
        and m.params.get("min_remaining_seconds", 0) >= 60
    ]
    return eligible[0] if eligible else (results[0] if results else None)


def _metrics_from_trades(
    name: str,
    params: dict[str, Any],
    opportunities: list[BuyOpportunity],
    trade_pnls: list[tuple[float, float, bool]],
) -> BacktestMetrics:
    total_notional = sum(x[0] for x in trade_pnls)
    total_pnl = sum(x[1] for x in trade_pnls)
    wins = sum(1 for _, _, won in trade_pnls if won)
    trades = len(trade_pnls)
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for _, pnl, _ in trade_pnls:
        equity += pnl
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    roi = total_pnl / total_notional if total_notional else 0.0
    win_rate = wins / trades if trades else 0.0
    avg_pnl = total_pnl / trades if trades else 0.0
    score = total_pnl - 0.5 * max_drawdown + 0.01 * trades
    return BacktestMetrics(
        name=name,
        params=params,
        opportunities=len(opportunities),
        trades=trades,
        wins=wins,
        losses=trades - wins,
        skipped=max(0, len(opportunities) - trades),
        total_notional_usd=round(total_notional, 4),
        total_pnl_usd=round(total_pnl, 4),
        roi=round(roi, 6),
        win_rate=round(win_rate, 6),
        avg_pnl_usd=round(avg_pnl, 6),
        max_drawdown_usd=round(max_drawdown, 4),
        score=round(score, 6),
    )


def _edge_distribution(opportunities: list[BuyOpportunity]) -> dict[str, Any]:
    if not opportunities:
        return {"count": 0}
    edges = sorted(opp.edge for opp in opportunities)
    return {
        "count": len(edges),
        "min": round(edges[0], 4),
        "p25": round(_quantile(edges, 0.25), 4),
        "median": round(_quantile(edges, 0.50), 4),
        "p75": round(_quantile(edges, 0.75), 4),
        "max": round(edges[-1], 4),
        "positive_edge_share": round(sum(1 for x in edges if x > 0) / len(edges), 4),
    }


def _metric_line(label: str, m: dict[str, Any]) -> str:
    return (
        f"- {label}: trades={m['trades']}, pnl=${m['total_pnl_usd']:+.2f}, "
        f"roi={m['roi']:.1%}, win={m['win_rate']:.1%}, max_dd=${m['max_drawdown_usd']:.2f}"
    )


def _recent_closes(closes: dict[int, float], trade_ts: int, lookback_seconds: int) -> list[float]:
    return [
        closes[ts]
        for ts in sorted(closes)
        if trade_ts - lookback_seconds < ts <= trade_ts
    ]


def _close_at(closes: dict[int, float], ts: int) -> float:
    if not closes:
        raise RuntimeError("No Binance closes available for market window.")
    seconds = sorted(closes)
    idx = bisect_right(seconds, ts) - 1
    if idx < 0:
        idx = 0
    return closes[seconds[idx]]


def _quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    idx = (len(values) - 1) * q
    lo = int(idx)
    hi = min(lo + 1, len(values) - 1)
    weight = idx - lo
    return values[lo] * (1 - weight) + values[hi] * weight


def _as_float(value: str | None) -> float:
    try:
        return float(value or 0)
    except ValueError:
        return 0.0
