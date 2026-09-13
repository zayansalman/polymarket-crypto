# Market execution strategy — parked (backlog)

Status as of 2026-09-13: **parked, not finished.** Operator asked to backlog it.

- Engine half (SPEC §1–8) was implemented and its tests were written.
- Dashboard half (SPEC §9–14) was mid-build when parked: route, panel, JS/CSS and route
  tests exist but were not finished or verified. Docs (§14) not done.
- Nothing here has been reviewed. Re-run the full suite and ruff before picking it up.

Operator's choice: Market shows **Buy Up / Buy Down** buttons (not auto side).

---

# SPEC — "Market" execution strategy (Buy Up / Buy Down)

Worktree: /Users/zayankhan/projects/polymarket-crypto/.claude/worktrees/market-execution (branch feature/market-execution).
Edit ONLY files inside that worktree (absolute paths). Never read/print .env or secrets. Never place live orders; tests mock the executor.

Python: `cd <worktree> && PYTHON_DOTENV_DISABLED=1 /Library/Frameworks/Python.framework/Versions/3.14/bin/python3 -m pytest -q tests -p no:cacheprovider`
Baseline: 977 collected, 969 pass, 8 PRE-EXISTING failures (7 stale ems-grid/ems-content/box-shadow dashboard assertions in tests/unit/test_dashboard.py + tests/e2e/test_dashboard_flow.py, 1 date-dependent tests/unit/test_daily_market.py). Do not fix those. Ruff: `<py> -m ruff check polymarket_exec/ polymarket_bot/ tests/ tools/` has 21 pre-existing errors; add zero new ones. pytest is strict asyncio mode (`@pytest.mark.asyncio`, `@pytest_asyncio.fixture`).

Maps of the current code (read the relevant ones first):
scratchpad/map_0.md (paper.py entry/exit), map_1.md (controller/threading), map_2.md (dashboard), map_3.md (live executor + gate), map_4.md (tests/docs/CI).
Scratchpad dir: /private/tmp/claude-501/-Users-zayankhan-projects-polymarket-crypto/b87cacdf-95ed-48d6-bedb-9fe0ff10102a/scratchpad

## What the operator gets
- New execution strategy setting: **Model** (default, today's auto entries) or **Market**.
- In Market: model auto-entries are OFF. Dashboard shows **BUY UP** / **BUY DOWN** buttons. A click instantly buys that side at the current ask in the selected market through the SAME shared entry pipeline (paper fill or LiveExecutor.submit_entry — mode is only the railroad switch). Normal exits (settle / scalp TIME/TARGET/STOP) then manage it. Ticks, snapshot, exits and shadow recording keep running.
- The bot must be Started. No auto-start. No browser dialogs — the click is the intent. Outcome shown as a toast + activity feed.

## Hard rules
- One pipeline for paper and live. No manual-only fill/persist/exit code; no paper-only or live-only branches in decision logic beyond the existing `_live_executor is not None` switch.
- Nothing that opens positions runs on the dashboard (uvicorn) event loop. The runner thread consumes the click inside `paper_tick_once`.
- Model path behavior must stay identical (existing tests call `paper._maybe_open_position(snapshot)` directly — keep its signature).
- No silent failures: every refusal returns a human-readable detail to the click.

## Engine (Stage A)

### 1. `polymarket_bot/runtime_knobs.py`
Add FIRST in KNOBS: `"execution_strategy": Knob("runtime.execution.strategy", "model", "enum", "Execution strategy", choices=("model", "market"), group="Execution")`.
Readers treat any value other than "market" as "model" (enum decode doesn't re-validate).

### 2. `db.py`
Add `entry_source TEXT` to POSITION_COLUMN_MIGRATIONS (NULL = model). Values written: 'model' | 'market'.

### 3. New `polymarket_bot/manual_entry.py` (one-line docstring; imported by paper.py and controller.py)
Thread-safe single-slot handoff between the dashboard loop and the runner thread. Contents:
- `INTENT_TTL_SECONDS = 15.0`
- `@dataclass(frozen=True) class EntryOutcome: status: str  # "filled" | "placed" | "blocked" | "pending" | "error"; detail: str; mode: str | None = None; side: str | None = None; price: float | None = None; shares: float | None = None; notional_usd: float | None = None; position_id: int | None = None`
  - filled = paper fill, or live order fully matched at placement; placed = live order accepted but (partly) resting; blocked = refused by a check/gate; pending = dashboard stopped waiting but the runner already started it; error = unexpected exception.
- `@dataclass class ManualEntryIntent: side: str; window_slug: str; seen_ask: float | None; mode: str; requested_at: float (time.monotonic()); future: concurrent.futures.Future`
- module `wake = threading.Event()`, a `threading.Lock`, one `_slot`.
- `submit(intent) -> bool` (False if a slot is occupied), sets wake.
- `take() -> ManualEntryIntent | None` (clears slot and wake).
- `has_pending() -> bool`.
- `cancel_pending(reason: str) -> None` — take the slot; if its future isn't done, `set_result(EntryOutcome("blocked", reason, ...))` (guard against InvalidStateError).
- `reset_for_tests()` helper that clears slot+wake.

### 4. `polymarket_bot/paper.py`
a) Split `_maybe_open_position(snapshot)`:
   - Keep `_maybe_open_position(snapshot) -> None` as the MODEL wrapper: signal_side/notional check, adaptive auto-pause, settle one-entry-per-window check, then `await _execute_entry(snapshot, snapshot.signal_side, snapshot.notional_usd, entry_source="model", entry_reason=snapshot.reason, confidence=snapshot.confidence, edge=snapshot.edge, reference_price=None)`; ignore the return.
   - New `async def _execute_entry(snapshot, side, notional, *, entry_source, entry_reason, confidence, edge, reference_price) -> EntryOutcome`: everything shared — open-row COUNT (max 1), side ask present, venue-min bump, top-of-book cap, LIVE branch (resync_flat → provisional row → submit_entry(token for `side`, side_price=reference_price if not None else ask, notional, window_slug) → delete row on failure / update terms on success), PAPER branch (gate.block_reason with side_price=reference_price if not None else entry ask, best_ask=entry ask → journal BLOCKED mode='paper' on block → insert → record_buy_notional), notify + log. Use `side` param everywhere (token id, row, BLOCKED journal, notify) instead of snapshot.signal_side. Return an EntryOutcome at every exit point (blocked with a clear detail, e.g. "A position is already open (max 1) — wait for it to exit", "No ask on the Up book", "Blocked: <gate reason>"). For the model path (reference_price=None) the values passed to gate/executor must be exactly what they are today.
   - For entry_source="market" add a "[market] " prefix to the notify message (keep event types paper_entry/live_entry).
   - Live outcome: ok → "filled" if the order fully matched at placement (inspect LiveOrderResult / executor state, e.g. `_entry_order_id is None`), else "placed" with detail saying the remainder rests; not ok → "blocked" with result error/status text.
b) `_insert_position_row(...)`: add keyword-only overrides `side`, `confidence`, `edge`, `entry_reason`, `entry_source` (default: fall back to snapshot fields / 'model') so existing callers/tests are unaffected. Market rows store confidence=NULL, edge=NULL, entry_reason=f"market: operator Buy {side} @ {ask:.3f}", entry_source='market'. strategy_style stays = exit_style knob; quote_source unchanged.
c) Manual consumer `async def _consume_manual_intent(intent, snapshot) -> None`:
   - `if not intent.future.set_running_or_notify_cancel(): return` (dashboard gave up).
   - try: refuse (resolve "blocked") when: age > INTENT_TTL_SECONDS ("Click expired before the bot could act — click again"); runner mode ("live" if _live_executor else "paper") != intent.mode ("Mode changed — click again"); execution strategy != market ("Execution strategy is Model — switch to Market"); snapshot.window_slug != intent.window_slug ("Window rolled to a new market — click again"); remaining_seconds <= 0 ("Window already ended"); side ask missing/<=0 ("No ask on the {side} book"); book crossed (bid present and bid > ask).
   - Size: shares = `_risk_gate.runtime_trade_shares` if gate and it's set (>0) else `DEFAULT_MIN_ORDER_SIZE`; notional = shares × side ask.
   - `outcome = await _execute_entry(snapshot, side, notional, entry_source="market", entry_reason=..., confidence=None, edge=None, reference_price=intent.seen_ask)`; set_result(outcome).
   - Bypassed for Market (model-quality filters, not safety): edge min/max, min confidence, entry price band, entry_min_remaining_seconds, feed-degraded skip, adaptive auto-pause, settle one-entry-per-window.
   - Kept (all inside _execute_entry/gate/executor): kill switch, loss halt, max-1 (ledger + gate/executor), per-trade cap, bankroll cap, slippage guard, venue min bump/ceiling, top-of-book cap, resync_flat, row-before-order.
   - except Exception: structlog error + set_result(EntryOutcome("error", f"Market order failed: {exc}")). The future must ALWAYS resolve.
d) `paper_tick_once`: in the entry slot:
   ```
   intent = manual_entry.take()
   if not kill_active:
       if intent is not None: await _consume_manual_intent(intent, snapshot)
       elif _execution_strategy() == "model": await _maybe_open_position(snapshot)
   elif intent is not None: resolve blocked "Kill switch is armed — entries are off"  (respect set_running_or_notify_cancel)
   ```
   `_execution_strategy()` reads `_knobs.cached("execution_strategy")`.
e) `_sleep_interruptible`: also return early when `manual_entry.wake.is_set()`.
f) `run_paper_loop` finally (every exit branch, incl. superseded-generation): `manual_entry.cancel_pending("Bot stopped — order not placed")`.
g) `_exit_reason`: skip BAND_REENTRY (model-edge exit) for rows with entry_source == 'market'. TIME/TARGET/STOP/settle unchanged. Make sure the position rows passed in carry entry_source (check the SELECT used by _close_due_positions).
h) Market mode detail/tick reason: where `_set_detail`/`_detail_from_snapshot` describe the model signal, append/prefix a short note in Market mode ("Market mode — model auto-entries off") so the status line doesn't imply the model will trade. Keep this minimal.

### 5. `polymarket_bot/adaptive.py`
`rolling_performance` SQL: add `AND COALESCE(entry_source, 'model') = 'model'` so Market trades can't auto-pause the model.

### 6. `polymarket_bot/controller.py`
`async def request_manual_entry(side: str, *, window_slug: str, seen_ask: float | None, timeout: float = 20.0) -> EntryOutcome`:
- side must be "Up"/"Down" else blocked.
- Refuse (blocked "Bot is stopped — press ▶ Start first") unless `_is_runner_alive()` and `_stop_event` exists and not set and `_desired_running`.
- mode = `_mode_cache`; build Future + intent; `manual_entry.submit` False → blocked "A Market order is already in progress".
- Wait without blocking the dashboard loop: `await asyncio.to_thread(fut.result, timeout)` catching `concurrent.futures.TimeoutError`: then if `fut.cancel()` succeeds → (take the slot if still ours) blocked "The bot didn't pick up the order in time — not placed"; else if done → its result; else → EntryOutcome("pending", "Order is still being processed — watch the activity feed").
- `request_stop` (after join) and `request_start` (before spawning): `manual_entry.cancel_pending("Bot stopped — order not placed")`.

### 7. `polymarket_exec/execution/gate.py`
Slippage block message: neutral wording that fits both model and manual (e.g. "entry slippage: best ask 0.550 is 0.030 above the expected price 0.520 (cap 0.020)"). Update any test that pins the old text.

### 8. Engine tests — new `tests/unit/test_market_execution.py`
Copy `bot_db`, `_snapshot`, `_mock_executor` patterns from tests/unit/test_live_wiring.py. Reset `manual_entry` slot per test. Cover:
- paper Market Buy Up and Buy Down: one row, entry_price = side ask, shares ≥ 5, mode='paper', entry_source='market', edge/confidence NULL, executor untouched; outcome filled with price/shares.
- live (mock executor): submit_entry called with that side's token id and side_price = seen_ask; blocked result → no row + outcome blocked.
- second click while a position is open → blocked (max 1); missing ask → blocked; window slug mismatch → blocked; expired intent → blocked; mode mismatch → blocked.
- paper gate still applies: real RiskGate with breached halt or kill switch → blocked + BLOCKED journal row mode='paper'.
- paper slippage: seen_ask 0.50 vs snapshot ask 0.55 (cap 0.02) → blocked.
- adaptive auto-pause does NOT block Market; settle one-entry-per-window does NOT block Market (but max-1 does).
- strategy=market: paper_tick_once with a model signal snapshot does NOT auto-enter; strategy=model unchanged.
- scalp BAND_REENTRY skipped for a market row but still fires for a model row; TIME exit still applies to market row.
- adaptive rolling_performance excludes market rows.
- controller.request_manual_entry: stopped runner → blocked; a fake runner (stub thread alive + a consumer thread calling take()/set_result) → returns the outcome; timeout path → blocked and slot cleared; cancel_pending resolves waiting futures.
- knob: execution_strategy invalid value rejected.

## Dashboard (Stage B)

### 9. Route `POST /api/market-order` in `polymarket_exec/ops/dashboard/app.py` (next to /api/start)
Body `{side, window_slug, ask, asset, timeframe}`. Always HTTP 200 JSON `{status, detail, mode, side, price, shares, notional_usd}`.
Order: require `_has_dashboard_token(request)` in BOTH modes (else status error "Missing dashboard token — reload the page") → `_BTC_BOT_AVAILABLE` → side ∈ {Up, Down} → `await _knobs.get("execution_strategy") == "market"` else error "Switch execution strategy to Market first" → `sel = await market_selection.get_selection()`; body asset/timeframe mismatch → error "Market changed — refresh"; `not sel.loop_supported` → error "Market orders only run on BTC 5m for now — the loop isn't wired for {ASSET} {tf}" → window_slug required → `await controller.request_manual_entry(side, window_slug=..., seen_ask=float(ask) or None)` → return its fields. Exceptions → status error. structlog the request + outcome (no secrets). Never touch paper._live_executor/_risk_gate here.

### 10. New pure panel `polymarket_exec/ops/dashboard/panels/execution.py`
`render(*, strategy, running, mode, loop_supported, selection_label, tick, trade_shares, open_position_count, kill_armed) -> str` returning `<section class="card wide" id="exec-card" data-window="..." data-asset="..." data-timeframe="..." data-mode="...">`.
- Header "EXECUTION". A one-click segmented toggle MODEL | MARKET reusing `.mode-toggle/.mode-opt` look, `onclick="setExecutionStrategy('market')"` etc., active state from `strategy`.
- Model: one line "Model — the strategy enters automatically when its filters pass."
- Market: two big buttons in a 2-col grid: `<button class="mo-btn up" data-side="Up" onclick="buyMarket('Up')">BUY UP</button>` showing the side ask (tick up/down_best_ask), "≈ $X · N sh" (N = trade_shares or 5), and a mode tag (PAPER / LIVE — real order). Below: one status line = the disabled reason or "Click to buy at the current ask. Exits follow your exit style."
- `disabled_reason(...) -> str | None` (pure, tested), in order: not running → "Press ▶ Start first"; not loop_supported → "Loop isn't wired for {selection} yet"; no tick / no window → "Waiting for market data"; tick.remaining_seconds <= 0 → "Window ended — waiting for the next one"; open_position_count > 0 → "Position open — wait for it to exit (max 1)"; kill_armed → "Kill switch armed". Per-side: ask missing → that button disabled. Do NOT include model filters or auto-pause. Server/runner remain the authority.
- Escape all interpolated text (html.escape).

### 11. `execution_view.py`
Insert the exec panel before the controls card (keep grid pairing). Gather: strategy via `_knobs.get("execution_strategy")`; running from the controller's thread liveness (not the stale DB state row) — check what's available; mode = requested mode; selection via market_selection.get_selection(); tick (already loaded); trade_shares (already computed); open_position_count via a new unfiltered `_data.open_position_count()` (`state='open'`, any style/mode); kill_armed (reuse ribbon's logic — extract to a tiny shared helper if needed).
Pass `execution_strategy` into `decision_engine.render` (new kwarg, default "model"): in Market mode the banner reads "MARKET — auto entries off · model signal shown for reference" instead of ENTER UP/DOWN.
Blotter: if rows include entry_source == 'market', show a small "MKT" chip (keep minimal; ensure the loader selects the column).

### 12. `static/dashboard.js` + `static/style.css`
- Refactor `setKnob` core into `postKnob(name, value, label)`; add `setExecutionStrategy(value)` using it (toast "Execution strategy → Market", then refreshAll).
- `buyMarket(side)`: read data-* from #exec-card and the clicked button's ask (data-ask); set `window.pendingMarketOrder={side}`; add `.pending` and disable both `.mo-btn`; `fetch('/api/market-order', {method:'POST', headers: dashboardHeaders(), body: JSON.stringify({side, window_slug, ask, asset, timeframe})})`; toast by status: filled/placed → success (e.g. "Paper BUY Up 5 sh @ 0.531"), pending → info, blocked/error → error (6000ms); `.catch` → error toast; `.finally` → clear pending, `setTimeout(refreshAll, 300)`.
- After the SSE swap in updateDashboard, re-apply pending/disabled state if `window.pendingMarketOrder` is set.
- Never use confirm/alert/prompt (tests regex `\b(confirm|alert|prompt)\(` over the JS, even in comments).
- CSS: `.mo-grid` 2 columns; `.mo-btn.up/.down` reuse --pos/--neg tints like `.book-side`; `.mo-btn[disabled]` muted; `.mo-btn.pending` reuses existing pulse keyframes; square corners, mono, no shadows. Style LIVE tag in the red/live accent.

### 13. Dashboard tests — new `tests/unit/test_market_order_route.py`
Isolated TestClient pattern from tests/unit/test_mode_switch.py (DB_PATH monkeypatch before importing app, `_token(client)`, stub `_ensure_runner_started`/`_ensure_watchdog_started`, reset controller globals). Cover: no token → error; bad side → error; strategy model → error; unwired selection (eth/15m) → error; stopped bot → blocked detail "Start"; happy path with `controller.request_manual_entry` monkeypatched → returns its fields; knob toggle via /api/runtime-config key execution_strategy persists; panel render: model shows no buy buttons, market shows both, disabled reasons (stopped, open position, unwired, missing ask); decision banner in market mode; JS contains buyMarket/setExecutionStrategy, uses dashboardHeaders, passes the no-dialog regex.

### 14. Docs
- docs/CODE_MAP.md routing table rows (hand-maintained part only): "Market (manual Buy Up/Down) execution → polymarket_bot/manual_entry.py handoff + polymarket_bot/paper.py:_execute_entry; dashboard panels/execution.py + /api/market-order".
- docs/OPERATIONS_RUNBOOK.md "## Paper Trading": short bullets on the Execution strategy (Model default / Market buttons; bot must be Started; exits follow exit style; LIVE still needs the LIVE click + armed gates + Start; the click places a real order in LIVE).
- Then from the WORKTREE run `PYTHON_DOTENV_DISABLED=1 <py> tools/gen_docs.py` (non-fast) to regenerate FILE_MAP/AGENTS/CODE_MAP generated blocks. Do not hand-edit generated blocks.
