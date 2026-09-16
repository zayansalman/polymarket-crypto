
---

## Addendum (operator-approved 2026-09-15): research-driven records

Source: alphaXiv literature sweep of 21 papers (2604.24366, 2606.19517, 2608.25348, 2407.16527, 2409.12721, 2606.31675 and others). The operator approved: record the book through the entry window with a fair value, save and audit the candles each decision used, record Kronos sampling noise, and fold the order-style findings into the order-style plan. None of this changes a strategy's rule or blocks a bet: these are observations.

### Task 9: Hourly book record through the entry window, with a spot-based fair value

**Why:** only our own data can answer whether Polymarket's price at the hour open already leans against the last hour. Hourly Mean Reversion stops paying at a 57.3% hit rate once the side it buys costs about 55.6c including the fee. The record covers every hour the hourly loop runs, bet or no bet.

**Files:**
- Create: `polymarket_bot/hourly/book_record.py`
- Modify: `db.py` (SCHEMA: `hourly_book_snapshots`)
- Modify: `polymarket_bot/hourly/engine.py` (`tick` calls the recorder right after `build_snapshot`, before settlement/decisions/entries)
- Test: `tests/unit/test_hourly_book_record.py`; one test appended to `tests/unit/test_hourly_engine.py`

**Interfaces (`book_record.py`):**
- `OFFSETS_S: tuple[int, ...] = (0, 10, 30, 60, 120)`; each offset owns the window up to the next one (`0→[0,10)`, `10→[10,30)`, `30→[30,60)`, `60→[60,120)`, `120→[120,180)`).
- `offset_for(elapsed_s: int) -> int | None` — the offset whose window contains `elapsed_s`, else None.
- `@dataclass(frozen=True) BookLevels(bids: list[tuple[float, float]], asks: list[tuple[float, float]], book_ts_ms: int | None)` — top `depth` levels, **best first** (CLOB arrays list worst→best, so take the tail and reverse). `book_ts_ms` from the response's `timestamp` field when present and numeric.
- `async fetch_levels(client, token_id: str, depth: int = 3) -> BookLevels | None` — GET `{config.POLYMARKET_CLOB_API}/book?token_id=…`; None (and a `hourly_book_record.book_failed` warning) on any HTTP/parse error.
- `hour_sigma(candles: list[Candle]) -> float | None` — sample stdev (ddof=1) of `ln(close/open)` over the last 168 closed spot candles; None with fewer than 2 usable candles or zero stdev.
- `fair_up(spot: float, hour_open: float, sigma_1h: float | None, seconds_left: float) -> float | None`:
  - `tau = seconds_left / 3600`; if `tau <= 0`: `1.0 if spot >= hour_open else 0.0`;
  - None if `sigma_1h` is None/≤0 or either price ≤0;
  - else `Phi(ln(spot/hour_open)/(sigma*sqrt(tau)) - sigma*sqrt(tau)/2)` with `Phi(x) = 0.5*(1+erf(x/sqrt(2)))` (digital-option value, 2606.19517 eq. 3.3, zero rate).
- `async maybe_record(client, *, snapshot, market: HourMarket, now: int) -> bool` — writes at most one row per (hour, offset); returns True iff a row was written. Never raises: any exception is logged as `hourly_book_record.failed` and returns False.
  - elapsed = `now - market.window_start_ts`; offset = `offset_for(elapsed)`; skip if None or a row already exists (use `INSERT OR IGNORE` on the unique index, plus an in-memory `set[(start, offset)]` to avoid refetching).
  - fetch Up and Down levels (depth 3);
  - sigma and H-1 return: fetch 170 closed spot candles once per hour (cache per `window_start_ts`, via `market.fetch_closed_candles(..., market="spot", ...)`); `prev_hour_return = ln(close/open)` of the candle whose `open_time_ms == (start-3600)*1000` (None if absent); `prev_hour_vol_units = prev_hour_return / sigma`.
  - `fair_up` from `snapshot.spot_price` and `snapshot.reference_price` (None when either is 0).
  - mirror gaps (feed-consistency check, never a signal): `mirror_gap_ask = up_best_ask - (1 - down_best_bid)`, `mirror_gap_bid = up_best_bid - (1 - down_best_ask)` (None when a side is missing). Log `hourly_book_record.mirror_mismatch` at info when `abs(gap) > 0.011`.

**Table (`db.py` SCHEMA, after the hourly_strategy_context indexes):**

```sql
-- Hourly BTC book through the entry window (0/10/30/60/120 s after H:00) with a
-- spot-based fair value, every hour the hourly loop runs, bet or no bet. Answers
-- whether the opening price already leans against the previous hour.
CREATE TABLE IF NOT EXISTS hourly_book_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  window_slug TEXT NOT NULL,
  window_start_ts INTEGER NOT NULL,
  offset_s INTEGER NOT NULL,
  elapsed_s INTEGER NOT NULL,
  fetched_at_ms INTEGER NOT NULL,
  up_best_bid REAL, up_best_ask REAL, down_best_bid REAL, down_best_ask REAL,
  up_bids_json TEXT, up_asks_json TEXT, down_bids_json TEXT, down_asks_json TEXT,
  up_book_ts_ms INTEGER, down_book_ts_ms INTEGER,
  mirror_gap_ask REAL, mirror_gap_bid REAL,
  spot REAL, hour_open REAL, sigma_1h REAL, seconds_left INTEGER, fair_up REAL,
  prev_hour_return REAL, prev_hour_vol_units REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_hourly_book_window_offset
  ON hourly_book_snapshots(window_slug, offset_s);
```

**Engine wiring:** in `tick`, directly after `snapshot = await build_snapshot(client, now)`:

```python
    await book_record.maybe_record(
        client, snapshot=snapshot, market=await _market_for(client, market.hour_start(now)), now=now
    )
```

(`_market_for` is cached, so this adds no Gamma call.) Recording happens before our own entry so the snapshot never includes our order.

**Tests (write first):**
- `fair_up`: equals 0.5·(1+erf(…)) for a hand-computed case; `spot == open`, `tau = 1`, `sigma = 0.005` → just below 0.5; spot above open → > 0.5; `seconds_left = 0` → 1.0 when spot ≥ open else 0.0; sigma None → None.
- `offset_for`: 0→0, 9→0, 10→10, 29→10, 119→60, 120→120, 179→120, 180→None, -1→None.
- `fetch_levels`: CLOB-style arrays worst→best give best-first top 3 and the `timestamp`; a 500 returns None.
- `hour_sigma`: matches `statistics.stdev` of `ln(c/o)`; one candle → None.
- `maybe_record` (tmp DB + `httpx.MockTransport`): at elapsed 2 s writes offset 0 with fair value, mirror gaps and prev-hour fields; a second call at elapsed 5 s writes nothing and makes no book request; elapsed 12 s writes offset 10; elapsed 200 s writes nothing; a transport that raises returns False and leaves the table empty (no exception).
- Engine: a `tick` at `H + 2` leaves exactly one `hourly_book_snapshots` row (offset 0) for `SLUG`.

**Commit:** `feat(hourly): record the book through the entry window with a spot-based fair value`

---

### Task 10: Save the candles each decision used and audit them against Binance's archive

**Why:** Hourly Mean Reversion's cut-offs are sharp; a slightly different taker-buy volume on a just-closed candle can flip a bet. Point-in-time audits (2608.25348) found one-bar timing slips turned every skilled run into a loser. The audit is observation only.

**Files:**
- Modify: `polymarket_bot/hourly/mean_reversion.py` (`FlowPush` gains `window_mean`, `window_sd`, `window_n`; `decide` records the H-1 rows and window stats in `signal`)
- Modify: `polymarket_bot/hourly/engine.py` (`decide_hour` adds `candles_fetched_at_ms` and `decided_at_ms` to the signal; `tick` calls the audit)
- Modify: `db.py` (`hourly_strategy_context` CREATE TABLE gains `candle_audit TEXT`, `candle_audit_json TEXT`, `candle_audited_at TEXT` — the table has never shipped, so extend the CREATE statement; also add a `HOURLY_CONTEXT_COLUMN_MIGRATIONS` dict with the same three columns, applied in `init_db` like the others, so a dev DB created earlier on this branch upgrades)
- Create: `polymarket_bot/hourly/candle_audit.py`
- Test: `tests/unit/test_hourly_candle_audit.py`; extend `tests/unit/test_hourly_mean_reversion.py`

**Signal additions (`decide`):**
- `spot_h1`, `perp_h1`: dicts of the last candle used (`open_time_ms, open, high, low, close, volume, quote_volume, taker_buy_volume`).
- `spot_window`, `perp_window`: `{"n": 168, "mean": …, "sd": …}` of the imbalances (sd is the ddof=1 value already used for z; None when undefined).
- Parity: the frozen rule and its outputs are unchanged (re-run the Task 2 parity check: still 220 bets / 0.5727).

**Interfaces (`candle_audit.py`):**
- `ARCHIVE_BASE = "https://data.binance.vision/data"`; `archive_url(venue: str, day: str) -> str`:
  - spot: `{base}/spot/daily/klines/BTCUSDT/1h/BTCUSDT-1h-{day}.zip`
  - perp: `{base}/futures/um/daily/klines/BTCUSDT/1h/BTCUSDT-1h-{day}.zip`
- `parse_archive(zip_bytes: bytes) -> dict[int, Candle]` keyed by `open_time_ms`. Verified formats (2026-09-15): spot CSV has **no header** and **microsecond** timestamps (`1789257600000000`); futures CSV has a **header row** and millisecond timestamps. Skip rows whose first field is not an integer; divide open time by 1000 when it is ≥ 10**14.
- `recheck(signal: dict, spot: Candle, perp: Candle, recorded_side: str | None) -> dict` (pure):
  - for each venue, swap the recorded H-1 imbalance `x` for the archive imbalance `x'` inside the recorded window stats exactly: `S = n·mean`, `Q = (n-1)·sd² + n·mean²`; `S' = S - x + x'`, `Q' = Q - x² + x'²`; `mean' = S'/n`; `sd' = sqrt((Q' - n·mean'²)/(n-1))`; `z' = (x' - mean')/sd'`;
  - direction, CLV and `fz'` from the archive spot candle; perp `fz'` uses the archive perp candle's own direction as `flow_push` does;
  - apply the frozen rule (import the constants from `mean_reversion`) to get `side'`;
  - return `{"fields_differ": [<venue.field>…], "spot_fz": …, "perp_fz": …, "clv": …, "side": side', "recorded_side": recorded_side, "flipped": side' != recorded_side}`. Field comparison uses a relative tolerance of 1e-9.
- `AUDIT_AFTER_S = 6 * 3600` after the end of the UTC day containing H-1; `GIVE_UP_AFTER_S = 7 * 86400` after that same day end.
- `async audit_due(client, now: int) -> int` — audits at most **one UTC day** per call (≤ 2 archive requests) and retries a missing archive at most once per hour per day (module-level `dict[day, last_attempt_ts]`). Selects `hourly_mean_reversion` rows with `candle_audit IS NULL`; rows whose `signal_json` lacks `spot_h1` get `NO_CANDLES`; a 404 before give-up leaves the row NULL, after give-up sets `UNAVAILABLE`; otherwise `MATCH` (no field differs), `MISMATCH` (fields differ, same side) or `FLIPPED`. Writes `candle_audit`, `candle_audit_json` (the recheck dict) and `candle_audited_at`. `FLIPPED` also logs `hourly_candle_audit.flipped` at warning and calls `notify("hourly_candle_audit", …)`. Never raises: errors log `hourly_candle_audit.failed` and return 0. Returns rows written.

**Engine wiring:** at the end of `tick`, before `_log_tick`: `await candle_audit.audit_due(client, now)`.

**Tests (write first):**
- `parse_archive` on an in-memory spot zip (no header, microseconds) and a futures zip (header, ms) returns the same `open_time_ms` keys and taker-buy volumes.
- `recheck` window math: for a synthetic 168-imbalance window, swapping the last value reproduces `statistics.mean/stdev` of the modified window to 1e-12.
- `recheck` identical archive rows → `fields_differ == []`, `flipped is False`.
- `recheck` with the archive spot taker-buy volume lowered so spot fz drops below 1.20 → `flipped is True`, `side is None`.
- `decide` signal carries `spot_h1`/`perp_h1`/`spot_window`/`perp_window` consistent with the inputs.
- `audit_due` end-to-end (tmp DB, MockTransport serving both zips): a recorded decision two days old becomes `MATCH`; the same with a perturbed archive becomes `FLIPPED` and notifies; a 404 at 1 day old leaves NULL and a second call in the same hour makes no request; a 404 at 8 days old sets `UNAVAILABLE`; a row without `spot_h1` becomes `NO_CANDLES`; a transport that raises returns 0 without an exception.

**Commit:** `feat(hourly): save the candles each decision used and audit them against Binance's archive`

---

### Task 11: Kronos sampling noise and order-style requirements (docs)

**Files:** `docs/strategies/hourly-btc-strategies.md`; this plan's roadmap.

1. Strategy doc, "Every hour is recorded" list: add the book record (Task 9: 0/10/30/60/120 s, top 3 levels, fair value, mirror gaps) and the candle audit (Task 10: `MATCH`/`MISMATCH`/`FLIPPED`/`UNAVAILABLE`), both observation only.
2. Strategy doc, Kronos "How it decides": add a **Sampling noise** step — with N paths, `P(up)` carries a standard error of `sqrt(P(1-P)/N)`, about **7 points at N = 50**, larger than the 0.05 edge threshold, so part of what crosses the threshold is sampling noise. Each Kronos decision records N and that standard error. N becomes a dashboard setting; PR 2 measures run time for 50/100/200/500 paths on the operator's M2 and sets the default to the largest N that reliably finishes inside the entry deadline (500 paths gives about 2.2 points but is expected to take roughly 2–3 minutes). The results panel reports Kronos hit rate by edge size.
3. Strategy doc, "Order style" bullet: add the requirements the order-style PR must meet —
   - log every signal both ways: the pay-the-ask counterfactual (ask at decision, fee, outcome) and the resting attempt (limit, size queued ahead, fill second, fill price, fill type, Binance move and fair value at fill, token mid 30 s and 120 s after fill, outcome filled or not);
   - strict paper fill rule for resting orders: fill for certain only when the best ask drops below our price or a trade prints below it; fill at exactly our price only after volume traded there since posting exceeds the queue ahead plus our size; never fill on a touch; track the lowest ask and trades between polls;
   - refresh a book older than a few seconds before sending and log its age;
   - store the fee (and any maker rebate) actually charged per fill; check Polymarket's current fee/rebate rules for the hourly BTC market;
   - optional operator setting: a limit-price cap per strategy so a swept ask is never paid above its measured break-even (about 55.6c for Hourly Mean Reversion at 57.3%);
   - why: resting fills are adverse-selected (2407.16527: about a third never fill, mostly the favourable ones; 2409.12721: 66–89% of simple resting fills adverse); at 57.3% a bid 1c better only beats paying the ask if at least ~65% of signals fill even with no adverse selection.
4. This plan's roadmap: add the same requirements under PR 3 (order style), and under PR 4 (results panel): 95% ranges on hit rate, break-even lines from prices actually paid, hit rate minus average all-in price, baselines on the same hours (always Up; always the side above 50c), and a count of every filter/factor ever tried shown beside any factor split — observation only, never a gate.

**Commit:** `docs(hourly): book record, candle audit, Kronos sampling noise, order-style requirements`
