"""Collect and join every backtest input for the daily BTC/ETH Up/Down markets.

    python3 -m tools.cryptotrader_lm.collect markets
    python3 -m tools.cryptotrader_lm.collect prices     # CLOB mids, Binance 5m klines
    python3 -m tools.cryptotrader_lm.collect trades     # Polymarket taker trades
    python3 -m tools.cryptotrader_lm.collect news       # crypto-outlet news archives
    python3 -m tools.cryptotrader_lm.collect build      # -> inputs.jsonl

Every fetch is cached on disk (per market, or per news site and month), so steps resume.
"""
from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx

from tools.cryptotrader_lm import binance, markets, news, orderflow, pricing
from tools.cryptotrader_lm.timing import market_times

CLOB_HISTORY_URL = "https://clob.polymarket.com/prices-history"
DEFAULT_OUT = Path("data/cryptotrader_lm")
ASSETS = ("btc", "eth")
YOUNG_MARKET_S = 3 * 3_600
NEWS_KEPT = 20
MID_LOOKBACK_S = 6 * 3_600


def _client() -> httpx.Client:
    return httpx.Client(timeout=60.0, headers={"User-Agent": "polymarket-crypto-research"})


def _read(path: Path):
    return json.loads(path.read_text())


def _write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj))
    tmp.replace(path)


def load_markets(out: Path) -> list[markets.Market]:
    today = datetime.now(UTC).date().isoformat()
    rows = _read(out / "markets.json")
    return [markets.Market(**r) for r in rows if r["outcome"] and r["market_date"] < today]


def _key(m: markets.Market) -> str:
    return f"{m.asset}_{m.market_date}"


def cmd_markets(out: Path) -> None:
    with _client() as client:
        rows = [markets.to_dict(m) for a in ASSETS for m in markets.fetch_markets(client, a)]
    _write(out / "markets.json", rows)
    print(f"markets: {len(rows)} ({sum(1 for r in rows if r['outcome'])} resolved)")


def _fetch_mid_history(client: httpx.Client, m: markets.Market) -> list[dict]:
    t = market_times(date.fromisoformat(m.market_date))
    resp = client.get(CLOB_HISTORY_URL, params={
        "market": m.up_token, "startTs": t.t_dec - MID_LOOKBACK_S, "endTs": t.t_dec, "fidelity": 1,
    })
    resp.raise_for_status()
    return resp.json().get("history", [])


def cmd_prices(out: Path) -> None:
    todo = [m for m in load_markets(out) if not (out / "clob" / f"{_key(m)}.json").exists()]
    with _client() as client, ThreadPoolExecutor(6) as pool:
        def job(m):
            _write(out / "clob" / f"{_key(m)}.json", _fetch_mid_history(client, m))
        list(pool.map(job, todo))
    print(f"clob mids fetched: {len(todo)}")
    ms = load_markets(out)
    with _client() as client:
        for asset in ASSETS:
            t_decs = [market_times(date.fromisoformat(m.market_date)).t_dec for m in ms if m.asset == asset]
            start = min(t_decs) - 32 * binance.DAY_S
            rows = binance.fetch_klines(client, binance.SYMBOL[asset], start, max(t_decs))
            _write(out / f"klines_{asset}.json", rows)
            print(f"klines {asset}: {len(rows)}")


def cmd_trades(out: Path) -> None:
    todo = [m for m in load_markets(out) if not (out / "trades" / f"{_key(m)}.json").exists()]
    with _client() as client, ThreadPoolExecutor(3) as pool:
        def job(m):
            t = market_times(date.fromisoformat(m.market_date))
            trades = orderflow.fetch_taker_trades(client, m.condition_id, m.created_at, t.t_dec)
            _write(out / "trades" / f"{_key(m)}.json", trades)
        list(pool.map(job, todo))
    print(f"trades fetched: {len(todo)}")


def _months(first: date, last: date) -> list[tuple[int, int]]:
    months, y, m = [], first.year, first.month
    while (y, m) <= (last.year, last.month):
        months.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return months


def _news_window(out: Path) -> tuple[date, date]:
    t_decs = [market_times(date.fromisoformat(m.market_date)).t_dec for m in load_markets(out)]
    first = datetime.fromtimestamp(min(t_decs) - 2 * binance.DAY_S, UTC).date()
    return first, datetime.fromtimestamp(max(t_decs), UTC).date()


def cmd_news(out: Path) -> None:
    first, last = _news_window(out)
    months = _months(first, last)
    this_month = (datetime.now(UTC).year, datetime.now(UTC).month)

    def job(site: str) -> int:
        fetched = 0
        with _client() as client:
            for y, m in months:
                path = out / "news_wp" / site / f"{y}-{m:02d}.json"
                if path.exists() and (y, m) != this_month:
                    continue
                _write(path, news.fetch_month(client, site, y, m))
                fetched += 1
        print(f"news {site}: {fetched} months fetched", flush=True)
        return fetched

    with ThreadPoolExecutor(len(news.SITES)) as pool:
        list(pool.map(job, news.SITES))


def load_archive(out: Path) -> news.Archive:
    posts = [p for path in sorted((out / "news_wp").glob("*/*.json")) for p in _read(path)]
    return news.Archive(posts)


def build_row(m: markets.Market, history: list[dict], candles: binance.Candles,
              trades: list[dict], archive: news.Archive) -> dict:
    t = market_times(date.fromisoformat(m.market_date))
    mid = pricing.entry_mid(history, t.t_dec)
    skip = None
    if m.created_at > t.t_dec:
        skip = "created_after_decision"
    elif mid is None:
        skip = "no_recent_mid"
    return {
        "asset": m.asset,
        "market_date": m.market_date,
        "decision_date_et": (date.fromisoformat(m.market_date) - timedelta(days=1)).isoformat(),
        "slug": m.slug,
        "t_dec": t.t_dec,
        "outcome": m.outcome,
        "fee_rate": m.fee_rate,
        "fee_exponent": m.fee_exponent,
        "up_mid": mid,
        "skip_reason": skip,
        "market_age_s": t.t_dec - m.created_at,
        "young_market": t.t_dec - m.created_at < YOUNG_MARKET_S,
        **binance.price_features(candles, t.t_dec),
        "pm_flow_3h": orderflow.up_pressure(trades, t.t_dec - 3 * 3_600, t.t_dec),
        "pm_flow_all": orderflow.up_pressure(trades, m.created_at, t.t_dec),
        "news": news.select_news(archive, m.asset, t.t_dec, NEWS_KEPT),
    }


def cmd_build(out: Path) -> None:
    ms = load_markets(out)
    candles = {a: binance.Candles(_read(out / f"klines_{a}.json")) for a in ASSETS}
    archive = load_archive(out)
    rows = [build_row(m, _read(out / "clob" / f"{_key(m)}.json"), candles[m.asset],
                      _read(out / "trades" / f"{_key(m)}.json"), archive) for m in ms]
    with (out / "inputs.jsonl").open("w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    usable = sum(1 for r in rows if r["skip_reason"] is None)
    thin = sum(1 for r in rows if len(r["news"]) < 5)
    print(f"inputs: {len(rows)} rows, {usable} with a tradable entry price, "
          f"{len(archive.posts)} news posts, {thin} rows with fewer than 5 news items")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("step", choices=["markets", "prices", "trades", "news", "build"])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    {"markets": cmd_markets, "prices": cmd_prices, "trades": cmd_trades,
     "news": cmd_news, "build": cmd_build}[args.step](args.out)


if __name__ == "__main__":
    main()
