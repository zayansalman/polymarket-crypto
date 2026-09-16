# Hourly Engine + Hourly Mean Reversion Implementation Plan (PR 1 of 4)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The trading loop trades Polymarket's hourly BTC Up/Down market with the Hourly Mean Reversion strategy, in whichever mode the operator selects (paper or live):
- one decision per hour;
- one open position per strategy, in both modes;
- settlement from the Binance 1h candle;
- a per-hour decision record for every hour;
- paper settlement charged the same taker fee live pays.

**Operator rule (2026-09-14):** there is no paper-only strategy. A strategy runs in the mode the operator selects at Start. Nothing in this PR refuses live for 1h or builds a paper-only path.

**Architecture:**
- New package `polymarket_bot/hourly/`:
  - `market.py`: window timing, Gamma discovery, Binance candles and settlement.
  - `mean_reversion.py`: the pure signal.
  - `ledger.py`: `hourly_strategy_context` table ops.
  - `engine.py`: one hourly tick; entries and settlement route through paper or the live slot by mode.
- `paper.py` routes a tick to the engine when the loop was started on the BTC 1h selection. The controller pins the selection at Start, exactly like mode.
- Each strategy owns one position slot. The RiskGate is unchanged: `EntryRequest.position_open` now means "this strategy's slot already holds a position".
- Live: the `LiveExecutor` built at Start is the **account executor** (legacy loop slot). `slot_executor(strategy_id)` returns a per-strategy executor sharing its authenticated client and RiskGate, with its own position/order tracking. Kill switch, Stop and boot reconciliation cover every slot. Journal rows carry `strategy_id` so two strategies in the same hour never adopt each other's orders.

**Tech Stack:** Python 3.11+, asyncio, httpx, aiosqlite, pytest + pytest-asyncio (strict mode). Stdlib `statistics` for the signal math (no pandas).

**Spec:** `docs/strategies/hourly-btc-strategies.md` (operator-facing strategy doc) and `docs/superpowers/specs/2026-09-14-hourly-btc-flow-strategy-design.md` (PR #234).

**Roadmap (separate plans; each runs in both modes):**
- PR 2: Kronos worker + Kronos BTC Fine Tune.
- PR 3: order style choice (pay the ask / resting post-only at the bid, cancelled at the deadline).
- PR 4: per-strategy results panel and venue-flow factors (after #234 merges).

## Global Constraints

- **Worktree:** `/Users/zayankhan/projects/polymarket-crypto/.claude/worktrees/hourly-btc-strategies`, branch `feature/hourly-btc-strategies` (from `origin/develop` @ `0c21b10`). PR into `develop`.
- **RiskGate:** never edit `polymarket_exec/execution/gate.py` or its tests.
- **Mode:** strategies never read mode. The engine's entry and settlement steps are the only places that branch on it, and both modes run the identical decision. Never add a live refusal for 1h.
- **Live safety:** agents never click LIVE or Start and never place live orders. Live behaviour is proven with mocked CLOB clients only.
- **Market rules:**
  - Resolution: **Up iff the Binance BTCUSDT 1h candle close ≥ its open** (ties Up). The candle is the one whose open time equals the market's `eventStartTime`.
  - Hourly slug: `polymarket_exec/connectors/updown_quote.py:window_slug("btc", "1h", <tz-aware datetime>)`.
  - Amended (Claude, 2026-09-15, branch-review finding dst-fallback-slug-collision): the ET slug repeats on the November fall-back day (2026-11-01 05:00Z and 06:00Z are both `1am-et`). Hourly records are keyed by `window_start_ts` (UTC), and discovery falls back to the Gamma series `btc-up-or-down-hourly` (id 10114) by `eventStartTime` when the slug lookup misses.
- **Binance endpoints:**
  - Spot: `config.BINANCE_API_BASE` + `/api/v3/klines`.
  - Perp: `https://fapi.binance.com/fapi/v1/klines`.
  - Kline row: `[open_time, open, high, low, close, volume, close_time, quote_volume, trades, taker_buy_base, taker_buy_quote, ignore]`.
  - Closed-candle read (`fetch_closed_candles`, no `startTime`): Binance always returns its own current (forming) candle as the last row, so drop that last row, then keep only rows with `close_time < now_ms`. Both checks together mean a local clock running ahead of Binance cannot hand the strategy a previous hour that is still forming.
  - Settlement read (`fetch_hour_candle`): the hour candle is closed only when `close_time < now_ms` **and** Binance already returns the next hour's candle (request `limit: 2`; the second row's open time is `start + 3600` s). Binance opens the next candle only after processing every trade of this one, so a local clock running ahead cannot settle on a forming candle.
- **Hourly Mean Reversion constants (frozen):**
  - `WINDOW = 168`, `SPOT_FZ_MIN = 1.20`, `PERP_FZ_MAX = 1.24`, `CLV_MIN = 0.80`.
  - z uses the sample standard deviation (ddof = 1) over the 168 hourly imbalances **ending at and including** hour H-1.
- **Strategy ids:** `hourly_mean_reversion` (this PR) and `kronos_btc_finetune` (PR 2). Timeframe value: `1h`.
- **Taker fee:** `polymarket_bot/shadow/fees.py:taker_fee_per_share(price)` = `0.07 × p × (1 − p)`.
- **Tests:**
  - Run with `PYTHON_DOTENV_DISABLED=1`.
  - Isolate the DB: `monkeypatch.setattr(_db, "DB_PATH", tmp_path / "t.db")` then `await _db.init_db()`.
  - Never touch the network: use `httpx.MockTransport` or fake clients.
- **Lint:** `ruff==0.15.12`, line length 100. Every new `.py` starts with a one-line module docstring.
- **Gates before the PR:**
  1. `PYTHON_DOTENV_DISABLED=1 DATA_DIR="$(mktemp -d)" python3 -m pytest tests/ -q`
  2. `python3 -m ruff check polymarket_exec/ polymarket_bot/ tests/ tools/`
  3. `PYTHON_DOTENV_DISABLED=1 python3 tools/gen_docs.py`, then `… --check`
  4. The banned-strings grep from the `docs-drift` job in `.github/workflows/ci.yml`

## File Structure

| File | Responsibility |
|---|---|
| Create `polymarket_bot/hourly/__init__.py` | Package marker |
| Create `polymarket_bot/hourly/market.py` | `HOUR_S`, `hour_start`, `slug_for`, `HourMarket`, `Candle`, `HourCandle`, `discover`, `fetch_closed_candles`, `fetch_hour_candle`, `fetch_spot`, `up_won` |
| Create `polymarket_bot/hourly/mean_reversion.py` | `FlowPush`, `Decision`, `flow_push`, `decide` (pure) |
| Modify `db.py` | `hourly_strategy_context` table; `paper_positions` gains `strategy_id`, `market_timeframe`, `window_start_ts` |
| Create `polymarket_bot/hourly/ledger.py` | Context rows: `record_decision`, `get_decision`, `set_action`, `unsettled_windows`, `settle_window` |
| Modify `db.py` (again) | `live_orders.strategy_id`; `journal_live_order(strategy_id=…)` |
| Modify `polymarket_exec/execution/live.py` | Strategy slots: `slot_executor`, `cancel_open_all`, kill switch over every slot, per-slot boot reconciliation, hourly window resolution, journal rows tagged with the slot |
| Modify `polymarket_bot/paper.py` | Paper settled closes net of taker fee; 5m paths ignore 1h rows; `_executor_for(pos)`; `run_paper_loop(..., timeframe)`; route 1h ticks to the engine; Stop cancels every slot and closes 1h rows at the current hour's bid |
| Create `polymarket_bot/hourly/engine.py` | `build_snapshot`, `settle_due`, `decide_hour`, `open_entries`, `tick` (paper and live) |
| Modify `polymarket_bot/runtime_knobs.py` | `hourly_mean_reversion_enabled`, `hourly_entry_deadline_seconds` |
| Modify `polymarket_bot/controller.py` | Pin the market selection at Start; pass timeframe to the runner |
| Modify `polymarket_bot/market_selection.py` | `LOOP_SUPPORTED` gains `("btc", "1h")` |
| Modify `polymarket_exec/ops/dashboard/panels/market_selector.py` | Open-position glow recognises hourly slugs |
| Modify `AGENTS.md`, `docs/CODE_MAP.md` | Rule "one open position per strategy, per mode"; BTC hourly authorized for live (operator, 2026-09-14); routing row; regenerate docs |
| Tests | `tests/unit/test_hourly_market.py`, `test_hourly_mean_reversion.py`, `test_hourly_ledger.py`, `test_hourly_engine.py`, `test_hourly_loop.py`; appended slot tests in `test_live_executor.py`; edits in `test_loop_watchdog.py`, `test_settle_style.py` |

---

### Task 1: Hourly market helpers

**Files:**
- Create: `polymarket_bot/hourly/__init__.py`, `polymarket_bot/hourly/market.py`
- Test: `tests/unit/test_hourly_market.py`

**Interfaces:**
- Produces:
  - `HOUR_S = 3600`; `BINANCE_FAPI = "https://fapi.binance.com"`
  - `hour_start(now: int) -> int`; `slug_for(start_ts: int) -> str`
  - `@dataclass(frozen=True) HourMarket(slug: str, question: str, window_start_ts: int, up_token_id: str, down_token_id: str)`
  - `@dataclass(frozen=True) Candle(open_time_ms: int, open: float, high: float, low: float, close: float, volume: float, quote_volume: float, taker_buy_volume: float)`
  - `@dataclass(frozen=True) HourCandle(open: float, close: float, closed: bool)`
  - `async discover(client, start_ts: int) -> HourMarket | None` (amended, Claude, 2026-09-15, branch-review finding dst-fallback-slug-collision: if the slug lookup misses or its `eventStartTime` is another hour, query `/events?series_id=10114` by end date and take the market whose `eventStartTime` is `start_ts`; `HourMarket.slug` is the venue's own slug)
  - `async fetch_closed_candles(client, *, market: str, symbol: str, now_ms: int, limit: int) -> list[Candle]` (`market` is `"spot"` or `"perp"`)
  - `async fetch_hour_candle(client, start_ts: int, now_ms: int) -> HourCandle | None`
  - `async fetch_spot(client) -> float | None`
  - `up_won(open_price: float, close_price: float) -> bool`

- [ ] **Step 1: Write the failing tests**

```python
"""Hourly BTC market helpers: slug/timing, Gamma discovery, Binance candles and settlement."""
from __future__ import annotations

import json

import httpx
import pytest

import config as _config
from polymarket_bot.hourly import market as hm

H = 1_789_326_000  # 2026-09-13 19:00:00 UTC = 3PM ET (EDT)


def _row(slug: str, start_iso: str | None = "2026-09-13T19:00:00Z") -> dict:
    row = {
        "slug": slug,
        "question": "Bitcoin Up or Down - September 13, 3PM ET",
        "outcomes": json.dumps(["Up", "Down"]),
        "clobTokenIds": json.dumps(["111", "222"]),
    }
    if start_iso is not None:
        row["eventStartTime"] = start_iso
    return row


def _kline(open_s: int, o: float, c: float, *, hi: float | None = None,
           lo: float | None = None, vol: float = 10.0, tb: float = 5.0) -> list:
    return [open_s * 1000, str(o), str(hi if hi is not None else max(o, c)),
            str(lo if lo is not None else min(o, c)), str(c), str(vol),
            open_s * 1000 + 3_600_000 - 1, "0", 7, str(tb), "0", "0"]


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_hour_start_and_slug() -> None:
    assert hm.hour_start(H + 1799) == H
    assert hm.slug_for(H) == "bitcoin-up-or-down-september-13-2026-3pm-et"
    assert hm.slug_for(H - 15 * 3600) == "bitcoin-up-or-down-september-13-2026-12am-et"


def test_up_won_tie_goes_up() -> None:
    assert hm.up_won(100.0, 100.0) is True
    assert hm.up_won(100.0, 99.99) is False


@pytest.mark.asyncio
async def test_discover_returns_tokens_and_checks_event_start() -> None:
    slug = hm.slug_for(H)

    def handle(request: httpx.Request) -> httpx.Response:
        assert str(request.url).startswith(f"{_config.POLYMARKET_GAMMA_API}/markets")
        assert request.url.params["slug"] == slug
        return httpx.Response(200, json=[_row(slug)])

    async with _client(handle) as client:
        m = await hm.discover(client, H)
    assert m == hm.HourMarket(slug, "Bitcoin Up or Down - September 13, 3PM ET", H, "111", "222")


@pytest.mark.asyncio
async def test_discover_rejects_wrong_event_start_and_missing_market() -> None:
    slug = hm.slug_for(H)
    async with _client(lambda r: httpx.Response(
            200, json=[_row(slug, "2026-09-13T20:00:00Z")])) as client:
        assert await hm.discover(client, H) is None
    async with _client(lambda r: httpx.Response(200, json=[])) as client:
        assert await hm.discover(client, H) is None
    async with _client(lambda r: httpx.Response(200, json=[_row(slug, None)])) as client:
        assert (await hm.discover(client, H)).up_token_id == "111"  # field absent: slug is enough


@pytest.mark.asyncio
async def test_fetch_closed_candles_drops_forming_and_routes_perp() -> None:
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json=[_kline(H - 3600, 1, 2, tb=8.0), _kline(H, 2, 3)])

    async with _client(handle) as client:
        spot = await hm.fetch_closed_candles(client, market="spot", symbol="BTCUSDT",
                                             now_ms=(H + 30) * 1000, limit=170)
        perp = await hm.fetch_closed_candles(client, market="perp", symbol="BTCUSDT",
                                             now_ms=(H + 30) * 1000, limit=170)
    assert [c.open_time_ms for c in spot] == [(H - 3600) * 1000]
    assert spot[0].taker_buy_volume == 8.0 and spot[0].close == 2.0
    assert len(perp) == 1
    assert seen[0].startswith(f"{_config.BINANCE_API_BASE}/api/v3/klines")
    assert seen[1].startswith(f"{hm.BINANCE_FAPI}/fapi/v1/klines")

    # Local clock says H-1 is over, but Binance's last row is still H-1 (forming): drop it.
    def clock_ahead(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[_kline(H - 7200, 1, 2), _kline(H - 3600, 2, 3)])

    async with _client(clock_ahead) as client:
        lagged = await hm.fetch_closed_candles(client, market="spot", symbol="BTCUSDT",
                                               now_ms=(H + 2) * 1000, limit=170)
    assert lagged[-1].open_time_ms == (H - 7200) * 1000


@pytest.mark.asyncio
async def test_fetch_hour_candle_open_and_closed_flag() -> None:
    def forming_only(request: httpx.Request) -> httpx.Response:
        assert request.url.params["startTime"] == str(H * 1000)
        assert request.url.params["limit"] == "2"
        return httpx.Response(200, json=[_kline(H, 100.0, 99.0)])

    def with_next(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[_kline(H, 100.0, 99.0), _kline(H + 3600, 99.0, 99.0)])

    async with _client(forming_only) as client:
        forming = await hm.fetch_hour_candle(client, H, (H + 60) * 1000)
        # Local clock says the hour is over, but Binance has not opened the next candle yet.
        clock_ahead = await hm.fetch_hour_candle(client, H, (H + 3600) * 1000)
    async with _client(with_next) as client:
        done = await hm.fetch_hour_candle(client, H, (H + 3600) * 1000)
    assert forming == hm.HourCandle(open=100.0, close=99.0, closed=False)
    assert clock_ahead == hm.HourCandle(open=100.0, close=99.0, closed=False)
    assert done == hm.HourCandle(open=100.0, close=99.0, closed=True)
    async with _client(lambda r: httpx.Response(200, json=[_kline(H + 3600, 1, 1)])) as client:
        assert await hm.fetch_hour_candle(client, H, (H + 7200) * 1000) is None  # wrong hour


@pytest.mark.asyncio
async def test_fetch_spot() -> None:
    async with _client(lambda r: httpx.Response(200, json={"price": "77123.5"})) as client:
        assert await hm.fetch_spot(client) == 77123.5
    async with _client(lambda r: httpx.Response(500)) as client:
        assert await hm.fetch_spot(client) is None
    async with _client(lambda r: httpx.Response(200, json=[])) as client:
        assert await hm.fetch_spot(client) is None  # non-dict body degrades, never raises
```

- [ ] **Step 2: Run them to verify they fail**

Run: `PYTHON_DOTENV_DISABLED=1 python3 -m pytest tests/unit/test_hourly_market.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'polymarket_bot.hourly'`

- [ ] **Step 3: Write the implementation**

`polymarket_bot/hourly/__init__.py`:

```python
"""Hourly BTC Up/Down strategies: Hourly Mean Reversion and Kronos BTC Fine Tune."""
```

`polymarket_bot/hourly/market.py`:

```python
"""Hourly BTC Up/Down market: window timing, Gamma discovery, Binance candles and settlement.

The market resolves Up iff the Binance BTCUSDT 1h candle that starts at the market's
``eventStartTime`` closes at or above its open (ties go Up). Settlement is read from
Binance directly: Gamma stops listing a resolved hourly market about 12 minutes after
close, so nothing here depends on Polymarket still returning a past window.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

import config as _config
from logging_setup import get_logger
from polymarket_exec.connectors.updown_quote import window_slug

log = get_logger("hourly_market")

HOUR_S = 3600
BINANCE_FAPI = "https://fapi.binance.com"


@dataclass(frozen=True)
class HourMarket:
    slug: str
    question: str
    window_start_ts: int
    up_token_id: str
    down_token_id: str


@dataclass(frozen=True)
class Candle:
    open_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float
    taker_buy_volume: float


@dataclass(frozen=True)
class HourCandle:
    open: float
    close: float
    closed: bool


def hour_start(now: int) -> int:
    return now - now % HOUR_S


def slug_for(start_ts: int) -> str:
    return window_slug("btc", "1h", datetime.fromtimestamp(start_ts, UTC))


def up_won(open_price: float, close_price: float) -> bool:
    return close_price >= open_price


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    return value if isinstance(value, list) else []


def _tokens(row: dict[str, Any]) -> tuple[str, str] | None:
    tokens = _json_list(row.get("clobTokenIds"))
    if len(tokens) != 2:
        return None
    labels = [str(o).lower() for o in _json_list(row.get("outcomes"))]
    up = labels.index("up") if "up" in labels else 0
    return str(tokens[up]), str(tokens[1 - up])


async def discover(client: httpx.AsyncClient, start_ts: int) -> HourMarket | None:
    """The hourly BTC market for the hour starting at ``start_ts``, or None."""
    slug = slug_for(start_ts)
    resp = await client.get(f"{_config.POLYMARKET_GAMMA_API}/markets", params={"slug": slug})
    resp.raise_for_status()
    rows = resp.json()
    row = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else None
    if row is None:
        return None
    event_start = row.get("eventStartTime")
    if event_start:
        started = int(datetime.fromisoformat(str(event_start).replace("Z", "+00:00")).timestamp())
        if started != start_ts:
            log.warning("hourly_market.event_start_mismatch", slug=slug,
                        event_start=event_start, expected=start_ts)
            return None
    tokens = _tokens(row)
    if tokens is None:
        return None
    return HourMarket(slug, str(row.get("question") or slug), start_ts, tokens[0], tokens[1])


def _klines_url(market: str) -> str:
    if market == "spot":
        return f"{_config.BINANCE_API_BASE}/api/v3/klines"
    return f"{BINANCE_FAPI}/fapi/v1/klines"


async def fetch_closed_candles(
    client: httpx.AsyncClient, *, market: str, symbol: str, now_ms: int, limit: int
) -> list[Candle]:
    """Most recent closed 1h candles, oldest first (the forming candle is dropped).

    Without ``startTime`` Binance always returns its own current (forming) candle last, so
    that row is dropped by position, on Binance's clock. The ``close_time < now_ms`` check
    stays as a second guard. A local clock running ahead of Binance cannot then hand the
    strategy a previous hour that is still forming.
    """
    resp = await client.get(
        _klines_url(market), params={"symbol": symbol, "interval": "1h", "limit": limit}
    )
    resp.raise_for_status()
    return [
        Candle(
            open_time_ms=int(r[0]),
            open=float(r[1]),
            high=float(r[2]),
            low=float(r[3]),
            close=float(r[4]),
            volume=float(r[5]),
            quote_volume=float(r[7]),
            taker_buy_volume=float(r[9]),
        )
        for r in resp.json()[:-1]
        if int(r[6]) < now_ms
    ]


async def fetch_hour_candle(
    client: httpx.AsyncClient, start_ts: int, now_ms: int
) -> HourCandle | None:
    """The Binance spot 1h candle that opens at ``start_ts`` (forming or closed), or None.

    ``closed`` needs Binance's own clock as well as ours: the next hour's candle must already
    exist, because Binance only opens it after processing every trade of this one. A local
    clock running ahead of Binance cannot then settle on a candle that is still forming.
    """
    resp = await client.get(
        _klines_url("spot"),
        params={"symbol": "BTCUSDT", "interval": "1h", "startTime": start_ts * 1000, "limit": 2},
    )
    resp.raise_for_status()
    rows = resp.json()
    if not rows or int(rows[0][0]) != start_ts * 1000:
        return None
    row = rows[0]
    nxt = (start_ts + HOUR_S) * 1000
    closed = int(row[6]) < now_ms and len(rows) > 1 and int(rows[1][0]) == nxt
    return HourCandle(open=float(row[1]), close=float(row[4]), closed=closed)


async def fetch_spot(client: httpx.AsyncClient) -> float | None:
    try:
        resp = await client.get(
            f"{_config.BINANCE_API_BASE}/api/v3/ticker/price", params={"symbol": "BTCUSDT"}
        )
        resp.raise_for_status()
        return float(resp.json()["price"])
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        log.warning("hourly_market.spot_read_failed", error=str(exc))
        return None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHON_DOTENV_DISABLED=1 python3 -m pytest tests/unit/test_hourly_market.py -q`
Expected: PASS (7 passed)

- [ ] **Step 5: Commit**

```bash
git add polymarket_bot/hourly/__init__.py polymarket_bot/hourly/market.py tests/unit/test_hourly_market.py
git commit -m "feat(hourly): BTC hourly market discovery, Binance candles and settlement read"
```

---

### Task 2: Hourly Mean Reversion signal

**Files:**
- Create: `polymarket_bot/hourly/mean_reversion.py`
- Test: `tests/unit/test_hourly_mean_reversion.py`

**Interfaces:**
- Consumes: `Candle` (Task 1).
- Produces:
  - constants `STRATEGY_ID = "hourly_mean_reversion"`, `WINDOW = 168`, `SPOT_FZ_MIN = 1.20`, `PERP_FZ_MAX = 1.24`, `CLV_MIN = 0.80`
  - `@dataclass(frozen=True) FlowPush(imbalance: float | None, z: float | None, direction: int, fz: float | None, clv: float | None)`
  - `@dataclass(frozen=True) Decision(side: str | None, reason: str, signal: dict[str, Any])`
  - `flow_push(candles: list[Candle]) -> FlowPush` (raises `ValueError` with fewer than `WINDOW` candles)
  - `decide(spot: list[Candle], perp: list[Candle]) -> Decision`

- [ ] **Step 1: Write the failing tests**

```python
"""Hourly Mean Reversion: flow push, close location and the frozen rule."""
from __future__ import annotations

import statistics

import pytest

from polymarket_bot.hourly import mean_reversion as mr
from polymarket_bot.hourly.market import Candle


def _series(last_tb_share: float, *, o: float = 100.0, c: float = 110.0,
            hi: float = 110.0, lo: float = 99.0, n: int = 170) -> list[Candle]:
    """n hourly candles: baseline taker-buy share alternating 0.45/0.55, then the last one."""
    out = []
    for i in range(n - 1):
        share = 0.55 if i % 2 else 0.45
        out.append(Candle(i * 3_600_000, 100.0, 101.0, 99.0, 100.5, 10.0, 1000.0, 10.0 * share))
    out.append(Candle((n - 1) * 3_600_000, o, hi, lo, c, 10.0, 1000.0, 10.0 * last_tb_share))
    return out


def test_flow_push_matches_sample_stdev_over_window_including_last() -> None:
    candles = _series(0.9)
    fp = mr.flow_push(candles)
    imbs = [2 * c.taker_buy_volume / c.volume - 1 for c in candles[-mr.WINDOW:]]
    expected_z = (imbs[-1] - statistics.mean(imbs)) / statistics.stdev(imbs)
    assert fp.imbalance == pytest.approx(0.8)
    assert fp.z == pytest.approx(expected_z)
    assert fp.direction == 1 and fp.fz == pytest.approx(expected_z)
    assert fp.clv == pytest.approx(1.0)  # (2*110 - 110 - 99) / 11


def test_flow_push_needs_a_full_window() -> None:
    with pytest.raises(ValueError):
        mr.flow_push(_series(0.9, n=mr.WINDOW - 1))


def test_rule_fires_down_after_pushed_up_hour_closing_at_high() -> None:
    d = mr.decide(_series(0.9), _series(0.5))  # perps show no push
    assert d.side == "Down"
    assert d.signal["spot_fz"] > mr.SPOT_FZ_MIN and d.signal["perp_fz"] <= mr.PERP_FZ_MAX
    assert d.signal["wider_rule_fired"] is True
    assert d.reason.startswith("enter Down")


def test_rule_fires_up_after_pushed_down_hour_closing_at_low() -> None:
    spot = _series(0.1, o=110.0, c=100.0, hi=111.0, lo=100.0)  # sellers pushed it down
    perp = _series(0.5, o=110.0, c=100.0, hi=111.0, lo=100.0)
    assert mr.decide(spot, perp).side == "Up"


def test_no_bet_when_perps_confirm_weak_push_mid_close_or_flat() -> None:
    assert mr.decide(_series(0.9), _series(0.9)).side is None  # perps confirmed
    weak = mr.decide(_series(0.56), _series(0.5))
    assert weak.side is None and weak.signal["wider_rule_fired"] is False
    mid = mr.decide(_series(0.9, c=104.5, hi=110.0, lo=99.0), _series(0.5))
    assert mid.side is None and "extreme" in mid.reason
    flat = mr.decide(_series(0.9, o=100.0, c=100.0, hi=101.0, lo=99.0), _series(0.5))
    assert flat.side is None and "flat" in flat.reason
```

- [ ] **Step 2: Run them to verify they fail**

Run: `PYTHON_DOTENV_DISABLED=1 python3 -m pytest tests/unit/test_hourly_mean_reversion.py -q`
Expected: FAIL with `ImportError: cannot import name 'mean_reversion'`

- [ ] **Step 3: Write the implementation**

```python
"""Hourly Mean Reversion: fade an hour pushed by aggressive spot flow that perps did not confirm.

Bet against hour H-1 when all hold (thresholds frozen from 2023-10..2025-10 discovery data):
spot flow push > 1.20, perp flow push <= 1.24, and H-1 closed beyond +-0.80 of its range in
the direction it moved. Flow push = z-score of the taker-buy imbalance against the trailing
168 hours (sample stdev, including H-1) times the sign of H-1's move.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any

from polymarket_bot.hourly.market import Candle

STRATEGY_ID = "hourly_mean_reversion"
WINDOW = 168
SPOT_FZ_MIN = 1.20
PERP_FZ_MAX = 1.24
CLV_MIN = 0.80


@dataclass(frozen=True)
class FlowPush:
    imbalance: float | None
    z: float | None
    direction: int
    fz: float | None
    clv: float | None


@dataclass(frozen=True)
class Decision:
    side: str | None
    reason: str
    signal: dict[str, Any]


def _imbalance(c: Candle) -> float | None:
    return 2 * c.taker_buy_volume / c.volume - 1 if c.volume > 0 else None


def flow_push(candles: list[Candle]) -> FlowPush:
    """Flow push of the LAST candle against the trailing WINDOW candles (including it)."""
    if len(candles) < WINDOW:
        raise ValueError(f"need {WINDOW} closed candles, got {len(candles)}")
    window = candles[-WINDOW:]
    last = window[-1]
    imbs = [_imbalance(c) for c in window]
    z: float | None = None
    if all(i is not None for i in imbs):
        sd = statistics.stdev(imbs)
        if sd > 0:
            z = (imbs[-1] - statistics.mean(imbs)) / sd
    direction = 1 if last.close > last.open else (-1 if last.close < last.open else 0)
    rng = last.high - last.low
    clv = (2 * last.close - last.high - last.low) / rng if rng > 0 else None
    return FlowPush(
        imbalance=imbs[-1],
        z=z,
        direction=direction,
        fz=z * direction if z is not None else None,
        clv=clv,
    )


def decide(spot: list[Candle], perp: list[Candle]) -> Decision:
    s = flow_push(spot)
    p = flow_push(perp)
    signal: dict[str, Any] = {
        "spot_imbalance": s.imbalance,
        "spot_z": s.z,
        "spot_fz": s.fz,
        "perp_z": p.z,
        "perp_fz": p.fz,
        "clv": s.clv,
        "direction": s.direction,
        "wider_rule_fired": s.fz is not None and s.fz > SPOT_FZ_MIN,
        "hour_open_time_ms": spot[-1].open_time_ms,
    }
    if s.direction == 0:
        return Decision(None, "no signal: last hour was flat", signal)
    if s.fz is None or s.fz <= SPOT_FZ_MIN:
        return Decision(None, f"no signal: spot push {s.fz} not above {SPOT_FZ_MIN}", signal)
    if p.fz is None or p.fz > PERP_FZ_MAX:
        return Decision(None, f"no signal: perps confirmed the push ({p.fz})", signal)
    if s.clv is None or s.clv * s.direction <= CLV_MIN:
        return Decision(None, f"no signal: did not close at the extreme (CLV {s.clv})", signal)
    side = "Down" if s.direction > 0 else "Up"
    return Decision(
        side,
        f"enter {side}: spot push {s.fz:.2f}, perp push {p.fz:.2f}, close location {s.clv:+.2f}",
        signal,
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHON_DOTENV_DISABLED=1 python3 -m pytest tests/unit/test_hourly_mean_reversion.py -q`
Expected: PASS (5 passed)

- [ ] **Step 5: Parity check against the research data (not committed)**

With the research scratchpad CSVs from 2026-09-14 (`btc_1h_flow.csv`, `btc_perp_1h_flow.csv`), build `Candle` lists and run `decide()` at every hour from 2025-10-18 14:00 UTC onward. Compare against the pandas Tier A mask (`spot fz > 1.20 & perp fz <= 1.24 & CLV beyond ±0.8`). Expected: identical firing hours (220 validation bets, 57.3% reversal wins).

```bash
python3 - <<'EOF'
import sys; sys.path.insert(0, ".")
import pandas as pd
from polymarket_bot.hourly.market import Candle
from polymarket_bot.hourly import mean_reversion as mr
B = "/private/tmp/claude-501/-Users-zayankhan-projects-polymarket-crypto/6f03044c-3dcc-4df5-b5f5-73f1372f4182/scratchpad/kronos-test"
def load(n):
    d = pd.read_csv(f"{B}/{n}", parse_dates=["ts"])
    return [Candle(int(r.ts.timestamp()*1000), r.open, r.high, r.low, r.close, r.volume, r.amount, r.taker_buy_base) for r in d.itertuples()]
spot, perp = load("btc_1h_flow.csv"), load("btc_perp_1h_flow.csv")
start = next(i for i, c in enumerate(spot) if c.open_time_ms >= int(pd.Timestamp("2025-10-18 14:00").timestamp()*1000))
bets = wins = 0
for i in range(start, len(spot)):
    d = mr.decide(spot[i-168:i], perp[i-168:i])
    if d.side:
        bets += 1; wins += int((spot[i].close > spot[i].open) == (d.side == "Up"))
print("bets", bets, "win rate", round(wins / bets, 4))
EOF
```

Expected output: `bets 220 win rate 0.5727` (±1 bet at a flat-hour edge). If it differs by more, stop and reconcile the math before continuing.

- [ ] **Step 6: Commit**

```bash
git add polymarket_bot/hourly/mean_reversion.py tests/unit/test_hourly_mean_reversion.py
git commit -m "feat(hourly): Hourly Mean Reversion signal (frozen rule, stdlib math)"
```

---

### Task 3: Decision record table and ledger; per-strategy position columns

**Files:**
- Modify: `db.py`
  - SCHEMA literal: new table, appended after the `idx_daily_shadow_positions_asset` index.
  - `POSITION_COLUMN_MIGRATIONS`: three new columns.
- Create: `polymarket_bot/hourly/ledger.py`
- Test: `tests/unit/test_hourly_ledger.py`

**Interfaces:**
- Produces:
  - `paper_positions` columns `strategy_id TEXT`, `market_timeframe TEXT`, `window_start_ts INTEGER`
  - `async record_decision(*, strategy_id: str, window_slug: str, window_start_ts: int, side: str | None, reason: str, signal: dict, factors: dict, up_bid: float | None, up_ask: float | None, down_bid: float | None, down_ask: float | None, hour_open: float | None, mode: str, late: bool) -> bool` (sets `action` to `PENDING` when `side` is set and not late, `MISSED` when set and late, `NO_SIGNAL` when `side` is None; returns True iff a row was inserted)
  - `async get_decision(window_start_ts: int, strategy_id: str, *, mode: str) -> dict | None`
  - `async set_action(window_start_ts: int, strategy_id: str, action: str, position_id: int | None = None, *, mode: str, expected_action: str | None = None) -> None` (with `expected_action`, updates only while the row still holds that action; Claude, 2026-09-15, branch-review finding hourly-ambiguous-post-error-retried)
  - Amended (Claude, 2026-09-15, branch-review finding dst-fallback-slug-collision): rows are unique on `(window_start_ts, strategy_id)`, not the slug; the code blocks below predate this.
  - Amended (Claude, 2026-09-15, branch-review finding decision-row-shared-across-modes): rows are unique on `(window_start_ts, strategy_id, mode)`. The engine works out the mode once per tick and passes it to `decide_hour`, `open_entries` and every row lookup, so a paper run and a live run in the same hour each act on their own row. The code blocks below predate this.
  - `async unsettled_windows(now: int) -> list[int]` (distinct `window_start_ts` with `settled_at IS NULL` and `window_start_ts + 3600 <= now`)
  - `async settle_window(window_start_ts: int, hour_open: float, hour_close: float) -> None`

- [ ] **Step 1: Write the failing tests**

```python
"""Hourly decision record: one row per (hour, strategy), actions, and settlement of every hour."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import pytest_asyncio

import db as _db
from polymarket_bot.hourly import ledger

H = 1_789_326_000
SLUG = "bitcoin-up-or-down-september-13-2026-3pm-et"


@pytest_asyncio.fixture
async def test_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "t.db")
    await _db.init_db()
    return _db


async def _record(strategy_id: str = "hourly_mean_reversion", side: str | None = "Down",
                  late: bool = False, start: int = H, slug: str = SLUG) -> bool:
    return await ledger.record_decision(
        strategy_id=strategy_id, window_slug=slug, window_start_ts=start, side=side,
        reason="enter Down: test", signal={"spot_fz": 2.5}, factors={"weekend": False},
        up_bid=0.49, up_ask=0.50, down_bid=0.50, down_ask=0.51, hour_open=77000.0,
        mode="paper", late=late,
    )


@pytest.mark.asyncio
async def test_record_is_idempotent_per_hour_and_strategy(test_db) -> None:
    assert await _record() is True
    assert await _record() is False
    assert await _record(strategy_id="kronos_btc_finetune", side=None) is True
    row = await ledger.get_decision(SLUG, "hourly_mean_reversion")
    assert row["action"] == "PENDING" and row["decision_side"] == "Down"
    assert json.loads(row["signal_json"]) == {"spot_fz": 2.5}
    assert (await ledger.get_decision(SLUG, "kronos_btc_finetune"))["action"] == "NO_SIGNAL"


@pytest.mark.asyncio
async def test_late_signal_is_recorded_missed(test_db) -> None:
    await _record(late=True)
    assert (await ledger.get_decision(SLUG, "hourly_mean_reversion"))["action"] == "MISSED"


@pytest.mark.asyncio
async def test_set_action_and_settle_every_strategy_row(test_db) -> None:
    await _record()
    await _record(strategy_id="kronos_btc_finetune", side=None)
    await ledger.set_action(SLUG, "hourly_mean_reversion", "ENTERED", position_id=7)
    assert await ledger.unsettled_windows(H + 3599) == []
    assert await ledger.unsettled_windows(H + 3600) == [H]
    await ledger.settle_window(H, 77000.0, 76900.0)
    for sid in ("hourly_mean_reversion", "kronos_btc_finetune"):
        row = await ledger.get_decision(SLUG, sid)
        assert row["outcome_side"] == "Down" and row["hour_close"] == 76900.0
        assert row["settled_at"] is not None
    assert (await ledger.get_decision(SLUG, "hourly_mean_reversion"))["position_id"] == 7
    assert await ledger.unsettled_windows(H + 7200) == []


@pytest.mark.asyncio
async def test_positions_table_has_strategy_columns(test_db) -> None:
    async with _db.connect() as conn:
        cur = await conn.execute("PRAGMA table_info(paper_positions)")
        cols = {r["name"] for r in await cur.fetchall()}
    assert {"strategy_id", "market_timeframe", "window_start_ts"} <= cols
```

- [ ] **Step 2: Run them to verify they fail**

Run: `PYTHON_DOTENV_DISABLED=1 python3 -m pytest tests/unit/test_hourly_ledger.py -q`
Expected: FAIL with `ImportError: cannot import name 'ledger'`

- [ ] **Step 3: Add the table to `db.py`**

In the `SCHEMA` literal, directly after `CREATE INDEX IF NOT EXISTS idx_daily_shadow_positions_asset ON daily_shadow_positions(asset);`:

```sql
-- Hourly BTC strategies: one decision row per (hour window, strategy), written for
-- EVERY hour whether or not it traded, then settled from the Binance 1h candle, so
-- the operator can score each strategy's calls (and would-have-bet hours) honestly.
CREATE TABLE IF NOT EXISTS hourly_strategy_context (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  strategy_id TEXT NOT NULL,
  window_slug TEXT NOT NULL,
  window_start_ts INTEGER NOT NULL,
  decision_side TEXT,
  decision_reason TEXT NOT NULL,
  signal_json TEXT,
  factors_json TEXT,
  up_bid REAL,
  up_ask REAL,
  down_bid REAL,
  down_ask REAL,
  hour_open REAL,
  action TEXT NOT NULL,
  position_id INTEGER,
  mode TEXT NOT NULL,
  hour_close REAL,
  outcome_side TEXT,
  settled_at TEXT
);
-- Amended: Claude, 2026-09-15, branch-review finding dst-fallback-slug-collision.
-- Amended: Claude, 2026-09-15, branch-review finding decision-row-shared-across-modes.
DROP INDEX IF EXISTS idx_hourly_context_window_strategy;
DROP INDEX IF EXISTS idx_hourly_context_start_strategy;
CREATE UNIQUE INDEX IF NOT EXISTS idx_hourly_context_start_strategy_mode
  ON hourly_strategy_context(window_start_ts, strategy_id, mode);
```

In `POSITION_COLUMN_MIGRATIONS`, after `"mode": "TEXT",` add:

```python
    # Hourly strategies (2026-09-14): each strategy owns one open-position slot.
    # NULL strategy_id / market_timeframe = the legacy 5m loop's rows.
    "strategy_id": "TEXT",
    "market_timeframe": "TEXT",
    "window_start_ts": "INTEGER",
```

- [ ] **Step 4: Write the ledger**

```python
"""Hourly strategy decision record: one row per (hour, strategy), actions, and settlement."""
from __future__ import annotations

import json
from typing import Any

import db as _db
from polymarket_bot.hourly.market import HOUR_S, up_won


async def record_decision(
    *,
    strategy_id: str,
    window_slug: str,
    window_start_ts: int,
    side: str | None,
    reason: str,
    signal: dict[str, Any],
    factors: dict[str, Any],
    up_bid: float | None,
    up_ask: float | None,
    down_bid: float | None,
    down_ask: float | None,
    hour_open: float | None,
    mode: str,
    late: bool,
) -> bool:
    """INSERT OR IGNORE the hour's decision; True iff this call wrote the row."""
    if side is None:
        action = "NO_SIGNAL"
    else:
        action = "MISSED" if late else "PENDING"
    async with _db.connect() as conn:
        cur = await conn.execute(
            """
            INSERT OR IGNORE INTO hourly_strategy_context(
              created_at, strategy_id, window_slug, window_start_ts, decision_side,
              decision_reason, signal_json, factors_json, up_bid, up_ask, down_bid,
              down_ask, hour_open, action, mode
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _db.utc_now_iso(), strategy_id, window_slug, window_start_ts, side, reason,
                json.dumps(signal, default=str), json.dumps(factors, default=str),
                up_bid, up_ask, down_bid, down_ask, hour_open, action, mode,
            ),
        )
        await conn.commit()
        return cur.rowcount == 1


async def get_decision(window_slug: str, strategy_id: str) -> dict[str, Any] | None:
    async with _db.connect() as conn:
        cur = await conn.execute(
            "SELECT * FROM hourly_strategy_context WHERE window_slug = ? AND strategy_id = ?",
            (window_slug, strategy_id),
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def set_action(
    window_slug: str,
    strategy_id: str,
    action: str,
    position_id: int | None = None,
    *,
    expected_action: str | None = None,
) -> None:
    # Claude, 2026-09-15, branch-review finding hourly-ambiguous-post-error-retried:
    # expected_action makes the update apply only while the row still holds that action,
    # so closing out an unfinished attempt never overwrites a result written meanwhile.
    async with _db.connect() as conn:
        await conn.execute(
            "UPDATE hourly_strategy_context SET action = ?, "
            "position_id = COALESCE(?, position_id) "
            "WHERE window_slug = ? AND strategy_id = ? AND (? IS NULL OR action = ?)",
            (action[:240], position_id, window_slug, strategy_id,
             expected_action, expected_action),
        )
        await conn.commit()


async def unsettled_windows(now: int) -> list[int]:
    async with _db.connect() as conn:
        cur = await conn.execute(
            "SELECT DISTINCT window_start_ts FROM hourly_strategy_context "
            "WHERE settled_at IS NULL AND window_start_ts + ? <= ? ORDER BY window_start_ts",
            (HOUR_S, now),
        )
        return [int(r["window_start_ts"]) for r in await cur.fetchall()]


async def settle_window(window_start_ts: int, hour_open: float, hour_close: float) -> None:
    outcome = "Up" if up_won(hour_open, hour_close) else "Down"
    async with _db.connect() as conn:
        await conn.execute(
            "UPDATE hourly_strategy_context SET hour_open = COALESCE(hour_open, ?), "
            "hour_close = ?, outcome_side = ?, settled_at = ? "
            "WHERE window_start_ts = ? AND settled_at IS NULL",
            (hour_open, hour_close, outcome, _db.utc_now_iso(), window_start_ts),
        )
        await conn.commit()
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `PYTHON_DOTENV_DISABLED=1 python3 -m pytest tests/unit/test_hourly_ledger.py tests/unit/test_daily_ledger.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add db.py polymarket_bot/hourly/ledger.py tests/unit/test_hourly_ledger.py
git commit -m "feat(hourly): per-hour strategy decision record and per-strategy position columns"
```

---

### Task 4: Paper settlement charges the taker fee; 5m paths ignore hourly rows

**Files:**
- Modify: `polymarket_bot/paper.py`
  - `_close_position` paper branch.
  - `_maybe_open_position` open-row check.
  - `_close_due_positions` row query.
- Test: `tests/unit/test_settle_style.py` (append)

**Interfaces:**
- Consumes: `taker_fee_per_share` from `polymarket_bot/shadow/fees.py`.
- Produces:
  - A paper settled close books `shares × (payout − entry) − shares × taker_fee_per_share(entry)`, the same number live `record_settlement` books.
  - The 5m loop's open-row checks and settlement only see rows where `market_timeframe IS NULL OR market_timeframe != '1h'`.

- [ ] **Step 1: Write the failing tests** (append to `tests/unit/test_settle_style.py`)

```python
@pytest.mark.asyncio
async def test_paper_settled_close_is_net_of_entry_taker_fee(test_db, monkeypatch):
    """Paper/live parity: live record_settlement books payout minus the entry taker fee."""
    monkeypatch.setattr(paper, "_live_executor", None)
    monkeypatch.setattr(paper, "_risk_gate", None)
    snap = _snapshot()
    await _insert_settle_pos(snap)
    await paper._close_position(dict(_POS), snap, 1.0, "WINDOW_ROLL", settled=True)
    async with paper.connect() as db:
        async with db.execute("SELECT realized_pnl_usd FROM paper_positions") as cur:
            row = dict(await cur.fetchone())
    # 6 * (1.0 - 0.5) - 6 * 0.07 * 0.5 * 0.5
    assert row["realized_pnl_usd"] == pytest.approx(3.0 - 0.105)


@pytest.mark.asyncio
async def test_five_minute_paths_ignore_hourly_rows(test_db, monkeypatch):
    monkeypatch.setattr(paper, "_live_executor", None)
    snap = _snapshot()
    async with paper.connect() as db:
        await db.execute(
            "INSERT INTO paper_positions(opened_at, window_slug, side, state, entry_price,"
            " notional_usd, shares, quote_source, strategy_style, strategy_id,"
            " market_timeframe, window_start_ts)"
            " VALUES (?, 'bitcoin-up-or-down-september-13-2026-3pm-et', 'Down', 'open',"
            " 0.5, 2.5, 5.0, 'clob', 'settle', 'hourly_mean_reversion', '1h', 1789326000)",
            (snap.created_at,),
        )
        await db.commit()
    settle = AsyncMock()
    monkeypatch.setattr(paper, "_settle_position_outcome", settle)
    await paper._close_due_positions(snap, client=MagicMock())
    settle.assert_not_called()  # the 5m loop never tries to settle an hourly row
    assert await paper._open_legacy_position_exists() is False
```

- [ ] **Step 2: Run them to verify they fail**

Run: `PYTHON_DOTENV_DISABLED=1 python3 -m pytest tests/unit/test_settle_style.py -q -k "taker_fee or ignore_hourly"`
Expected: FAIL. The first asserts `3.0 == approx(2.895)`; the second raises `AttributeError: … '_open_legacy_position_exists'`.

- [ ] **Step 3: Implement**

In `polymarket_bot/paper.py`, add to the imports:

```python
from polymarket_bot.shadow.fees import taker_fee_per_share
```

Add near `count_open_positions`:

```python
# The 5m loop's rows (strategy-less legacy slot). Hourly strategy rows own their own slots.
_LEGACY_ROWS_SQL = "(market_timeframe IS NULL OR market_timeframe != '1h')"


async def _open_legacy_position_exists() -> bool:
    async with connect() as db:
        async with db.execute(
            f"SELECT COUNT(*) AS n FROM paper_positions WHERE state = 'open' AND {_LEGACY_ROWS_SQL}"
        ) as cur:
            return bool((await cur.fetchone())["n"])
```

In `_maybe_open_position`, replace the open-row check block:

```python
    async with connect() as db:
        async with db.execute(
            "SELECT COUNT(*) AS n FROM paper_positions WHERE state = 'open'"
        ) as cur:
            if (await cur.fetchone())["n"]:
                return
```

with:

```python
    if await _open_legacy_position_exists():
        return
    async with connect() as db:
```

(Keep the following `if _knobs.cached('exit_style') == "settle":` block inside the same `async with connect() as db:`.)

In `_close_due_positions`, change the query to:

```python
            f"SELECT * FROM paper_positions WHERE state = 'open' AND {_LEGACY_ROWS_SQL} "
            "ORDER BY opened_at"
```

In `_close_position`, replace the paper branch's first line:

```python
        pnl = float(pos["shares"]) * (exit_price - entry_price)
```

with:

```python
        pnl = float(pos["shares"]) * (exit_price - entry_price)
        if settled:
            # Paper/live parity: live record_settlement books the payout net of the
            # entry taker fee (0.07·p·(1−p) per share); paper books the same number.
            pnl -= float(pos["shares"]) * taker_fee_per_share(entry_price)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHON_DOTENV_DISABLED=1 python3 -m pytest tests/unit/test_settle_style.py tests/unit/test_live_wiring.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add polymarket_bot/paper.py tests/unit/test_settle_style.py
git commit -m "fix(paper): settled paper closes net of the entry taker fee; 5m paths skip hourly rows"
```

---

### Task 5: Live executor — one position slot per strategy

**Files:**
- Modify: `db.py` (`LIVE_ORDERS_COLUMN_MIGRATIONS`, `journal_live_order`)
- Modify: `polymarket_exec/execution/live.py`
- Test: `tests/unit/test_live_executor.py` (append; reuses its `journal_db`, `_mock_client`, `_executor`, `_journal_rows`, `UP_TOKEN`)

**Interfaces:**
- Produces:
  - `live_orders.strategy_id TEXT`; `journal_live_order(..., strategy_id: str | None = None)`
  - `LiveExecutor(..., gate: RiskGate | None = None, slot: str | None = None)`
  - `LiveExecutor.slot_executor(strategy_id: str) -> LiveExecutor` (same object on every call for the run)
  - `async LiveExecutor.cancel_open_all(reason: str = "CANCEL_REQUEST") -> list[str]`
  - `_row_window_resolved(row: dict, *, now: float | None = None) -> bool`
  - Behaviour:
    - `enforce_kill_switch` cancels resting orders in every slot;
    - boot reconciliation adopts at most one open row per slot (refuses with "max 1 per strategy" otherwise), skips paper rows of strategy slots, finds each row's entry order by `window_slug` **and** `strategy_id`;
    - an hourly row whose entry order the CLOB has pruned is adopted from the journal's recorded fill (so it settles from the Binance candle), instead of being closed at zero.
    - adoption restores the shares already sold, in the legacy slot and every strategy slot (Claude, 2026-09-15, branch-review finding reconcile-resets-sold-size-double-books). A live Stop can sell part of the current hour's position and leave the row open; the next live Start must not settle the sold shares again. `_reconcile_row` sums this slot's EXIT orders journalled for the row's window after the adopted entry (`window_slug`, `strategy_id IS ?`, `id >` the entry's id). Each order's filled size comes from `get_order` size_matched. If the venue gives no answer, it is the larger of the order's `EXIT UNFILLED` row size and the SELL's placement `makingAmount`. It is never the SELL's requested size. The sum, rounded down and capped at the matched size, becomes `_entry_sold_size`, so exits and settlement use only matched minus sold. Nothing is re-booked: those fills are already in the reloaded gate and in the row's `realized_pnl_usd`. When every filled share was already sold, the row closes as `RECONCILED_FLAT` with its existing `realized_pnl_usd`. One gap is left to `tools/reconcile_live_ledger.py`: an exit whose cancel failed just before shutdown was never booked by that session, so its shares count as sold but its PnL is not booked.
- Unchanged: `gate.py`; otherwise every legacy (strategy-less) path behaves exactly as before.

- [ ] **Step 1: Write the failing tests** (append to `tests/unit/test_live_executor.py`; add `_row_window_resolved` to the existing `from polymarket_exec.execution.live import (...)` block)

```python
# ---------------------------------------------------------------------------
# Strategy slots: one open position per strategy (operator decision 2026-09-14)
# ---------------------------------------------------------------------------

MR = "hourly_mean_reversion"
KR = "kronos_btc_finetune"
HSLUG = "bitcoin-up-or-down-september-13-2026-3pm-et"


def _distinct_order_ids(client: MagicMock) -> None:
    ids = iter(f"0xORDER{i}" for i in range(1, 100))
    client.create_and_post_order.side_effect = lambda args: {
        "success": True, "errorMsg": "", "orderID": next(ids), "status": "live",
    }


async def _seed_hourly_row(journal_db, strategy_id: str, *, mode: str = "live",
                           start: int | None = None) -> int:
    start = start if start is not None else int(time.time()) // 3600 * 3600
    async with journal_db.connect() as conn:
        cur = await conn.execute(
            "INSERT INTO paper_positions(opened_at, window_slug, side, state, entry_price,"
            " notional_usd, shares, mode, strategy_id, market_timeframe, window_start_ts)"
            " VALUES ('2026-09-13T19:00:30+00:00', ?, 'Down', 'open', 0.57, 3.0, 5.26, ?, ?,"
            " '1h', ?)",
            (HSLUG, mode, strategy_id, start),
        )
        await conn.commit()
        return int(cur.lastrowid)


async def _row(journal_db, position_id: int) -> dict:
    async with journal_db.connect() as conn:
        async with conn.execute(
            "SELECT * FROM paper_positions WHERE position_id = ?", (position_id,)
        ) as cur:
            return dict(await cur.fetchone())


@pytest.mark.asyncio
async def test_slots_share_client_and_gate_but_hold_their_own_position(
    journal_db, tmp_path: Path
) -> None:
    client = _mock_client()
    account = _executor(client, tmp_path)
    mr, kr = account.slot_executor(MR), account.slot_executor(KR)
    assert account.slot_executor(MR) is mr
    assert mr.gate is account.gate and mr._client is client

    assert (await mr.submit_entry(UP_TOKEN, 0.57, 3.0, window_slug=HSLUG)).ok

    assert "max 1" in (mr.entry_block_reason(3.0) or "")
    assert kr.entry_block_reason(3.0) is None
    assert account.entry_block_reason(3.0) is None
    entry = [r for r in await _journal_rows(journal_db) if r["intent"] == "ENTRY"][-1]
    assert entry["strategy_id"] == MR


def test_slot_executor_needs_a_client_and_the_account_executor(tmp_path: Path) -> None:
    unbuilt = LiveExecutor(private_key="0x" + "1" * 64, kill_switch_path=tmp_path / "KILL")
    with pytest.raises(RuntimeError):
        unbuilt.slot_executor(MR)
    slot = _executor(_mock_client(), tmp_path).slot_executor(MR)
    with pytest.raises(RuntimeError):
        slot.slot_executor(KR)


@pytest.mark.asyncio
async def test_kill_switch_and_stop_cancel_resting_orders_in_every_slot(
    journal_db, tmp_path: Path
) -> None:
    client = _mock_client()
    _distinct_order_ids(client)
    account = _executor(client, tmp_path)
    await account.slot_executor(MR).submit_entry(UP_TOKEN, 0.57, 3.0, window_slug=HSLUG)
    await account.slot_executor(KR).submit_entry(UP_TOKEN, 0.57, 3.0, window_slug=HSLUG)
    (tmp_path / "KILL").touch()

    assert await account.enforce_kill_switch() is True

    cancelled = {c.args[0].orderID for c in client.cancel_order.call_args_list}
    assert cancelled == {"0xORDER1", "0xORDER2"}
    assert account.slot_executor(MR)._entry_order_id is None
    assert account.slot_executor(KR)._entry_order_id is None


@pytest.mark.asyncio
async def test_cancel_open_all_covers_the_account_and_every_slot(
    journal_db, tmp_path: Path
) -> None:
    client = _mock_client()
    _distinct_order_ids(client)
    account = _executor(client, tmp_path)
    await account.submit_entry(UP_TOKEN, 0.57, 3.0)
    await account.slot_executor(MR).submit_entry(UP_TOKEN, 0.57, 3.0, window_slug=HSLUG)

    assert sorted(await account.cancel_open_all(reason="LOOP_STOP")) == ["0xORDER1", "0xORDER2"]


@pytest.mark.asyncio
async def test_boot_adopts_one_row_per_strategy_using_that_strategys_order(
    journal_db, tmp_path: Path
) -> None:
    mr_id = await _seed_hourly_row(journal_db, MR)
    kr_id = await _seed_hourly_row(journal_db, KR)
    # Same hour, same token: only strategy_id tells the two entries apart.
    await journal_db.journal_live_order(
        intent="ENTRY", side="BUY", status="SUBMITTED", window_slug=HSLUG,
        token_id=UP_TOKEN, price=0.57, size=5.26, clob_order_id="0xMR", strategy_id=MR,
    )
    await journal_db.journal_live_order(
        intent="ENTRY", side="BUY", status="SUBMITTED", window_slug=HSLUG,
        token_id=UP_TOKEN, price=0.57, size=5.26, clob_order_id="0xKR", strategy_id=KR,
    )
    client = _mock_client()
    client.get_order.side_effect = lambda oid: {
        "size_matched": "5.26" if oid == "0xMR" else "0", "price": "0.57",
    }
    account = _executor(client, tmp_path)

    await account.start()

    assert "max 1" in (account.slot_executor(MR).entry_block_reason(3.0) or "")
    assert (await _row(journal_db, mr_id))["state"] == "open"
    assert (await _row(journal_db, kr_id))["exit_reason"] == "RECONCILED_UNFILLED"
    assert account.slot_executor(KR).entry_block_reason(3.0) is None


@pytest.mark.asyncio
async def test_boot_refuses_two_open_rows_for_one_strategy(journal_db, tmp_path: Path) -> None:
    await _seed_hourly_row(journal_db, MR)
    await _seed_hourly_row(journal_db, MR)
    with pytest.raises(LiveBootRefused, match="max 1 per strategy"):
        await _executor(_mock_client(), tmp_path).start()


@pytest.mark.asyncio
async def test_boot_leaves_paper_rows_of_strategy_slots_for_paper_settlement(
    journal_db, tmp_path: Path
) -> None:
    paper_id = await _seed_hourly_row(journal_db, MR, mode="paper")
    account = _executor(_mock_client(), tmp_path)

    await account.start()

    row = await _row(journal_db, paper_id)
    assert row["state"] == "open" and row["exit_reason"] is None
    assert account.slot_executor(MR).entry_block_reason(3.0) is None


@pytest.mark.asyncio
async def test_boot_adopts_resolved_hourly_row_from_journal_so_it_settles(
    journal_db, tmp_path: Path
) -> None:
    """An hourly row settles from the Binance candle, which stays readable after
    resolution, so a pruned order with a journal-recorded fill is adopted, not zeroed."""
    past = int(time.time()) // 3600 * 3600 - 3 * 3600
    position_id = await _seed_hourly_row(journal_db, MR, start=past)
    await journal_db.journal_live_order(
        intent="ENTRY", side="BUY", status="SUBMITTED", window_slug=HSLUG,
        token_id=UP_TOKEN, price=0.57, size=5.26, clob_order_id="0xPRUNED",
        details={"response": {"status": "matched", "takingAmount": "5.26"}},
        strategy_id=MR,
    )
    client = _mock_client()
    client.get_order.return_value = None
    account = _executor(client, tmp_path)

    await account.start()

    assert (await _row(journal_db, position_id))["state"] == "open"
    settled = await account.slot_executor(MR).record_settlement(True, HSLUG)
    assert settled.ok and settled.size == pytest.approx(5.26)


def test_hourly_rows_resolve_an_hour_after_their_own_start() -> None:
    row = {"window_slug": HSLUG, "market_timeframe": "1h", "window_start_ts": 1_000_000}
    assert _row_window_resolved(row, now=1_000_000 + 3600 + 59) is False
    assert _row_window_resolved(row, now=1_000_000 + 3600 + 60) is True
    legacy = {"window_slug": "btc-updown-5m-1000000"}
    assert _row_window_resolved(legacy, now=1_000_000 + 359) is False
    assert _row_window_resolved(legacy, now=1_000_000 + 360) is True
```

- [ ] **Step 2: Run them to verify they fail**

Run: `PYTHON_DOTENV_DISABLED=1 python3 -m pytest tests/unit/test_live_executor.py -q -k "slot or strategy or hourly"`
Expected: FAIL with `ImportError: cannot import name '_row_window_resolved'`

- [ ] **Step 3: Tag journal rows with the slot (`db.py`)**

In `LIVE_ORDERS_COLUMN_MIGRATIONS`, after `"placement_status": "TEXT",` add:

```python
    # Hourly strategies (2026-09-14): which strategy slot placed the order, so
    # two strategies trading the same hour never adopt each other's entries.
    "strategy_id": "TEXT",
```

In `journal_live_order`, add the keyword `strategy_id: str | None = None,` after `mode: str = "live",`, add `strategy_id` to the INSERT column list after `placement_status`, add one more `?` to `VALUES`, and append `strategy_id,` to the parameter tuple after `placement_status,`.

- [ ] **Step 4: Slots in `polymarket_exec/execution/live.py`**

(a) Module docstring: change "one open position max" to "one open position per strategy slot".

(b) Below `_WINDOW_RESOLVE_GRACE_SECONDS = 60` add:

```python
_HOURLY_WINDOW_SECONDS = 3600


def _row_window_resolved(row: dict[str, Any], *, now: float | None = None) -> bool:
    """``_window_resolved`` for a ledger row: hourly rows carry their own window start."""
    if row.get("market_timeframe") == "1h" and row.get("window_start_ts") is not None:
        now_s = time.time() if now is None else now
        return now_s >= (
            int(row["window_start_ts"]) + _HOURLY_WINDOW_SECONDS + _WINDOW_RESOLVE_GRACE_SECONDS
        )
    return _window_resolved(row["window_slug"], now=now)
```

(c) `LiveExecutor.__init__`: add the keyword parameters `gate: RiskGate | None = None,` and `slot: str | None = None,` after `client`. Build the gate only when none is passed:

```python
        if gate is not None:
            # A strategy slot shares the account executor's gate: the daily loss
            # halt, caps and kill switch are account-wide, never per strategy.
            self.gate = gate
        else:
            gate_cfg = GateConfig(...)  # the existing GateConfig(...) block, unchanged
            # is_live=True → the gate halts on the live (real-money) leg (#76).
            self.gate = RiskGate(gate_cfg, is_live=True)
```

After `self._started = client is not None` add:

```python
        # None = the account executor (the legacy loop's slot). A strategy id =
        # that strategy's slot, created by slot_executor() on the account executor.
        self._slot = slot
        self._slots: dict[str, LiveExecutor] = {}
```

Change the comment `# Position / order tracking (max 1 open position by design)` to `# Position / order tracking (max 1 open position per slot)`.

(d) Journal every row with the slot. Replace every `await journal_live_order(` inside the class with `await self._journal(` (they are all in instance methods; `_journal_blocked` included), then add next to `_journal_blocked`:

```python
    async def _journal(self, **fields: Any) -> None:
        await journal_live_order(strategy_id=self._slot, **fields)
```

(e) Add after `resync_flat`:

```python
    def slot_executor(self, strategy_id: str) -> LiveExecutor:
        """The executor owning ``strategy_id``'s single position slot.

        Shares this account executor's authenticated client and RiskGate; only
        position/order tracking is per strategy. Created once per run.
        """
        if self._slot is not None:
            raise RuntimeError("strategy slots are created from the account executor")
        if self._client is None:
            raise RuntimeError("the account executor has no CLOB client yet")
        slot = self._slots.get(strategy_id)
        if slot is None:
            slot = LiveExecutor(
                self._private_key,
                self._funder,
                self._signature_type,
                host=self._host,
                chain_id=self._chain_id,
                exit_fill_timeout_seconds=self._exit_fill_timeout_override,
                client=self._client,
                gate=self.gate,
                slot=strategy_id,
            )
            self._slots[strategy_id] = slot
        return slot

    async def cancel_open_all(self, reason: str = "CANCEL_REQUEST") -> list[str]:
        """Cancel tracked resting orders in the account slot and every strategy slot."""
        cancelled = await self.cancel_open(reason=reason)
        for slot in self._slots.values():
            cancelled += await slot.cancel_open(reason=reason)
        return cancelled
```

(f) In `enforce_kill_switch`, replace `await self.cancel_open(reason="KILL_SWITCH")` with `await self.cancel_open_all(reason="KILL_SWITCH")`.

(g) In `_reconcile_account`, replace step 2 (from `# 2) Re-adopt any open ledger position` to the end of the method) with:

```python
        # 2) Re-adopt open ledger positions so they keep being managed: at most
        # one per slot (the legacy loop's slot plus one per strategy). Paper rows
        # of strategy slots hold no real tokens; the hourly engine settles them.
        async with connect() as db:
            async with db.execute(
                "SELECT * FROM paper_positions WHERE state = 'open' "
                "AND NOT (strategy_id IS NOT NULL AND COALESCE(mode, '') = 'paper') "
                "ORDER BY opened_at"
            ) as cur:
                open_rows = [dict(r) for r in await cur.fetchall()]
        by_slot: dict[str | None, list[dict[str, Any]]] = {}
        for row in open_rows:
            by_slot.setdefault(row.get("strategy_id"), []).append(row)
        crowded = sorted(str(s or "the legacy loop") for s, rows in by_slot.items() if len(rows) > 1)
        if crowded:
            raise LiveBootRefused(
                f"Boot reconciliation failed: more than one open ledger position for "
                f"{', '.join(crowded)} (max 1 per strategy by design). Resolve them "
                "manually (flatten on Polymarket, then UPDATE paper_positions SET "
                "state='closed', exit_reason='MANUAL' for each row) before restarting "
                "live mode."
            )
        for slot_id, rows in by_slot.items():
            owner = self if slot_id is None else self.slot_executor(slot_id)
            await owner._reconcile_row(rows[0])

    async def _reconcile_row(self, row: dict[str, Any]) -> None:
        """Adopt or close one open ledger row on the slot that owns it."""
```

The body of `_reconcile_row` is the old code that followed `row = open_rows[0]`, moved verbatim, with three edits:
1. The journal lookup matches the slot:
   ```python
                   "WHERE intent = 'ENTRY' AND status = 'SUBMITTED' AND window_slug = ? "
                   "AND strategy_id IS ? ORDER BY id DESC LIMIT 1",
                   (row["window_slug"], row.get("strategy_id")),
   ```
2. In the `if matched is None:` branch, directly after `journal_matched = _journal_filled_shares(...)`, replace `if _window_resolved(row["window_slug"]):` with:
   ```python
               # An hourly row settles from the Binance candle, which stays readable
               # after resolution: adopt a journal-recorded fill and settle it for
               # real instead of closing the row at zero.
               settles_from_candle = row.get("strategy_id") is not None and journal_matched > 0
               if _row_window_resolved(row) and not settles_from_candle:
   ```
3. Nothing else changes: adoption still sets this executor's own `_entry_*` fields and `_position_open`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `PYTHON_DOTENV_DISABLED=1 python3 -m pytest tests/unit/test_live_executor.py tests/unit/test_live_wiring.py tests/unit/test_placement_status.py tests/unit/test_reconcile_ledger.py -q`
Expected: PASS (every pre-existing live test unchanged and green).

- [ ] **Step 6: Commit**

```bash
git add db.py polymarket_exec/execution/live.py tests/unit/test_live_executor.py
git commit -m "feat(live): one position slot per strategy on a shared client and RiskGate"
```

---

### Task 6: Hourly engine

**Files:**
- Create: `polymarket_bot/hourly/engine.py`
- Modify: `polymarket_bot/runtime_knobs.py` (add knobs)
- Modify: `polymarket_bot/paper.py` (`_executor_for`, used by `_close_position`)
- Test: `tests/unit/test_hourly_engine.py`

**Interfaces:**
- Consumes: Tasks 1–5.
- Consumes from `polymarket_bot.paper`, lazily inside functions (paper imports the engine): `PaperSnapshot`, `_now`, `_fetch_clob_book`, `_log_tick`, `_close_position`, `_delete_position_row`, `_update_position_terms`, `_risk_gate`, `_live_executor`, `connect`.
- Produces in `paper.py`: `_executor_for(pos: dict) -> LiveExecutor | None`.
- Produces:
  - `TIMEFRAME = "1h"`
  - `STRATEGIES: tuple[tuple[str, str], ...] = (("hourly_mean_reversion", "hourly_mean_reversion_enabled"),)`
  - knobs `hourly_mean_reversion_enabled` (bool, default True) and `hourly_entry_deadline_seconds` (int, default 120, 10–1800)
  - `SUBMITTING`, `UNCERTAIN_PREFIX`, `UNFINISHED_ATTEMPT` decision-record actions for an entry attempt (Claude, 2026-09-15, branch-review finding hourly-ambiguous-post-error-retried)
  - `open_entries` links a `PENDING`/`SUBMITTING` hour to this strategy's same-hour position that is still open (both modes; open-only rule from a review by Claude session polymarket-crypto-95, 2026-09-16) as `ENTERED`, before the deadline check; in live only while `LiveExecutor.tracks_position` is true for the strategy's slot (Claude, 2026-09-15, branch-review finding crash-after-entry-marks-missed)
  - `reset_caches() -> None`
  - `async build_snapshot(client, now: int | None = None) -> PaperSnapshot`
  - `async settle_due(client, snapshot, now: int) -> None`
  - `async decide_hour(client, snapshot, now: int) -> dict[str, dict]` (strategy_id → decision row)
  - `async open_entries(snapshot, now: int, *, allow_entries: bool) -> None`
  - `async tick(client, *, allow_entries: bool = True) -> PaperSnapshot`

- [ ] **Step 1: Add the knobs** — in `polymarket_bot/runtime_knobs.py`, append to `KNOBS` before the closing `}`:

```python
    # --- Hourly BTC strategies (polymarket_bot/hourly/engine.py) -----------
    "hourly_mean_reversion_enabled": Knob(
        "runtime.hourly.mean_reversion_enabled", True, "bool",
        "Hourly Mean Reversion enabled", group="Hourly BTC",
    ),
    "hourly_entry_deadline_seconds": Knob(
        "runtime.hourly.entry_deadline_seconds", 120, "int",
        "Entry deadline after the hour opens", 10, 1800, unit="s", group="Hourly BTC",
    ),
```

- [ ] **Step 2: Write the failing tests**

```python
"""Hourly engine: one decision per hour, per-strategy slots, deadline, gate, Binance settlement."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import pytest_asyncio

import config as _config
import db as _db
from polymarket_bot import paper
from polymarket_bot import runtime_knobs as _knobs
from polymarket_bot.hourly import engine, ledger
from polymarket_bot.hourly import market as hm
from polymarket_exec.execution.live import LiveOrderResult

H = 1_789_326_000  # 3PM ET hour
SLUG = hm.slug_for(H)
NEXT_SLUG = hm.slug_for(H + 3600)


def _kline(open_s: int, o: float, c: float, hi: float, lo: float, tb_share: float) -> list:
    return [open_s * 1000, str(o), str(hi), str(lo), str(c), "10", open_s * 1000 + 3_599_999,
            "1000", 5, str(10 * tb_share), "0", "0"]


def _history(end_hour: int, last_tb: float, *, last_close: float = 110.0) -> list[list]:
    """170 rows: 169 closed hours ending at end_hour-1 (last one pushed up), plus forming end_hour."""
    rows = []
    for k in range(169, 1, -1):
        share = 0.55 if k % 2 else 0.45
        rows.append(_kline(end_hour - k * 3600, 100.0, 100.5, 101.0, 99.0, share))
    rows.append(_kline(end_hour - 3600, 100.0, last_close, 110.0, 99.0, last_tb))
    rows.append(_kline(end_hour, last_close, last_close, last_close, last_close, 0.5))
    return rows


class _Venue:
    """httpx handler for Gamma, CLOB books, Binance spot/perp klines and ticker."""

    def __init__(self, *, hour_close: float = 109.0) -> None:
        self.hour_close = hour_close

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url, p = str(request.url), request.url.params
        if url.startswith(f"{_config.POLYMARKET_GAMMA_API}/markets"):
            slug = p["slug"]
            start = H if slug == SLUG else H + 3600
            iso = hm.datetime.fromtimestamp(start, hm.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            return httpx.Response(200, json=[{
                "slug": slug, "question": slug, "eventStartTime": iso,
                "outcomes": json.dumps(["Up", "Down"]),
                "clobTokenIds": json.dumps([f"up-{start}", f"down-{start}"]),
            }])
        if url.startswith(f"{_config.POLYMARKET_CLOB_API}/book"):
            return httpx.Response(200, json={
                "bids": [{"price": "0.48", "size": "300"}],
                "asks": [{"price": "0.52", "size": "300"}],
            })
        if url.startswith(f"{_config.BINANCE_API_BASE}/api/v3/ticker/price"):
            return httpx.Response(200, json={"price": "110.0"})
        if "startTime" in p:  # hour candle for open / settlement
            start = int(p["startTime"]) // 1000
            rows = [_kline(start, 110.0, self.hour_close, 111.0, 108.0, 0.5)]
            if start + 3600 <= self.end_hour:  # Binance returns the next candle once it began
                c = self.hour_close
                rows.append(_kline(start + 3600, c, c, c, c, 0.5))
            return httpx.Response(200, json=rows)
        if url.startswith(f"{_config.BINANCE_API_BASE}/api/v3/klines"):
            return httpx.Response(200, json=_history(self.end_hour, 0.9))
        if url.startswith(f"{hm.BINANCE_FAPI}/fapi/v1/klines"):
            return httpx.Response(200, json=_history(self.end_hour, 0.5))
        return httpx.Response(404)

    end_hour = H


@pytest_asyncio.fixture
async def test_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "t.db")
    await _db.init_db()
    await _knobs.refresh_cache()
    engine.reset_caches()
    monkeypatch.setattr(paper, "_live_executor", None)
    monkeypatch.setattr(paper, "_risk_gate", None)
    return _db


async def _tick(monkeypatch, now: int, venue: _Venue, *, allow: bool = True):
    monkeypatch.setattr(paper, "_now", lambda: now)
    async with httpx.AsyncClient(transport=httpx.MockTransport(venue)) as client:
        return await engine.tick(client, allow_entries=allow)


async def _positions() -> list[dict]:
    async with _db.connect() as conn:
        cur = await conn.execute("SELECT * FROM paper_positions ORDER BY position_id")
        return [dict(r) for r in await cur.fetchall()]


@pytest.mark.asyncio
async def test_signal_enters_once_on_its_own_slot_and_settles_net_of_fee(test_db, monkeypatch):
    venue = _Venue(hour_close=109.0)  # hour H closes below its 110 open -> Down wins
    snap = await _tick(monkeypatch, H + 30, venue)
    assert snap.window_slug == SLUG and snap.reference_price == 110.0
    await _tick(monkeypatch, H + 35, venue)  # same hour: no second entry
    pos = await _positions()
    assert len(pos) == 1
    p = pos[0]
    assert (p["side"], p["strategy_id"], p["market_timeframe"], p["window_start_ts"]) == (
        "Down", "hourly_mean_reversion", "1h", H)
    assert p["entry_price"] == 0.52 and p["shares"] == 5.0 and p["mode"] == "paper"
    row = await ledger.get_decision(SLUG, "hourly_mean_reversion")
    assert row["action"] == "ENTERED" and row["position_id"] == p["position_id"]

    venue.end_hour = H + 3600
    await _tick(monkeypatch, H + 3600 + 20, venue)
    closed = (await _positions())[0]
    assert closed["state"] == "closed" and closed["exit_price"] == 1.0
    assert closed["realized_pnl_usd"] == pytest.approx(5 * (1 - 0.52) - 5 * 0.07 * 0.52 * 0.48)
    settled = await ledger.get_decision(SLUG, "hourly_mean_reversion")
    assert settled["outcome_side"] == "Down" and settled["hour_close"] == 109.0


@pytest.mark.asyncio
async def test_other_strategy_slot_does_not_block_but_own_slot_does(test_db, monkeypatch):
    async with _db.connect() as conn:
        await conn.execute(
            "INSERT INTO paper_positions(opened_at, window_slug, side, state, entry_price,"
            " notional_usd, shares, strategy_id, market_timeframe, window_start_ts, mode)"
            " VALUES ('x', ?, 'Up', 'open', 0.5, 2.5, 5, 'kronos_btc_finetune', '1h', ?, 'paper')",
            (SLUG, H),
        )
        await conn.commit()
    await _tick(monkeypatch, H + 30, _Venue())
    sids = sorted(p["strategy_id"] for p in await _positions() if p["state"] == "open")
    assert sids == ["hourly_mean_reversion", "kronos_btc_finetune"]


@pytest.mark.asyncio
async def test_after_deadline_signal_is_missed_and_no_entry(test_db, monkeypatch):
    await _tick(monkeypatch, H + 121, _Venue())
    assert await _positions() == []
    assert (await ledger.get_decision(SLUG, "hourly_mean_reversion"))["action"] == "MISSED"


@pytest.mark.asyncio
async def test_gate_block_is_recorded_once_and_journaled(test_db, monkeypatch):
    gate = MagicMock()
    gate.trade_shares = 5.0
    gate.block_reason = MagicMock(return_value="daily loss halt: test")
    monkeypatch.setattr(paper, "_risk_gate", gate)
    await _tick(monkeypatch, H + 30, _Venue())
    await _tick(monkeypatch, H + 40, _Venue())
    assert await _positions() == []
    row = await ledger.get_decision(SLUG, "hourly_mean_reversion")
    assert row["action"] == "BLOCKED:daily loss halt: test"
    async with _db.connect() as conn:
        cur = await conn.execute("SELECT COUNT(*) AS n FROM live_orders WHERE status='BLOCKED'")
        assert (await cur.fetchone())["n"] == 1


@pytest.mark.asyncio
async def test_disabled_strategy_records_nothing_and_kill_holds_entries(test_db, monkeypatch):
    await _knobs.set("hourly_mean_reversion_enabled", False)
    await _tick(monkeypatch, H + 30, _Venue())
    assert await ledger.get_decision(SLUG, "hourly_mean_reversion") is None
    await _knobs.set("hourly_mean_reversion_enabled", True)
    await _tick(monkeypatch, H + 31, _Venue(), allow=False)
    assert await _positions() == []
    assert (await ledger.get_decision(SLUG, "hourly_mean_reversion"))["action"] == "PENDING"


@pytest.mark.asyncio
async def test_tick_straddling_the_hour_uses_one_clock_read(test_db, monkeypatch):
    clock = iter([H + 3599, H + 3601, H + 3602, H + 3603])
    monkeypatch.setattr(paper, "_now", lambda: next(clock))
    async with httpx.AsyncClient(transport=httpx.MockTransport(_Venue())) as client:
        snap = await engine.tick(client)
    assert snap.window_slug == SLUG
    async with _db.connect() as conn:
        cur = await conn.execute("SELECT window_slug, window_start_ts FROM hourly_strategy_context")
        rows = [dict(r) for r in await cur.fetchall()]
    assert all(hm.slug_for(r["window_start_ts"]) == r["window_slug"] for r in rows)


@pytest.mark.asyncio
async def test_waits_until_previous_hour_is_closed(test_db, monkeypatch):
    venue = _Venue()
    venue.end_hour = H - 3600  # Binance hasn't produced a closed H-1 candle yet
    await _tick(monkeypatch, H + 2, venue)
    assert await ledger.get_decision(SLUG, "hourly_mean_reversion") is None


# --- Live mode: the same decision, routed through the strategy's live slot ---------


def _live_account(entry: LiveOrderResult | None = None) -> MagicMock:
    """Account executor fake: slot_executor(strategy_id) returns one mock slot per strategy."""
    slots: dict[str, MagicMock] = {}

    def slot_executor(strategy_id: str) -> MagicMock:
        if strategy_id not in slots:
            slot = MagicMock()
            slot.resync_flat = AsyncMock(return_value=False)
            slot.submit_entry = AsyncMock(return_value=entry or LiveOrderResult(
                ok=True, status="SUBMITTED", order_id="0xE", price=0.52, size=5.0,
                notional_usd=2.6))
            slot.record_settlement = AsyncMock(return_value=LiveOrderResult(
                ok=True, status="SETTLED", price=1.0, size=5.0, notional_usd=2.2))
            slots[strategy_id] = slot
        return slots[strategy_id]

    account = MagicMock()
    account.slot_executor = MagicMock(side_effect=slot_executor)
    account.slots = slots
    return account


def _live_gate() -> MagicMock:
    gate = MagicMock()
    gate.trade_shares = 5.0
    gate.block_reason = MagicMock(side_effect=AssertionError("live entries gate inside submit_entry"))
    gate.record_realized_pnl = AsyncMock()
    gate.record_buy_notional = AsyncMock()
    return gate


async def _go_live(monkeypatch, entry: LiveOrderResult | None = None) -> MagicMock:
    account = _live_account(entry)
    monkeypatch.setattr(paper, "_live_executor", account)
    monkeypatch.setattr(paper, "_risk_gate", _live_gate())
    return account


async def _insert_hourly(start: int, mode: str, side: str = "Down") -> None:
    async with _db.connect() as conn:
        await conn.execute(
            "INSERT INTO paper_positions(opened_at, window_slug, side, state, entry_price,"
            " notional_usd, shares, strategy_id, market_timeframe, window_start_ts, mode)"
            " VALUES ('x', ?, ?, 'open', 0.5, 2.5, 5, 'hourly_mean_reversion', '1h', ?, ?)",
            (hm.slug_for(start), side, start, mode),
        )
        await conn.commit()


@pytest.mark.asyncio
async def test_live_entry_uses_the_strategy_slot_and_settles_through_it(test_db, monkeypatch):
    account = await _go_live(monkeypatch)
    venue = _Venue(hour_close=109.0)
    await _tick(monkeypatch, H + 30, venue)
    slot = account.slots["hourly_mean_reversion"]
    slot.resync_flat.assert_awaited_once()
    slot.submit_entry.assert_awaited_once_with(
        token_id=f"down-{H}", side_price=0.52, notional_usd=pytest.approx(2.6), window_slug=SLUG)
    p = (await _positions())[0]
    assert (p["mode"], p["strategy_id"], p["entry_price"], p["shares"]) == (
        "live", "hourly_mean_reversion", 0.52, 5.0)
    assert (await ledger.get_decision(SLUG, "hourly_mean_reversion"))["mode"] == "live"

    venue.end_hour = H + 3600
    await _tick(monkeypatch, H + 3600 + 20, venue)
    slot.record_settlement.assert_awaited_once_with(True, SLUG)
    closed = (await _positions())[0]
    assert closed["state"] == "closed" and closed["realized_pnl_usd"] == pytest.approx(2.2)


@pytest.mark.asyncio
async def test_live_blocked_entry_is_recorded_once_and_leaves_no_row(test_db, monkeypatch):
    account = await _go_live(monkeypatch, LiveOrderResult(
        ok=False, status="BLOCKED", reason="daily loss halt: test"))
    await _tick(monkeypatch, H + 30, _Venue())
    await _tick(monkeypatch, H + 40, _Venue())
    assert await _positions() == []
    assert account.slots["hourly_mean_reversion"].submit_entry.await_count == 1
    row = await ledger.get_decision(SLUG, "hourly_mean_reversion")
    assert row["action"] == "BLOCKED:daily loss halt: test"


# Claude, 2026-09-15, branch-review finding hourly-ambiguous-post-error-retried: a failed
# post may still have reached the book, so it ends the hour instead of retrying.
@pytest.mark.asyncio
async def test_live_order_error_ends_the_hour_as_uncertain_without_a_second_post(
    test_db, monkeypatch
):
    account = await _go_live(monkeypatch, LiveOrderResult(ok=False, status="ERROR", reason="venue"))
    await _tick(monkeypatch, H + 30, _Venue())
    await _tick(monkeypatch, H + 40, _Venue())
    submit = account.slots["hourly_mean_reversion"].submit_entry
    assert submit.await_count == 1 and await _positions() == []
    assert (await ledger.get_decision(SLUG, "hourly_mean_reversion"))["action"] == "UNCERTAIN:ERROR venue"
    await _tick(monkeypatch, H + 121, _Venue())
    assert (await ledger.get_decision(SLUG, "hourly_mean_reversion"))["action"] == "UNCERTAIN:ERROR venue"
    assert submit.await_count == 1


@pytest.mark.asyncio
async def test_paper_mode_leaves_live_rows_to_the_live_executor(test_db, monkeypatch):
    await _insert_hourly(H - 3600, "live")
    await _tick(monkeypatch, H + 30, _Venue())
    rows = await _positions()
    assert [r["state"] for r in rows if r["mode"] == "live"] == ["open"]
    assert len([r for r in rows if r["mode"] == "paper"]) == 1  # slots are per mode


@pytest.mark.asyncio
async def test_live_mode_settles_a_paper_row_paper_style(test_db, monkeypatch):
    account = await _go_live(monkeypatch)
    await _insert_hourly(H - 3600, "paper")
    await _tick(monkeypatch, H + 30, _Venue(hour_close=109.0))  # Down won H-1
    row = [r for r in await _positions() if r["mode"] == "paper"][0]
    assert row["state"] == "closed"
    assert row["realized_pnl_usd"] == pytest.approx(5 * 0.5 - 5 * 0.07 * 0.5 * 0.5)
    account.slots["hourly_mean_reversion"].record_settlement.assert_not_awaited()
```

- [ ] **Step 3: Run them to verify they fail**

Run: `PYTHON_DOTENV_DISABLED=1 python3 -m pytest tests/unit/test_hourly_engine.py -q`
Expected: FAIL with `ImportError: cannot import name 'engine'`

- [ ] **Step 4: Write the engine**

```python
"""Hourly BTC engine: one decision per hour per strategy, per-strategy entries, Binance settlement.

Called by ``polymarket_bot.paper.paper_tick_once`` when the loop was started on the BTC 1h
selection. Strategies never read mode; entries go through the same RiskGate as every other
entry. Each strategy owns one open-position slot (operator decision 2026-09-14).
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import httpx

from db import journal_live_order, notify
from logging_setup import get_logger
from polymarket_bot import runtime_knobs as _knobs
from polymarket_bot.hourly import ledger, market, mean_reversion
from polymarket_bot.hourly.market import HOUR_S, HourMarket
from polymarket_exec.execution.gate import EntryRequest
from polymarket_exec.execution.live import DEFAULT_MIN_ORDER_SIZE

if TYPE_CHECKING:
    from polymarket_bot.paper import PaperSnapshot

log = get_logger("hourly_engine")

TIMEFRAME = "1h"
_ET = ZoneInfo("America/New_York")
_CANDLES = mean_reversion.WINDOW + 2

# (strategy_id, enable knob). Kronos BTC Fine Tune joins in PR 2.
STRATEGIES: tuple[tuple[str, str], ...] = (
    (mean_reversion.STRATEGY_ID, "hourly_mean_reversion_enabled"),
)

# Claude, 2026-09-15, branch-review finding hourly-ambiguous-post-error-retried:
# decision-record actions for an entry attempt. SUBMITTING is written before any order
# goes out; UNCERTAIN ends an hour whose order may have reached the venue.
SUBMITTING = "SUBMITTING"
UNCERTAIN_PREFIX = "UNCERTAIN:"
UNFINISHED_ATTEMPT = f"{UNCERTAIN_PREFIX}entry attempt did not finish"

_market_cache: dict[int, HourMarket] = {}
_open_cache: dict[int, float] = {}


def reset_caches() -> None:
    _market_cache.clear()
    _open_cache.clear()


async def _market_for(client: httpx.AsyncClient, start_ts: int) -> HourMarket:
    if start_ts not in _market_cache:
        found = await market.discover(client, start_ts)
        if found is None:
            raise RuntimeError(f"Could not discover the BTC hourly market {market.slug_for(start_ts)}")
        _market_cache.clear()
        _market_cache[start_ts] = found
    return _market_cache[start_ts]


async def _hour_open(client: httpx.AsyncClient, start_ts: int, now_ms: int) -> float | None:
    if start_ts not in _open_cache:
        candle = await market.fetch_hour_candle(client, start_ts, now_ms)
        if candle is None:
            return None
        _open_cache.clear()
        _open_cache[start_ts] = candle.open
    return _open_cache[start_ts]


async def build_snapshot(client: httpx.AsyncClient, now: int | None = None) -> PaperSnapshot:
    from polymarket_bot import paper as P

    now = P._now() if now is None else now
    start = market.hour_start(now)
    m = await _market_for(client, start)
    up_book = await P._fetch_clob_book(client, m.up_token_id)
    down_book = await P._fetch_clob_book(client, m.down_token_id)
    hour_open = await _hour_open(client, start, now * 1000)
    spot = await market.fetch_spot(client)
    return P.PaperSnapshot(
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        window_slug=m.slug,
        market_question=m.question,
        remaining_seconds=max(0, start + HOUR_S - now),
        spot_price=spot or 0.0,
        reference_price=hour_open or 0.0,
        sigma_per_second=0.0,
        market_up_price=up_book.best_ask,
        market_down_price=down_book.best_ask,
        fair_up_prob=0.5,
        edge=0.0,
        signal_side=None,
        confidence=0.0,
        notional_usd=0.0,
        reason="hourly: waiting",
        feed_source=(
            f"spot={'binance_rest' if spot else 'unavailable'};"
            f"ref={'binance_kline' if hour_open else 'unavailable'};vol=none;quotes=clob"
        ),
        up_token_id=m.up_token_id,
        down_token_id=m.down_token_id,
        up_best_bid=up_book.best_bid,
        up_best_ask=up_book.best_ask,
        up_bid_size=up_book.bid_size,
        up_ask_size=up_book.ask_size,
        down_best_bid=down_book.best_bid,
        down_best_ask=down_book.best_ask,
        down_bid_size=down_book.bid_size,
        down_ask_size=down_book.ask_size,
        quote_source="clob",
        feed_degraded=hour_open is None,
    )


def _factors(start_ts: int) -> dict[str, Any]:
    utc = datetime.fromtimestamp(start_ts, UTC)
    return {
        "us_open_hour": utc.astimezone(_ET).hour == 9,
        "expiry_08utc": utc.hour == 8,
        "weekend": utc.weekday() >= 5,
    }


async def decide_hour(
    client: httpx.AsyncClient, snapshot: PaperSnapshot, now: int
) -> dict[str, dict[str, Any]]:
    """Record this hour's decision for every enabled strategy (once), return the rows."""
    from polymarket_bot import paper as P

    start = market.hour_start(now)
    rows: dict[str, dict[str, Any]] = {}
    pending: list[str] = []
    for sid, knob in STRATEGIES:
        if not _knobs.cached(knob):
            continue
        row = await ledger.get_decision(snapshot.window_slug, sid)
        if row is None:
            pending.append(sid)
        else:
            rows[sid] = row
    if not pending or snapshot.reference_price <= 0:
        return rows
    now_ms = now * 1000
    spot = await market.fetch_closed_candles(
        client, market="spot", symbol="BTCUSDT", now_ms=now_ms, limit=_CANDLES
    )
    perp = await market.fetch_closed_candles(
        client, market="perp", symbol="BTCUSDT", now_ms=now_ms, limit=_CANDLES
    )
    prev_ms = (start - HOUR_S) * 1000
    if not (
        len(spot) >= mean_reversion.WINDOW and len(perp) >= mean_reversion.WINDOW
        and spot[-1].open_time_ms == prev_ms and perp[-1].open_time_ms == prev_ms
    ):
        log.info("hourly_engine.waiting_for_previous_hour", window_slug=snapshot.window_slug)
        return rows
    late = now - start > _knobs.cached("hourly_entry_deadline_seconds")
    mode = "live" if P._live_executor is not None else "paper"
    for sid in pending:
        if sid == mean_reversion.STRATEGY_ID:
            d = mean_reversion.decide(spot, perp)
        else:  # pragma: no cover - PR 2 adds Kronos
            continue
        await ledger.record_decision(
            strategy_id=sid, window_slug=snapshot.window_slug, window_start_ts=start,
            side=d.side, reason=d.reason, signal=d.signal, factors=_factors(start),
            up_bid=snapshot.up_best_bid, up_ask=snapshot.up_best_ask,
            down_bid=snapshot.down_best_bid, down_ask=snapshot.down_best_ask,
            hour_open=snapshot.reference_price, mode=mode, late=late,
        )
        rows[sid] = await ledger.get_decision(snapshot.window_slug, sid) or {}
    return rows


async def _open_row_for(strategy_id: str, mode: str) -> bool:
    """True while this strategy's slot in this mode still holds an open row."""
    from polymarket_bot import paper as P

    async with P.connect() as db:
        async with db.execute(
            "SELECT COUNT(*) AS n FROM paper_positions "
            "WHERE state = 'open' AND strategy_id = ? AND mode = ?",
            (strategy_id, mode),
        ) as cur:
            return bool((await cur.fetchone())["n"])


async def _insert_row(
    snapshot: PaperSnapshot, *, strategy_id: str, side: str, price: float, notional: float,
    shares: float, start: int, reason: str | None, mode: str,
) -> int:
    from polymarket_bot import paper as P

    async with P.connect() as db:
        cur = await db.execute(
            """
            INSERT INTO paper_positions(
              opened_at, window_slug, market_question, side, state, entry_price,
              notional_usd, shares, opened_spot, confidence, edge, entry_reason,
              feed_source, quote_source, strategy_style, mode, strategy_id,
              market_timeframe, window_start_ts
            ) VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?, 0, 0, ?, ?, 'clob', 'settle', ?, ?, ?, ?)
            """,
            (snapshot.created_at, snapshot.window_slug, snapshot.market_question, side, price,
             notional, shares, snapshot.spot_price, reason, snapshot.feed_source, mode,
             strategy_id, TIMEFRAME, start),
        )
        position_id = int(cur.lastrowid or 0)
        await db.commit()
    return position_id


async def _enter(snapshot: PaperSnapshot, strategy_id: str, side: str, start: int) -> None:
    from polymarket_bot import paper as P

    executor = P._live_executor
    mode = "live" if executor is not None else "paper"
    if await _open_row_for(strategy_id, mode):
        return  # this strategy's slot is still held (previous hour settling): retry next tick
    ask = snapshot.up_best_ask if side == "Up" else snapshot.down_best_ask
    top = snapshot.up_ask_size if side == "Up" else snapshot.down_ask_size
    if ask is None or ask <= 0 or (top is not None and top <= 0):
        return  # no executable ask this tick: retry until the deadline
    gate = P._risk_gate
    shares = max(gate.trade_shares if gate is not None else DEFAULT_MIN_ORDER_SIZE,
                 DEFAULT_MIN_ORDER_SIZE)
    if top is not None:
        shares = min(shares, top)
    notional = shares * ask
    token = snapshot.up_token_id if side == "Up" else snapshot.down_token_id
    reason = (await ledger.get_decision(snapshot.window_slug, strategy_id) or {}).get(
        "decision_reason")
    # Claude, 2026-09-15, branch-review finding hourly-ambiguous-post-error-retried:
    # record the attempt before any order goes out, in both modes. open_entries only
    # enters from PENDING, so a tick that dies mid-attempt (or a restart within the
    # hour) can never place a second order for this hour.
    await ledger.set_action(snapshot.window_slug, strategy_id, SUBMITTING)
    if executor is not None:
        slot = executor.slot_executor(strategy_id)
        # The ledger says this strategy is flat in live: heal any phantom slot
        # state an interrupted stop left behind (same as the legacy loop, #91).
        await slot.resync_flat()
        # Row first, then the real order (the legacy loop's ordering): a failed
        # submit deletes the row, and a crash after submit leaves a row that boot
        # reconciliation adopts from the journal. RiskGate runs inside submit_entry.
        # Claude, 2026-09-15, branch-review finding hourly-reentry-after-untraced-post:
        # a crash between the post and its journal write leaves no journal entry, so
        # reconciliation closes the row instead; the SUBMITTING record above is what
        # stops a second post for this hour.
        position_id = await _insert_row(
            snapshot, strategy_id=strategy_id, side=side, price=ask, notional=notional,
            shares=shares, start=start, reason=reason, mode=mode,
        )
        result = await slot.submit_entry(
            token_id=token, side_price=ask, notional_usd=notional,
            window_slug=snapshot.window_slug,
        )
        if not result.ok:
            await P._delete_position_row(position_id)
            if result.status == "BLOCKED":
                # Refused before any post (gate, no token id, venue minimum, kill
                # switch): nothing reached the venue.
                await ledger.set_action(
                    snapshot.window_slug, strategy_id, f"BLOCKED:{result.reason}")
                log.warning("hourly_engine.live_entry_not_placed", strategy_id=strategy_id,
                            status=result.status, reason=result.reason)
                return
            # Claude, 2026-09-15, branch-review finding hourly-ambiguous-post-error-retried:
            # every other failure happened at or after the post (an exception or timeout,
            # or a response without success and an order id). The venue may still have
            # accepted the order, and a re-post is a new order, so the hour ends here.
            uncertain = f"{UNCERTAIN_PREFIX}{result.status} {result.reason}".strip()
            await ledger.set_action(snapshot.window_slug, strategy_id, uncertain)
            await notify(
                "live_entry_uncertain",
                f"LIVE entry for {strategy_id} may or may not be on Polymarket "
                f"({result.status}: {result.reason}). No retry this hour; check the "
                "account's open orders and trades.",
                {"window_slug": snapshot.window_slug, "strategy_id": strategy_id,
                 "token_id": token},
            )
            log.warning("hourly_engine.live_entry_outcome_unknown", strategy_id=strategy_id,
                        status=result.status, reason=result.reason)
            return
        ask = result.price or ask
        notional = result.notional_usd or notional
        shares = result.size or notional / ask
        await P._update_position_terms(position_id, ask, notional, shares)
    else:
        if gate is not None:
            blocked = gate.block_reason(EntryRequest(
                notional_usd=notional, position_open=False, entry_order_resting=False,
                side_price=ask, best_ask=ask,
            ))
            if blocked is not None:
                await journal_live_order(
                    intent="ENTRY", side="BUY", status="BLOCKED",
                    window_slug=snapshot.window_slug, token_id=token, price=ask, size=shares,
                    notional_usd=notional, error=blocked, mode="paper",
                    strategy_id=strategy_id,
                )
                await ledger.set_action(snapshot.window_slug, strategy_id, f"BLOCKED:{blocked}")
                log.info("hourly_engine.entry_blocked", strategy_id=strategy_id, reason=blocked)
                return
        position_id = await _insert_row(
            snapshot, strategy_id=strategy_id, side=side, price=ask, notional=notional,
            shares=shares, start=start, reason=reason, mode=mode,
        )
        if gate is not None:
            await gate.record_buy_notional(round(ask * shares, 4))
    await ledger.set_action(snapshot.window_slug, strategy_id, "ENTERED", position_id)
    label = "LIVE" if executor is not None else "Paper"
    await notify(
        f"{mode}_entry",
        f"{label} BUY {side} ${notional:.2f} @ {ask:.3f} ({strategy_id})",
        {"window_slug": snapshot.window_slug, "strategy_id": strategy_id},
    )
    log.info("hourly_engine.entered", mode=mode, strategy_id=strategy_id, side=side,
             price=ask, shares=shares, window_slug=snapshot.window_slug)


async def open_entries(snapshot: PaperSnapshot, now: int, *, allow_entries: bool) -> None:
    start = market.hour_start(now)
    deadline = _knobs.cached("hourly_entry_deadline_seconds")
    for sid, knob in STRATEGIES:
        if not _knobs.cached(knob):
            continue
        row = await ledger.get_decision(snapshot.window_slug, sid)
        if row is not None and row["action"] == SUBMITTING:
            # Claude, 2026-09-15, branch-review finding hourly-ambiguous-post-error-retried:
            # an earlier tick started an entry and never recorded its result (it raised,
            # or the process stopped). Close the hour out; never enter again.
            # Claude, 2026-09-15, branch-review finding hourly-reentry-after-untraced-post:
            # a live post can land right before its journal write fails. Boot reconciliation
            # then closes that row as RECONCILED_NO_LIVE_TRACE and nothing tracks the
            # tokens, so tell the operator before closing the hour (notify first: if the
            # record write fails, the next tick still finds SUBMITTING and repeats both).
            await notify(
                "entry_attempt_unfinished",
                f"Entry attempt for {sid} ({snapshot.window_slug}) did not finish: the tick "
                "failed or the bot stopped mid-order. No retry this hour. If the bot was "
                "LIVE, an order may be on Polymarket with no ledger row; check the "
                "account's open orders and trades.",
                {"window_slug": snapshot.window_slug, "strategy_id": sid},
            )
            await ledger.set_action(snapshot.window_slug, sid, UNFINISHED_ATTEMPT,
                                    expected_action=SUBMITTING)
            continue
        if row is None or row["action"] != "PENDING":
            continue
        if now - start > deadline:
            await ledger.set_action(snapshot.window_slug, sid, "MISSED")
            continue
        if allow_entries:
            await _enter(snapshot, sid, str(row["decision_side"]), start)


async def settle_due(client: httpx.AsyncClient, snapshot: PaperSnapshot, now: int) -> None:
    """Settle hourly positions and decision rows whose hour candle has closed on Binance."""
    from polymarket_bot import paper as P

    now_ms = now * 1000
    candles: dict[int, market.HourCandle | None] = {}

    async def closed_candle(start_ts: int) -> market.HourCandle | None:
        if start_ts not in candles:
            try:
                candles[start_ts] = await market.fetch_hour_candle(client, start_ts, now_ms)
            except httpx.HTTPError as exc:
                log.warning("hourly_engine.settle_read_failed", start=start_ts, error=str(exc))
                candles[start_ts] = None
        c = candles[start_ts]
        return c if c is not None and c.closed else None

    async with P.connect() as db:
        async with db.execute(
            "SELECT * FROM paper_positions WHERE state = 'open' AND market_timeframe = ? "
            "ORDER BY opened_at",
            (TIMEFRAME,),
        ) as cur:
            open_rows = [dict(r) for r in await cur.fetchall()]
    for pos in open_rows:
        start_ts = int(pos["window_start_ts"])
        live_row = pos.get("mode") == "live"
        if start_ts + HOUR_S > now or (live_row and P._live_executor is None):
            continue  # hour still running, or real tokens only the live executor may book
        c = await closed_candle(start_ts)
        if c is None:
            continue
        up = market.up_won(c.open, c.close)
        won = up if pos["side"] == "Up" else not up
        held: float | None = None
        pnl: float | None = None
        if live_row:
            slot = P._live_executor.slot_executor(str(pos["strategy_id"]))
            result = await slot.record_settlement(won, pos["window_slug"])
            if not result.ok and result.status != "SKIPPED":
                continue  # registration failed: keep the row open and retry next tick
            # Book the venue's real held size and fee-true PnL (as the legacy loop does).
            held, pnl = result.size or 0.0, result.notional_usd
        await P._close_position(
            pos, snapshot, 1.0 if won else 0.0, "SETTLED",
            settled=True, settled_held=held, settled_pnl=pnl,
        )
    for start_ts in await ledger.unsettled_windows(now):
        c = await closed_candle(start_ts)
        if c is not None:
            await ledger.settle_window(start_ts, c.open, c.close)


def _reason_line(rows: dict[str, dict[str, Any]]) -> str:
    if not rows:
        return "hourly: waiting for the previous hour to close"
    return " | ".join(f"{sid}: {r.get('action')} ({r.get('decision_reason')})"
                      for sid, r in rows.items())


async def tick(client: httpx.AsyncClient, *, allow_entries: bool = True) -> PaperSnapshot:
    from polymarket_bot import paper as P

    # One clock read per tick: the snapshot's market and every decision, entry and
    # settlement step must agree on the hour, even when a tick straddles H:00.
    now = P._now()
    snapshot = await build_snapshot(client, now)
    await settle_due(client, snapshot, now)
    rows = await decide_hour(client, snapshot, now)
    await open_entries(snapshot, now, allow_entries=allow_entries)
    for sid in list(rows):
        rows[sid] = await ledger.get_decision(snapshot.window_slug, sid) or rows[sid]
    snapshot.reason = _reason_line(rows)
    entered = [r for r in rows.values() if r.get("action") == "ENTERED"]
    if entered:
        snapshot.signal_side = entered[0].get("decision_side")
    await P._log_tick(snapshot)
    return snapshot
```

- [ ] **Step 5: Route each row's close to the executor that owns it (`polymarket_bot/paper.py`)**

Add above `_close_position`:

```python
def _executor_for(pos: dict[str, Any]) -> LiveExecutor | None:
    """The live executor owning ``pos``: the account slot, a strategy slot, or None.

    Paper rows of a strategy slot hold no real tokens, so they close paper-style even
    while the loop runs live. Strategy-less (legacy loop) rows are unchanged.
    """
    if _live_executor is None:
        return None
    strategy_id = pos.get("strategy_id")
    if not strategy_id:
        return _live_executor
    if pos.get("mode") != "live":
        return None
    return _live_executor.slot_executor(str(strategy_id))
```

In `_close_position`, replace `executor = _live_executor` with `executor = _executor_for(pos)`.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `PYTHON_DOTENV_DISABLED=1 python3 -m pytest tests/unit/test_hourly_engine.py tests/unit/test_runtime_knobs.py tests/unit/test_live_wiring.py tests/unit/test_settle_style.py -q`
Expected: PASS. If `test_runtime_knobs.py` pins the knob list or count, add the two new knobs to its expectation in the same commit.

- [ ] **Step 7: Commit**

```bash
git add polymarket_bot/hourly/engine.py polymarket_bot/runtime_knobs.py polymarket_bot/paper.py tests/unit/test_hourly_engine.py tests/unit/test_runtime_knobs.py
git commit -m "feat(hourly): engine - one decision per hour, per-strategy slots in paper and live"
```

---

### Task 7: Loop routing, pinned selection, Stop covers every slot

**Files:**
- Modify: `polymarket_bot/paper.py`
  - `run_paper_loop` signature, globals, `cancel_open_all` on stop.
  - `paper_tick_once` routing.
  - `force_close_open_positions` hourly rows.
  - `_detail_from_snapshot` None-safe prices.
- Modify: `polymarket_bot/controller.py`
  - `request_start` pins the selection.
  - `_ensure_runner_started` args.
  - `_run_loop_in_thread`.
- Modify: `polymarket_bot/market_selection.py` (`LOOP_SUPPORTED`)
- Modify: `tests/unit/test_loop_watchdog.py` (fake runner signature)
- Test: `tests/unit/test_hourly_loop.py`

**Interfaces:**
- Consumes: `engine.TIMEFRAME`, `engine.tick`, `engine.build_snapshot` (Task 6); `LiveExecutor.cancel_open_all` (Task 5); `paper._executor_for` (Task 6).
- Produces:
  - `paper.run_paper_loop(stop_event, mode: str | None = None, timeframe: str = "5m")`
  - module global `paper._timeframe: str`
  - `controller._run_loop_in_thread(stop_event, mode, timeframe="5m")`
  - `market_selection.LOOP_SUPPORTED == frozenset({("btc", "5m"), ("btc", "1h")})`

- [ ] **Step 1: Write the failing tests**

```python
"""The loop trades BTC 1h when started on that selection, in whichever mode Start picked."""
from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

import db as _db
from polymarket_bot import controller, market_selection, paper
from polymarket_bot.hourly import engine
from polymarket_bot.hourly import market as hm
from polymarket_exec.execution.live import LiveOrderResult

H = 1_789_326_000
SLUG = hm.slug_for(H)


@pytest_asyncio.fixture
async def test_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "t.db")
    await _db.init_db()
    monkeypatch.setattr(paper, "_timeframe", "5m")  # restored after each test
    return _db


def test_btc_1h_is_loop_supported() -> None:
    assert ("btc", "1h") in market_selection.LOOP_SUPPORTED


@pytest.mark.asyncio
async def test_live_on_1h_starts_like_any_live_run_and_stop_cancels_every_slot(
    test_db, monkeypatch
) -> None:
    executor = MagicMock()
    executor.start = AsyncMock()
    executor.cancel_open_all = AsyncMock(return_value=[])
    monkeypatch.setattr(paper, "build_live_executor", MagicMock(return_value=executor))
    feed = MagicMock()
    feed.run = AsyncMock()
    monkeypatch.setattr(paper, "ChainlinkWsFeed", MagicMock(return_value=feed))
    stop = threading.Event()
    stop.set()  # start, then stop straight away

    await paper.run_paper_loop(stop, mode="live", timeframe="1h")

    executor.start.assert_awaited_once()
    executor.cancel_open_all.assert_awaited_once_with(reason="LOOP_STOP")
    assert paper._timeframe == "1h"
    assert await _db.get_config("polymarket_bot.mode") == "live"
    assert await _db.get_config("polymarket_bot.state") == "stopped"


@pytest.mark.asyncio
async def test_stop_in_live_sells_current_hour_rows_through_their_own_slot(
    test_db, monkeypatch
) -> None:
    async with _db.connect() as conn:
        await conn.execute(
            "INSERT INTO paper_positions(opened_at, window_slug, side, state, entry_price,"
            " notional_usd, shares, strategy_id, market_timeframe, window_start_ts, mode)"
            " VALUES ('x', ?, 'Down', 'open', 0.52, 2.6, 5, 'hourly_mean_reversion', '1h', ?,"
            " 'live')",
            (SLUG, H),
        )
        await conn.commit()
    slot = MagicMock()
    slot.submit_exit = AsyncMock(return_value=LiveOrderResult(
        ok=True, status="SUBMITTED", order_id="0xX", price=0.50, size=5.0, notional_usd=2.5))
    account = MagicMock()
    account.slot_executor = MagicMock(return_value=slot)
    monkeypatch.setattr(paper, "_live_executor", account)
    snap = SimpleNamespace(window_slug=SLUG, created_at="y", spot_price=1.0,
                           up_best_bid=0.49, down_best_bid=0.50)
    monkeypatch.setattr(engine, "build_snapshot", AsyncMock(return_value=snap))

    assert await paper.force_close_open_positions("STOP_REQUEST") == 1

    account.slot_executor.assert_called_with("hourly_mean_reversion")
    slot.submit_exit.assert_awaited_once_with(side_price=0.50, size=5.0, window_slug=SLUG)
    account.submit_exit.assert_not_called()


@pytest.mark.asyncio
async def test_paper_tick_routes_to_engine_on_1h(test_db, monkeypatch) -> None:
    snap = MagicMock()
    tick = AsyncMock(return_value=snap)
    monkeypatch.setattr(engine, "tick", tick)
    monkeypatch.setattr(paper, "_timeframe", "1h")
    monkeypatch.setattr(paper, "_risk_gate", None)
    monkeypatch.setattr(paper, "_live_executor", None)
    assert await paper.paper_tick_once() is snap
    tick.assert_awaited_once()
    assert tick.await_args.kwargs == {"allow_entries": True}


@pytest.mark.asyncio
async def test_start_pins_the_selected_timeframe(test_db, monkeypatch) -> None:
    await market_selection.set_selection("btc", "1h")
    started: list[tuple] = []
    monkeypatch.setattr(controller, "_ensure_runner_started", lambda force=False: started.append(
        (controller._mode_cache, controller._timeframe_cache)))
    monkeypatch.setattr(controller, "_ensure_watchdog_started", lambda: None)
    await controller.request_start()
    assert started == [("paper", "1h")]
    controller._desired_running = False


@pytest.mark.asyncio
async def test_start_refuses_an_unsupported_selection(test_db, monkeypatch) -> None:
    await market_selection.set_selection("eth", "1h")
    spawn = MagicMock()
    monkeypatch.setattr(controller, "_ensure_runner_started", spawn)
    status = await controller.request_start()
    spawn.assert_not_called()
    assert status.state == "stopped" and "not wired" in status.detail
```

- [ ] **Step 2: Run them to verify they fail**

Run: `PYTHON_DOTENV_DISABLED=1 python3 -m pytest tests/unit/test_hourly_loop.py -q`
Expected: FAIL (`LOOP_SUPPORTED` lacks 1h; `run_paper_loop()` got an unexpected keyword `timeframe`)

- [ ] **Step 3: Implement in `polymarket_bot/paper.py`**

Add to the imports:

```python
from polymarket_bot.hourly import engine as hourly_engine
```

Add a module global next to `_live_executor`:

```python
# Market timeframe pinned at Start ("5m" legacy loop, "1h" hourly strategies).
_timeframe: str = "5m"
```

Change the `run_paper_loop` signature and global line:

```python
async def run_paper_loop(
    stop_event: threading.Event, mode: str | None = None, timeframe: str = "5m"
) -> None:
```

```python
    global _live_executor, _chainlink_feed, _risk_gate, _loop_generation, _timeframe
```

Directly after `if mode is None: mode = …`, insert (no mode check: the selected mode runs as selected):

```python
    _timeframe = timeframe
```

In the `finally` block, replace `await _live_executor.cancel_open(reason="LOOP_STOP")` with:

```python
                await _live_executor.cancel_open_all(reason="LOOP_STOP")
```

so Stop and the loss-halt stop cancel resting orders in every strategy slot before flattening.

In `paper_tick_once`, replace the `async with _make_settlement_client() as client:` block with:

```python
    async with _make_settlement_client() as client:
        if _timeframe == hourly_engine.TIMEFRAME:
            return await hourly_engine.tick(client, allow_entries=not kill_active)
        snapshot = await _build_snapshot(client)
        await _log_tick(snapshot)
        await _close_due_positions(snapshot, client)
        if not kill_active:
            await _maybe_open_position(snapshot)
        await _record_and_settle_shadow(snapshot, client)
    return snapshot
```

Decide `kill_active` the same way in both modes before this block. Live keeps `await _live_executor.enforce_kill_switch()` (its cancel sweep is the only live-only side effect); paper sets `kill_active = _risk_gate.kill_switch_active()`. Under kill, both modes hold the hourly row `PENDING`, enter if the file is removed before the entry deadline, and record `MISSED` if not. Paper no longer records a final `BLOCKED:KILL…` plus a `live_orders` row. (Claude, 2026-09-15, branch-review finding kill-switch-paper-blocked-live-pending)

In `force_close_open_positions`, replace everything from `if not positions:` to the end of the function with:

```python
    if not positions:
        return 0
    hourly = [p for p in positions if p.get("market_timeframe") == hourly_engine.TIMEFRAME]
    legacy = [p for p in positions if p.get("market_timeframe") != hourly_engine.TIMEFRAME]
    closed = 0
    async with _make_settlement_client() as client:
        if legacy:
            snapshot = await _build_snapshot(client)
            for pos in legacy:
                if await _close_position(
                    pos, snapshot, _current_price_for_side(snapshot, pos["side"]), exit_reason
                ):
                    closed += 1
        if hourly:
            # Amended: Claude, 2026-09-15, branch-review finding dst-fallback-slug-collision.
            now = _now()
            current_start = hourly_market.hour_start(now)
            snapshot = await hourly_engine.build_snapshot(client, now)
            for pos in hourly:
                bid = _current_price_for_side(snapshot, pos["side"])
                if pos["window_start_ts"] != current_start or bid is None:
                    # A past hour can't be sold; it settles from Binance on the next start.
                    log.warning("force_close.hourly_left_for_settlement",
                                position_id=pos["position_id"], window_slug=pos["window_slug"])
                    continue
                if await _close_position(pos, snapshot, bid, exit_reason):
                    closed += 1
    return closed
```

In `_detail_from_snapshot`, replace the `Polymarket Up:` line with a None-safe version:

```python
        f"Polymarket Up: {_fmt3(snapshot.market_up_price)}; fair Up: {snapshot.fair_up_prob:.3f}; "
```

and add the helper above `_detail_from_snapshot`:

```python
def _fmt3(value: float | None) -> str:
    return f"{value:.3f}" if value is not None else "—"
```

- [ ] **Step 4: Implement in `polymarket_bot/controller.py` and `market_selection.py`**

`market_selection.py`:

```python
LOOP_SUPPORTED: frozenset[tuple[str, str]] = frozenset({("btc", "5m"), ("btc", "1h")})
```

`controller.py`:
- Add the global `_timeframe_cache: str = "5m"` next to `_mode_cache`.
- Extend the `global` line in `request_start` with `_timeframe_cache`.
- Right after `mode = await current_mode()`, insert:

```python
    from polymarket_bot import market_selection

    selection = await market_selection.get_selection()
    if not selection.loop_supported:
        detail = (
            f"Start refused: the loop is not wired for {selection.asset.upper()} "
            f"{selection.timeframe} yet. Select BTC 5m or BTC 1h."
        )
        await set_config("polymarket_bot.state", "stopped")
        await set_config("polymarket_bot.updated_at", now)
        await set_config("polymarket_bot.detail", detail)
        return await get_status()
```

After `_mode_cache = mode` add `_timeframe_cache = selection.timeframe`. Replace the two `detail = (...)` start messages with:

```python
    market_label = f"BTC {selection.timeframe}"
    if mode == "live":
        detail = (
            f"BTC LIVE loop starting — orders are REAL. It will discover the current "
            f"{market_label} market and place risk-gated CLOB orders."
        )
    else:
        detail = (
            f"BTC paper loop starting. It will discover the current {market_label} "
            "market and log simulated trades only."
        )
```

In `_ensure_runner_started`, use `args=(_stop_event, _mode_cache, _timeframe_cache),`. Change the runner:

```python
def _run_loop_in_thread(stop_event: threading.Event, mode: str, timeframe: str = "5m") -> None:
    asyncio.run(run_paper_loop(stop_event, mode=mode, timeframe=timeframe))
```

`tests/unit/test_loop_watchdog.py`: change the fake runner's signature to

```python
        def fake_run(
            stop_event: threading.Event, mode: str | None = None, timeframe: str = "5m"
        ) -> None:
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `PYTHON_DOTENV_DISABLED=1 python3 -m pytest tests/unit/test_hourly_loop.py tests/unit/test_loop_watchdog.py tests/unit/test_mode_switch.py tests/unit/test_live_wiring.py tests/unit/test_runtime_config.py -q`
Expected: PASS. If `test_runtime_config.py` expects `("btc","1h")` to be unsupported, change that expectation to another unwired pair such as `("eth","1h")`.

- [ ] **Step 6: Commit**

```bash
git add polymarket_bot/paper.py polymarket_bot/controller.py polymarket_bot/market_selection.py tests/unit/test_hourly_loop.py tests/unit/test_loop_watchdog.py tests/unit/test_runtime_config.py
git commit -m "feat(hourly): pin selection at Start, route 1h ticks to the engine in either mode"
```

---

### Task 8: Selector glow, rule text, live authorization, docs, gates, smoke run

**Files:**
- Modify: `polymarket_exec/ops/dashboard/panels/market_selector.py` (`open_market_pnl`)
- Modify: `AGENTS.md` (position rule; live authorization for BTC hourly; Active Scope)
- Modify: `docs/CODE_MAP.md` (routing row)
- Test: `tests/unit/test_market_selector.py` (append; create it if absent)

**Interfaces:**
- Produces: `market_selector.open_market_pnl` maps `bitcoin-up-or-down-…-<h><am|pm>-et` rows to `("btc", "1h")`.

- [ ] **Step 1: Write the failing test** (append to `tests/unit/test_market_selector.py`, creating the file with a one-line docstring if absent)

```python
from polymarket_exec.ops.dashboard.panels import market_selector as msel


def test_hourly_slug_rows_glow_the_btc_1h_button() -> None:
    rows = [{"window_slug": "bitcoin-up-or-down-september-13-2026-3pm-et", "side": "Down",
             "entry_price": 0.52, "shares": 5.0}]
    assert msel.open_market_pnl(open_pos=rows, daily_open=[], tick=None) == {("btc", "1h"): None}
```

- [ ] **Step 2: Run it to verify it fails**

Run: `PYTHON_DOTENV_DISABLED=1 python3 -m pytest tests/unit/test_market_selector.py -q -k hourly`
Expected: FAIL with `assert {} == {('btc', '1h'): None}`

- [ ] **Step 3: Implement** — in `market_selector.py`, below `_UPDOWN_SLUG`, add:

```python
_HOURLY_SLUG = re.compile(
    r"^(bitcoin|ethereum|solana|xrp|dogecoin|bnb)-up-or-down-[a-z]+-\d+-\d{4}-\d{1,2}(am|pm)-et$"
)
_LONG_TO_ASSET = {"bitcoin": "btc", "ethereum": "eth", "solana": "sol", "xrp": "xrp",
                  "dogecoin": "doge", "bnb": "bnb"}
```

In `open_market_pnl`, replace

```python
        m = _UPDOWN_SLUG.match(str(p.get("window_slug") or ""))
        if not m:
            continue
```

with

```python
        slug = str(p.get("window_slug") or "")
        m = _UPDOWN_SLUG.match(slug)
        hourly = None if m else _HOURLY_SLUG.match(slug)
        if not m and not hourly:
            continue
        key = (m.group(1), m.group(2)) if m else (_LONG_TO_ASSET[hourly.group(1)], "1h")
```

and change the final `_add((m.group(1), m.group(2)), pnl)` to `_add(key, pnl)`.

- [ ] **Step 4: Rule text and routing**

In `AGENTS.md` (operator decision 2026-09-14: "there is nothing such as paper mode only from now on, we will run what we run when we select mode"):

1. Replace the rule `- One open BTC paper position at a time.` with:

```markdown
- One open position per strategy at a time, per mode (operator decision
  2026-09-14). The legacy 5m loop is one slot; each hourly BTC strategy owns its
  own slot, in paper and in live. Every entry still passes RiskGate (loss halt,
  caps, slippage, kill switch).
```

2. Replace the Absolute Rules bullet that starts `- **Live trading (real capital) is not currently authorized for any market.**` (all four lines) with:

```markdown
- **Live trading (real capital) is authorized for the BTC hourly Up/Down market**
  (`polymarket_bot/hourly/`, operator decision 2026-09-14). There is no
  paper-only strategy: a strategy runs in whichever mode the operator selects at
  Start. Every other market stays research/shadow-only until this file names it.
```

3. In Scope Fence → Out of scope, replace the bullet that starts `- **Any market, on the live trading path (real capital).**` (all six lines) with:

```markdown
- **Any market other than BTC hourly Up/Down, on the live trading path (real
  capital).** BTC hourly is authorized (2026-09-14); everything else stays
  research/shadow-only until this file is explicitly updated naming it.
```

4. In Active Scope, change the sentence `it stays off unless the operator explicitly arms every gate **and** this file names an authorized live-trading market (none is currently authorized — see Absolute Rules).` to `it stays off unless the operator explicitly arms every gate **and** this file names an authorized live-trading market (BTC hourly Up/Down — see Absolute Rules).`, and add a third item to the numbered strategy list:

```markdown
3. **BTC hourly Up/Down strategies** (`polymarket_bot/hourly/engine.py`) — the
   loop runs them when the operator selects **BTC 1h** and presses ▶ Start, in
   the mode selected (paper or live). One decision per hour per strategy, one
   open position per strategy, held to resolution and settled from the Binance
   1h candle. Strategy doc: `docs/strategies/hourly-btc-strategies.md`.
```

Agents still never flip the live gate, click LIVE or Start, or place live orders.

In `docs/CODE_MAP.md`, add under `"I want to change X → edit Y"` after the "Plug in a new strategy" row:

```markdown
| Hourly BTC strategies (decision, entries, settlement, per-hour record) | `polymarket_bot/hourly/engine.py` (tick) + `mean_reversion.py` (Hourly Mean Reversion) + `market.py` (discovery/Binance) + `ledger.py` (`hourly_strategy_context`); strategy doc `docs/strategies/hourly-btc-strategies.md` |
```

- [ ] **Step 5: Regenerate docs and run every gate**

```bash
PYTHON_DOTENV_DISABLED=1 DATA_DIR="$(mktemp -d)" python3 -m pytest tests/ -q
python3 -m ruff check polymarket_exec/ polymarket_bot/ tests/ tools/
PYTHON_DOTENV_DISABLED=1 python3 tools/gen_docs.py && PYTHON_DOTENV_DISABLED=1 python3 tools/gen_docs.py --check
```

Also run the banned-strings grep from the `docs-drift` job in `.github/workflows/ci.yml` and confirm it prints nothing.
Expected: all tests pass, ruff `All checks passed!`, `--check` exits 0.

- [ ] **Step 6: Paper smoke run**

Start the app from the worktree with an isolated DB and no `.env`, using a preview config that sets `PYTHON_DOTENV_DISABLED=1 BOT_MODE=paper DATA_DIR=<scratch> DB_PATH=<scratch>/smoke.db DASHBOARD_SERVER_PORT=7872`. Then:
1. Select **BTC 1h** in the header and press **Start**.
2. Within one tick, `paper_ticks` has a row whose `window_slug` is the current `bitcoin-up-or-down-…-et` slug, with a book and a reference price.
3. `hourly_strategy_context` has a row for `hourly_mean_reversion` for the current hour once H-1 is closed.
4. Press Stop. Do **not** click LIVE: live behaviour is covered by the mocked-client tests in Tasks 5–7, and agents never arm live.
5. Stop the smoke app and remove the preview config entry.

- [ ] **Step 7: Commit**

```bash
git add polymarket_exec/ops/dashboard/panels/market_selector.py tests/unit/test_market_selector.py AGENTS.md docs/CODE_MAP.md docs/FILE_MAP.md
git commit -m "feat(hourly): selector glow for hourly rows; one position per strategy; BTC hourly live-authorized; docs"
```
