# Venue Flow Feeds Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record one closed-hour trade-flow bar per venue feed (Binance spot BTCUSDT and ETHUSDT, Binance perp BTCUSDT, Binance liquidations, Kraken BTC/USD, Kraken Futures PF_XBTUSD) plus hourly perp state (funding, mark/index, open interest), always on, and show every feed on the FEEDS card.

**Architecture:**
- Pure types, hour aggregation and message parsers live in `polymarket_exec/connectors/`. A shared reconnecting WebSocket loop lives there too.
- An always-on `FlowRecorder` in `polymarket_exec/ops/` is started from the dashboard lifespan next to PR #231's feed monitor. Once a minute it writes closed REST bars, once an hour it writes perp snapshots, and it rolls the WS trade aggregators into bars.
- Rows go to two new SQLite tables.
- The FEEDS card renders the recorder's in-memory status and never touches the network.

**Tech Stack:** Python 3.11+, asyncio, httpx, websockets (core dep), aiosqlite, pytest + pytest-asyncio (strict mode).

**Spec:** `docs/superpowers/specs/2026-09-14-hourly-btc-flow-strategy-design.md` (part A)

## Amendment found while executing (2026-09-14, verified live)

The first smoke run showed the Binance liquidations row as OK on a stream that sent
nothing. Binance now serves USD-M futures market streams under `/market/`: the legacy
`wss://fstream.binance.com/ws` still connects and acknowledges `SUBSCRIBE`, but delivers
no market data (checked with `btcusdt@aggTrade` too). `…/market/ws/!forceOrder@arr`
delivers frames in the documented shape.

The branch implements these corrections, which replace the Task 3 and Task 5 code shown
below where they differ:

- `run_ws_forever`: `on_message` returns `bool` (True = live data frame), and
  `WsStatus.last_message_at` is renamed `last_data_at`. It is set only for data frames,
  so acks and heartbeats can't make a silent stream look healthy.
- `flow_recorder`: `BINANCE_FAPI_WS` + `SUBSCRIBE` is replaced by
  `BINANCE_FORCE_ORDER_WS = "wss://fstream.binance.com/market/ws/!forceOrder@arr"` with
  no subscribe message. Trade handlers return whether the frame was live data. For
  liquidations, any symbol's `forceOrder` frame counts as live (`_DATA_FRAME`), while
  only BTCUSDT is aggregated.
- Tests pin both: `test_ws_runner.py` asserts the data timestamp, and
  `test_flow_recorder.py::test_liquidation_frames_for_any_symbol_count_as_live_data`.

## Global Constraints

- Base branch: PR #231 (`fix/feeds-live`) rewrote `feeds.py`, `execution_view.py`, `app.py` lifespan and `tests/conftest.py`, and this plan edits those files.
  - Until #231 merges: `git fetch origin && git switch -c feature/venue-flow-feeds origin/fix/feeds-live`.
  - After it merges: `git switch -c feature/venue-flow-feeds origin/develop`.
  - Open the PR into `develop`.
- Never edit `polymarket_exec/execution/gate.py` or its tests.
- Recording is observation only. Nothing here decides, gates, or reads mode.
- No network calls in any dashboard render path. Panels read the recorder's in-memory snapshot only.
- torch, pandas and einops are not used anywhere in this plan.
- Every test isolates the DB (`monkeypatch.setattr(_db, "DB_PATH", tmp_path / "t.db")` then `await _db.init_db()`) and stubs the network. Async tests use `@pytest.mark.asyncio`; async fixtures use `@pytest_asyncio.fixture`.
- Run tests with `PYTHON_DOTENV_DISABLED=1` so the real `.env` is never loaded.
- Pinned lint: `ruff==0.15.12`, `ruff check polymarket_exec/ polymarket_bot/ tests/ tools/`, line length 100.
- Every new `.py` file starts with a one-line module docstring (it becomes the FILE_MAP Role).
- Feed and venue keys are exact strings, used verbatim everywhere:
  - venues: `binance_spot`, `binance_perp`, `binance_liq`, `kraken_spot`, `kraken_futures`
  - symbols: `BTCUSDT`, `ETHUSDT`, `BTC/USD`, `PF_XBTUSD`
- Message shapes were captured live on 2026-09-13:
  - Kraken WS v2 `trade`: `{"channel":"trade","type":"update","data":[{"symbol":"BTC/USD","side":"buy","price":77313.3,"qty":0.0084064,"ord_type":"limit","trade_id":107612614,"timestamp":"2026-09-13T20:52:59.976634Z"}]}`
  - Kraken Futures WS v1 `trade`: `{"product_id":"PF_XBTUSD","feed":"trade","uid":"…","side":"buy","type":"fill","time":1789332835217,"qty":0.019,"price":77320.0,"seq":447666}` (the first frame is `trade_snapshot` with a `trades` list; it is skipped)
  - Binance `premiumIndex`: `{"symbol":"BTCUSDT","markPrice":"77318.00847826","indexPrice":"77346.00152174","lastFundingRate":"0.00009166","nextFundingTime":1789344000000,"time":1789332813000,…}`; `openInterest`: `{"symbol":"BTCUSDT","openInterest":"104994.803","time":1789332805237}`
  - Kraken Futures `tickers` entry: `{"symbol":"PF_XBTUSD","markPrice":77322.77,"indexPrice":77314.89,"fundingRate":0.8274803189537242,"openInterest":1907.2899,…}` (`fundingRate` is absolute USD per contract per hour)
  - Binance `!forceOrder@arr` (documented shape): `{"e":"forceOrder","E":…,"o":{"s":"BTCUSDT","S":"SELL","q":"0.014","p":"9910","ap":"9910","X":"FILLED","l":"0.014","z":"0.014","T":…}}`. Order side `SELL` means a long was liquidated.
  - Binance klines row: `[open_time, open, high, low, close, volume, close_time, quote_volume, trades, taker_buy_base, taker_buy_quote, ignore]`
- The gates before opening the PR (feature PRs get no CI):
  1. `PYTHON_DOTENV_DISABLED=1 DATA_DIR="$(mktemp -d)" python3 -m pytest tests/ -q`
  2. `python3 -m ruff check polymarket_exec/ polymarket_bot/ tests/ tools/`
  3. `PYTHON_DOTENV_DISABLED=1 python3 tools/gen_docs.py`, then `PYTHON_DOTENV_DISABLED=1 python3 tools/gen_docs.py --check`
  4. the banned-strings grep from the `docs-drift` job in `.github/workflows/ci.yml` (run the exact command from that file; it must print nothing)

## File Structure

| File | Responsibility |
|---|---|
| Create `polymarket_exec/connectors/venue_flow.py` | `HourBar`, `VenueSnapshot`, `hour_floor_ms`, Binance kline parsing, `HourAggregator` |
| Create `polymarket_exec/connectors/venue_messages.py` | Pure parsers for Kraken spot/futures trade frames, Binance liquidation frames, perp state payloads |
| Create `polymarket_exec/connectors/ws_runner.py` | `WsStatus` + `run_ws_forever` reconnecting loop that reports disconnected stretches as gaps |
| Modify `db.py` (SCHEMA literal) | Tables `venue_flow_hourly`, `venue_snapshot` |
| Create `polymarket_exec/storage/venue_flow_store.py` | Upsert bars, insert snapshots, read recent rows |
| Create `polymarket_exec/ops/flow_recorder.py` | `FlowRecorder` (REST bars, hourly snapshots, WS aggregators, status snapshot) + process-wide `current()` |
| Modify `polymarket_exec/ops/dashboard/app.py` | Start/stop the recorder in the lifespan |
| Modify `tests/conftest.py` | Autouse stub so dashboard tests never run the recorder live |
| Modify `polymarket_exec/ops/dashboard/panels/feeds.py` | Flow feed rows |
| Modify `polymarket_exec/ops/dashboard/execution_view.py` | Pass the recorder snapshot to the FEEDS card |
| Modify `docs/CODE_MAP.md` | Routing row; regenerate generated docs |
| Tests | `tests/unit/test_venue_flow.py`, `test_venue_messages.py`, `test_ws_runner.py`, `test_venue_flow_store.py`, `test_flow_recorder.py`, `test_feeds_panel.py` (extend) |

---

### Task 1: Hour bars, Binance kline parsing, and the hour aggregator

**Files:**
- Create: `polymarket_exec/connectors/venue_flow.py`
- Test: `tests/unit/test_venue_flow.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `HOUR_MS: int = 3_600_000`
  - `Side = Literal["buy", "sell"]`
  - `hour_floor_ms(ts_ms: int) -> int`
  - `@dataclass(frozen=True) HourBar(venue: str, symbol: str, hour_start_ms: int, open: float | None, high: float | None, low: float | None, close: float | None, volume: float, taker_buy_volume: float, trades: int, complete: bool, source: str)`, with property `imbalance -> float | None`
  - `@dataclass(frozen=True) VenueSnapshot(venue: str, symbol: str, taken_at_ms: int, mark_price: float | None, index_price: float | None, funding_rate: float | None, next_funding_ms: int | None, open_interest: float | None, source: str)`
  - `parse_binance_klines(rows: list[list[Any]], *, venue: str, symbol: str, now_ms: int) -> list[HourBar]`
  - `class HourAggregator(*, venue: str, symbol: str, source: str, started_at_ms: int)` with `add(ts_ms: int, price: float, qty: float, taker_side: Side) -> None`, `mark_gap(from_ms: int, to_ms: int) -> None`, and `roll(now_ms: int) -> list[HourBar]`

- [ ] **Step 1: Write the failing tests**

```python
"""Venue flow bars: closed-candle kline parsing and live-trade hour aggregation."""
from __future__ import annotations

from polymarket_exec.connectors.venue_flow import (
    HOUR_MS,
    HourAggregator,
    HourBar,
    hour_floor_ms,
    parse_binance_klines,
)

H0 = 1_789_326_000_000  # an hour boundary (2026-09-13 19:00 UTC)


def _kline(open_ms: int, o: float, h: float, lo: float, c: float, v: float, tb: float) -> list:
    return [open_ms, str(o), str(h), str(lo), str(c), str(v), open_ms + HOUR_MS - 1,
            "0", 42, str(tb), "0", "0"]


def test_hour_floor() -> None:
    assert hour_floor_ms(H0 + 59 * 60_000) == H0
    assert hour_floor_ms(H0) == H0


def test_parse_binance_klines_drops_the_forming_candle() -> None:
    rows = [_kline(H0, 1, 2, 0.5, 1.5, 10, 7), _kline(H0 + HOUR_MS, 1.5, 2, 1, 1.2, 4, 1)]
    now = H0 + HOUR_MS + 5_000  # second candle still forming
    bars = parse_binance_klines(rows, venue="binance_spot", symbol="BTCUSDT", now_ms=now)
    assert len(bars) == 1
    bar = bars[0]
    assert (bar.hour_start_ms, bar.close, bar.volume, bar.taker_buy_volume, bar.trades) == (
        H0, 1.5, 10.0, 7.0, 42
    )
    assert bar.complete and bar.source == "rest_klines"
    assert abs(bar.imbalance - 0.4) < 1e-12  # 2*7/10 - 1


def test_imbalance_is_none_without_volume() -> None:
    bar = HourBar("kraken_spot", "BTC/USD", H0, None, None, None, None, 0.0, 0.0, 0, False, "x")
    assert bar.imbalance is None


def test_aggregator_builds_bar_and_marks_start_hour_incomplete() -> None:
    agg = HourAggregator(venue="kraken_spot", symbol="BTC/USD", source="ws_v2_trade",
                         started_at_ms=H0 + 10 * 60_000)
    agg.add(H0 + 11 * 60_000, 100.0, 2.0, "buy")
    agg.add(H0 + 12 * 60_000, 101.0, 1.0, "sell")
    agg.add(H0 + HOUR_MS + 1_000, 102.0, 3.0, "buy")
    assert agg.roll(H0 + HOUR_MS - 1) == []  # hour not over yet
    first = agg.roll(H0 + HOUR_MS)
    assert len(first) == 1
    b = first[0]
    assert (b.open, b.high, b.low, b.close) == (100.0, 101.0, 100.0, 101.0)
    assert (b.volume, b.taker_buy_volume, b.trades) == (3.0, 2.0, 2)
    assert b.complete is False  # recorder started 10 minutes into the hour
    second = agg.roll(H0 + 2 * HOUR_MS)
    assert len(second) == 1 and second[0].complete is True and second[0].volume == 3.0


def test_aggregator_gap_marks_every_hour_it_spans_and_emits_empty_hours() -> None:
    agg = HourAggregator(venue="kraken_spot", symbol="BTC/USD", source="ws_v2_trade",
                         started_at_ms=H0)
    agg.roll(H0 + HOUR_MS)  # flush the (incomplete) start hour
    agg.mark_gap(H0 + HOUR_MS + 50 * 60_000, H0 + 2 * HOUR_MS + 5 * 60_000)
    bars = agg.roll(H0 + 4 * HOUR_MS)
    assert [b.hour_start_ms for b in bars] == [H0 + HOUR_MS, H0 + 2 * HOUR_MS, H0 + 3 * HOUR_MS]
    assert [b.complete for b in bars] == [False, False, True]
    assert all(b.volume == 0.0 and b.trades == 0 and b.open is None for b in bars)


def test_late_print_for_a_rolled_hour_is_ignored() -> None:
    agg = HourAggregator(venue="kraken_spot", symbol="BTC/USD", source="s", started_at_ms=H0)
    agg.roll(H0 + HOUR_MS)
    agg.add(H0 + 30 * 60_000, 99.0, 1.0, "buy")  # belongs to an already-written hour
    bars = agg.roll(H0 + 2 * HOUR_MS)
    assert bars[0].trades == 0
```

- [ ] **Step 2: Run them to verify they fail**

Run: `PYTHON_DOTENV_DISABLED=1 python3 -m pytest tests/unit/test_venue_flow.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'polymarket_exec.connectors.venue_flow'`

- [ ] **Step 3: Write the implementation**

```python
"""Closed-hour trade-flow bars and venue state snapshots for the Binance and Kraken feeds."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

HOUR_MS = 3_600_000

Side = Literal["buy", "sell"]


def hour_floor_ms(ts_ms: int) -> int:
    return ts_ms - ts_ms % HOUR_MS


@dataclass(frozen=True)
class HourBar:
    venue: str
    symbol: str
    hour_start_ms: int
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: float
    taker_buy_volume: float
    trades: int
    complete: bool
    source: str

    @property
    def imbalance(self) -> float | None:
        """Aggressive-buy share mapped to [-1, 1]; None for an hour with no volume."""
        if self.volume <= 0:
            return None
        return 2 * self.taker_buy_volume / self.volume - 1


@dataclass(frozen=True)
class VenueSnapshot:
    venue: str
    symbol: str
    taken_at_ms: int
    mark_price: float | None
    index_price: float | None
    funding_rate: float | None
    next_funding_ms: int | None
    open_interest: float | None
    source: str


def parse_binance_klines(
    rows: list[list[Any]], *, venue: str, symbol: str, now_ms: int
) -> list[HourBar]:
    """Closed 1h klines only: Binance always returns the forming candle last."""
    bars: list[HourBar] = []
    for row in rows:
        if int(row[6]) >= now_ms:
            continue
        bars.append(
            HourBar(
                venue=venue,
                symbol=symbol,
                hour_start_ms=int(row[0]),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5]),
                taker_buy_volume=float(row[9]),
                trades=int(row[8]),
                complete=True,
                source="rest_klines",
            )
        )
    return bars


@dataclass
class _Bucket:
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    taker_buy_volume: float = 0.0
    trades: int = 0


class HourAggregator:
    """Folds live trades into per-hour bars; any hour touched by a feed gap is incomplete."""

    def __init__(self, *, venue: str, symbol: str, source: str, started_at_ms: int) -> None:
        self.venue = venue
        self.symbol = symbol
        self.source = source
        self._buckets: dict[int, _Bucket] = {}
        self._gap_hours: set[int] = set()
        self._next_hour_ms = hour_floor_ms(started_at_ms)
        # Trades before the recorder started were never seen.
        self.mark_gap(started_at_ms, started_at_ms)

    def add(self, ts_ms: int, price: float, qty: float, taker_side: Side) -> None:
        hour = hour_floor_ms(ts_ms)
        if hour < self._next_hour_ms:
            return
        bucket = self._buckets.get(hour)
        if bucket is None:
            bucket = self._buckets[hour] = _Bucket(open=price, high=price, low=price, close=price)
        bucket.high = max(bucket.high, price)
        bucket.low = min(bucket.low, price)
        bucket.close = price
        bucket.volume += qty
        if taker_side == "buy":
            bucket.taker_buy_volume += qty
        bucket.trades += 1

    def mark_gap(self, from_ms: int, to_ms: int) -> None:
        hour = hour_floor_ms(from_ms)
        while hour <= to_ms:
            self._gap_hours.add(hour)
            hour += HOUR_MS

    def roll(self, now_ms: int) -> list[HourBar]:
        """Emit one bar for every hour that has fully ended, in order."""
        bars: list[HourBar] = []
        while self._next_hour_ms + HOUR_MS <= now_ms:
            hour = self._next_hour_ms
            bucket = self._buckets.pop(hour, None)
            complete = hour not in self._gap_hours
            self._gap_hours.discard(hour)
            bars.append(
                HourBar(
                    venue=self.venue,
                    symbol=self.symbol,
                    hour_start_ms=hour,
                    open=bucket.open if bucket else None,
                    high=bucket.high if bucket else None,
                    low=bucket.low if bucket else None,
                    close=bucket.close if bucket else None,
                    volume=bucket.volume if bucket else 0.0,
                    taker_buy_volume=bucket.taker_buy_volume if bucket else 0.0,
                    trades=bucket.trades if bucket else 0,
                    complete=complete,
                    source=self.source,
                )
            )
            self._next_hour_ms += HOUR_MS
        return bars
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHON_DOTENV_DISABLED=1 python3 -m pytest tests/unit/test_venue_flow.py -q`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
git add polymarket_exec/connectors/venue_flow.py tests/unit/test_venue_flow.py
git commit -m "feat(feeds): hour flow bars, closed-kline parsing, live trade hour aggregator"
```

---

### Task 2: Venue message parsers

**Files:**
- Create: `polymarket_exec/connectors/venue_messages.py`
- Test: `tests/unit/test_venue_messages.py`

**Interfaces:**
- Consumes: `Side` and `VenueSnapshot` from Task 1.
- Produces:
  - `Trade = tuple[int, float, float, Side]`, meaning `(ts_ms, price, qty, taker_side)`
  - `kraken_spot_trades(msg: dict[str, Any], symbol: str = "BTC/USD") -> list[Trade]`
  - `kraken_futures_trades(msg: dict[str, Any], product_id: str = "PF_XBTUSD") -> list[Trade]`
  - `binance_liquidation(msg: dict[str, Any], symbol: str = "BTCUSDT") -> Trade | None`
  - `binance_perp_snapshot(premium: dict[str, Any], open_interest: dict[str, Any], *, symbol: str) -> VenueSnapshot`
  - `kraken_futures_snapshot(tickers: dict[str, Any], *, symbol: str, now_ms: int) -> VenueSnapshot | None`

- [ ] **Step 1: Write the failing tests**

```python
"""Venue message parsers, pinned to frames captured live on 2026-09-13."""
from __future__ import annotations

from polymarket_exec.connectors.venue_messages import (
    binance_liquidation,
    binance_perp_snapshot,
    kraken_futures_snapshot,
    kraken_futures_trades,
    kraken_spot_trades,
)

KRAKEN_SPOT_UPDATE = {
    "channel": "trade",
    "type": "update",
    "data": [
        {"symbol": "BTC/USD", "side": "buy", "price": 77313.3, "qty": 0.0084064,
         "ord_type": "limit", "trade_id": 107612614, "timestamp": "2026-09-13T20:52:59.976634Z"},
        {"symbol": "BTC/USD", "side": "sell", "price": 77313.2, "qty": 0.5,
         "ord_type": "market", "trade_id": 107612615, "timestamp": "2026-09-13T20:53:00.000000Z"},
    ],
}


def test_kraken_spot_update_yields_taker_side_trades() -> None:
    trades = kraken_spot_trades(KRAKEN_SPOT_UPDATE)
    assert trades == [
        (1789332779976, 77313.3, 0.0084064, "buy"),
        (1789332780000, 77313.2, 0.5, "sell"),
    ]


def test_kraken_spot_ignores_status_heartbeat_snapshot_and_other_symbols() -> None:
    assert kraken_spot_trades({"channel": "heartbeat"}) == []
    assert kraken_spot_trades({"channel": "status", "type": "update", "data": []}) == []
    assert kraken_spot_trades({**KRAKEN_SPOT_UPDATE, "type": "snapshot"}) == []
    eth = {"channel": "trade", "type": "update", "data": [
        {**KRAKEN_SPOT_UPDATE["data"][0], "symbol": "ETH/USD"}]}
    assert kraken_spot_trades(eth) == []


def test_kraken_futures_fill_and_skipped_frames() -> None:
    fill = {"product_id": "PF_XBTUSD", "feed": "trade", "uid": "12d69b78", "side": "buy",
            "type": "fill", "time": 1789332835217, "qty": 0.019, "price": 77320.0, "seq": 447666}
    assert kraken_futures_trades(fill) == [(1789332835217, 77320.0, 0.019, "buy")]
    snapshot = {"feed": "trade_snapshot", "product_id": "PF_XBTUSD", "trades": [fill]}
    assert kraken_futures_trades(snapshot) == []
    subscribed = {"event": "subscribed", "feed": "trade", "product_ids": ["PF_XBTUSD"]}
    assert kraken_futures_trades(subscribed) == []
    assert kraken_futures_trades({**fill, "type": "termination"}) == []


def test_binance_liquidation_parses_btc_and_skips_others() -> None:
    msg = {"e": "forceOrder", "E": 1789332900000, "o": {
        "s": "BTCUSDT", "S": "SELL", "q": "0.014", "p": "77000", "ap": "77010.5",
        "X": "FILLED", "l": "0.014", "z": "0.014", "T": 1789332899999}}
    assert binance_liquidation(msg) == (1789332899999, 77010.5, 0.014, "sell")
    assert binance_liquidation({**msg, "o": {**msg["o"], "s": "ETHUSDT"}}) is None
    assert binance_liquidation({"result": None, "id": 1}) is None


def test_binance_perp_snapshot() -> None:
    premium = {"symbol": "BTCUSDT", "markPrice": "77318.00847826", "indexPrice": "77346.00152174",
               "lastFundingRate": "0.00009166", "nextFundingTime": 1789344000000,
               "time": 1789332813000}
    snap = binance_perp_snapshot(premium, {"symbol": "BTCUSDT", "openInterest": "104994.803"},
                                 symbol="BTCUSDT")
    assert snap.venue == "binance_perp" and snap.symbol == "BTCUSDT"
    assert snap.taken_at_ms == 1789332813000 and snap.next_funding_ms == 1789344000000
    assert snap.funding_rate == 0.00009166 and snap.open_interest == 104994.803


def test_kraken_futures_snapshot_stores_relative_funding() -> None:
    tickers = {"tickers": [
        {"symbol": "PI_XBTUSD", "markPrice": 1.0},
        {"symbol": "PF_XBTUSD", "markPrice": 77322.77, "indexPrice": 77314.89,
         "fundingRate": 0.8274803189537242, "openInterest": 1907.2899},
    ]}
    snap = kraken_futures_snapshot(tickers, symbol="PF_XBTUSD", now_ms=1789332900000)
    assert snap is not None and snap.venue == "kraken_futures"
    assert abs(snap.funding_rate - 0.8274803189537242 / 77314.89) < 1e-15
    assert snap.open_interest == 1907.2899 and snap.taken_at_ms == 1789332900000
    assert kraken_futures_snapshot({"tickers": []}, symbol="PF_XBTUSD", now_ms=0) is None
```

- [ ] **Step 2: Run them to verify they fail**

Run: `PYTHON_DOTENV_DISABLED=1 python3 -m pytest tests/unit/test_venue_messages.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'polymarket_exec.connectors.venue_messages'`

- [ ] **Step 3: Write the implementation**

```python
"""Parsers for venue feed frames: Kraken spot/futures trades, Binance liquidations, perp state."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from polymarket_exec.connectors.venue_flow import Side, VenueSnapshot

Trade = tuple[int, float, float, Side]  # (ts_ms, price, qty, taker side)

_SIDES: dict[str, Side] = {"buy": "buy", "sell": "sell"}


def _iso_ms(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


def _f(value: Any) -> float | None:
    return None if value is None else float(value)


def kraken_spot_trades(msg: dict[str, Any], symbol: str = "BTC/USD") -> list[Trade]:
    """Trades from a Kraken WS v2 ``trade`` update; ``side`` is the taker side.

    Snapshot frames are skipped: they replay trades from before the connection and
    would double count after a reconnect.
    """
    if msg.get("channel") != "trade" or msg.get("type") != "update":
        return []
    trades: list[Trade] = []
    for t in msg.get("data") or []:
        side = _SIDES.get(t.get("side"))
        if t.get("symbol") != symbol or side is None:
            continue
        trades.append((_iso_ms(t["timestamp"]), float(t["price"]), float(t["qty"]), side))
    return trades


def kraken_futures_trades(msg: dict[str, Any], product_id: str = "PF_XBTUSD") -> list[Trade]:
    """A live fill from Kraken Futures WS v1; the ``trade_snapshot`` backlog is skipped."""
    if "event" in msg or msg.get("feed") != "trade" or msg.get("product_id") != product_id:
        return []
    side = _SIDES.get(msg.get("side"))
    if side is None or msg.get("type") not in ("fill", "liquidation"):
        return []
    return [(int(msg["time"]), float(msg["price"]), float(msg["qty"]), side)]


def binance_liquidation(msg: dict[str, Any], symbol: str = "BTCUSDT") -> Trade | None:
    """A forced-liquidation order from ``!forceOrder@arr``; SELL means a long was liquidated."""
    order = msg.get("o") if msg.get("e") == "forceOrder" else None
    if not isinstance(order, dict) or order.get("s") != symbol:
        return None
    side = _SIDES.get(str(order.get("S", "")).lower())
    qty = float(order.get("z") or order.get("q") or 0.0)
    if side is None or qty <= 0:
        return None
    price = float(order.get("ap") or order.get("p"))
    return (int(order["T"]), price, qty, side)


def binance_perp_snapshot(
    premium: dict[str, Any], open_interest: dict[str, Any], *, symbol: str
) -> VenueSnapshot:
    return VenueSnapshot(
        venue="binance_perp",
        symbol=symbol,
        taken_at_ms=int(premium["time"]),
        mark_price=float(premium["markPrice"]),
        index_price=float(premium["indexPrice"]),
        funding_rate=float(premium["lastFundingRate"]),
        next_funding_ms=int(premium["nextFundingTime"]),
        open_interest=float(open_interest["openInterest"]),
        source="fapi_premiumIndex_openInterest",
    )


def kraken_futures_snapshot(
    tickers: dict[str, Any], *, symbol: str, now_ms: int
) -> VenueSnapshot | None:
    """PF ticker. ``fundingRate`` is absolute (USD per contract per hour); stored relative to index."""
    row = next((t for t in tickers.get("tickers") or [] if t.get("symbol") == symbol), None)
    if row is None:
        return None
    index = _f(row.get("indexPrice"))
    rate = _f(row.get("fundingRate"))
    return VenueSnapshot(
        venue="kraken_futures",
        symbol=symbol,
        taken_at_ms=now_ms,
        mark_price=_f(row.get("markPrice")),
        index_price=index,
        funding_rate=rate / index if rate is not None and index else None,
        next_funding_ms=None,
        open_interest=_f(row.get("openInterest")),
        source="futures_tickers",
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHON_DOTENV_DISABLED=1 python3 -m pytest tests/unit/test_venue_messages.py -q`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
git add polymarket_exec/connectors/venue_messages.py tests/unit/test_venue_messages.py
git commit -m "feat(feeds): parsers for Kraken spot/futures trades, Binance liquidations, perp state"
```

---

### Task 3: Reconnecting WebSocket loop

**Files:**
- Create: `polymarket_exec/connectors/ws_runner.py`
- Test: `tests/unit/test_ws_runner.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `@dataclass WsStatus(connected: bool = False, connected_since: float | None = None, last_message_at: float | None = None, last_error: str | None = None)`
  - `async run_ws_forever(*, name: str, url: str, subscribe: dict[str, Any] | None, on_message: Callable[[dict[str, Any]], None], on_gap: Callable[[int, int], None], stop_event: asyncio.Event, status: WsStatus, connect: Callable[[str], Any] | None = None, time_fn: Callable[[], float] = time.time, recv_timeout_s: float = 30.0, initial_backoff_s: float = 1.0, max_backoff_s: float = 60.0) -> None`

- [ ] **Step 1: Write the failing tests**

```python
"""Reconnecting WS loop: subscribes, feeds frames, reports every disconnected stretch as a gap."""
from __future__ import annotations

import asyncio
import json

import pytest

from polymarket_exec.connectors.ws_runner import WsStatus, run_ws_forever


class _FakeWs:
    def __init__(self, frames: list, fail_after: bool, stop_event: asyncio.Event) -> None:
        self.frames = list(frames)
        self.sent: list[str] = []
        self.fail_after = fail_after
        self.stop_event = stop_event

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def send(self, text: str) -> None:
        self.sent.append(text)

    async def recv(self) -> str:
        if self.frames:
            return self.frames.pop(0)
        if self.fail_after:
            raise ConnectionError("socket closed")
        self.stop_event.set()
        await asyncio.sleep(0)
        return "not json"


@pytest.mark.asyncio
async def test_reconnects_reports_gaps_and_delivers_frames() -> None:
    stop = asyncio.Event()
    clock = iter(range(1000, 10_000, 10))
    connections = [
        _FakeWs([json.dumps({"n": 1})], fail_after=True, stop_event=stop),
        _FakeWs([json.dumps({"n": 2}), "[1,2]"], fail_after=False, stop_event=stop),
    ]
    made: list[_FakeWs] = []

    def connect(url: str) -> _FakeWs:
        assert url == "wss://example.test/ws"
        ws = connections.pop(0)
        made.append(ws)
        return ws

    messages: list[dict] = []
    gaps: list[tuple[int, int]] = []
    status = WsStatus()
    await asyncio.wait_for(
        run_ws_forever(
            name="t", url="wss://example.test/ws", subscribe={"op": "sub"},
            on_message=messages.append, on_gap=lambda a, b: gaps.append((a, b)),
            stop_event=stop, status=status, connect=connect,
            time_fn=lambda: float(next(clock)), recv_timeout_s=1.0, initial_backoff_s=0.001,
        ),
        timeout=5,
    )
    assert messages == [{"n": 1}, {"n": 2}]  # the list frame is ignored
    assert [ws.sent for ws in made] == [['{"op": "sub"}'], ['{"op": "sub"}']]
    assert len(gaps) == 2
    assert gaps[1][0] > gaps[0][1]  # second gap starts after the first connection was up
    assert status.connected is False and status.last_error == "ConnectionError: socket closed"


@pytest.mark.asyncio
async def test_no_subscribe_message_when_none() -> None:
    stop = asyncio.Event()
    ws = _FakeWs([], fail_after=False, stop_event=stop)
    await asyncio.wait_for(
        run_ws_forever(
            name="t", url="wss://x", subscribe=None, on_message=lambda m: None,
            on_gap=lambda a, b: None, stop_event=stop, status=WsStatus(),
            connect=lambda url: ws, recv_timeout_s=1.0, initial_backoff_s=0.001,
        ),
        timeout=5,
    )
    assert ws.sent == []
```

- [ ] **Step 2: Run them to verify they fail**

Run: `PYTHON_DOTENV_DISABLED=1 python3 -m pytest tests/unit/test_ws_runner.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'polymarket_exec.connectors.ws_runner'`

- [ ] **Step 3: Write the implementation**

```python
"""Reconnecting WebSocket loop shared by the venue trade feeds (Kraken, Binance liquidations)."""
from __future__ import annotations

import asyncio
import json
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Callable

from logging_setup import get_logger

log = get_logger("ws_runner")


@dataclass
class WsStatus:
    connected: bool = False
    connected_since: float | None = None
    last_message_at: float | None = None
    last_error: str | None = None


def _default_connect(url: str) -> Any:
    import websockets

    return websockets.connect(url, open_timeout=15, ping_interval=20)


async def run_ws_forever(
    *,
    name: str,
    url: str,
    subscribe: dict[str, Any] | None,
    on_message: Callable[[dict[str, Any]], None],
    on_gap: Callable[[int, int], None],
    stop_event: asyncio.Event,
    status: WsStatus,
    connect: Callable[[str], Any] | None = None,
    time_fn: Callable[[], float] = time.time,
    recv_timeout_s: float = 30.0,
    initial_backoff_s: float = 1.0,
    max_backoff_s: float = 60.0,
) -> None:
    """Connect, subscribe and feed JSON object frames to ``on_message`` until ``stop_event``.

    Each disconnected stretch (including the one before the first connect) is
    reported once the socket is up again as ``on_gap(from_ms, to_ms)``.
    """
    connect = connect or _default_connect
    backoff = initial_backoff_s
    down_since_ms = int(time_fn() * 1000)
    while not stop_event.is_set():
        try:
            async with connect(url) as ws:
                if subscribe is not None:
                    await ws.send(json.dumps(subscribe))
                now = time_fn()
                on_gap(down_since_ms, int(now * 1000))
                # last_error is kept: the card shows it only while the feed is down.
                status.connected, status.connected_since = True, now
                backoff = initial_backoff_s
                while not stop_event.is_set():
                    try:
                        frame = await asyncio.wait_for(ws.recv(), timeout=recv_timeout_s)
                    except asyncio.TimeoutError:
                        continue  # quiet feeds are normal; the library's pings detect dead sockets
                    status.last_message_at = time_fn()
                    try:
                        msg = json.loads(frame)
                    except (TypeError, ValueError):
                        continue
                    if isinstance(msg, dict):
                        on_message(msg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — any socket error means reconnect
            status.last_error = f"{type(exc).__name__}: {exc}"[:200]
            log.warning("venue_ws.disconnected", feed=name, error=status.last_error,
                        retry_in_s=backoff)
        finally:
            if status.connected:
                down_since_ms = int(time_fn() * 1000)
            status.connected = False
        if stop_event.is_set():
            break
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=backoff)
        backoff = min(backoff * 2, max_backoff_s)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHON_DOTENV_DISABLED=1 python3 -m pytest tests/unit/test_ws_runner.py -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add polymarket_exec/connectors/ws_runner.py tests/unit/test_ws_runner.py
git commit -m "feat(feeds): reconnecting WS loop that reports disconnected stretches as gaps"
```

---

### Task 4: Storage tables and store

**Files:**
- Modify: `db.py` (SCHEMA literal, right after the `idx_daily_shadow_positions_asset` index and before the closing `"""`)
- Create: `polymarket_exec/storage/venue_flow_store.py`
- Test: `tests/unit/test_venue_flow_store.py`

**Interfaces:**
- Consumes: `HourBar`, `VenueSnapshot` (Task 1).
- Produces:
  - `async upsert_hour_bars(bars: list[HourBar]) -> int`
  - `async insert_snapshot(snap: VenueSnapshot) -> None`
  - `async recent_hours(venue: str, symbol: str, limit: int) -> list[dict[str, Any]]`, newest first
  - `async latest_snapshot(venue: str, symbol: str) -> dict[str, Any] | None`

- [ ] **Step 1: Write the failing tests**

```python
"""Venue flow store: bars upsert idempotently and never downgrade a complete row."""
from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

import db as _db
from polymarket_exec.connectors.venue_flow import HOUR_MS, HourBar, VenueSnapshot
from polymarket_exec.storage import venue_flow_store as store

H0 = 1_789_326_000_000


@pytest_asyncio.fixture
async def test_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "t.db")
    await _db.init_db()
    return _db


def _bar(hour: int, volume: float, complete: bool) -> HourBar:
    return HourBar("kraken_spot", "BTC/USD", hour, 1.0, 2.0, 0.5, 1.5, volume, volume / 2,
                   3, complete, "ws_v2_trade")


@pytest.mark.asyncio
async def test_upsert_is_idempotent_and_keeps_the_more_complete_row(test_db) -> None:
    assert await store.upsert_hour_bars([_bar(H0, 10.0, True), _bar(H0 + HOUR_MS, 4.0, False)]) == 2
    await store.upsert_hour_bars([_bar(H0, 1.0, False)])  # must not replace the complete row
    await store.upsert_hour_bars([_bar(H0 + HOUR_MS, 5.0, True)])  # may upgrade an incomplete one
    rows = await store.recent_hours("kraken_spot", "BTC/USD", 10)
    assert [(r["hour_start_ms"], r["volume"], r["complete"]) for r in rows] == [
        (H0 + HOUR_MS, 5.0, 1),
        (H0, 10.0, 1),
    ]
    assert await store.upsert_hour_bars([]) == 0


@pytest.mark.asyncio
async def test_snapshots_latest_first(test_db) -> None:
    for ts, oi in ((H0, 100.0), (H0 + HOUR_MS, 110.0)):
        await store.insert_snapshot(VenueSnapshot("binance_perp", "BTCUSDT", ts, 1.0, 1.0,
                                                  0.0001, ts + 8 * HOUR_MS, oi, "fapi"))
    latest = await store.latest_snapshot("binance_perp", "BTCUSDT")
    assert latest is not None and latest["open_interest"] == 110.0
    assert await store.latest_snapshot("kraken_futures", "PF_XBTUSD") is None
```

- [ ] **Step 2: Run them to verify they fail**

Run: `PYTHON_DOTENV_DISABLED=1 python3 -m pytest tests/unit/test_venue_flow_store.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'polymarket_exec.storage.venue_flow_store'`

- [ ] **Step 3: Add the tables to the `db.py` SCHEMA literal**

Insert this SQL inside `SCHEMA`, directly after `CREATE INDEX IF NOT EXISTS idx_daily_shadow_positions_asset ON daily_shadow_positions(asset);`:

```sql
-- Venue flow feeds: one closed-hour trade-flow bar per (venue, symbol, hour).
-- complete=0 marks an hour a live WS feed did not see end to end (a reconnect,
-- or the recorder starting mid-hour). Observation data for the hourly BTC strategy.
CREATE TABLE IF NOT EXISTS venue_flow_hourly (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  venue TEXT NOT NULL,
  symbol TEXT NOT NULL,
  hour_start_ms INTEGER NOT NULL,
  open REAL,
  high REAL,
  low REAL,
  close REAL,
  volume REAL NOT NULL,
  taker_buy_volume REAL NOT NULL,
  trades INTEGER NOT NULL,
  complete INTEGER NOT NULL,
  source TEXT NOT NULL,
  recorded_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_venue_flow_hourly_key
  ON venue_flow_hourly(venue, symbol, hour_start_ms);

-- Perp venue state (mark, index, funding, open interest) sampled each hour.
CREATE TABLE IF NOT EXISTS venue_snapshot (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  venue TEXT NOT NULL,
  symbol TEXT NOT NULL,
  taken_at_ms INTEGER NOT NULL,
  mark_price REAL,
  index_price REAL,
  funding_rate REAL,
  next_funding_ms INTEGER,
  open_interest REAL,
  source TEXT NOT NULL,
  recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_venue_snapshot_key
  ON venue_snapshot(venue, symbol, taken_at_ms);
```

- [ ] **Step 4: Write the store**

```python
"""SQLite read/write for venue flow hour bars and perp venue snapshots."""
from __future__ import annotations

from typing import Any

import db as _db
from polymarket_exec.connectors.venue_flow import HourBar, VenueSnapshot


async def upsert_hour_bars(bars: list[HourBar]) -> int:
    """Write bars; an existing row is replaced only by one that is at least as complete."""
    if not bars:
        return 0
    recorded_at = _db.utc_now_iso()
    async with _db.connect() as conn:
        await conn.executemany(
            """
            INSERT INTO venue_flow_hourly(
              venue, symbol, hour_start_ms, open, high, low, close, volume,
              taker_buy_volume, trades, complete, source, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(venue, symbol, hour_start_ms) DO UPDATE SET
              open = excluded.open, high = excluded.high, low = excluded.low,
              close = excluded.close, volume = excluded.volume,
              taker_buy_volume = excluded.taker_buy_volume, trades = excluded.trades,
              complete = excluded.complete, source = excluded.source,
              recorded_at = excluded.recorded_at
            WHERE excluded.complete >= venue_flow_hourly.complete
            """,
            [
                (
                    b.venue, b.symbol, b.hour_start_ms, b.open, b.high, b.low, b.close,
                    b.volume, b.taker_buy_volume, b.trades, int(b.complete), b.source,
                    recorded_at,
                )
                for b in bars
            ],
        )
        await conn.commit()
    return len(bars)


async def insert_snapshot(snap: VenueSnapshot) -> None:
    async with _db.connect() as conn:
        await conn.execute(
            """
            INSERT INTO venue_snapshot(
              venue, symbol, taken_at_ms, mark_price, index_price, funding_rate,
              next_funding_ms, open_interest, source, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snap.venue, snap.symbol, snap.taken_at_ms, snap.mark_price, snap.index_price,
                snap.funding_rate, snap.next_funding_ms, snap.open_interest, snap.source,
                _db.utc_now_iso(),
            ),
        )
        await conn.commit()


async def recent_hours(venue: str, symbol: str, limit: int) -> list[dict[str, Any]]:
    async with _db.connect() as conn:
        cur = await conn.execute(
            "SELECT * FROM venue_flow_hourly WHERE venue = ? AND symbol = ? "
            "ORDER BY hour_start_ms DESC LIMIT ?",
            (venue, symbol, limit),
        )
        return [dict(r) for r in await cur.fetchall()]


async def latest_snapshot(venue: str, symbol: str) -> dict[str, Any] | None:
    async with _db.connect() as conn:
        cur = await conn.execute(
            "SELECT * FROM venue_snapshot WHERE venue = ? AND symbol = ? "
            "ORDER BY taken_at_ms DESC LIMIT 1",
            (venue, symbol),
        )
        row = await cur.fetchone()
        return dict(row) if row else None
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `PYTHON_DOTENV_DISABLED=1 python3 -m pytest tests/unit/test_venue_flow_store.py tests/unit/test_daily_ledger.py -q`
Expected: PASS (the daily ledger tests confirm `init_db` still runs cleanly)

- [ ] **Step 6: Commit**

```bash
git add db.py polymarket_exec/storage/venue_flow_store.py tests/unit/test_venue_flow_store.py
git commit -m "feat(feeds): venue_flow_hourly and venue_snapshot tables with store"
```

---

### Task 5: Flow recorder

**Files:**
- Create: `polymarket_exec/ops/flow_recorder.py`
- Test: `tests/unit/test_flow_recorder.py`

**Interfaces:**
- Consumes: Tasks 1–4. Also `config.BINANCE_API_BASE`, which is `https://data-api.binance.vision` by default.
- Produces:
  - feed keys (str constants): `BINANCE_SPOT_BTC = "binance_spot:BTCUSDT"`, `BINANCE_SPOT_ETH = "binance_spot:ETHUSDT"`, `BINANCE_PERP_BTC = "binance_perp:BTCUSDT"`, `BINANCE_PERP_STATE = "binance_perp_state:BTCUSDT"`, `BINANCE_LIQ = "binance_liq:BTCUSDT"`, `KRAKEN_SPOT = "kraken_spot:BTC/USD"`, `KRAKEN_FUTURES = "kraken_futures:PF_XBTUSD"`, `KRAKEN_FUTURES_STATE = "kraken_futures_state:PF_XBTUSD"`
  - `REST_KEYS: tuple[str, ...]` and `WS_KEYS: tuple[str, ...]`
  - `@dataclass(frozen=True) FeedStatus(kind: str, ok: bool | None, connected: bool, connected_since: float | None, last_event_at: float | None, last_hour_ms: int | None, detail: str | None)`
  - `@dataclass(frozen=True) FlowSnapshot(taken_at: float, started_at: float, interval_s: float, feeds: dict[str, FeedStatus])`
  - `class FlowRecorder(*, interval_s: float = 60.0, client_factory: Callable[[], httpx.AsyncClient] | None = None, connect: Callable[[str], Any] | None = None, time_fn: Callable[[], float] = time.time)` with `snapshot() -> FlowSnapshot`, `async record_once(client, now_ms: int) -> None`, `async run(stop_event: asyncio.Event) -> None`, and `aggregator(key: str) -> HourAggregator`
  - `set_current(recorder: FlowRecorder | None) -> None` and `current() -> FlowRecorder | None`

- [ ] **Step 1: Write the failing tests**

```python
"""Flow recorder: REST bars each pass, perp snapshots once an hour, WS aggregators rolled to bars."""
from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

import config as _config
import db as _db
from polymarket_exec.connectors.venue_flow import HOUR_MS
from polymarket_exec.ops import flow_recorder as fr
from polymarket_exec.storage import venue_flow_store as store

# Captured before the autouse conftest fixture swaps ``run`` for an offline stub.
_REAL_RUN = fr.FlowRecorder.run

H0 = 1_789_326_000_000
NOW_MS = H0 + 2 * HOUR_MS + 30_000  # 30 s into hour H0+2h


def _klines(symbol_close: float) -> list:
    rows = []
    for i in range(3):  # H0, H0+1h closed; H0+2h forming
        open_ms = H0 + i * HOUR_MS
        rows.append([open_ms, "1", "2", "0.5", str(symbol_close), "10", open_ms + HOUR_MS - 1,
                     "0", 5, "6", "0", "0"])
    return rows


def _handler(*, perp_status: int = 200, calls: list[str] | None = None):
    def handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if calls is not None:
            calls.append(url)
        if url.startswith(f"{_config.BINANCE_API_BASE}/api/v3/klines"):
            close = 77_000.0 if request.url.params["symbol"] == "BTCUSDT" else 3_000.0
            return httpx.Response(200, json=_klines(close))
        if url.startswith(f"{fr.BINANCE_FAPI}/fapi/v1/klines"):
            if perp_status != 200:
                return httpx.Response(perp_status, json={"code": -1})
            return httpx.Response(200, json=_klines(77_010.0))
        if url.startswith(f"{fr.BINANCE_FAPI}/fapi/v1/premiumIndex"):
            return httpx.Response(200, json={"symbol": "BTCUSDT", "markPrice": "77318.0",
                                             "indexPrice": "77346.0", "lastFundingRate": "0.0001",
                                             "nextFundingTime": H0 + 8 * HOUR_MS,
                                             "time": NOW_MS})
        if url.startswith(f"{fr.BINANCE_FAPI}/fapi/v1/openInterest"):
            return httpx.Response(200, json={"symbol": "BTCUSDT", "openInterest": "104994.8"})
        if url.startswith(f"{fr.KRAKEN_FUTURES_API}/tickers"):
            return httpx.Response(200, json={"tickers": [
                {"symbol": "PF_XBTUSD", "markPrice": 77322.0, "indexPrice": 77314.0,
                 "fundingRate": 0.8, "openInterest": 1907.3}]})
        return httpx.Response(404)

    return handle


@pytest_asyncio.fixture
async def test_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "t.db")
    await _db.init_db()
    return _db


def _recorder(started_ms: int = H0 + 10 * 60_000) -> fr.FlowRecorder:
    return fr.FlowRecorder(time_fn=lambda: started_ms / 1000)


@pytest.mark.asyncio
async def test_record_once_writes_closed_rest_bars_and_hourly_snapshots(test_db) -> None:
    rec = _recorder()
    calls: list[str] = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler(calls=calls))) as client:
        await rec.record_once(client, NOW_MS)
        n_first = len(calls)
        await rec.record_once(client, NOW_MS + 60_000)  # same hour: no second snapshot
    btc = await store.recent_hours("binance_spot", "BTCUSDT", 10)
    assert [r["hour_start_ms"] for r in btc] == [H0 + HOUR_MS, H0]  # forming candle dropped
    assert (await store.recent_hours("binance_spot", "ETHUSDT", 10))[0]["close"] == 3_000.0
    assert (await store.recent_hours("binance_perp", "BTCUSDT", 10))[0]["close"] == 77_010.0
    assert (await store.latest_snapshot("binance_perp", "BTCUSDT"))["open_interest"] == 104994.8
    kraken = await store.latest_snapshot("kraken_futures", "PF_XBTUSD")
    assert kraken is not None and abs(kraken["funding_rate"] - 0.8 / 77314.0) < 1e-12
    assert len(calls) - n_first == 3  # second pass: only the three kline fetches
    snap = rec.snapshot()
    assert snap.feeds[fr.BINANCE_SPOT_BTC].ok is True
    assert snap.feeds[fr.BINANCE_SPOT_BTC].last_hour_ms == H0 + HOUR_MS
    assert snap.feeds[fr.BINANCE_PERP_STATE].ok is True


@pytest.mark.asyncio
async def test_a_failing_feed_is_reported_and_others_still_record(test_db) -> None:
    rec = _recorder()
    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler(perp_status=503))) as c:
        await rec.record_once(c, NOW_MS)
    feeds = rec.snapshot().feeds
    assert feeds[fr.BINANCE_PERP_BTC].ok is False
    assert "503" in (feeds[fr.BINANCE_PERP_BTC].detail or "")
    assert feeds[fr.BINANCE_SPOT_BTC].ok is True
    assert await store.recent_hours("binance_perp", "BTCUSDT", 10) == []


@pytest.mark.asyncio
async def test_ws_aggregators_roll_into_stored_bars(test_db) -> None:
    rec = _recorder(started_ms=H0 + 10 * 60_000)
    rec.aggregator(fr.KRAKEN_SPOT).add(H0 + 20 * 60_000, 77_000.0, 2.0, "buy")
    rec.aggregator(fr.BINANCE_LIQ).add(H0 + 21 * 60_000, 76_900.0, 0.5, "sell")
    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler())) as client:
        await rec.record_once(client, H0 + HOUR_MS + fr.ROLL_GRACE_MS)
    kraken = await store.recent_hours("kraken_spot", "BTC/USD", 5)
    assert len(kraken) == 1 and kraken[0]["volume"] == 2.0 and kraken[0]["complete"] == 0
    liq = await store.recent_hours("binance_liq", "BTCUSDT", 5)
    assert liq[0]["volume"] == 0.5 and liq[0]["taker_buy_volume"] == 0.0
    assert rec.snapshot().feeds[fr.KRAKEN_SPOT].last_hour_ms == H0


def test_snapshot_lists_every_feed_before_any_check() -> None:
    rec = _recorder()
    feeds = rec.snapshot().feeds
    assert set(feeds) == set(fr.REST_KEYS) | set(fr.WS_KEYS)
    assert all(feeds[k].ok is None and feeds[k].kind == "rest" for k in fr.REST_KEYS)
    assert all(feeds[k].connected is False and feeds[k].kind == "ws" for k in fr.WS_KEYS)


@pytest.mark.asyncio
async def test_run_starts_ws_feeds_and_stops_cleanly(test_db) -> None:
    urls: list[str] = []

    class _Idle:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc) -> bool:
            return False

        async def send(self, text: str) -> None:
            pass

        async def recv(self) -> str:
            await asyncio.sleep(3600)
            return "{}"

    def connect(url: str) -> _Idle:
        urls.append(url)
        return _Idle()

    rec = fr.FlowRecorder(
        interval_s=3600, connect=connect,
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(_handler())),
    )
    stop = asyncio.Event()
    task = asyncio.create_task(_REAL_RUN(rec, stop))
    for _ in range(50):
        if len(urls) == 3:
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, timeout=5)
    assert sorted(urls) == sorted([fr.KRAKEN_SPOT_WS, fr.KRAKEN_FUTURES_WS, fr.BINANCE_FAPI_WS])


def test_current_registry() -> None:
    rec = _recorder()
    fr.set_current(rec)
    try:
        assert fr.current() is rec
    finally:
        fr.set_current(None)
    assert fr.current() is None
```

- [ ] **Step 2: Run them to verify they fail**

Run: `PYTHON_DOTENV_DISABLED=1 python3 -m pytest tests/unit/test_flow_recorder.py -q`
Expected: FAIL with `ImportError: cannot import name 'flow_recorder' from 'polymarket_exec.ops'`

- [ ] **Step 3: Write the implementation**

```python
"""Always-on recorder for venue trade-flow feeds: Binance spot/perp/liquidations and Kraken.

Started from the dashboard lifespan (next to the feed monitor), so hour bars accrue
whether or not the bot loop runs. Each pass (``interval_s``):

* fetches the last closed 1h klines for Binance spot BTCUSDT/ETHUSDT and perp BTCUSDT;
* once per hour, samples perp state (Binance premiumIndex + openInterest, Kraken
  Futures ticker);
* rolls the live WS trade aggregators (Kraken spot, Kraken Futures, Binance
  liquidations) into bars for every hour that has ended.

Pure observation data for the hourly BTC strategy; nothing here decides or gates.
"""
from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import httpx

import config as _config
from logging_setup import get_logger
from polymarket_exec.connectors import venue_messages as vm
from polymarket_exec.connectors.venue_flow import (
    HourAggregator,
    hour_floor_ms,
    parse_binance_klines,
)
from polymarket_exec.connectors.ws_runner import WsStatus, run_ws_forever
from polymarket_exec.storage import venue_flow_store as store

log = get_logger("flow_recorder")

BINANCE_FAPI = "https://fapi.binance.com"
BINANCE_FAPI_WS = "wss://fstream.binance.com/ws"
KRAKEN_SPOT_WS = "wss://ws.kraken.com/v2"
KRAKEN_FUTURES_WS = "wss://futures.kraken.com/ws/v1"
KRAKEN_FUTURES_API = "https://futures.kraken.com/derivatives/api/v3"

DEFAULT_INTERVAL_S = 60.0
# Venue trade clocks can run slightly behind ours; wait this long past an hour
# boundary before closing that hour's WS bar.
ROLL_GRACE_MS = 10_000

BINANCE_SPOT_BTC = "binance_spot:BTCUSDT"
BINANCE_SPOT_ETH = "binance_spot:ETHUSDT"
BINANCE_PERP_BTC = "binance_perp:BTCUSDT"
BINANCE_PERP_STATE = "binance_perp_state:BTCUSDT"
BINANCE_LIQ = "binance_liq:BTCUSDT"
KRAKEN_SPOT = "kraken_spot:BTC/USD"
KRAKEN_FUTURES = "kraken_futures:PF_XBTUSD"
KRAKEN_FUTURES_STATE = "kraken_futures_state:PF_XBTUSD"

# (feed key, market, symbol) for closed-kline REST bars.
_REST_BARS = (
    (BINANCE_SPOT_BTC, "spot", "BTCUSDT"),
    (BINANCE_SPOT_ETH, "spot", "ETHUSDT"),
    (BINANCE_PERP_BTC, "perp", "BTCUSDT"),
)
REST_KEYS = (BINANCE_SPOT_BTC, BINANCE_SPOT_ETH, BINANCE_PERP_BTC, BINANCE_PERP_STATE,
             KRAKEN_FUTURES_STATE)
WS_KEYS = (KRAKEN_SPOT, KRAKEN_FUTURES, BINANCE_LIQ)


@dataclass(frozen=True)
class FeedStatus:
    kind: str  # "rest" | "ws"
    ok: bool | None  # rest: last check (None = not checked yet); ws: None
    connected: bool
    connected_since: float | None
    last_event_at: float | None  # rest: last success; ws: last frame
    last_hour_ms: int | None  # newest bar written for this feed
    detail: str | None


@dataclass(frozen=True)
class FlowSnapshot:
    taken_at: float
    started_at: float
    interval_s: float
    feeds: dict[str, FeedStatus]


@dataclass(frozen=True)
class _RestState:
    ok: bool
    last_ok_at: float | None
    detail: str | None


class _Unusable(Exception):
    """The venue answered, but not with anything recordable."""


def _default_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=15.0)


class FlowRecorder:
    def __init__(
        self,
        *,
        interval_s: float = DEFAULT_INTERVAL_S,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
        connect: Callable[[str], Any] | None = None,
        time_fn: Callable[[], float] = time.time,
    ) -> None:
        self.interval_s = interval_s
        self._client_factory = client_factory or _default_client
        self._connect = connect
        self._time_fn = time_fn
        self._started_at = time_fn()
        self._aggs = self._new_aggregators(int(self._started_at * 1000))
        self._ws_status = {key: WsStatus() for key in WS_KEYS}
        self._rest: dict[str, _RestState] = {}
        self._last_hour: dict[str, int] = {}
        self._snapshot_hour: dict[str, int] = {}

    @staticmethod
    def _new_aggregators(started_at_ms: int) -> dict[str, HourAggregator]:
        return {
            KRAKEN_SPOT: HourAggregator(venue="kraken_spot", symbol="BTC/USD",
                                        source="ws_v2_trade", started_at_ms=started_at_ms),
            KRAKEN_FUTURES: HourAggregator(venue="kraken_futures", symbol="PF_XBTUSD",
                                           source="ws_v1_trade", started_at_ms=started_at_ms),
            BINANCE_LIQ: HourAggregator(venue="binance_liq", symbol="BTCUSDT",
                                        source="ws_forceOrder", started_at_ms=started_at_ms),
        }

    def aggregator(self, key: str) -> HourAggregator:
        return self._aggs[key]

    def snapshot(self) -> FlowSnapshot:
        feeds: dict[str, FeedStatus] = {}
        for key in REST_KEYS:
            st = self._rest.get(key)
            feeds[key] = FeedStatus(
                kind="rest",
                ok=None if st is None else st.ok,
                connected=False,
                connected_since=None,
                last_event_at=None if st is None else st.last_ok_at,
                last_hour_ms=self._last_hour.get(key),
                detail=None if st is None else st.detail,
            )
        for key in WS_KEYS:
            ws = self._ws_status[key]
            feeds[key] = FeedStatus(
                kind="ws",
                ok=None,
                connected=ws.connected,
                connected_since=ws.connected_since,
                last_event_at=ws.last_message_at,
                last_hour_ms=self._last_hour.get(key),
                detail=ws.last_error,
            )
        return FlowSnapshot(self._time_fn(), self._started_at, self.interval_s, feeds)

    async def run(self, stop_event: asyncio.Event) -> None:
        """Hold the WS trade feeds and record every ``interval_s`` until ``stop_event``."""
        self._started_at = self._time_fn()
        self._aggs = self._new_aggregators(int(self._started_at * 1000))
        specs: list[tuple[str, str, dict[str, Any], Callable[[dict[str, Any]], list]]] = [
            (KRAKEN_SPOT, KRAKEN_SPOT_WS,
             {"method": "subscribe",
              "params": {"channel": "trade", "symbol": ["BTC/USD"], "snapshot": False}},
             vm.kraken_spot_trades),
            (KRAKEN_FUTURES, KRAKEN_FUTURES_WS,
             {"event": "subscribe", "feed": "trade", "product_ids": ["PF_XBTUSD"]},
             vm.kraken_futures_trades),
            (BINANCE_LIQ, BINANCE_FAPI_WS,
             {"method": "SUBSCRIBE", "params": ["!forceOrder@arr"], "id": 1},
             lambda msg: [t] if (t := vm.binance_liquidation(msg)) else []),
        ]
        tasks = [
            asyncio.create_task(
                run_ws_forever(
                    name=key,
                    url=url,
                    subscribe=subscribe,
                    on_message=self._trade_handler(key, parse),
                    on_gap=self._aggs[key].mark_gap,
                    stop_event=stop_event,
                    status=self._ws_status[key],
                    connect=self._connect,
                    time_fn=self._time_fn,
                )
            )
            for key, url, subscribe, parse in specs
        ]
        try:
            async with self._client_factory() as client:
                while not stop_event.is_set():
                    await self.record_once(client, int(self._time_fn() * 1000))
                    with suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(stop_event.wait(), timeout=self.interval_s)
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with suppress(asyncio.CancelledError, Exception):
                    await task

    def _trade_handler(
        self, key: str, parse: Callable[[dict[str, Any]], list]
    ) -> Callable[[dict[str, Any]], None]:
        def handle(msg: dict[str, Any]) -> None:
            agg = self._aggs[key]
            try:
                for ts_ms, price, qty, side in parse(msg):
                    agg.add(ts_ms, price, qty, side)
            except (KeyError, TypeError, ValueError) as exc:
                log.warning("flow_recorder.bad_frame", feed=key, error=str(exc)[:200])

        return handle

    async def record_once(self, client: httpx.AsyncClient, now_ms: int) -> None:
        """One recording pass. Never raises; each feed's failure is kept for the FEEDS card."""
        for key, market, symbol in _REST_BARS:
            await self._guard(key, lambda k=key, m=market, s=symbol: self._rest_bars(
                client, k, m, s, now_ms))
        hour = hour_floor_ms(now_ms)
        for key, take in ((BINANCE_PERP_STATE, self._binance_state),
                          (KRAKEN_FUTURES_STATE, self._kraken_state)):
            if self._snapshot_hour.get(key) != hour and await self._guard(
                key, lambda t=take: t(client, now_ms)
            ):
                self._snapshot_hour[key] = hour
        for key, agg in self._aggs.items():
            bars = agg.roll(now_ms - ROLL_GRACE_MS)
            if not bars:
                continue
            try:
                await store.upsert_hour_bars(bars)
            except Exception as exc:  # noqa: BLE001 — a DB hiccup must not stop recording
                log.warning("flow_recorder.store_failed", feed=key, error=str(exc)[:200])
            else:
                self._last_hour[key] = bars[-1].hour_start_ms

    async def _guard(self, key: str, check: Callable[[], Awaitable[None]]) -> bool:
        previous = self._rest.get(key)
        try:
            await check()
        except Exception as exc:  # noqa: BLE001 — every failure is a feed status
            detail = str(exc) if isinstance(exc, _Unusable) else f"{type(exc).__name__}: {exc}"
            if previous is None or previous.ok:
                log.warning("flow_recorder.feed_down", feed=key, error=detail[:200])
            self._rest[key] = _RestState(False, previous.last_ok_at if previous else None,
                                         detail[:200])
            return False
        if previous is not None and not previous.ok:
            log.info("flow_recorder.feed_recovered", feed=key)
        self._rest[key] = _RestState(True, self._time_fn(), None)
        return True

    async def _rest_bars(
        self, client: httpx.AsyncClient, key: str, market: str, symbol: str, now_ms: int
    ) -> None:
        if market == "spot":
            url, venue = f"{_config.BINANCE_API_BASE}/api/v3/klines", "binance_spot"
        else:
            url, venue = f"{BINANCE_FAPI}/fapi/v1/klines", "binance_perp"
        resp = await client.get(url, params={"symbol": symbol, "interval": "1h", "limit": 3})
        resp.raise_for_status()
        bars = parse_binance_klines(resp.json(), venue=venue, symbol=symbol, now_ms=now_ms)
        if not bars:
            raise _Unusable("no closed candle returned")
        await store.upsert_hour_bars(bars)
        self._last_hour[key] = bars[-1].hour_start_ms

    async def _binance_state(self, client: httpx.AsyncClient, now_ms: int) -> None:
        premium = await client.get(f"{BINANCE_FAPI}/fapi/v1/premiumIndex",
                                   params={"symbol": "BTCUSDT"})
        premium.raise_for_status()
        oi = await client.get(f"{BINANCE_FAPI}/fapi/v1/openInterest", params={"symbol": "BTCUSDT"})
        oi.raise_for_status()
        await store.insert_snapshot(
            vm.binance_perp_snapshot(premium.json(), oi.json(), symbol="BTCUSDT")
        )

    async def _kraken_state(self, client: httpx.AsyncClient, now_ms: int) -> None:
        resp = await client.get(f"{KRAKEN_FUTURES_API}/tickers")
        resp.raise_for_status()
        snap = vm.kraken_futures_snapshot(resp.json(), symbol="PF_XBTUSD", now_ms=now_ms)
        if snap is None:
            raise _Unusable("PF_XBTUSD missing from tickers")
        await store.insert_snapshot(snap)


# Process-wide recorder, set by the dashboard lifespan. None outside the app.
_current: FlowRecorder | None = None


def set_current(recorder: FlowRecorder | None) -> None:
    global _current
    _current = recorder


def current() -> FlowRecorder | None:
    return _current
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHON_DOTENV_DISABLED=1 python3 -m pytest tests/unit/test_flow_recorder.py -q`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
git add polymarket_exec/ops/flow_recorder.py tests/unit/test_flow_recorder.py
git commit -m "feat(feeds): always-on flow recorder for Binance spot/perp/liquidations and Kraken"
```

---

### Task 6: Start the recorder with the dashboard (and keep tests offline)

**Files:**
- Modify: `polymarket_exec/ops/dashboard/app.py` (`_lifespan`)
- Modify: `tests/conftest.py` (add an autouse fixture after `_no_feed_monitor_network`)
- Test: `tests/unit/test_flow_recorder.py` (append)

**Interfaces:**
- Consumes: `FlowRecorder`, `set_current`, `current` (Task 5).
- Produces: while the dashboard app is up, `flow_recorder.current()` returns the running recorder.

- [ ] **Step 1: Write the failing test** (append to `tests/unit/test_flow_recorder.py`)

```python
def test_dashboard_lifespan_registers_and_clears_the_recorder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "t.db")
    from fastapi.testclient import TestClient

    from polymarket_exec.ops.dashboard.app import app

    with TestClient(app):
        assert isinstance(fr.current(), fr.FlowRecorder)
    assert fr.current() is None
```

- [ ] **Step 2: Run it to verify it fails**

Run: `PYTHON_DOTENV_DISABLED=1 python3 -m pytest tests/unit/test_flow_recorder.py::test_dashboard_lifespan_registers_and_clears_the_recorder -q`
Expected: FAIL with `assert False` (`current()` is `None`)

- [ ] **Step 3: Add the conftest stub** (in `tests/conftest.py`, directly after `_no_feed_monitor_network`)

```python
@pytest.fixture(autouse=True)
def _no_flow_recorder_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dashboard tests boot the app lifespan; keep the venue flow recorder offline.

    The recorder's REST and WS paths are unit-tested with fakes in test_flow_recorder.py.
    """
    from polymarket_exec.ops.flow_recorder import FlowRecorder

    async def _idle_run(self, stop_event):  # noqa: ANN001
        await stop_event.wait()

    monkeypatch.setattr(FlowRecorder, "run", _idle_run)
```

- [ ] **Step 4: Wire the lifespan** (in `polymarket_exec/ops/dashboard/app.py`)

After the feed monitor block (`_paper.set_shared_chainlink_feed(monitor.chainlink_ws)`), add:

```python
    # Venue flow recorder: hourly trade-flow bars from Binance (spot, perp,
    # liquidations) and Kraken (spot, futures) for the hourly BTC strategy,
    # recorded whether or not the bot loop runs.
    from polymarket_exec.ops import flow_recorder as _flow_recorder

    recorder = _flow_recorder.FlowRecorder()
    flow_stop_event = asyncio.Event()
    flow_task = asyncio.create_task(recorder.run(flow_stop_event))
    _flow_recorder.set_current(recorder)
```

After `yield`, next to `_feed_monitor.set_current(None)`, add:

```python
    _flow_recorder.set_current(None)
```

and add `(flow_stop_event, flow_task),` to the shutdown tuple:

```python
    for stop_event, task in (
        (daily_stop_event, daily_task),
        (quote_stop_event, quote_task),
        (feeds_stop_event, feeds_task),
        (flow_stop_event, flow_task),
    ):
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `PYTHON_DOTENV_DISABLED=1 python3 -m pytest tests/unit/test_flow_recorder.py tests/unit/test_dashboard.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add polymarket_exec/ops/dashboard/app.py tests/conftest.py tests/unit/test_flow_recorder.py
git commit -m "feat(feeds): run the flow recorder for the dashboard's lifetime"
```

---

### Task 7: Flow feed rows on the FEEDS card

**Files:**
- Modify: `polymarket_exec/ops/dashboard/panels/feeds.py`
- Modify: `polymarket_exec/ops/dashboard/execution_view.py` (the FEEDS render call)
- Test: `tests/unit/test_feeds_panel.py` (append)

**Interfaces:**
- Consumes: `FlowSnapshot`, `FeedStatus`, the feed keys (Task 5).
- Produces:
  - `feeds.build_rows(snap: fm.FeedsSnapshot | None, flow: fr.FlowSnapshot | None = None) -> list[FeedRow]`
  - `feeds.render(snap: fm.FeedsSnapshot | None, flow: fr.FlowSnapshot | None = None) -> str`
  - `feeds.WS_STALE_S: float = 120.0`
  - `feeds.WS_CONNECT_GRACE_S: float = 30.0`

- [ ] **Step 1: Write the failing tests** (append to `tests/unit/test_feeds_panel.py`)

```python
from polymarket_exec.ops import flow_recorder as fr


def _flow(feeds: dict, taken_at: float = 10_000.0, started_at: float = 9_000.0) -> fr.FlowSnapshot:
    base = {k: fr.FeedStatus("rest", None, False, None, None, None, None) for k in fr.REST_KEYS}
    base.update({k: fr.FeedStatus("ws", None, False, None, None, None, None) for k in fr.WS_KEYS})
    base.update(feeds)
    return fr.FlowSnapshot(taken_at, started_at, 60.0, base)


def _flow_rows(flow: fr.FlowSnapshot | None) -> dict:
    return {(r.name, r.role): r for r in feeds.build_rows(None, flow)}


def test_no_flow_rows_without_a_recorder() -> None:
    # Keeps the monitor-only card (and its existing tests) unchanged outside the app.
    names = {r.name for r in feeds.build_rows(None)}
    assert "Kraken BTC/USD" not in names and "Binance liquidations" not in names


def test_flow_rest_rows_ok_down_stale_and_checking() -> None:
    flow = _flow({
        fr.BINANCE_SPOT_BTC: fr.FeedStatus("rest", True, False, None, 9_990.0, 1, None),
        fr.BINANCE_PERP_BTC: fr.FeedStatus("rest", False, False, None, None, None,
                                           "HTTPStatusError: 503"),
        fr.BINANCE_SPOT_ETH: fr.FeedStatus("rest", True, False, None, 9_000.0, 1, None),
    })
    rows = _flow_rows(flow)
    assert rows[("Binance BTCUSDT", "hourly flow")].status == "OK"
    perp = rows[("Binance perp BTCUSDT", "hourly flow")]
    assert (perp.status, perp.level, perp.detail) == ("DOWN", "down", "HTTPStatusError: 503")
    assert rows[("Binance ETHUSDT", "hourly flow")].status == "STALE"  # 1000 s > 3 × 60 s
    assert rows[("Kraken PF_XBTUSD", "funding · OI")].status == "CHECKING"


def test_flow_ws_rows_connecting_ok_stale_quiet_down() -> None:
    flow = _flow(
        {
            fr.KRAKEN_SPOT: fr.FeedStatus("ws", None, True, 9_500.0, 9_995.0, None, None),
            fr.KRAKEN_FUTURES: fr.FeedStatus("ws", None, True, 9_500.0, 9_600.0, None, None),
            fr.BINANCE_LIQ: fr.FeedStatus("ws", None, False, None, None, None,
                                          "ConnectionError: closed"),
        },
    )
    rows = _flow_rows(flow)
    assert rows[("Kraken BTC/USD", "hourly flow")].status == "OK"
    assert rows[("Kraken PF_XBTUSD", "hourly flow")].status == "QUIET"  # sparse feed, 400 s
    liq = rows[("Binance liquidations", "liquidation flow")]
    assert (liq.status, liq.detail) == ("DOWN", "ConnectionError: closed")
    stale = _flow_rows(_flow({
        fr.KRAKEN_SPOT: fr.FeedStatus("ws", None, True, 9_000.0, 9_500.0, None, None)}))
    assert stale[("Kraken BTC/USD", "hourly flow")].status == "STALE"
    early = _flow_rows(_flow({}, taken_at=9_010.0, started_at=9_000.0))
    assert early[("Kraken BTC/USD", "hourly flow")].status == "CONNECTING"


def test_render_includes_flow_rows() -> None:
    html = feeds.render(None, _flow({}))
    assert "Kraken BTC/USD" in html and "Binance liquidations" in html
```

- [ ] **Step 2: Run them to verify they fail**

Run: `PYTHON_DOTENV_DISABLED=1 python3 -m pytest tests/unit/test_feeds_panel.py -q`
Expected: FAIL with `TypeError: build_rows() takes 1 positional argument but 2 were given`

- [ ] **Step 3: Implement the rows** (in `polymarket_exec/ops/dashboard/panels/feeds.py`)

Add to the imports:

```python
from polymarket_exec.ops import flow_recorder as fr
```

Add these below `_REST_FEEDS`:

```python
# (flow feed key, name, used for, source, quiet_ok) — venue flow rows, in card order.
# quiet_ok: silence on a connected socket is normal (sparse futures fills, liquidations).
_FLOW_FEEDS = (
    (fr.BINANCE_SPOT_BTC, "Binance BTCUSDT", "hourly flow", "REST klines", False),
    (fr.BINANCE_SPOT_ETH, "Binance ETHUSDT", "hourly flow", "REST klines", False),
    (fr.BINANCE_PERP_BTC, "Binance perp BTCUSDT", "hourly flow", "fapi REST", False),
    (fr.BINANCE_PERP_STATE, "Binance perp BTCUSDT", "funding · OI", "fapi REST", False),
    (fr.BINANCE_LIQ, "Binance liquidations", "liquidation flow", "fstream WS", True),
    (fr.KRAKEN_SPOT, "Kraken BTC/USD", "hourly flow", "WS v2 trades", False),
    (fr.KRAKEN_FUTURES, "Kraken PF_XBTUSD", "hourly flow", "WS v1 trades", True),
    (fr.KRAKEN_FUTURES_STATE, "Kraken PF_XBTUSD", "funding · OI", "tickers REST", False),
)
# A connected trade socket with no frame for this long is flagged (or QUIET if normal).
WS_STALE_S = 120.0
# A socket that has not connected yet this soon after start is "connecting", not down.
WS_CONNECT_GRACE_S = 30.0


def _flow_row(
    flow: fr.FlowSnapshot, key: str, name: str, role: str, source: str, quiet_ok: bool
) -> FeedRow:
    st = flow.feeds.get(key)
    if st is None or (st.kind == "rest" and st.ok is None):
        return FeedRow(name, role, source, "—", "CHECKING", "idle")
    if st.kind == "rest":
        age = flow.taken_at - st.last_event_at if st.last_event_at is not None else None
        delay = _secs(age) if age is not None else "—"
        if not st.ok:
            return FeedRow(name, role, source, delay, "DOWN", "down", False, st.detail)
        if age is not None and age > flow.interval_s * 3:
            return FeedRow(name, role, source, delay, "STALE", "warn", True,
                           f"last success {delay} ago")
        return FeedRow(name, role, source, delay, "OK", "on")
    last = st.last_event_at if st.last_event_at is not None else st.connected_since
    age = flow.taken_at - last if last is not None else None
    delay = _secs(flow.taken_at - st.last_event_at) if st.last_event_at is not None else "—"
    if not st.connected:
        if flow.taken_at - flow.started_at <= WS_CONNECT_GRACE_S:
            return FeedRow(name, role, source, delay, "CONNECTING", "idle")
        return FeedRow(name, role, source, delay, "DOWN", "down", False,
                       st.detail or "not connected (reconnecting)")
    if age is not None and age > WS_STALE_S:
        if quiet_ok:
            return FeedRow(name, role, source, delay, "QUIET", "idle", False,
                           "connected; no events lately (normal for this feed)")
        return FeedRow(name, role, source, delay, "STALE", "warn", True,
                       "connected, but no recent trades")
    return FeedRow(name, role, source, delay, "OK", "on")
```

Replace `build_rows` and `render`'s signature with:

```python
def build_rows(
    snap: fm.FeedsSnapshot | None, flow: fr.FlowSnapshot | None = None
) -> list[FeedRow]:
    if snap is None:
        # Feed monitor not running (only outside the dashboard app).
        names = [("Chainlink BTC/USD", "spot · vol", "Polymarket WS")] + [
            (n, r, s) for _k, n, r, s in _REST_FEEDS
        ]
        rows = [FeedRow(n, r, s, "—", "OFF", "idle") for n, r, s in names]
    else:
        rows = [_ws_row(snap)] + [_rest_row(snap, *feed) for feed in _REST_FEEDS]
    if flow is not None:
        rows += [_flow_row(flow, *feed) for feed in _FLOW_FEEDS]
    return rows


def render(snap: fm.FeedsSnapshot | None, flow: fr.FlowSnapshot | None = None) -> str:
    rows = build_rows(snap, flow)
```

The rest of `render` stays unchanged.

- [ ] **Step 4: Pass the snapshot in** (in `polymarket_exec/ops/dashboard/execution_view.py`)

Add `from polymarket_exec.ops import flow_recorder` next to `from polymarket_exec.ops import feed_monitor`, then replace the FEEDS render lines with:

```python
    monitor = feed_monitor.current()
    recorder = flow_recorder.current()
    feeds_html = feeds.render(
        monitor.snapshot() if monitor is not None else None,
        recorder.snapshot() if recorder is not None else None,
    )
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `PYTHON_DOTENV_DISABLED=1 python3 -m pytest tests/unit/test_feeds_panel.py tests/unit/test_dashboard.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add polymarket_exec/ops/dashboard/panels/feeds.py polymarket_exec/ops/dashboard/execution_view.py tests/unit/test_feeds_panel.py
git commit -m "feat(feeds): FEEDS card rows for Binance spot/perp/liquidations and Kraken flow"
```

---

### Task 8: Docs, generated inventory, and gates

**Files:**
- Modify: `docs/CODE_MAP.md` (routing table, hand-written part)
- Regenerate: `AGENTS.md`, `docs/CODE_MAP.md`, `docs/FILE_MAP.md` (generated blocks)

**Interfaces:**
- Consumes: all tasks.
- Produces: a clean `gen_docs.py --check` and green gates.

- [ ] **Step 1: Add the routing row** — in `docs/CODE_MAP.md`, under `## "I want to change X → edit Y"`, directly after the `Feed health checks behind the FEEDS card` row:

```markdown
| Venue trade-flow feeds (Binance spot/perp/liquidations, Kraken spot/futures) | `polymarket_exec/ops/flow_recorder.py` (recording) + `connectors/venue_flow.py`, `connectors/venue_messages.py`, `connectors/ws_runner.py` + `storage/venue_flow_store.py` (tables `venue_flow_hourly`, `venue_snapshot`) |
```

- [ ] **Step 2: Regenerate docs with the full test count**

Run: `PYTHON_DOTENV_DISABLED=1 python3 tools/gen_docs.py && PYTHON_DOTENV_DISABLED=1 python3 tools/gen_docs.py --check`
Expected: `--check` exits 0

- [ ] **Step 3: Run every gate**

Run:
```bash
PYTHON_DOTENV_DISABLED=1 DATA_DIR="$(mktemp -d)" python3 -m pytest tests/ -q
python3 -m ruff check polymarket_exec/ polymarket_bot/ tests/ tools/
```
Expected: all tests pass and ruff reports `All checks passed!`; then run the banned-strings grep from the `docs-drift` job in `.github/workflows/ci.yml` and confirm it prints nothing

- [ ] **Step 4: Smoke-run the dashboard and confirm the rows go green**

Start the app from this branch using the `.claude/launch.json` preview config. Open the dashboard, wait about 90 s, then read the FEEDS card:
- the Binance REST rows and both `funding · OI` rows show `OK`;
- `Kraken BTC/USD` shows `OK`;
- `Kraken PF_XBTUSD` shows `OK` or `QUIET`;
- `Binance liquidations` shows `OK` or `QUIET`.

After the next hour boundary plus about 60 s, confirm rows exist:

```bash
sqlite3 data/btc_5m_binary_fair_value.db "SELECT venue, symbol, datetime(hour_start_ms/1000,'unixepoch'), volume, complete FROM venue_flow_hourly ORDER BY hour_start_ms DESC LIMIT 8;"
```

Expected: rows for all 5 venues. WS rows for the first hour have `complete=0` (the recorder started mid-hour); the next hour's are `complete=1`.

- [ ] **Step 5: Commit**

```bash
git add docs/CODE_MAP.md AGENTS.md docs/FILE_MAP.md
git commit -m "docs: route venue flow feeds; regenerate code map and file map"
```
