# Tsinghua-Kronos BTC 24h on the Daily BTC Market — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The trading loop runs "Tsinghua-Kronos BTC 24h" on Polymarket's daily BTC Up/Down market, in whichever mode the operator selects. It has these parts:
- once per noon-ET window, the Kronos team's own Kronos-mini 24-hour forecast;
- a bet when that forecast beats the market price;
- one open position of its own;
- settlement from the two Binance 1-minute closes the market resolves on, with ties paying 50-50;
- a decision record for every window.

**Architecture:**
- **Model worker.** Kronos-mini runs in an isolated worker process:
  - `polymarket_bot/kronos_forecast/worker.py`, started as `python -I` with a minimal environment;
  - it uses the MIT-licensed official Kronos code, vendored under `third_party/kronos_67b630e/`.
- **New package `polymarket_bot/daily_btc/`:**
  - `market.py`: windows, discovery and Binance reads;
  - `tsinghua_kronos_btc_24h.py`: the pure rule;
  - `ledger.py`: the decision table;
  - `engine.py`: one tick.
- **Shared entry code.** The hourly engine's entry state machine (one order attempt per window, paper and live) moves to `polymarket_bot/strategy_slot_entry.py`, so both engines share one implementation.
- **Loop.** `paper.py` routes a tick to the daily engine when the loop was started on BTC 1d. Every run also settles due rows of the other timeframes.

**Tech Stack:**
- The app: Python 3.11+, asyncio, httpx, aiosqlite, pytest with pytest-asyncio (strict mode).
- The worker only: torch, einops, pandas, safetensors, huggingface_hub, tqdm. These are an optional `kronos` extra and are never imported by the app process.

**Spec:** `docs/strategies/tsinghua-kronos-btc-24h.md`. Evidence: branch `research/kronos-mini-official-btcusdt-24h-demo-recreation`, folder `research/kronos_mini_official_btcusdt_demo/`.

## Global Constraints

- **Base.** This branch (`feature/tsinghua-kronos-btc-24h-daily-btc-market`, worktree `/Users/zayankhan/projects/polymarket-crypto/.claude/worktrees/tsinghua-kronos-btc-24h`) merges the hourly session's finished `feature/hourly-btc-strategies` and `feature/btc-daily-fade-3d` (Task 0). It never commits to branches other sessions own.
- **Names (Zayan, 2026-09-16).**
  - Display name `Tsinghua-Kronos BTC 24h`; strategy id `tsinghua_kronos_btc_24h`; timeframe value `1d`.
  - No vague or catchy names anywhere, in code, knobs, logs, notify text, docs or tests.
- **Sources.** Every rule, threshold and default carries a source line: a paper or repo id; "Zayan (operator), <date>" for an operator idea; "Claude, <date>" for an assistant choice.
- **Mode.** Strategies never read mode. The entry and settlement steps are the only places that branch on it. Never add a paper-only path or a live refusal.
- **RiskGate.** Never edit `polymarket_exec/execution/gate.py` or its tests.
- **Market rules.** Verified from Gamma on 2026-09-16.
  - Window = 12:00 America/New_York on day D to 12:00 on D+1. Slug `bitcoin-up-or-down-on-<month>-<day>-<year>`, named by the **end** day.
  - Gamma `eventStartTime` = window start; `endDate` = window end.
  - Resolution: Up iff the close of the Binance BTCUSDT 1-minute candle opening at the end is **higher** than the close of the one opening at the start; Down if lower; a tie pays 0.50 per share on both sides.
  - Taker fee `0.07·p·(1−p)` (`polymarket_bot/shadow/fees.py:taker_fee_per_share`).
- **Forecast recipe.** Fixed, to match the scored record.
  - Kronos-mini `NeoQuasar/Kronos-mini@f4e68697d9d5aed55cef5c96aabc3376bcad9f81` with `NeoQuasar/Kronos-Tokenizer-2k@26966d0035065a0cae0ebad7af8ece35bc1fb51c`, max context 512, CPU, eval mode.
  - Input: 383 closed Binance spot BTCUSDT 1h candles before the window start. Columns open, high, low, close, volume, amount (= quote volume); naive UTC open times.
  - Sampling: 24 steps, 30 paths, T=1.0, top_p=0.95, top_k=0, seed = window_start_ts // 3600.
  - `P(up)` = share of paths whose step-24 close is strictly above the last input close.
- **Worker safety.**
  - Started as `python -I -B <abs path>/worker.py` in its own process group (one worker at a time; the timeout includes waiting for that lock) with an explicit environment dict: `PATH, HOME, HF_HUB_OFFLINE, HF_HUB_DISABLE_TELEMETRY, TRANSFORMERS_OFFLINE, OMP_NUM_THREADS, PYTHON_DOTENV_DISABLED, PYTHONDONTWRITEBYTECODE`.
  - It never imports `config`, `db`, `polymarket_bot`, `polymarket_exec` or `dotenv`, and never downloads.
  - Timeout 90 s, under the loop watchdog's 180 s stall threshold (`controller.WATCHDOG_STALL_SECONDS`).
- **Tests.**
  - Run with `PYTHON_DOTENV_DISABLED=1`.
  - Isolate the DB: `monkeypatch.setattr(_db, "DB_PATH", tmp_path / "t.db")` then `await _db.init_db()`.
  - No network: use `httpx.MockTransport` and fake worker scripts.
  - torch is never required. Tests needing pandas use `pytest.importorskip("pandas")`.
- **Machine.** 8 GB RAM: run one test process at a time.
- **Lint.** `ruff==0.15.12` on `polymarket_exec/ polymarket_bot/ tests/ tools/`. Every new `.py` file starts with a one-line module docstring.
- **Commits** end with a blank line then `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.
- **Generated docs.** `AGENTS.md`, `docs/CODE_MAP.md` and `docs/FILE_MAP.md` generated blocks are regenerated only in Task 8 (`PYTHON_DOTENV_DISABLED=1 python3 tools/gen_docs.py`, not `--fast`).
- **Gates before the PR:**
  1. `PYTHON_DOTENV_DISABLED=1 DATA_DIR="$(mktemp -d)" python3 -m pytest tests/ -q -p no:cacheprovider`
  2. The ruff command above
  3. `gen_docs.py`, then `gen_docs.py --check`
  4. The banned-strings grep from the `docs-drift` job in `.github/workflows/ci.yml`

## File Structure

| File | Responsibility |
|---|---|
| Create `third_party/kronos_67b630e/model/{__init__,kronos,module}.py`, `LICENSE`, `SOURCE.md` | Official Kronos model code (MIT) at commit 67b630e, unchanged |
| Create `polymarket_bot/kronos_forecast/__init__.py` | Package marker |
| Create `polymarket_bot/kronos_forecast/client.py` | `ModelSpec`, pinned revisions, weights check, minimal worker env, `run_forecast` |
| Create `polymarket_bot/kronos_forecast/worker.py` | One forecast per process; `validate`, `build_frames`, `upside_probability`, `run`, `main` |
| Create `tools/fetch_kronos_mini_weights.py` | Download the pinned weights |
| Create `tools/check_tsinghua_kronos_btc_24h_against_published.py` | Rerun the worker on published hours; compare with the Kronos team's published numbers |
| Modify `config.py` | `KRONOS_PYTHON` |
| Modify `pyproject.toml` | Optional extra `kronos` |
| Modify `polymarket_bot/daily_btc/market.py` (from `feature/btc-daily-fade-3d`) | Add `current_window` and `payout`; everything else reused as is |
| Create `polymarket_bot/daily_btc/forecast_input.py` | The 383 closed 1h candles before a window's reference noon |
| Create `polymarket_bot/daily_btc/tsinghua_kronos_btc_24h.py` | Recipe constants, `request_for`, `decide` |
| Modify `db.py` | Table `btc_daily_market_decisions` |
| Create `polymarket_bot/daily_btc/ledger.py` | Decision-record operations |
| Create `polymarket_bot/strategy_slot_entry.py` | Shared entry state machine (moved out of the hourly engine) |
| Modify `polymarket_bot/hourly/engine.py` | Use the shared module through a decision adapter |
| Modify `polymarket_exec/execution/live.py` | 1d rows resolve after at most 25 h; `record_settlement(..., payout=)` for 50-50 |
| Create `polymarket_bot/daily_btc/engine.py` | `build_snapshot`, `decide_window`, `open_entries`, `settle_due`, `tick` |
| Modify `polymarket_bot/runtime_knobs.py` | Three "Daily BTC" knobs |
| Modify `polymarket_bot/paper.py` | Engine registry; 1d routing; cross-timeframe settlement; legacy SQL excludes 1d; Stop handles 1d rows |
| Modify `polymarket_bot/controller.py`, `polymarket_bot/market_selection.py` | BTC 1d is loop-supported |
| Modify `polymarket_exec/ops/dashboard/panels/market_selector.py` | Daily BTC slug glows the BTC 1d button |
| Modify `AGENTS.md`, `docs/CODE_MAP.md` | Active scope, live authorization for BTC daily, routing row |
| Tests | `tests/unit/test_kronos_forecast_client.py`, `test_kronos_forecast_worker.py`, `test_daily_btc_forecast_input.py`, additions to `test_daily_btc.py`, `test_tsinghua_kronos_btc_24h.py`, `test_daily_btc_ledger.py`, `test_strategy_slot_entry.py`, `test_daily_btc_engine.py`, `test_daily_btc_loop.py`; additions to `test_live_executor.py` |

---

### Task 0: Base branch (coordinated with the other sessions)

**Why:** the daily engine reuses the hourly work's strategy slots, per-strategy position columns and timeframe routing. It also reuses the daily BTC market helpers already written on `feature/btc-daily-fade-3d`. Other sessions own those branches (confirmed 2026-09-16):
- The hourly session (`polymarket-crypto-56`) owns `feature/hourly-btc-strategies` and every `fix/hourly-*` branch. It is merging its review fixes, renaming the hourly strategies and adding the book-record and candle-audit records.
- The macro-feeds session (`polymarket-crypto-c5`) owns `feature/macro-feeds` and the feeds and market-data files.
- **This plan never commits to those branches.**

- [ ] **Step 1: Wait for the hourly session's message** that `feature/hourly-btc-strategies` holds its merged fixes and renames. Do not start Task 5 or later before it arrives. Tasks 1–4 touch only new files plus `config.py` and `pyproject.toml`, so they may start on the current base.

- [ ] **Step 2: Merge the finished hourly branch and the daily market helpers into this branch**

```bash
cd /Users/zayankhan/projects/polymarket-crypto/.claude/worktrees/tsinghua-kronos-btc-24h
git merge --no-ff --no-edit feature/hourly-btc-strategies
git merge --no-ff --no-edit feature/btc-daily-fade-3d
PYTHON_DOTENV_DISABLED=1 python3 -m pytest tests/unit -q -p no:cacheprovider 2>&1 | tail -3
python3 -m ruff check polymarket_exec/ polymarket_bot/ tests/ tools/
```

Expected: tests pass and ruff is clean.
- **Conflicts in generated doc blocks** (`AGENTS.md`, `docs/CODE_MAP.md`, `docs/FILE_MAP.md`): take either side; they are regenerated in Task 8.
- **Any other conflict:** stop and ask the owning session.

- [ ] **Step 3: Record the reused API** (read, don't change) from `polymarket_bot/daily_btc/market.py` on `feature/btc-daily-fade-3d`:
  - `ET`
  - `DayWindow(market_date, reference_ts, settle_ts)`
  - `DayMarket(slug, question, window, up_token_id, down_token_id, fee_rate, fee_exponent)`
  - `noon_et(day)`, `window_for(market_date)`, `next_window(now_ts)`
  - `outcome(reference_close, settle_close) -> "Up" | "Down" | "tie"`
  - `parse_market(row, window)`, `async discover(client, window)`
  - `async fetch_minute_close(client, minute_ts, now_ts)`, `async fetch_price_at(client, ts, now_ts)`

  `next_window` returns the window whose reference noon is at or after `now`; that suits deciding *before* noon. This strategy decides just *after* noon, so Task 2 adds `current_window(now_ts)` next to it.

---

### Task 1: Kronos forecast worker, client, vendored model code, weights tool

**Files:**
- Create: `third_party/kronos_67b630e/model/__init__.py`, `model/kronos.py`, `model/module.py`, `LICENSE`, `SOURCE.md`
- Create: `polymarket_bot/kronos_forecast/__init__.py`, `client.py`, `worker.py`
- Create: `tools/fetch_kronos_mini_weights.py`
- Modify: `config.py` (add `KRONOS_PYTHON`), `pyproject.toml` (optional extra `kronos`)
- Test: `tests/unit/test_kronos_forecast_client.py`, `tests/unit/test_kronos_forecast_worker.py`

**Interfaces:**
- Produces (`polymarket_bot/kronos_forecast/client.py`):
  - `@dataclass(frozen=True) ModelSpec(model_repo: str, model_revision: str, tokenizer_repo: str, tokenizer_revision: str)`
  - `KRONOS_MINI_WITH_TOKENIZER_2K: ModelSpec`; `KRONOS_CODE_COMMIT: str`; `CODE_DIR: Path`; `WORKER_SCRIPT: Path`; `DEFAULT_TIMEOUT_S = 90.0`
  - `models_dir() -> Path`; `snapshot_dir(repo: str, revision: str) -> Path`; `missing_weights(spec: ModelSpec) -> str | None`
  - `@dataclass(frozen=True) ForecastRequest(candles: list[list[float]], horizon: int, paths: int, temperature: float, top_p: float, top_k: int, seed: int, max_context: int = 512, threads: int = 4)` (candle row = `[open_time_ms, open, high, low, close, volume, amount]`, oldest first)
  - `@dataclass(frozen=True) ForecastResult(ok: bool, upside_prob: float | None = None, last_close: float | None = None, final_closes: tuple[float, ...] = (), seconds: float | None = None, error: str | None = None)`
  - `worker_env() -> dict[str, str]`; `worker_python() -> str`
  - `async run_forecast(request: ForecastRequest, *, spec: ModelSpec = KRONOS_MINI_WITH_TOKENIZER_2K, timeout_s: float = DEFAULT_TIMEOUT_S) -> ForecastResult`
- Produces (`worker.py`): `REQUIRED_KEYS`, `validate(request) -> str | None`, `build_frames(candles, horizon)`, `upside_probability(final_closes, last_close) -> float`, `run(request) -> dict`, `main() -> int`
- Produces (`config.py`): `KRONOS_PYTHON: str` (env `KRONOS_PYTHON`, default `""` = use the app's interpreter)

- [ ] **Step 1: Vendor the official Kronos model code (MIT) unchanged**

```bash
cd /Users/zayankhan/projects/polymarket-crypto/.claude/worktrees/tsinghua-kronos-btc-24h
REF=67b630e67f6a18c9e9be918d9b4337c960db1e9a
gh api "repos/shiyu-coder/Kronos/contents/model?ref=$REF" --jq '.[].name'
mkdir -p third_party/kronos_67b630e/model
for f in __init__.py kronos.py module.py; do
  gh api -H "Accept: application/vnd.github.raw" "repos/shiyu-coder/Kronos/contents/model/$f?ref=$REF" > "third_party/kronos_67b630e/model/$f"
done
gh api -H "Accept: application/vnd.github.raw" "repos/shiyu-coder/Kronos/contents/LICENSE?ref=$REF" > third_party/kronos_67b630e/LICENSE
head -3 third_party/kronos_67b630e/LICENSE
```

Expected: the `model` folder lists exactly `__init__.py`, `kronos.py` and `module.py`. If it lists more `.py` files, fetch them too. LICENSE starts with "MIT License".

Write `third_party/kronos_67b630e/SOURCE.md`:

```markdown
# Kronos model code (vendored, unchanged)

- Source: https://github.com/shiyu-coder/Kronos, folder `model/`, commit 67b630e67f6a18c9e9be918d9b4337c960db1e9a (2026-04-13), MIT licence (LICENSE in this folder).
- Paper: Shi et al., "Kronos: A Foundation Model for the Language of Financial Markets", arXiv 2508.02739 (Tsinghua University).
- Why this and not github.com/shiyu-coder/Kronos-demo: the demo repo (whose forecast recipe Tsinghua-Kronos BTC 24h follows) has no licence file, so its code is not vendored. The recipe is reproduced with this MIT code: `KronosPredictor.predict_batch` over N identical copies with `sample_count=1` returns N independent sampled paths, the same as the demo's per-path `predict(sample_count=N)`. Checked against the Kronos team's published forecasts with `tools/check_tsinghua_kronos_btc_24h_against_published.py` (Claude, 2026-09-16).
- Used only by `polymarket_bot/kronos_forecast/worker.py`, which runs in its own process. The app process never imports it.
- Do not edit these files. To update, re-vendor a new commit into a new folder and update `KRONOS_CODE_COMMIT` / `CODE_DIR` in `polymarket_bot/kronos_forecast/client.py`.
```

- [ ] **Step 2: Write the failing worker tests** — `tests/unit/test_kronos_forecast_worker.py`

```python
"""Kronos forecast worker: request checks, input frames, upside share, isolation, subprocess run."""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

from polymarket_bot.kronos_forecast import worker

HOUR_MS = 3_600_000
WORKER = Path(worker.__file__).resolve()


def _candles(n: int, start_ms: int = 1_789_000_000_000 - 1_789_000_000_000 % HOUR_MS) -> list[list[float]]:
    return [[start_ms + i * HOUR_MS, 100.0, 101.0, 99.0, 100.0 + i, 10.0, 1000.0] for i in range(n)]


def _request(**overrides) -> dict:
    req = {"candles": _candles(5), "horizon": 3, "paths": 4, "temperature": 1.0, "top_p": 0.95,
           "top_k": 0, "seed": 42, "max_context": 512, "threads": 1,
           "code_dir": "/nonexistent", "model_dir": "/nonexistent", "tokenizer_dir": "/nonexistent"}
    req.update(overrides)
    return req


def test_validate_reports_missing_keys_bad_rows_and_gaps() -> None:
    assert worker.validate(_request()) is None
    bad = _request()
    del bad["seed"]
    assert "seed" in worker.validate(bad)
    assert "candles" in worker.validate(_request(candles=[[1, 2, 3]]))
    gap = _candles(3)
    gap[2][0] += HOUR_MS
    assert "consecutive" in worker.validate(_request(candles=gap))
    assert "positive" in worker.validate(_request(paths=0))


def test_upside_probability_counts_strictly_higher_closes() -> None:
    assert worker.upside_probability([101.0, 100.0, 99.0], 100.0) == pytest.approx(1 / 3)


def test_build_frames_uses_naive_utc_open_times_and_next_hours() -> None:
    pd = pytest.importorskip("pandas")
    candles = _candles(4)
    df, x_ts, y_ts = worker.build_frames(candles, 3)
    assert list(df.columns) == ["open", "high", "low", "close", "volume", "amount"]
    assert x_ts.iloc[-1] == pd.Timestamp(candles[-1][0], unit="ms")
    assert x_ts.dt.tz is None
    assert list(y_ts) == [pd.Timestamp(candles[-1][0] + k * HOUR_MS, unit="ms") for k in (1, 2, 3)]


def test_worker_imports_nothing_from_the_app() -> None:
    tree = ast.parse(WORKER.read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    assert not names & {"config", "db", "polymarket_bot", "polymarket_exec", "dotenv", "logging_setup"}


FAKE_TORCH = '''
import contextlib
from pathlib import Path
__version__ = "fake"
def set_num_threads(n): Path(__file__).with_name("threads.txt").write_text(str(n))
def manual_seed(s): Path(__file__).with_name("seed.txt").write_text(str(s))
def no_grad(): return contextlib.nullcontext()
'''

FAKE_MODEL = '''
import pandas as pd
class _Loaded:
    @classmethod
    def from_pretrained(cls, path): return cls()
    def eval(self): return self
class Kronos(_Loaded): pass
class KronosTokenizer(_Loaded): pass
class KronosPredictor:
    def __init__(self, model, tokenizer, device=None, max_context=512, clip=5):
        assert device == "cpu" and max_context == 512
    def predict_batch(self, df_list, x_timestamp_list, y_timestamp_list, pred_len, T, top_k, top_p,
                      sample_count, verbose):
        assert sample_count == 1 and (T, top_k, top_p) == (1.0, 0, 0.95)
        last = float(df_list[0]["close"].iloc[-1])
        return [pd.DataFrame({"close": [last * (0.99 if i % 4 == 0 else 1.01)] * pred_len},
                             index=y_timestamp_list[i]) for i in range(len(df_list))]
'''


def test_worker_process_runs_the_recipe_with_fake_torch_and_model(tmp_path: Path) -> None:
    pytest.importorskip("pandas")
    code = tmp_path / "code"
    (code / "model").mkdir(parents=True)
    (code / "torch.py").write_text(FAKE_TORCH)
    (code / "model" / "__init__.py").write_text(FAKE_MODEL)
    req = _request(code_dir=str(code), threads=3, seed=497_000)
    proc = subprocess.run([sys.executable, "-I", str(WORKER)], input=json.dumps(req),
                          capture_output=True, text=True, timeout=60,
                          env={"PATH": "/usr/bin:/bin", "PYTHON_DOTENV_DISABLED": "1"})
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert proc.returncode == 0 and out["ok"] is True, proc.stderr
    assert out["upside_prob"] == pytest.approx(0.75)  # 3 of 4 paths close above the last close
    assert out["last_close"] == req["candles"][-1][4]
    assert len(out["final_closes"]) == 4
    assert (code / "seed.txt").read_text() == "497000"
    assert (code / "threads.txt").read_text() == "3"


def test_worker_reports_errors_as_json(tmp_path: Path) -> None:
    proc = subprocess.run([sys.executable, "-I", str(WORKER)], input="not json",
                          capture_output=True, text=True, timeout=30,
                          env={"PATH": "/usr/bin:/bin"})
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert proc.returncode == 0 and out["ok"] is False and "JSONDecodeError" in out["error"]
```

- [ ] **Step 3: Run to verify they fail**

Run: `PYTHON_DOTENV_DISABLED=1 python3 -m pytest tests/unit/test_kronos_forecast_worker.py -q -p no:cacheprovider`
Expected: FAIL with `ModuleNotFoundError: No module named 'polymarket_bot.kronos_forecast'`

- [ ] **Step 4: Write the worker**

`polymarket_bot/kronos_forecast/__init__.py`:

```python
"""Kronos forecasts run in an isolated worker process (Tsinghua-Kronos BTC 24h)."""
```

`polymarket_bot/kronos_forecast/worker.py`:

```python
"""Kronos forecast worker: one Kronos forecast per process, JSON in on stdin, JSON out on stdout.

Started by client.py as ``python -I worker.py`` with a minimal environment. It imports
nothing from the app. The recipe follows github.com/shiyu-coder/Kronos-demo
update_predictions.py (commit eba16695), run with the MIT Kronos code vendored in
third_party/kronos_67b630e: ``predict_batch`` over ``paths`` identical copies of the input
with ``sample_count=1`` gives one independently sampled path per copy (Claude, 2026-09-16).
"""
from __future__ import annotations

import json
import sys
import time
from typing import Any

REQUIRED_KEYS = ("candles", "horizon", "paths", "temperature", "top_p", "top_k", "seed",
                 "max_context", "threads", "code_dir", "model_dir", "tokenizer_dir")
COLUMNS = ["open", "high", "low", "close", "volume", "amount"]
HOUR_MS = 3_600_000


def validate(request: dict[str, Any]) -> str | None:
    missing = [k for k in REQUIRED_KEYS if k not in request]
    if missing:
        return f"request is missing {', '.join(missing)}"
    candles = request["candles"]
    if not candles or any(len(row) != 7 for row in candles):
        return "candles must be non-empty rows of [open_time_ms, open, high, low, close, volume, amount]"
    times = [int(row[0]) for row in candles]
    if any(b - a != HOUR_MS for a, b in zip(times, times[1:])):
        return "candles must be consecutive 1h candles, oldest first"
    if int(request["paths"]) < 1 or int(request["horizon"]) < 1:
        return "paths and horizon must be positive"
    return None


def build_frames(candles: list[list[float]], horizon: int):
    import pandas as pd

    df = pd.DataFrame([row[1:] for row in candles], columns=COLUMNS, dtype="float64")
    x_ts = pd.Series(pd.to_datetime([int(row[0]) for row in candles], unit="ms"))
    first_future = pd.Timestamp(int(candles[-1][0]) + HOUR_MS, unit="ms")
    y_ts = pd.Series(pd.date_range(first_future, periods=horizon, freq="h"))
    return df, x_ts, y_ts


def upside_probability(final_closes: list[float], last_close: float) -> float:
    return sum(1 for value in final_closes if value > last_close) / len(final_closes)


def run(request: dict[str, Any]) -> dict[str, Any]:
    problem = validate(request)
    if problem:
        return {"ok": False, "error": problem}
    sys.path.insert(0, str(request["code_dir"]))
    import torch
    from model import Kronos, KronosPredictor, KronosTokenizer

    started = time.monotonic()
    torch.set_num_threads(int(request["threads"]))
    torch.manual_seed(int(request["seed"]))
    tokenizer = KronosTokenizer.from_pretrained(str(request["tokenizer_dir"]))
    model = Kronos.from_pretrained(str(request["model_dir"]))
    tokenizer.eval()
    model.eval()
    predictor = KronosPredictor(model, tokenizer, device="cpu",
                                max_context=int(request["max_context"]))
    horizon = int(request["horizon"])
    paths = int(request["paths"])
    df, x_ts, y_ts = build_frames(request["candles"], horizon)
    with torch.no_grad():
        preds = predictor.predict_batch(
            df_list=[df] * paths, x_timestamp_list=[x_ts] * paths,
            y_timestamp_list=[y_ts] * paths, pred_len=horizon,
            T=float(request["temperature"]), top_k=int(request["top_k"]),
            top_p=float(request["top_p"]), sample_count=1, verbose=False,
        )
    finals = [float(frame["close"].iloc[-1]) for frame in preds]
    last_close = float(request["candles"][-1][4])
    return {
        "ok": True,
        "upside_prob": upside_probability(finals, last_close),
        "last_close": last_close,
        "final_closes": finals,
        "seconds": round(time.monotonic() - started, 3),
        "torch": str(getattr(torch, "__version__", "")),
    }


def main() -> int:
    try:
        result = run(json.loads(sys.stdin.read()))
    except Exception as exc:  # noqa: BLE001 - every failure is reported as JSON
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    sys.stdout.write(json.dumps(result) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

If ruff flags the in-function imports (PLC0415), add `# noqa: PLC0415` to those three lines. They are deliberate: the app never imports torch.

- [ ] **Step 5: Run the worker tests**

Run: `PYTHON_DOTENV_DISABLED=1 python3 -m pytest tests/unit/test_kronos_forecast_worker.py -q -p no:cacheprovider`
Expected: PASS (6 passed; the pandas tests skip only where pandas is missing).

- [ ] **Step 6: Write the failing client tests** — `tests/unit/test_kronos_forecast_client.py`

```python
"""Kronos forecast client: minimal worker environment, weights check, timeout, error handling."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from polymarket_bot.kronos_forecast import client as kc

SPEC = kc.KRONOS_MINI_WITH_TOKENIZER_2K
REQUEST = kc.ForecastRequest(candles=[[0, 1, 1, 1, 1, 1, 1]], horizon=1, paths=1,
                             temperature=1.0, top_p=0.95, top_k=0, seed=1)


@pytest.fixture
def models(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(kc, "models_dir", lambda: tmp_path / "models")
    for repo, rev in ((SPEC.model_repo, SPEC.model_revision),
                      (SPEC.tokenizer_repo, SPEC.tokenizer_revision)):
        d = kc.snapshot_dir(repo, rev)
        d.mkdir(parents=True)
        (d / "config.json").write_text("{}")
        (d / "model.safetensors").write_bytes(b"x")
    return tmp_path


def _fake_worker(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "fake_worker.py"
    script.write_text(textwrap.dedent(body))
    return script


def test_pinned_sources() -> None:
    assert SPEC == kc.ModelSpec("NeoQuasar/Kronos-mini", "f4e68697d9d5aed55cef5c96aabc3376bcad9f81",
                                "NeoQuasar/Kronos-Tokenizer-2k",
                                "26966d0035065a0cae0ebad7af8ece35bc1fb51c")
    assert kc.KRONOS_CODE_COMMIT == "67b630e67f6a18c9e9be918d9b4337c960db1e9a"
    assert (kc.CODE_DIR / "model" / "kronos.py").is_file()


def test_worker_env_is_explicit_and_never_carries_the_app_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0x" + "1" * 64)
    monkeypatch.setattr(kc._config, "DATA_DIR", tmp_path)
    env = kc.worker_env()
    assert set(env) == {"PATH", "HOME", "HF_HUB_OFFLINE", "HF_HUB_DISABLE_TELEMETRY",
                        "TRANSFORMERS_OFFLINE", "OMP_NUM_THREADS", "PYTHON_DOTENV_DISABLED",
                        "PYTHONDONTWRITEBYTECODE"}
    assert env["HF_HUB_OFFLINE"] == "1" and env["PYTHON_DOTENV_DISABLED"] == "1"
    assert not any("1111" in value for value in env.values())


@pytest.mark.asyncio
async def test_missing_weights_are_reported_without_starting_a_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(kc, "models_dir", lambda: tmp_path / "empty")

    async def boom(*args, **kwargs):
        raise AssertionError("no process may start without weights")

    monkeypatch.setattr(kc.asyncio, "create_subprocess_exec", boom)
    result = await kc.run_forecast(REQUEST)
    assert result.ok is False and "fetch_kronos_mini_weights" in result.error


@pytest.mark.asyncio
async def test_success_line_is_parsed_and_the_key_is_not_visible(
    models: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0x" + "1" * 64)
    monkeypatch.setattr(kc._config, "DATA_DIR", models)
    monkeypatch.setattr(kc, "WORKER_SCRIPT", _fake_worker(models, """
        import json, os, sys
        req = json.loads(sys.stdin.read())
        if "POLYMARKET_PRIVATE_KEY" in os.environ:
            sys.exit(3)
        assert req["model_dir"].endswith("f4e68697d9d5aed55cef5c96aabc3376bcad9f81")
        print("torch warning line")
        print(json.dumps({"ok": True, "upside_prob": 0.6, "last_close": 100.0,
                          "final_closes": [101.0, 99.0], "seconds": 0.5}))
    """))
    result = await kc.run_forecast(REQUEST)
    assert result == kc.ForecastResult(ok=True, upside_prob=0.6, last_close=100.0,
                                       final_closes=(101.0, 99.0), seconds=0.5)


@pytest.mark.asyncio
async def test_worker_error_and_timeout_are_reported(
    models: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(kc._config, "DATA_DIR", models)
    monkeypatch.setattr(kc, "WORKER_SCRIPT", _fake_worker(models, """
        import json
        print(json.dumps({"ok": False, "error": "ModuleNotFoundError: No module named 'torch'"}))
    """))
    failed = await kc.run_forecast(REQUEST)
    assert failed.ok is False and "torch" in failed.error

    monkeypatch.setattr(kc, "WORKER_SCRIPT", _fake_worker(models, """
        import time
        time.sleep(30)
    """))
    slow = await kc.run_forecast(REQUEST, timeout_s=0.5)
    assert slow.ok is False and "timed out" in slow.error
```

- [ ] **Step 7: Run to verify they fail**

Run: `PYTHON_DOTENV_DISABLED=1 python3 -m pytest tests/unit/test_kronos_forecast_client.py -q -p no:cacheprovider`
Expected: FAIL with `ImportError: cannot import name 'client'`

- [ ] **Step 8: Add `KRONOS_PYTHON` to `config.py`** (directly after the `POLYMARKET_GAMMA_API` line):

```python
# Interpreter for the isolated Kronos forecast worker (Tsinghua-Kronos BTC 24h). Blank =
# the app's own interpreter; set it when torch lives in a different environment.
KRONOS_PYTHON = os.getenv("KRONOS_PYTHON", "")
```

- [ ] **Step 9: Write the client** — `polymarket_bot/kronos_forecast/client.py`

```python
"""Run one Kronos forecast in an isolated worker process and return the parsed result.

The worker (worker.py) starts as ``python -I worker.py`` with an explicit minimal
environment, so it never sees the app's environment (which holds the wallet key). It
reads pinned weights from a local folder with the Hugging Face hub offline. Pinned
sources: docs/strategies/tsinghua-kronos-btc-24h.md.
"""
from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

import config as _config
from logging_setup import get_logger

log = get_logger("kronos_forecast")

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKER_SCRIPT = Path(__file__).resolve().with_name("worker.py")
# github.com/shiyu-coder/Kronos model/ at this commit, MIT, vendored unchanged.
KRONOS_CODE_COMMIT = "67b630e67f6a18c9e9be918d9b4337c960db1e9a"
CODE_DIR = REPO_ROOT / "third_party" / "kronos_67b630e"
# Under the loop watchdog's 180 s stall threshold (controller.WATCHDOG_STALL_SECONDS).
DEFAULT_TIMEOUT_S = 90.0


@dataclass(frozen=True)
class ModelSpec:
    model_repo: str
    model_revision: str
    tokenizer_repo: str
    tokenizer_revision: str


# Weights unchanged since 2025-07-01 on Hugging Face (later commits edit only the README).
KRONOS_MINI_WITH_TOKENIZER_2K = ModelSpec(
    "NeoQuasar/Kronos-mini", "f4e68697d9d5aed55cef5c96aabc3376bcad9f81",
    "NeoQuasar/Kronos-Tokenizer-2k", "26966d0035065a0cae0ebad7af8ece35bc1fb51c",
)


@dataclass(frozen=True)
class ForecastRequest:
    candles: list[list[float]]
    horizon: int
    paths: int
    temperature: float
    top_p: float
    top_k: int
    seed: int
    max_context: int = 512
    threads: int = 4


@dataclass(frozen=True)
class ForecastResult:
    ok: bool
    upside_prob: float | None = None
    last_close: float | None = None
    final_closes: tuple[float, ...] = ()
    seconds: float | None = None
    error: str | None = None


def models_dir() -> Path:
    return Path(_config.DATA_DIR) / "kronos_models"


def snapshot_dir(repo: str, revision: str) -> Path:
    return models_dir() / f"models--{repo.replace('/', '--')}" / "snapshots" / revision


def missing_weights(spec: ModelSpec) -> str | None:
    for repo, revision in ((spec.model_repo, spec.model_revision),
                           (spec.tokenizer_repo, spec.tokenizer_revision)):
        folder = snapshot_dir(repo, revision)
        if not (folder / "config.json").is_file() or not (folder / "model.safetensors").is_file():
            return (f"Kronos weights for {repo}@{revision[:12]} are not in {folder}; "
                    "run python tools/fetch_kronos_mini_weights.py")
    return None


def worker_env() -> dict[str, str]:
    home = Path(_config.DATA_DIR) / "kronos_worker_home"
    home.mkdir(parents=True, exist_ok=True)
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(home),
        "HF_HUB_OFFLINE": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "OMP_NUM_THREADS": "4",
        "PYTHON_DOTENV_DISABLED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def worker_python() -> str:
    return _config.KRONOS_PYTHON or sys.executable


async def run_forecast(
    request: ForecastRequest,
    *,
    spec: ModelSpec = KRONOS_MINI_WITH_TOKENIZER_2K,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> ForecastResult:
    missing = missing_weights(spec)
    if missing:
        return ForecastResult(ok=False, error=missing)
    payload = {
        **asdict(request),
        "code_dir": str(CODE_DIR),
        "model_dir": str(snapshot_dir(spec.model_repo, spec.model_revision)),
        "tokenizer_dir": str(snapshot_dir(spec.tokenizer_repo, spec.tokenizer_revision)),
    }
    try:
        proc = await asyncio.create_subprocess_exec(
            worker_python(), "-I", str(WORKER_SCRIPT),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=worker_env(), cwd=str(models_dir()),
        )
    except OSError as exc:
        return ForecastResult(ok=False, error=f"could not start the Kronos worker: {exc}")
    try:
        out, err = await asyncio.wait_for(proc.communicate(json.dumps(payload).encode()),
                                          timeout_s)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        log.warning("kronos_forecast.worker_timeout", timeout_s=timeout_s)
        return ForecastResult(ok=False, error=f"Kronos worker timed out after {timeout_s:g} s")
    lines = out.decode("utf-8", "replace").strip().splitlines()
    try:
        data = json.loads(lines[-1]) if lines else {}
    except json.JSONDecodeError:
        data = {}
    if proc.returncode != 0 or not data.get("ok"):
        detail = (data.get("error") or err.decode("utf-8", "replace").strip()[-400:]
                  or f"worker exited with code {proc.returncode}")
        log.warning("kronos_forecast.worker_failed", returncode=proc.returncode, error=detail)
        return ForecastResult(ok=False, error=str(detail))
    return ForecastResult(
        ok=True,
        upside_prob=float(data["upside_prob"]),
        last_close=float(data["last_close"]),
        final_closes=tuple(float(v) for v in data["final_closes"]),
        seconds=float(data["seconds"]),
    )
```

- [ ] **Step 10: Run the client tests**

Run: `PYTHON_DOTENV_DISABLED=1 python3 -m pytest tests/unit/test_kronos_forecast_client.py tests/unit/test_kronos_forecast_worker.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 11: Weights tool and optional extra**

`tools/fetch_kronos_mini_weights.py`:

```python
"""Download the pinned Kronos-mini and Kronos-Tokenizer-2k weights (Tsinghua-Kronos BTC 24h).

Sources: huggingface.co/NeoQuasar/Kronos-mini and huggingface.co/NeoQuasar/Kronos-Tokenizer-2k
(MIT), at the revisions pinned in polymarket_bot/kronos_forecast/client.py. Only config.json
and model.safetensors are fetched; safetensors files never run code when loaded.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from huggingface_hub import snapshot_download  # noqa: E402

from polymarket_bot.kronos_forecast import client as kc  # noqa: E402


def main() -> int:
    spec = kc.KRONOS_MINI_WITH_TOKENIZER_2K
    kc.models_dir().mkdir(parents=True, exist_ok=True)
    for repo, revision in ((spec.model_repo, spec.model_revision),
                           (spec.tokenizer_repo, spec.tokenizer_revision)):
        path = snapshot_download(repo_id=repo, revision=revision, cache_dir=str(kc.models_dir()),
                                 allow_patterns=["config.json", "model.safetensors"])
        print(f"{repo}@{revision} -> {path}")
    missing = kc.missing_weights(spec)
    print("Kronos weights ready." if missing is None else missing)
    return 0 if missing is None else 1


if __name__ == "__main__":
    sys.exit(main())
```

`pyproject.toml`, under `[project.optional-dependencies]`, add:

```toml
# Isolated Kronos forecast worker (polymarket_bot/kronos_forecast/worker.py) for
# Tsinghua-Kronos BTC 24h. The app process never imports these.
kronos = ["torch>=2.2", "einops>=0.8", "pandas>=2.0", "safetensors>=0.4", "huggingface-hub>=0.26", "tqdm>=4.66"]
```

**Tsinghua-Kronos BTC 24h setup** (the operator does this once per machine; agents never install packages). The worker runs under `KRONOS_PYTHON` if that is set, otherwise under the app's own interpreter. That interpreter needs torch, einops and pandas. Do one of these:
- Install the optional extra into it. From the repo root, with that interpreter, run `python3 -m pip install -e '.[kronos]'`.
- Set `KRONOS_PYTHON=/absolute/path/to/python` in `.env`, pointing at an interpreter that already has them. A relative path is refused.

Then fetch the pinned weights once with `python3 tools/fetch_kronos_mini_weights.py`. Expected: "Kronos weights ready." Until both are done, every window is recorded `UNAVAILABLE`, and its reason says what is missing and how to fix it.

- [ ] **Step 12: Real-model check against the Kronos team's published numbers** (not a unit test; needs torch and the weights)

Write `tools/check_tsinghua_kronos_btc_24h_against_published.py`:

```python
"""Rerun the Tsinghua-Kronos BTC 24h forecast on hours the Kronos team published; compare.

Inputs (produced on branch research/kronos-mini-official-btcusdt-24h-demo-recreation):
  --published  research/kronos_mini_official_btcusdt_demo/data/published_forecasts_scored.csv
  --candles    research/kronos_mini_official_btcusdt_demo/data/btcusdt_1h_spot.csv
Prints, per hour, our upside probability vs the published one, then the mean absolute gap,
the correlation and how many gaps exceed 2 sampling standard errors. With 30 paths the gap
from sampling alone is about sqrt(2 * p(1-p)/30), roughly 13 points near 50%.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from polymarket_bot.daily_btc import tsinghua_kronos_btc_24h as rule  # noqa: E402
from polymarket_bot.hourly.market import Candle  # noqa: E402
from polymarket_bot.kronos_forecast import client as kc  # noqa: E402

HOUR_MS = 3_600_000


def load_candles(path: Path) -> dict[int, Candle]:
    out = {}
    with path.open() as f:
        for r in csv.DictReader(f):
            t = int(r["open_time_ms"])
            out[t] = Candle(t, float(r["open"]), float(r["high"]), float(r["low"]),
                            float(r["close"]), float(r["volume"]), float(r["quote_volume"]),
                            float(r["taker_buy_base"]))
    return out


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--published", type=Path, required=True)
    ap.add_argument("--candles", type=Path, required=True)
    ap.add_argument("--hours", type=int, default=40)
    args = ap.parse_args()
    candles = load_candles(args.candles)
    rows = []
    with args.published.open() as f:
        for r in csv.DictReader(f):
            if r["era"] == "mini":
                rows.append(r)
    step = max(1, len(rows) // args.hours)
    pairs = []
    for r in rows[::step][: args.hours]:
        start_ms = int(float(__import__("pandas").Timestamp(r["anchor"]).timestamp()) * 1000)
        window = [candles.get(start_ms - k * HOUR_MS) for k in range(rule.INPUT_CANDLES, 0, -1)]
        if any(c is None for c in window):
            continue
        result = await kc.run_forecast(rule.request_for(window, start_ms // 1000))
        if not result.ok:
            print("worker failed:", result.error)
            return 1
        published = float(r["p"])
        pairs.append((result.upside_prob, published))
        print(f"{r['anchor']}  ours={result.upside_prob:.3f}  published={published:.3f}  "
              f"({result.seconds:.1f} s)")
    ours = [a for a, _ in pairs]
    pub = [b for _, b in pairs]
    n = len(pairs)
    mae = sum(abs(a - b) for a, b in pairs) / n
    ma, mb = sum(ours) / n, sum(pub) / n
    cov = sum((a - ma) * (b - mb) for a, b in pairs)
    corr = cov / math.sqrt(sum((a - ma) ** 2 for a in ours) * sum((b - mb) ** 2 for b in pub))
    wide = sum(1 for a, b in pairs
               if abs(a - b) > 2 * math.sqrt(max(b * (1 - b), 1 / 30) * 2 / rule.PATHS))
    print(f"hours={n} mean_abs_gap={mae:.3f} correlation={corr:.3f} gaps_beyond_2se={wide}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
```

(This tool imports `tsinghua_kronos_btc_24h` from Task 3. Run it after Task 3 is committed.)

Run it later from the worktree, with pandas and torch available:

```bash
python3 tools/fetch_kronos_mini_weights.py
R=/Users/zayankhan/projects/polymarket-crypto/.claude/worktrees/research-kronos-mini-demo/research/kronos_mini_official_btcusdt_demo/data
python3 tools/check_tsinghua_kronos_btc_24h_against_published.py --published $R/published_forecasts_scored.csv --candles $R/btcusdt_1h_spot.csv --hours 40 | tail -3
```

Expected: correlation above 0.6, mean absolute gap under 0.15, and at most about 2 of 40 gaps beyond 2 standard errors. If the correlation is much lower, stop: the MIT code path does not reproduce the demo recipe.

- [ ] **Step 13: Commit**

```bash
git add third_party/kronos_67b630e polymarket_bot/kronos_forecast tools/fetch_kronos_mini_weights.py tools/check_tsinghua_kronos_btc_24h_against_published.py config.py pyproject.toml tests/unit/test_kronos_forecast_client.py tests/unit/test_kronos_forecast_worker.py
git commit -m "feat(kronos): isolated Kronos forecast worker with pinned Kronos-mini weights and vendored MIT code"
```

---

### Task 2: Current window, payout, forecast input

**Files:**
- Modify: `polymarket_bot/daily_btc/market.py` (from `feature/btc-daily-fade-3d`; add two functions only)
- Create: `polymarket_bot/daily_btc/forecast_input.py`
- Test: append to `tests/unit/test_daily_btc.py`; create `tests/unit/test_daily_btc_forecast_input.py`

**Interfaces:**
- Consumes (Task 0): `ET`, `DayWindow`, `window_for`, `noon_et` from `polymarket_bot.daily_btc.market`.
- Consumes: `polymarket_bot.hourly.market.Candle` and `fetch_closed_candles(client, *, market, symbol, now_ms, limit)`. The latter drops Binance's forming candle by position and keeps only rows with `close_time < now_ms`.
- Produces (market.py):
  - `current_window(now_ts: int) -> DayWindow`: the window whose reference noon is the latest at or before `now_ts`.
  - `payout(side: str, result: str) -> float`: 1.0, 0.0, or 0.5 for `"tie"`.
- Produces (`forecast_input.py`):
  - `INPUT_CANDLES = 383`
  - `async fetch_forecast_candles(client, window: DayWindow, now_ts: int) -> list[Candle] | None`: the 383 closed spot 1h candles ending with the one that opens an hour before `window.reference_ts`, or None while Binance has not closed it.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_daily_btc.py`:

```python
from datetime import UTC, date, datetime

from polymarket_bot.daily_btc import market as dbm


def _utc(y: int, mo: int, d: int, h: int, mi: int = 0, s: int = 0) -> int:
    return int(datetime(y, mo, d, h, mi, s, tzinfo=UTC).timestamp())


def test_current_window_is_the_latest_noon_at_or_before_now() -> None:
    # 2026-09-16 12:00 EDT = 16:00 UTC. Market dated Sep 17 covers Sep 16 noon -> Sep 17 noon.
    at_noon = dbm.current_window(_utc(2026, 9, 16, 16))
    assert at_noon.market_date == date(2026, 9, 17)
    assert at_noon.reference_ts == _utc(2026, 9, 16, 16)
    before = dbm.current_window(_utc(2026, 9, 16, 15, 59, 59))
    assert before.market_date == date(2026, 9, 16)
    winter = dbm.current_window(_utc(2026, 1, 15, 17, 0, 5))  # 12:00 EST = 17:00 UTC
    assert winter.reference_ts == _utc(2026, 1, 15, 17)


def test_current_window_across_fall_back_is_25_hours() -> None:
    w = dbm.current_window(_utc(2026, 10, 31, 16, 0, 30))
    assert w.reference_ts == _utc(2026, 10, 31, 16)   # noon EDT
    assert w.settle_ts == _utc(2026, 11, 1, 17)       # noon EST
    assert w.settle_ts - w.reference_ts == 25 * 3600


def test_payout_pays_half_on_a_tie() -> None:
    assert dbm.payout("Up", "Up") == 1.0
    assert dbm.payout("Down", "Up") == 0.0
    assert dbm.payout("Down", "tie") == 0.5
```

Create `tests/unit/test_daily_btc_forecast_input.py`:

```python
"""Tsinghua-Kronos BTC 24h input: 383 closed Binance spot 1h candles before the window's noon."""
from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

import config as _config
from polymarket_bot.daily_btc import forecast_input as fi
from polymarket_bot.daily_btc import market as dbm

S = int(datetime(2026, 9, 16, 16, tzinfo=UTC).timestamp())
WINDOW = dbm.current_window(S)


def _kline(open_s: int) -> list:
    return [open_s * 1000, "100", "101", "99", "100.5", "10", open_s * 1000 + 3_599_999,
            "1000", 5, "5", "0", "0"]


def _client(rows: list[list], seen: list | None = None) -> httpx.AsyncClient:
    def handle(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(200, json=rows)
    return httpx.AsyncClient(transport=httpx.MockTransport(handle))


@pytest.mark.asyncio
async def test_returns_the_383_candles_ending_an_hour_before_noon() -> None:
    rows = [_kline(S - k * 3600) for k in range(383, -1, -1)]  # ... S-1h, then forming S
    seen: list[httpx.Request] = []
    async with _client(rows, seen) as client:
        candles = await fi.fetch_forecast_candles(client, WINDOW, S + 5)
    assert candles is not None and len(candles) == fi.INPUT_CANDLES == 383
    assert candles[-1].open_time_ms == (S - 3600) * 1000
    assert candles[0].open_time_ms == (S - 383 * 3600) * 1000
    assert candles[-1].quote_volume == 1000.0
    req = seen[0]
    assert str(req.url).startswith(f"{_config.BINANCE_API_BASE}/api/v3/klines")
    assert req.url.params["interval"] == "1h" and req.url.params["limit"] == "384"


@pytest.mark.asyncio
async def test_none_while_the_hour_before_noon_is_not_closed_on_binance() -> None:
    rows = [_kline(S - k * 3600) for k in range(384, 0, -1)]  # Binance has not opened S yet
    async with _client(rows) as client:
        assert await fi.fetch_forecast_candles(client, WINDOW, S + 2) is None


@pytest.mark.asyncio
async def test_none_when_history_is_short() -> None:
    rows = [_kline(S - k * 3600) for k in range(10, -1, -1)]
    async with _client(rows) as client:
        assert await fi.fetch_forecast_candles(client, WINDOW, S + 5) is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `PYTHON_DOTENV_DISABLED=1 python3 -m pytest tests/unit/test_daily_btc.py tests/unit/test_daily_btc_forecast_input.py -q -p no:cacheprovider`
Expected: FAIL: `AttributeError: module 'polymarket_bot.daily_btc.market' has no attribute 'current_window'` and `ImportError: cannot import name 'forecast_input'`.

- [ ] **Step 3: Implement**

In `polymarket_bot/daily_btc/market.py`, add directly after `next_window`:

```python
def current_window(now_ts: int) -> DayWindow:
    """The market whose reference noon is the latest one at or before ``now_ts``.

    Tsinghua-Kronos BTC 24h decides just after noon ET, once the 1h candle ending at noon
    has closed (Claude, 2026-09-16), so it needs the window that has just started.
    """
    et = datetime.fromtimestamp(now_ts, ET)
    reference_day = et.date() if et.time() >= time(12) else et.date() - timedelta(days=1)
    return window_for(reference_day + timedelta(days=1))
```

and after `outcome`:

```python
def payout(side: str, result: str) -> float:
    """Per-share payout at resolution: an exact tie pays 0.50 to both sides (market rules)."""
    if result == "tie":
        return 0.5
    return 1.0 if side == result else 0.0
```

Create `polymarket_bot/daily_btc/forecast_input.py`:

```python
"""Tsinghua-Kronos BTC 24h input: the 383 closed Binance spot BTCUSDT 1h candles before noon ET.

Same as the Kronos team's demo (github.com/shiyu-coder/Kronos-demo update_predictions.py at
eba16695): it fetched 384 candles and dropped the still-forming one. Here the last input
candle must be the one that opens an hour before the window's reference noon, so its close
is the price at noon.
"""
from __future__ import annotations

import httpx

from polymarket_bot.daily_btc.market import DayWindow
from polymarket_bot.hourly.market import Candle, fetch_closed_candles

INPUT_CANDLES = 383
_HOUR_S = 3600


async def fetch_forecast_candles(
    client: httpx.AsyncClient, window: DayWindow, now_ts: int
) -> list[Candle] | None:
    candles = await fetch_closed_candles(
        client, market="spot", symbol="BTCUSDT", now_ms=now_ts * 1000, limit=INPUT_CANDLES + 1
    )
    if len(candles) < INPUT_CANDLES:
        return None
    if candles[-1].open_time_ms != (window.reference_ts - _HOUR_S) * 1000:
        return None
    return candles[-INPUT_CANDLES:]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHON_DOTENV_DISABLED=1 python3 -m pytest tests/unit/test_daily_btc.py tests/unit/test_daily_btc_forecast_input.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add polymarket_bot/daily_btc/market.py polymarket_bot/daily_btc/forecast_input.py tests/unit/test_daily_btc.py tests/unit/test_daily_btc_forecast_input.py
git commit -m "feat(daily_btc): current noon-ET window, tie payout and the 383-candle Kronos input"
```

---

### Task 3: Tsinghua-Kronos BTC 24h rule

**Files:**
- Create: `polymarket_bot/daily_btc/tsinghua_kronos_btc_24h.py`
- Test: `tests/unit/test_tsinghua_kronos_btc_24h.py`

**Interfaces:**
- Consumes: `kronos_forecast.client.ForecastRequest`, `ForecastResult`, `KRONOS_MINI_WITH_TOKENIZER_2K`, `KRONOS_CODE_COMMIT` (Task 1); `forecast_input.INPUT_CANDLES` (Task 2); `hourly.market.Candle`.
- Produces:
  - Constants: `STRATEGY_ID = "tsinghua_kronos_btc_24h"`, `DISPLAY_NAME = "Tsinghua-Kronos BTC 24h"`, `INPUT_CANDLES = 383`, `HORIZON_HOURS = 24`, `PATHS = 30`, `TEMPERATURE = 1.0`, `TOP_P = 0.95`, `TOP_K = 0`, `MAX_CONTEXT = 512`
  - `@dataclass(frozen=True) Decision(side: str | None, reason: str, signal: dict[str, Any], available: bool)`
  - `request_for(candles: list[Candle], reference_ts: int) -> ForecastRequest`
  - `decide(result: ForecastResult, *, candles: list[Candle], reference_ts: int, up_ask: float | None, down_ask: float | None, edge_threshold: float) -> Decision`

- [ ] **Step 1: Write the failing tests**

```python
"""Tsinghua-Kronos BTC 24h: recipe request, price-relative bet rule, recorded signal."""
from __future__ import annotations

import math

import pytest

from polymarket_bot.daily_btc import tsinghua_kronos_btc_24h as rule
from polymarket_bot.hourly.market import Candle
from polymarket_bot.kronos_forecast.client import ForecastResult

REF = 1_789_574_400  # 2026-09-16 16:00 UTC (noon EDT)
CANDLES = [Candle((REF - (383 - i) * 3600) * 1000, 100.0, 101.0, 99.0, 100.0 + i, 10.0, 1000.0, 5.0)
           for i in range(383)]


def _result(p: float) -> ForecastResult:
    return ForecastResult(ok=True, upside_prob=p, last_close=482.0, final_closes=(483.0, 481.0),
                          seconds=8.8)


def test_request_follows_the_kronos_demo_recipe() -> None:
    req = rule.request_for(CANDLES, REF)
    assert (req.horizon, req.paths, req.temperature, req.top_p, req.top_k, req.max_context) == (
        24, 30, 1.0, 0.95, 0, 512)
    assert req.seed == REF // 3600
    assert req.candles[-1] == [(REF - 3600) * 1000, 100.0, 101.0, 99.0, 482.0, 10.0, 1000.0]
    assert len(req.candles) == rule.INPUT_CANDLES == 383


def test_buys_up_when_the_forecast_beats_the_up_price_by_the_threshold() -> None:
    d = rule.decide(_result(0.60), candles=CANDLES, reference_ts=REF, up_ask=0.52,
                    down_ask=0.49, edge_threshold=0.05)
    assert d.side == "Up" and d.available
    assert d.signal["up_edge"] == pytest.approx(0.08)
    assert d.signal["down_edge"] == pytest.approx(-0.09)
    assert d.signal["sampling_se"] == pytest.approx(math.sqrt(0.6 * 0.4 / 30))
    assert d.signal["published_record_side"] == "Up"
    assert d.signal["model"] == "NeoQuasar/Kronos-mini@f4e68697d9d5aed55cef5c96aabc3376bcad9f81"
    assert d.signal["input_last_open_ms"] == (REF - 3600) * 1000
    assert d.reason.startswith("enter Up")


def test_buys_down_and_skips_below_the_threshold() -> None:
    down = rule.decide(_result(0.30), candles=CANDLES, reference_ts=REF, up_ask=0.52,
                       down_ask=0.49, edge_threshold=0.05)
    assert down.side == "Down" and down.signal["down_edge"] == pytest.approx(0.21)
    skip = rule.decide(_result(0.55), candles=CANDLES, reference_ts=REF, up_ask=0.52,
                       down_ask=0.49, edge_threshold=0.05)
    assert skip.side is None and skip.available and skip.reason.startswith("no bet")


def test_missing_asks_and_unavailable_forecasts() -> None:
    no_book = rule.decide(_result(0.9), candles=CANDLES, reference_ts=REF, up_ask=None,
                          down_ask=None, edge_threshold=0.05)
    assert no_book.side is None and no_book.signal["up_edge"] is None
    failed = rule.decide(ForecastResult(ok=False, error="timed out"), candles=CANDLES,
                         reference_ts=REF, up_ask=0.5, down_ask=0.5, edge_threshold=0.05)
    assert failed.side is None and failed.available is False
    assert failed.reason == "unavailable: timed out"


def test_a_published_record_side_is_none_at_exactly_half() -> None:
    d = rule.decide(_result(0.5), candles=CANDLES, reference_ts=REF, up_ask=0.5, down_ask=0.5,
                    edge_threshold=0.05)
    assert d.signal["published_record_side"] is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `PYTHON_DOTENV_DISABLED=1 python3 -m pytest tests/unit/test_tsinghua_kronos_btc_24h.py -q -p no:cacheprovider`
Expected: FAIL with `ImportError: cannot import name 'tsinghua_kronos_btc_24h'`

- [ ] **Step 3: Implement**

```python
"""Tsinghua-Kronos BTC 24h: the Kronos team's 24-hour BTCUSDT forecast, bet when it beats the price.

Name: Zayan (operator), 2026-09-16. Forecast recipe: github.com/shiyu-coder/Kronos-demo
update_predictions.py (commit eba16695), Kronos (Shi et al., arXiv 2508.02739, Tsinghua).
Bet only when the forecast beats the market price: Zayan (operator), 2026-09-13.
Default edge threshold 0.05: Claude, 2026-09-16. Full sources:
docs/strategies/tsinghua-kronos-btc-24h.md.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from polymarket_bot.daily_btc.forecast_input import INPUT_CANDLES
from polymarket_bot.hourly.market import Candle
from polymarket_bot.kronos_forecast.client import (
    KRONOS_CODE_COMMIT,
    KRONOS_MINI_WITH_TOKENIZER_2K,
    ForecastRequest,
    ForecastResult,
)

STRATEGY_ID = "tsinghua_kronos_btc_24h"
DISPLAY_NAME = "Tsinghua-Kronos BTC 24h"
# Recipe of the Kronos team's published forecast (research branch
# research/kronos-mini-official-btcusdt-24h-demo-recreation, README). Fixed so live calls
# stay comparable with that scored record.
HORIZON_HOURS = 24
PATHS = 30
TEMPERATURE = 1.0
TOP_P = 0.95
TOP_K = 0
MAX_CONTEXT = 512
__all__ = ["INPUT_CANDLES", "STRATEGY_ID", "DISPLAY_NAME", "Decision", "request_for", "decide"]


@dataclass(frozen=True)
class Decision:
    side: str | None
    reason: str
    signal: dict[str, Any]
    available: bool


def request_for(candles: list[Candle], reference_ts: int) -> ForecastRequest:
    return ForecastRequest(
        candles=[[c.open_time_ms, c.open, c.high, c.low, c.close, c.volume, c.quote_volume]
                 for c in candles],
        horizon=HORIZON_HOURS, paths=PATHS, temperature=TEMPERATURE, top_p=TOP_P, top_k=TOP_K,
        seed=reference_ts // 3600, max_context=MAX_CONTEXT,
    )


def _price(value: float | None) -> str:
    return f"{value:.2f}" if value is not None else "none"


def decide(
    result: ForecastResult,
    *,
    candles: list[Candle],
    reference_ts: int,
    up_ask: float | None,
    down_ask: float | None,
    edge_threshold: float,
) -> Decision:
    spec = KRONOS_MINI_WITH_TOKENIZER_2K
    signal: dict[str, Any] = {
        "strategy": DISPLAY_NAME,
        "model": f"{spec.model_repo}@{spec.model_revision}",
        "tokenizer": f"{spec.tokenizer_repo}@{spec.tokenizer_revision}",
        "code": f"shiyu-coder/Kronos@{KRONOS_CODE_COMMIT}",
        "recipe": "shiyu-coder/Kronos-demo update_predictions.py@eba16695",
        "paths": PATHS, "horizon_hours": HORIZON_HOURS, "temperature": TEMPERATURE,
        "top_p": TOP_P, "top_k": TOP_K, "seed": reference_ts // 3600,
        "input_candles": len(candles),
        "input_first_open_ms": candles[0].open_time_ms if candles else None,
        "input_last_open_ms": candles[-1].open_time_ms if candles else None,
        "up_ask": up_ask, "down_ask": down_ask, "edge_threshold": edge_threshold,
        "up_edge": None, "down_edge": None,
    }
    if not result.ok or result.upside_prob is None:
        signal["error"] = result.error
        return Decision(None, f"unavailable: {result.error}", signal, available=False)
    p = result.upside_prob
    se = math.sqrt(p * (1 - p) / PATHS)
    up_edge = p - up_ask if up_ask is not None and up_ask > 0 else None
    down_edge = (1 - p) - down_ask if down_ask is not None and down_ask > 0 else None
    signal.update({
        "p_up": p, "sampling_se": se, "last_close": result.last_close,
        "final_closes": list(result.final_closes), "worker_seconds": result.seconds,
        "up_edge": up_edge, "down_edge": down_edge,
        # The side the Kronos team's published record was scored on (P above/below 50%).
        "published_record_side": "Up" if p > 0.5 else ("Down" if p < 0.5 else None),
    })
    best: tuple[str, float] | None = None
    if up_edge is not None and up_edge >= edge_threshold:
        best = ("Up", up_edge)
    if down_edge is not None and down_edge >= edge_threshold and (best is None or down_edge > best[1]):
        best = ("Down", down_edge)
    if best is None:
        return Decision(
            None,
            f"no bet: P(up) {p:.2f} vs Up ask {_price(up_ask)} and Down ask {_price(down_ask)}; "
            f"no edge of {edge_threshold:.2f} or more",
            signal, available=True,
        )
    side, edge = best
    return Decision(
        side,
        f"enter {side}: P(up) {p:.2f} (sampling error {se:.2f}), edge {edge:+.2f} "
        f"at threshold {edge_threshold:.2f}",
        signal, available=True,
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHON_DOTENV_DISABLED=1 python3 -m pytest tests/unit/test_tsinghua_kronos_btc_24h.py -q -p no:cacheprovider`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add polymarket_bot/daily_btc/tsinghua_kronos_btc_24h.py tests/unit/test_tsinghua_kronos_btc_24h.py
git commit -m "feat(daily_btc): Tsinghua-Kronos BTC 24h rule - Kronos team recipe, bet when it beats the price"
```

Then run Task 1 Step 12 (the real-model check against the published numbers). It needs this module.

---

### Task 4: Daily decision record (starts after Task 0 Step 2)

**Files:**
- Modify: `db.py` (SCHEMA: new table and index, appended at the very end of the SCHEMA literal, after whatever the merged branches added)
- Create: `polymarket_bot/daily_btc/ledger.py`
- Test: `tests/unit/test_daily_btc_ledger.py`

**Adapted to the merged hourly code (Claude, 2026-09-16, while building this task):**
- `get_decision` and `set_action` take the same arguments, in the same order, as `polymarket_bot/hourly/ledger.py`: `mode` is keyword-only and comes after `action`. The planned daily order (`mode` before `action`, positional) differed from the hourly one. Both are plain strings, so a call written in the other ledger's order would silently match no row.
- `stale_rows` is replaced by `finalize_ended_windows`, which applies the merged hourly rules (`finalize_ended_hours`, branch-review finding pending-row-never-finalized) to daily windows. Those rules are:
  - It covers every mode, not only the running one.
  - A window's position counts in any state, except rows that boot reconciliation closed as never placed or never filled.
  - SUBMITTING becomes the unfinished-attempt action.
  - PENDING becomes MISSED.
  - Task 7 calls it at the top of `settle_due`, as the hourly engine does. That replaces the planned `_finalize_past_windows`.
- The tests add the finalization cases and drop the `stale_rows` ones.

**Interfaces:**
- Consumes: `DayMarket`, `DayWindow` (Task 0).
- Produces:
  - Action constants: `PENDING`, `NO_SIGNAL`, `MISSED`, `UNAVAILABLE`, `ENTERED`.
  - `async record_decision(*, strategy_id, mode, market: DayMarket, side, reason, signal, up_bid, up_ask, down_bid, down_ask, late: bool, available: bool) -> bool` (INSERT OR IGNORE; True iff this call wrote the row). The action is:
    - `UNAVAILABLE` if not available;
    - else `MISSED` if late;
    - else `NO_SIGNAL` if side is None;
    - else `PENDING`.
  - `async get_decision(reference_ts: int, strategy_id: str, *, mode: str) -> dict | None`
  - `async set_action(reference_ts, strategy_id, action, position_id=None, *, mode, expected_action=None) -> bool` (True iff a row changed)
  - `async finalize_ended_windows(current_reference_ts: int, unfinished_action: str) -> dict[str, int]`: closes out PENDING/SUBMITTING rows of windows before `current_reference_ts` and returns `{"entered", "unfinished", "missed"}` counts. The rules are those of `polymarket_bot.hourly.ledger.finalize_ended_hours`.
  - `async unsettled_windows(now_ts: int, grace_s: int) -> list[tuple[int, int]]`: distinct `(reference_ts, settle_ts)` with `settled_at IS NULL` and `settle_ts + grace_s <= now_ts`
  - `async settle_window(reference_ts: int, reference_close: float, settle_close: float, result: str) -> None` (`result` is `"Up"`, `"Down"` or `"tie"`)

- [ ] **Step 1: Write the failing tests**

```python
"""Daily BTC decision record: one row per (window, strategy, mode), actions, finalization, settlement."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
import pytest_asyncio

import db as _db
from polymarket_bot.daily_btc import ledger
from polymarket_bot.daily_btc.market import DayMarket, window_for

WINDOW = window_for(date(2026, 9, 17))  # Sep 16 noon ET -> Sep 17 noon ET
MARKET = DayMarket("bitcoin-up-or-down-on-september-17-2026", "Bitcoin Up or Down on September 17?",
                   WINDOW, "111", "222", 0.07, 1.0)
PREV_WINDOW = window_for(date(2026, 9, 16))  # Sep 15 noon ET -> Sep 16 noon ET
PREV_MARKET = DayMarket("bitcoin-up-or-down-on-september-16-2026",
                        "Bitcoin Up or Down on September 16?", PREV_WINDOW, "333", "444", 0.07, 1.0)
SID = "tsinghua_kronos_btc_24h"
SECOND_SID = "second_daily_btc_strategy_in_this_test"
UNFINISHED = "UNCERTAIN:entry attempt did not finish"


@pytest_asyncio.fixture
async def test_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "t.db")
    await _db.init_db()
    return _db


async def _record(mode: str = "paper", side: str | None = "Up", *, late: bool = False,
                  available: bool = True, market: DayMarket = MARKET,
                  strategy_id: str = SID) -> bool:
    return await ledger.record_decision(
        strategy_id=strategy_id, mode=mode, market=market, side=side, reason="enter Up: test",
        signal={"p_up": 0.6}, up_bid=0.5, up_ask=0.52, down_bid=0.48, down_ask=0.5,
        late=late, available=available,
    )


async def _insert_position(*, strategy_id: str, mode: str, reference_ts: int,
                           timeframe: str = "1d", state: str = "open",
                           exit_reason: str | None = None) -> int:
    async with _db.connect() as conn:
        cur = await conn.execute(
            "INSERT INTO paper_positions(opened_at, window_slug, side, state, entry_price,"
            " notional_usd, shares, strategy_id, market_timeframe, window_start_ts, mode,"
            " exit_reason) VALUES ('x', 'slug', 'Up', ?, 0.52, 2.6, 5, ?, ?, ?, ?, ?)",
            (state, strategy_id, timeframe, reference_ts, mode, exit_reason),
        )
        await conn.commit()
        return int(cur.lastrowid)


@pytest.mark.asyncio
async def test_one_row_per_window_strategy_and_mode(test_db) -> None:
    assert await _record() is True
    assert await _record() is False
    assert await _record(mode="live") is True
    paper = await ledger.get_decision(WINDOW.reference_ts, SID, mode="paper")
    assert paper["action"] == ledger.PENDING and paper["decision_side"] == "Up"
    assert paper["window_slug"] == MARKET.slug and paper["settle_ts"] == WINDOW.settle_ts
    assert json.loads(paper["signal_json"]) == {"p_up": 0.6}
    assert (paper["up_bid"], paper["up_ask"], paper["down_bid"], paper["down_ask"]) == (
        0.5, 0.52, 0.48, 0.5)
    assert (await ledger.get_decision(WINDOW.reference_ts, SID, mode="live"))["mode"] == "live"
    assert await ledger.get_decision(PREV_WINDOW.reference_ts, SID, mode="paper") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(("side", "late", "available", "action"), [
    ("Up", False, True, "PENDING"), ("Up", True, True, "MISSED"),
    (None, False, True, "NO_SIGNAL"), (None, True, True, "MISSED"),
    (None, False, False, "UNAVAILABLE"),
])
async def test_action_mapping(test_db, side, late, available, action) -> None:
    await _record(side=side, late=late, available=available)
    assert (await ledger.get_decision(WINDOW.reference_ts, SID, mode="paper"))["action"] == action


@pytest.mark.asyncio
async def test_set_action_with_expected_action_only_replaces_that_action(test_db) -> None:
    await _record()
    await _record(mode="live")
    ref = WINDOW.reference_ts
    assert await ledger.set_action(ref, SID, "SUBMITTING", mode="paper",
                                   expected_action="PENDING") is True
    assert await ledger.set_action(ref, SID, "MISSED", mode="paper",
                                   expected_action="PENDING") is False
    assert await ledger.set_action(ref, SID, "ENTERED", 7, mode="paper") is True
    row = await ledger.get_decision(ref, SID, mode="paper")
    assert (row["action"], row["position_id"]) == ("ENTERED", 7)
    assert (await ledger.get_decision(ref, SID, mode="live"))["action"] == "PENDING"
    assert await ledger.set_action(PREV_WINDOW.reference_ts, SID, "MISSED", mode="paper") is False


@pytest.mark.asyncio
async def test_settlement_of_every_row_in_the_window(test_db) -> None:
    await _record()
    await _record(mode="live", side=None)
    assert await ledger.unsettled_windows(WINDOW.settle_ts + 119, 120) == []
    assert await ledger.unsettled_windows(WINDOW.settle_ts + 120, 120) == [
        (WINDOW.reference_ts, WINDOW.settle_ts)]
    await ledger.settle_window(WINDOW.reference_ts, 100.0, 100.0, "tie")
    for mode in ("paper", "live"):
        row = await ledger.get_decision(WINDOW.reference_ts, SID, mode=mode)
        assert (row["reference_close"], row["settle_close"], row["outcome"]) == (
            100.0, 100.0, "tie")
        assert row["settled_at"] is not None
    assert await ledger.unsettled_windows(WINDOW.settle_ts + 999, 120) == []


# Same rules as the hourly record's finalize_ended_hours (Claude, 2026-09-15, branch-review
# finding pending-row-never-finalized), for noon-ET windows (Claude, 2026-09-16).
@pytest.mark.asyncio
async def test_open_decisions_of_ended_windows_are_finalized_per_strategy_and_mode(
    test_db,
) -> None:
    prev = PREV_WINDOW.reference_ts
    await _record(market=PREV_MARKET)
    await _record(market=PREV_MARKET, strategy_id=SECOND_SID)
    await _record(market=PREV_MARKET, strategy_id=SECOND_SID, mode="live")
    await _record()
    await _record(strategy_id=SECOND_SID, side=None)
    position_id = await _insert_position(strategy_id=SECOND_SID, mode="live", reference_ts=prev)
    await ledger.set_action(prev, SID, "SUBMITTING", mode="paper")

    counts = await ledger.finalize_ended_windows(WINDOW.reference_ts, UNFINISHED)

    assert counts == {"entered": 1, "unfinished": 1, "missed": 1}
    row = await ledger.get_decision(prev, SID, mode="paper")
    assert (row["action"], row["position_id"]) == (UNFINISHED, None)
    row = await ledger.get_decision(prev, SECOND_SID, mode="live")
    assert (row["action"], row["position_id"]) == ("ENTERED", position_id)
    row = await ledger.get_decision(prev, SECOND_SID, mode="paper")  # live position: not its own
    assert (row["action"], row["position_id"]) == ("MISSED", None)
    # The current window is left to the engine's entry step.
    assert (await ledger.get_decision(WINDOW.reference_ts, SID, mode="paper"))["action"] == (
        "PENDING")
    assert (await ledger.get_decision(WINDOW.reference_ts, SECOND_SID, mode="paper"))[
        "action"] == "NO_SIGNAL"
    assert await ledger.finalize_ended_windows(WINDOW.reference_ts, "x") == {
        "entered": 0, "unfinished": 0, "missed": 0}


@pytest.mark.asyncio
@pytest.mark.parametrize(("timeframe", "state", "exit_reason", "action"), [
    ("1d", "closed", "STOP_REQUEST", "ENTERED"),  # sold at Stop: the entry went through
    ("1d", "closed", "RECONCILED_NO_LIVE_TRACE", "MISSED"),  # boot found no order placed
    ("1d", "closed", "RECONCILED_UNFILLED", "MISSED"),  # boot found the order never filled
    ("1h", "open", None, "MISSED"),  # an hourly position is not this window's
])
async def test_ended_window_links_only_its_own_daily_position(
    test_db, timeframe, state, exit_reason, action
) -> None:
    prev = PREV_WINDOW.reference_ts
    await _record(market=PREV_MARKET)
    position_id = await _insert_position(strategy_id=SID, mode="paper", reference_ts=prev,
                                         timeframe=timeframe, state=state,
                                         exit_reason=exit_reason)
    await ledger.finalize_ended_windows(WINDOW.reference_ts, UNFINISHED)
    row = await ledger.get_decision(prev, SID, mode="paper")
    linked = position_id if action == "ENTERED" else None
    assert (row["action"], row["position_id"]) == (action, linked)
```

- [ ] **Step 2: Run to verify they fail**

Run: `PYTHON_DOTENV_DISABLED=1 python3 -m pytest tests/unit/test_daily_btc_ledger.py -q -p no:cacheprovider`
Expected: FAIL with `ImportError: cannot import name 'ledger'`

- [ ] **Step 3: Add the table** at the end of the `SCHEMA` literal in `db.py`:

```sql
-- Daily BTC strategies (Tsinghua-Kronos BTC 24h, 2026-09-16): one decision row per
-- (noon-ET window, strategy, mode), written for EVERY window whether or not it traded,
-- then settled from the two Binance 1-minute closes the market resolves on.
-- Keyed by the window's reference time and by mode, like hourly_strategy_context
-- (branch-review findings dst-fallback-slug-collision and decision-row-shared-across-modes).
CREATE TABLE IF NOT EXISTS btc_daily_market_decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  strategy_id TEXT NOT NULL,
  mode TEXT NOT NULL,
  window_slug TEXT NOT NULL,
  reference_ts INTEGER NOT NULL,
  settle_ts INTEGER NOT NULL,
  decision_side TEXT,
  decision_reason TEXT NOT NULL,
  signal_json TEXT,
  up_bid REAL,
  up_ask REAL,
  down_bid REAL,
  down_ask REAL,
  action TEXT NOT NULL,
  position_id INTEGER,
  reference_close REAL,
  settle_close REAL,
  outcome TEXT,
  settled_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_btc_daily_decisions_window_strategy_mode
  ON btc_daily_market_decisions(reference_ts, strategy_id, mode);
```

- [ ] **Step 4: Write the ledger** — `polymarket_bot/daily_btc/ledger.py`

```python
"""Daily BTC decision record: one row per (noon-ET window, strategy, mode), actions, settlement.

The operations and their signatures match polymarket_bot.hourly.ledger, so both engines
read and write their records the same way (Claude, 2026-09-16).
"""
from __future__ import annotations

import json
from typing import Any

import db as _db
from polymarket_bot.daily_btc.market import DayMarket

PENDING = "PENDING"
NO_SIGNAL = "NO_SIGNAL"
MISSED = "MISSED"
UNAVAILABLE = "UNAVAILABLE"
ENTERED = "ENTERED"


async def record_decision(
    *,
    strategy_id: str,
    mode: str,
    market: DayMarket,
    side: str | None,
    reason: str,
    signal: dict[str, Any],
    up_bid: float | None,
    up_ask: float | None,
    down_bid: float | None,
    down_ask: float | None,
    late: bool,
    available: bool,
) -> bool:
    """INSERT OR IGNORE the window's decision; True iff this call wrote the row.

    The action is UNAVAILABLE when the forecast could not run; else MISSED when the decision
    came after the entry deadline (the engine then skips the model, so there is no side);
    else NO_SIGNAL without a side; else PENDING (Claude, 2026-09-16).
    """
    if not available:
        action = UNAVAILABLE
    elif late:
        action = MISSED
    elif side is None:
        action = NO_SIGNAL
    else:
        action = PENDING
    async with _db.connect() as conn:
        cur = await conn.execute(
            """
            INSERT OR IGNORE INTO btc_daily_market_decisions(
              created_at, strategy_id, mode, window_slug, reference_ts, settle_ts,
              decision_side, decision_reason, signal_json, up_bid, up_ask, down_bid,
              down_ask, action
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (_db.utc_now_iso(), strategy_id, mode, market.slug, market.window.reference_ts,
             market.window.settle_ts, side, reason, json.dumps(signal, default=str),
             up_bid, up_ask, down_bid, down_ask, action),
        )
        await conn.commit()
        return cur.rowcount == 1


# Rows are looked up by the window's reference time and by mode, as in the hourly record
# (Claude, 2026-09-15, branch-review findings dst-fallback-slug-collision and
# decision-row-shared-across-modes).
async def get_decision(
    reference_ts: int, strategy_id: str, *, mode: str
) -> dict[str, Any] | None:
    async with _db.connect() as conn:
        cur = await conn.execute(
            "SELECT * FROM btc_daily_market_decisions "
            "WHERE reference_ts = ? AND strategy_id = ? AND mode = ?",
            (reference_ts, strategy_id, mode),
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def set_action(
    reference_ts: int,
    strategy_id: str,
    action: str,
    position_id: int | None = None,
    *,
    mode: str,
    expected_action: str | None = None,
) -> bool:
    """Set the row's action (and position, when given); True iff a row changed.

    ``expected_action`` applies the update only while the row still holds that action, so
    closing out an unfinished attempt never overwrites a result written meanwhile (Claude,
    2026-09-15, branch-review finding hourly-ambiguous-post-error-retried).
    """
    async with _db.connect() as conn:
        cur = await conn.execute(
            "UPDATE btc_daily_market_decisions SET action = ?, "
            "position_id = COALESCE(?, position_id) "
            "WHERE reference_ts = ? AND strategy_id = ? AND mode = ? "
            "AND (? IS NULL OR action = ?)",
            (action[:240], position_id, reference_ts, strategy_id, mode,
             expected_action, expected_action),
        )
        await conn.commit()
        return cur.rowcount > 0


async def finalize_ended_windows(
    current_reference_ts: int, unfinished_action: str
) -> dict[str, int]:
    """Close out decisions still PENDING or SUBMITTING in windows that already ended.

    The same rules as polymarket_bot.hourly.ledger.finalize_ended_hours (Claude, 2026-09-15,
    branch-review finding pending-row-never-finalized), applied to noon-ET windows (Claude,
    2026-09-16). A row left open when no tick ran before its window ended (Stop, crash,
    sleep, failing ticks, strategy switched off) otherwise stays open forever. The current
    window is left to the engine's entry step. For each ended window, per strategy and mode:
    - ENTERED with the position, when this strategy has a 1d position for that window in
      that mode that boot reconciliation did not close as never placed
      (RECONCILED_NO_LIVE_TRACE) or never filled (RECONCILED_UNFILLED);
    - otherwise an attempt that was started (SUBMITTING) becomes ``unfinished_action``
      (branch-review finding hourly-ambiguous-post-error-retried: never assume it failed);
    - otherwise MISSED.
    """
    position_for_row = (
        "SELECT p.position_id FROM paper_positions p "
        "WHERE p.window_start_ts = btc_daily_market_decisions.reference_ts "
        "AND p.strategy_id = btc_daily_market_decisions.strategy_id "
        "AND p.mode = btc_daily_market_decisions.mode "
        "AND p.market_timeframe = '1d' "
        "AND COALESCE(p.exit_reason, '') "
        "NOT IN ('RECONCILED_NO_LIVE_TRACE', 'RECONCILED_UNFILLED')"
    )
    async with _db.connect() as conn:
        entered = await conn.execute(
            "UPDATE btc_daily_market_decisions SET action = 'ENTERED', "
            f"position_id = ({position_for_row} ORDER BY p.position_id LIMIT 1) "
            "WHERE action IN ('PENDING', 'SUBMITTING') AND reference_ts < ? "
            f"AND EXISTS ({position_for_row})",
            (current_reference_ts,),
        )
        unfinished = await conn.execute(
            "UPDATE btc_daily_market_decisions SET action = ? "
            "WHERE action = 'SUBMITTING' AND reference_ts < ?",
            (unfinished_action[:240], current_reference_ts),
        )
        missed = await conn.execute(
            "UPDATE btc_daily_market_decisions SET action = 'MISSED' "
            "WHERE action = 'PENDING' AND reference_ts < ?",
            (current_reference_ts,),
        )
        await conn.commit()
        return {
            "entered": max(entered.rowcount, 0),
            "unfinished": max(unfinished.rowcount, 0),
            "missed": max(missed.rowcount, 0),
        }


async def unsettled_windows(now_ts: int, grace_s: int) -> list[tuple[int, int]]:
    """Distinct (reference_ts, settle_ts) of unsettled rows whose settlement minute + grace passed."""
    async with _db.connect() as conn:
        cur = await conn.execute(
            "SELECT DISTINCT reference_ts, settle_ts FROM btc_daily_market_decisions "
            "WHERE settled_at IS NULL AND settle_ts + ? <= ? ORDER BY reference_ts",
            (grace_s, now_ts),
        )
        return [(int(r["reference_ts"]), int(r["settle_ts"])) for r in await cur.fetchall()]


async def settle_window(
    reference_ts: int, reference_close: float, settle_close: float, result: str
) -> None:
    """Settle every row of one window; ``result`` is "Up", "Down" or "tie"."""
    async with _db.connect() as conn:
        await conn.execute(
            "UPDATE btc_daily_market_decisions SET reference_close = ?, settle_close = ?, "
            "outcome = ?, settled_at = ? WHERE reference_ts = ? AND settled_at IS NULL",
            (reference_close, settle_close, result, _db.utc_now_iso(), reference_ts),
        )
        await conn.commit()
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `PYTHON_DOTENV_DISABLED=1 python3 -m pytest tests/unit/test_daily_btc_ledger.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add db.py polymarket_bot/daily_btc/ledger.py tests/unit/test_daily_btc_ledger.py
git commit -m "feat(daily_btc): per-window, per-mode decision record for daily BTC strategies"
```

---

### Task 5: Shared strategy-slot entry (move out of the hourly engine; starts after Task 0 Step 2)

**Why:** the hourly engine's entry state machine is not hourly-specific: one order attempt per window, the SUBMITTING/UNCERTAIN records, and linking a still-open same-window position after a restart. The daily engine must behave identically. The hourly session agreed on 2026-09-16 that this branch does the move after its merge lands.

**Adapted to the merged hourly code (Claude, 2026-09-16, while building this task):**
- **Check order.** `advance` keeps the merged order, which differs from the order first planned here:
  - The still-open same-window link runs first, for PENDING and for SUBMITTING, with `expected_action` set to the current action. In live it applies only while `executor.slot_executor(strategy_id).tracks_position` is true.
  - SUBMITTING then notifies the operator and records the unfinished attempt.
- **Kill switch.** Held entries record nothing: the decision stays PENDING until the deadline records MISSED. A shared test pins this.
- **Same-window lookup.** The merged hourly code has its own query for this, `_same_hour_open_position_id`. It moved as `_same_window_open_position_id(window, mode)`: open rows of this strategy, timeframe, window start and mode. `open_row_for` returns the row, as planned, and is only the slot-held check in `enter`.
- **`advance` takes `mode`.** The merged `open_entries` takes it from the tick and uses it for the same-window lookup.
- **`SlotWindow` has no `window_slug`.** The moved code reads the slug from the snapshot, as the table below requires, so that field was never read.
- **Notices.** The two operator texts said "No retry this hour" and now say "No retry for this market", because the daily engine sends them too. Comments say "window" instead of "hour". Log events are `strategy_slot_entry.*` and carry `timeframe`.
- **Hourly adapter.** The merged `ledger.set_action` returns None, so `_HourlyDecision.set_action` awaits it and returns True. No `_slot` helper and no `_enter` wrapper were needed: no hourly test uses them.
- **Hourly tests.** One patch target was redirected, with no expectation changed: `test_paper_attempt_that_raised_is_not_retried_and_ends_uncertain` patches `slot_entry.insert_row` instead of `engine._insert_row`. No test patched `engine.notify`.
- **Shared tests.** They add cases for the merged behaviour:
  - the SUBMITTING link;
  - the live `tracks_position` check;
  - a slot held by an earlier window or another timeframe;
  - the kill switch;
  - finished actions left alone;
  - a live entry through the slot.

**Files:**
- Create: `polymarket_bot/strategy_slot_entry.py`
- Modify: `polymarket_bot/hourly/engine.py` (thin adapter; behaviour unchanged)
- Test: `tests/unit/test_strategy_slot_entry.py`; every existing hourly test stays unchanged and green

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) SlotWindow(strategy_id: str, timeframe: str, window_start_ts: int)`
  - `class DecisionHandle(Protocol)`:
    - `async def reason(self) -> str | None`
    - `async def set_action(self, action: str, position_id: int | None = None, *, expected_action: str | None = None) -> bool`
  - Constants, moved from the hourly module and re-exported there under their old names: `SUBMITTING`, `UNCERTAIN_PREFIX`, `UNFINISHED_ATTEMPT`, `PENDING`, `ENTERED`, `MISSED`.
  - `async def open_row_for(strategy_id: str, mode: str) -> dict | None`: the strategy's open row in this mode, or None.
  - `async def _same_window_open_position_id(window: SlotWindow, mode: str) -> int | None` (private): the still-open position of this strategy for this window and mode.
  - `async def insert_row(snapshot, window: SlotWindow, *, side, price, notional, shares, reason, mode) -> int`
  - `async def enter(snapshot, window: SlotWindow, side: str, decision: DecisionHandle) -> None`
  - `async def advance(snapshot, window: SlotWindow, *, action: str, side: str | None, now: int, deadline_s: int, allow_entries: bool, mode: str, decision: DecisionHandle) -> None`

- [ ] **Step 1: Read the merged hourly entry code**

```bash
cd /Users/zayankhan/projects/polymarket-crypto/.claude/worktrees/tsinghua-kronos-btc-24h
grep -n "^async def \|^def \|^SUBMITTING\|^UNCERTAIN_PREFIX\|^UNFINISHED_ATTEMPT" polymarket_bot/hourly/engine.py polymarket_bot/hourly/ledger.py
sed -n '/^async def _open_row_for/,/^async def settle_due/p' polymarket_bot/hourly/engine.py
```

Note the exact current bodies of `_open_row_for`, `_insert_row`, `_enter` and the per-strategy loop body of `open_entries`. They include every merged fix: kill-switch record parity, thin-book sizing, the attempt marker, the untraced-post notice and the still-open same-hour link.

- [ ] **Step 2: Write the failing shared-module test** — `tests/unit/test_strategy_slot_entry.py`

```python
"""Shared strategy-slot entry: the hourly entry steps work for a daily window and any decision record."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

import db as _db
from polymarket_bot import paper
from polymarket_bot import strategy_slot_entry as entry
from polymarket_exec.execution.live import LiveOrderResult

START = 1_789_574_400  # 2026-09-16 16:00 UTC: noon ET, the start of the Sep 17 daily window
SID = "tsinghua_kronos_btc_24h"
SLUG = "bitcoin-up-or-down-on-september-17-2026"
WINDOW = entry.SlotWindow(SID, "1d", START)
UNFINISHED = "UNCERTAIN:entry attempt did not finish"


class _Decision:
    """A decision record that keeps every action write as (action, position_id, expected_action)."""

    def __init__(self) -> None:
        self.actions: list[tuple[str, int | None, str | None]] = []

    async def reason(self) -> str | None:
        return "enter Up: test"

    async def set_action(self, action, position_id=None, *, expected_action=None) -> bool:
        self.actions.append((action, position_id, expected_action))
        return True


def _snapshot() -> paper.PaperSnapshot:
    return paper.PaperSnapshot(
        created_at="2026-09-16T16:00:10+00:00", window_slug=SLUG,
        market_question="q", remaining_seconds=86_000, spot_price=100.0, reference_price=100.0,
        sigma_per_second=0.0, market_up_price=0.52, market_down_price=0.49, fair_up_prob=0.5,
        edge=0.0, signal_side=None, confidence=0.0, notional_usd=0.0, reason="",
        feed_source="quotes=clob", up_token_id="U", down_token_id="D",
        up_best_bid=0.51, up_best_ask=0.52, up_bid_size=100.0, up_ask_size=100.0,
        down_best_bid=0.48, down_best_ask=0.49, down_bid_size=100.0, down_ask_size=100.0,
    )


@pytest_asyncio.fixture
async def test_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "t.db")
    await _db.init_db()
    monkeypatch.setattr(paper, "_live_executor", None)
    monkeypatch.setattr(paper, "_risk_gate", None)
    monkeypatch.setattr(entry, "notify", AsyncMock())
    return _db


def _live_account(monkeypatch: pytest.MonkeyPatch) -> tuple[MagicMock, MagicMock]:
    slot = MagicMock()
    slot.tracks_position = False
    slot.resync_flat = AsyncMock(return_value=False)
    slot.submit_entry = AsyncMock(return_value=LiveOrderResult(
        ok=True, status="SUBMITTED", order_id="0xE", price=0.52, size=5.0, notional_usd=2.6))
    account = MagicMock()
    account.slot_executor = MagicMock(return_value=slot)
    monkeypatch.setattr(paper, "_live_executor", account)
    return account, slot


async def _advance(action: str, *, now: int, decision: _Decision, mode: str = "paper",
                   allow_entries: bool = True) -> None:
    await entry.advance(_snapshot(), WINDOW, action=action, side="Up", now=now, deadline_s=300,
                        allow_entries=allow_entries, mode=mode, decision=decision)


async def _insert_row(*, mode: str = "paper", timeframe: str = "1d", start: int = START) -> int:
    return await entry.insert_row(_snapshot(), entry.SlotWindow(SID, timeframe, start),
                                  side="Up", price=0.52, notional=2.6, shares=5.0, reason="r",
                                  mode=mode)


@pytest.mark.asyncio
async def test_paper_entry_for_a_daily_window_records_the_attempt_then_entered(test_db) -> None:
    decision = _Decision()
    await _advance(entry.PENDING, now=START + 10, decision=decision)
    row = await entry.open_row_for(SID, "paper")
    assert decision.actions == [(entry.SUBMITTING, None, None),
                                (entry.ENTERED, row["position_id"], None)]
    assert (row["market_timeframe"], row["window_start_ts"], row["window_slug"], row["side"],
            row["entry_price"], row["shares"], row["entry_reason"]) == (
        "1d", START, SLUG, "Up", 0.52, 5.0, "enter Up: test")


@pytest.mark.asyncio
async def test_past_deadline_is_missed_and_submitting_never_retries(test_db) -> None:
    late = _Decision()
    await _advance(entry.PENDING, now=START + 301, decision=late)
    assert late.actions == [(entry.MISSED, None, None)]
    stuck = _Decision()
    await _advance(entry.SUBMITTING, now=START + 20, decision=stuck)
    assert stuck.actions == [(UNFINISHED, None, entry.SUBMITTING)]
    assert entry.UNFINISHED_ATTEMPT == UNFINISHED
    entry.notify.assert_awaited_once()
    assert entry.notify.await_args.args[0] == "entry_attempt_unfinished"
    assert await entry.open_row_for(SID, "paper") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["PENDING", "SUBMITTING"])
async def test_still_open_same_window_position_is_linked_not_missed(test_db, action) -> None:
    position_id = await _insert_row()
    decision = _Decision()
    await _advance(action, now=START + 400, decision=decision)
    assert decision.actions == [(entry.ENTERED, position_id, action)]
    entry.notify.assert_not_awaited()


# Claude, 2026-09-15, branch-review finding crash-after-entry-marks-missed: live links the
# window only while the strategy's slot tracks the entry.
@pytest.mark.asyncio
@pytest.mark.parametrize("tracks", [True, False])
async def test_live_link_needs_the_strategy_slot_to_track_the_entry(
    test_db, monkeypatch, tracks
) -> None:
    account, slot = _live_account(monkeypatch)
    slot.tracks_position = tracks
    position_id = await _insert_row(mode="live")
    decision = _Decision()
    await _advance(entry.SUBMITTING, now=START + 20, decision=decision, mode="live")
    account.slot_executor.assert_called_with(SID)
    if tracks:
        assert decision.actions == [(entry.ENTERED, position_id, entry.SUBMITTING)]
    else:
        assert decision.actions == [(UNFINISHED, None, entry.SUBMITTING)]
    slot.submit_entry.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(("timeframe", "start"), [("1d", START - 86_400), ("1h", START)])
async def test_open_row_of_another_window_holds_the_slot_and_records_nothing(
    test_db, timeframe, start
) -> None:
    await _insert_row(timeframe=timeframe, start=start)
    decision = _Decision()
    await _advance(entry.PENDING, now=START + 10, decision=decision)
    assert decision.actions == []
    async with _db.connect() as conn:
        cur = await conn.execute("SELECT COUNT(*) AS n FROM paper_positions")
        assert (await cur.fetchone())["n"] == 1


# Same record as the hourly kill-switch tests: entries held leave the window PENDING, and
# the deadline then records MISSED.
@pytest.mark.asyncio
async def test_entries_held_leave_the_window_pending_until_the_deadline(test_db) -> None:
    held = _Decision()
    await _advance(entry.PENDING, now=START + 10, decision=held, allow_entries=False)
    assert held.actions == []
    await _advance(entry.PENDING, now=START + 301, decision=held, allow_entries=False)
    assert held.actions == [(entry.MISSED, None, None)]
    assert await entry.open_row_for(SID, "paper") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("action", [
    "NO_SIGNAL", "UNAVAILABLE", "MISSED", "ENTERED", "BLOCKED:x", "UNCERTAIN:x"])
async def test_finished_actions_are_left_alone(test_db, action) -> None:
    decision = _Decision()
    await _advance(action, now=START + 10, decision=decision)
    assert decision.actions == []
    assert await entry.open_row_for(SID, "paper") is None


@pytest.mark.asyncio
async def test_live_entry_for_a_daily_window_goes_through_the_strategy_slot(
    test_db, monkeypatch
) -> None:
    _, slot = _live_account(monkeypatch)
    decision = _Decision()
    await _advance(entry.PENDING, now=START + 10, decision=decision, mode="live")
    slot.resync_flat.assert_awaited_once()
    slot.submit_entry.assert_awaited_once_with(
        token_id="U", side_price=0.52, notional_usd=pytest.approx(2.6), window_slug=SLUG)
    row = await entry.open_row_for(SID, "live")
    assert (row["market_timeframe"], row["window_start_ts"], row["mode"]) == ("1d", START, "live")
    assert decision.actions == [(entry.SUBMITTING, None, None),
                                (entry.ENTERED, row["position_id"], None)]
```

The merged hourly code records nothing while entries are held: the decision stays PENDING until the deadline records MISSED. `test_entries_held_leave_the_window_pending_until_the_deadline` pins the same record for a `1d` window.

- [ ] **Step 3: Run to verify it fails**

Run: `PYTHON_DOTENV_DISABLED=1 python3 -m pytest tests/unit/test_strategy_slot_entry.py -q -p no:cacheprovider`
Expected: FAIL with `ImportError: cannot import name 'strategy_slot_entry'`

- [ ] **Step 4: Create `polymarket_bot/strategy_slot_entry.py` by moving code, not rewriting it**

Module header and the new types:

```python
"""Shared entry step for strategy position slots (hourly and daily BTC strategies), paper and live.

Moved from polymarket_bot/hourly/engine.py (Claude, 2026-09-16, agreed with the hourly
session) so every strategy engine uses one implementation. The bodies are the hourly ones;
only the window and the decision record are now arguments. Behaviour sources:
- one open position per strategy: Zayan (operator), 2026-09-14;
- one order attempt per window, the SUBMITTING / UNCERTAIN records and the untraced-post
  notice: Claude, 2026-09-15, branch-review findings hourly-ambiguous-post-error-retried and
  hourly-reentry-after-untraced-post;
- a still-open same-window position is linked as ENTERED, in live only while the strategy's
  slot tracks the entry: Claude, 2026-09-15, branch-review finding
  crash-after-entry-marks-missed; open rows only, in both modes: review by Claude session
  polymarket-crypto-95, 2026-09-16;
- sizing on a thin top-of-book ask: Claude, 2026-09-15, branch-review finding
  thin-top-sizing-paper-vs-live.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from db import journal_live_order, notify
from logging_setup import get_logger
from polymarket_exec.execution.gate import EntryRequest
from polymarket_exec.execution.live import DEFAULT_MIN_ORDER_SIZE

if TYPE_CHECKING:
    from polymarket_bot.paper import PaperSnapshot

log = get_logger("strategy_slot_entry")

# Claude, 2026-09-15, branch-review finding hourly-ambiguous-post-error-retried:
# decision-record actions for an entry attempt. SUBMITTING is written before any order
# goes out; UNCERTAIN ends a window whose order may have reached the venue.
SUBMITTING = "SUBMITTING"
UNCERTAIN_PREFIX = "UNCERTAIN:"
UNFINISHED_ATTEMPT = f"{UNCERTAIN_PREFIX}entry attempt did not finish"
# The other decision-record actions this step reads or writes (same words in every ledger).
PENDING = "PENDING"
ENTERED = "ENTERED"
MISSED = "MISSED"


@dataclass(frozen=True)
class SlotWindow:
    """The market window a strategy's entry is for, as paper_positions records it."""

    strategy_id: str
    timeframe: str  # paper_positions.market_timeframe, e.g. "1h" or "1d"
    window_start_ts: int  # paper_positions.window_start_ts: the window's start, UTC seconds


class DecisionHandle(Protocol):
    """One strategy's decision record for one window and mode (each engine adapts its ledger)."""

    async def reason(self) -> str | None: ...

    async def set_action(
        self, action: str, position_id: int | None = None, *, expected_action: str | None = None
    ) -> bool: ...
```

Then move, keeping each body verbatim apart from the substitutions in this table:

| From `polymarket_bot/hourly/engine.py` (or `ledger.py`) | To `strategy_slot_entry.py` | Substitutions |
|---|---|---|
| The `SUBMITTING`, `UNCERTAIN_PREFIX`, `UNFINISHED_ATTEMPT` constants, plus `PENDING = "PENDING"`, `ENTERED = "ENTERED"`, `MISSED = "MISSED"` | same names | none |
| `_open_row_for(strategy_id, mode)` | `open_row_for(strategy_id, mode) -> dict \| None` | return the row dict (`SELECT *` … `ORDER BY position_id DESC LIMIT 1`) instead of a bool; callers test `is not None` |
| `_same_hour_open_position_id(strategy_id, start, mode)` | `_same_window_open_position_id(window, mode)` | `strategy_id` → `window.strategy_id`; `TIMEFRAME` → `window.timeframe`; `start` → `window.window_start_ts`; "hour" → "window" in the docstring |
| `_insert_row(snapshot, *, strategy_id, side, price, notional, shares, start, reason, mode)` | `insert_row(snapshot, window, *, side, price, notional, shares, reason, mode)` | `strategy_id` → `window.strategy_id`; `start` → `window.window_start_ts`; the hourly `TIMEFRAME` constant → `window.timeframe` |
| `_enter(snapshot, strategy_id, side, start)` | `enter(snapshot, window, side, decision)` | Every `ledger.get_decision(...)` read of the reason → `await decision.reason()`. Every `ledger.set_action(<key>, strategy_id, ACTION, <pos>, mode=..., expected_action=...)` → `await decision.set_action(ACTION, <pos>, expected_action=...)`. `snapshot.window_slug` stays. Log event prefixes `hourly_engine.` → `strategy_slot_entry.`, with `timeframe=window.timeframe` added to each log call. The notify texts stay word for word, except "No retry this hour" → "No retry for this market". A local `strategy_id = window.strategy_id` keeps the rest of the body unchanged. |
| The per-strategy body of `open_entries` (everything inside `for sid, knob in STRATEGIES:` after the knob check and the row read) | `advance(snapshot, window, *, action, side, now, deadline_s, allow_entries, mode, decision)` | `row["action"]` → `action`; `row["decision_side"]` → `side`; `sid` → `window.strategy_id`; `start` → `window.window_start_ts`; `deadline` → `deadline_s`; `continue` → `return`; `_same_hour_open_position_id(sid, start, mode)` → `_same_window_open_position_id(window, mode)`; `_enter(...)` → `enter(snapshot, window, str(side), decision)`; the ledger writes → `decision.set_action(...)`; "No retry this hour" → "No retry for this market"; `P._live_executor` is read inside `advance` |

`advance` must keep this order, which is the hourly order after the merged fixes:
1. `PENDING` or `SUBMITTING`, and `_same_window_open_position_id` finds this window's open row. If the run is paper, or the live slot's `tracks_position` is true, record `ENTERED` with its position id (expected: the current action) and return.
2. `SUBMITTING`: notify the operator, then close out as `UNFINISHED_ATTEMPT` (expected `SUBMITTING`).
3. If the action is not `PENDING`: return.
4. Past the deadline: `MISSED`.
5. Entries held (kill switch): record nothing; the decision stays `PENDING`.
6. Otherwise: `enter`.

- [ ] **Step 5: Turn the hourly engine into a thin adapter**

In `polymarket_bot/hourly/engine.py`, delete the moved functions and constants. `journal_live_order`, `EntryRequest` and `DEFAULT_MIN_ORDER_SIZE` are no longer imported there. Then add the import and the old names:

```python
from polymarket_bot import strategy_slot_entry as slot_entry

# The entry-attempt actions moved to strategy_slot_entry (Claude, 2026-09-16); this module
# keeps its old names for them.
from polymarket_bot.strategy_slot_entry import (  # noqa: F401 - old import path
    SUBMITTING,
    UNCERTAIN_PREFIX,
    UNFINISHED_ATTEMPT,
)
```

Replace `open_entries` with the decision adapter and the loop that calls `advance`. The knob check and the `row is None` skip stay:

```python
class _HourlyDecision:
    """The hourly decision record for one (hour, strategy, mode) as a slot_entry.DecisionHandle."""

    def __init__(self, start: int, strategy_id: str, mode: str) -> None:
        self.start, self.strategy_id, self.mode = start, strategy_id, mode

    async def reason(self) -> str | None:
        row = await ledger.get_decision(self.start, self.strategy_id, mode=self.mode)
        return (row or {}).get("decision_reason")

    async def set_action(
        self, action: str, position_id: int | None = None, *, expected_action: str | None = None
    ) -> bool:
        # ledger.set_action returns None; the write either happened or raised.
        await ledger.set_action(self.start, self.strategy_id, action, position_id,
                                mode=self.mode, expected_action=expected_action)
        return True


async def open_entries(
    snapshot: PaperSnapshot, now: int, *, allow_entries: bool, mode: str
) -> None:
    start = market.hour_start(now)
    deadline = _knobs.cached("hourly_entry_deadline_seconds")
    for sid, knob in STRATEGIES:
        if not _knobs.cached(knob):
            continue
        # Only this mode's row (Claude, 2026-09-15, branch-review finding
        # decision-row-shared-across-modes).
        row = await ledger.get_decision(start, sid, mode=mode)
        if row is None:
            continue
        await slot_entry.advance(
            snapshot, slot_entry.SlotWindow(sid, TIMEFRAME, start), action=row["action"],
            side=row["decision_side"], now=now, deadline_s=deadline,
            allow_entries=allow_entries, mode=mode, decision=_HourlyDecision(start, sid, mode),
        )
```

- The merged `ledger.set_action` returns None, so the adapter returns `True` after awaiting it. The adapter is the only place allowed to differ from the table above.
- No hourly test imports `_enter`, so it is not kept as a wrapper.
- Point hourly tests at the moved code only where they patch it, and say so in the commit message. Only `engine._insert_row` needed this; it became `slot_entry.insert_row`.

- [ ] **Step 6: Run the shared test and every hourly test**

Run: `PYTHON_DOTENV_DISABLED=1 python3 -m pytest tests/unit/test_strategy_slot_entry.py tests/unit/test_hourly_engine.py tests/unit/test_hourly_loop.py tests/unit/test_hourly_ledger.py tests/unit/test_live_wiring.py -q -p no:cacheprovider`
Expected: PASS, with no hourly test expectation changed. If an hourly assertion fails, the move changed behaviour: fix the move, never the test.

- [ ] **Step 7: Full unit suite, ruff, commit**

```bash
PYTHON_DOTENV_DISABLED=1 python3 -m pytest tests/unit -q -p no:cacheprovider 2>&1 | tail -3
python3 -m ruff check polymarket_exec/ polymarket_bot/ tests/ tools/
git add polymarket_bot/strategy_slot_entry.py polymarket_bot/hourly/engine.py tests/unit/test_strategy_slot_entry.py tests/unit/test_hourly_engine.py
git commit -m "refactor(strategies): move the hourly entry state machine to strategy_slot_entry for reuse"
```

---

### Task 6: Live executor — daily rows and 50-50 settlement (starts after Task 0 Step 2)

**Why:**
- **Daily rows look resolved too early.** A daily slug ends in the year (`...-september-17-2026`), so `_window_resolved` would parse `2026` as a start second and treat every daily row as long resolved. On restart, boot reconciliation could then close a live daily row whose order the CLOB has pruned, even though its window is still open.
- **Ties.** A tie pays 0.50 per share, and `record_settlement` only knows win or lose.

**Files:**
- Modify: `polymarket_exec/execution/live.py`
- Test: append to `tests/unit/test_live_executor.py`

**Interfaces:**
- Produces:
  - `_DAILY_WINDOW_MAX_SECONDS = 25 * 3600`
  - `_row_window_resolved(row, *, now=None)`: rows with `market_timeframe == "1d"` and a `window_start_ts` count as resolved at `window_start_ts + 25 h + grace`.
  - `LiveExecutor.record_settlement(won: bool, window_slug: str, *, payout: float | None = None) -> LiveOrderResult`. `payout` defaults to 1.0/0.0 from `won`. `0.5` books `held × (0.5 − entry) − entry fee`.

- [ ] **Step 1: Write the failing tests** (append to `tests/unit/test_live_executor.py`, and add nothing to its imports beyond what is already imported there, including `_row_window_resolved`)

```python
DAILY_SLUG = "bitcoin-up-or-down-on-september-17-2026"


def test_daily_rows_resolve_25_hours_after_their_noon_start() -> None:
    row = {"window_slug": DAILY_SLUG, "market_timeframe": "1d", "window_start_ts": 1_000_000}
    assert _row_window_resolved(row, now=1_000_000 + 25 * 3600 + 59) is False
    assert _row_window_resolved(row, now=1_000_000 + 25 * 3600 + 60) is True
    # Without the daily branch the slug's trailing "2026" parsed as a start second.
    assert _row_window_resolved(row, now=1_000_000 + 3600) is False


@pytest.mark.asyncio
async def test_settlement_tie_pays_half_net_of_the_entry_fee(journal_db, tmp_path: Path) -> None:
    executor = await _taker_entry_executor(tmp_path)

    result = await executor.record_settlement(False, DAILY_SLUG, payout=0.5)

    expected = 5.09 * (0.5 - 0.51) - _FEE_1768
    assert result.ok and result.status == "SETTLED" and result.price == 0.5
    assert result.notional_usd == pytest.approx(expected, abs=1e-4)
    assert executor.daily_realized_pnl == pytest.approx(expected, abs=1e-4)
    settle = [r for r in await _journal_rows(journal_db) if r["intent"] == "SETTLEMENT"][-1]
    assert settle["price"] == 0.5 and settle["error"] is None
    assert '"payout_per_share": 0.5' in settle["details_json"]


@pytest.mark.asyncio
async def test_boot_keeps_an_unresolved_daily_row_whose_order_was_pruned(
    journal_db, tmp_path: Path
) -> None:
    start = int(time.time()) - 3600  # the daily window opened an hour ago
    async with journal_db.connect() as conn:
        cur = await conn.execute(
            "INSERT INTO paper_positions(opened_at, window_slug, side, state, entry_price,"
            " notional_usd, shares, mode, strategy_id, market_timeframe, window_start_ts)"
            " VALUES ('2026-09-16T16:00:10+00:00', ?, 'Up', 'open', 0.51, 2.6, 5.09, 'live',"
            " 'tsinghua_kronos_btc_24h', '1d', ?)",
            (DAILY_SLUG, start),
        )
        position_id = int(cur.lastrowid)
        await conn.commit()
    await journal_db.journal_live_order(
        intent="ENTRY", side="BUY", status="SUBMITTED", window_slug=DAILY_SLUG,
        token_id=UP_TOKEN, price=0.51, size=5.09, clob_order_id="0xPRUNED",
        details={"response": {"status": "live"}}, strategy_id="tsinghua_kronos_btc_24h",
    )
    client = _mock_client()
    client.get_order.return_value = None
    with pytest.raises(LiveBootRefused, match="has not resolved yet"):
        await _executor(client, tmp_path).start()
    async with journal_db.connect() as conn:
        cur = await conn.execute("SELECT state FROM paper_positions WHERE position_id = ?",
                                 (position_id,))
        assert (await cur.fetchone())["state"] == "open"
```

The third test pins today's safe behaviour for an unresolved window with unknowable fill state: boot refuses rather than closing the row. If the merged reconcile code words that refusal differently, match its current message.

- [ ] **Step 2: Run to verify they fail**

Run: `PYTHON_DOTENV_DISABLED=1 python3 -m pytest tests/unit/test_live_executor.py -q -p no:cacheprovider -k "daily or tie"`
Expected: FAIL. The first test gets `True` for the "+1 h" case. The tie test fails with `TypeError: ... unexpected keyword argument 'payout'`. The boot test fails because the row is closed as stale-resolved.

- [ ] **Step 3: Implement**

Below `_HOURLY_WINDOW_SECONDS = 3600`:

```python
# A noon-ET daily window is 23, 24 or 25 hours (daylight-saving days); the longest bounds it.
_DAILY_WINDOW_MAX_SECONDS = 25 * 3600
```

In `_row_window_resolved`, before the final `return _window_resolved(...)`:

```python
    if row.get("market_timeframe") == "1d" and row.get("window_start_ts") is not None:
        # Daily slugs end in the year, not a start second (Claude, 2026-09-16,
        # Tsinghua-Kronos BTC 24h): bound the window by its longest possible length.
        now_s = time.time() if now is None else now
        return now_s >= (
            int(row["window_start_ts"]) + _DAILY_WINDOW_MAX_SECONDS
            + _WINDOW_RESOLVE_GRACE_SECONDS
        )
```

In `LiveExecutor.record_settlement`:
- change the signature to `async def record_settlement(self, won: bool, window_slug: str, *, payout: float | None = None) -> LiveOrderResult:`;
- replace `payout = 1.0 if won else 0.0` with:

```python
        # Daily BTC markets pay 0.50 per share on an exact tie (market rules, 2026-09-16).
        payout = (1.0 if won else 0.0) if payout is None else payout
```

- in the SETTLEMENT journal call, replace `error=None if won else "resolved against position; tokens worthless"` with `error=None if payout > 0 else "resolved against position; tokens worthless"`, and `details={"entry_taker_fee_usd": fee}` with `details={"entry_taker_fee_usd": fee, "payout_per_share": payout}`;
- add `payout=payout` to the `log.info("live_executor.settled", ...)` call.

- [ ] **Step 4: Run the live tests**

Run: `PYTHON_DOTENV_DISABLED=1 python3 -m pytest tests/unit/test_live_executor.py tests/unit/test_live_wiring.py tests/unit/test_reconcile_ledger.py -q -p no:cacheprovider`
Expected: PASS (all earlier live tests unchanged)

- [ ] **Step 5: Commit**

```bash
git add polymarket_exec/execution/live.py tests/unit/test_live_executor.py
git commit -m "feat(live): daily BTC rows resolve after their noon-ET window; 50-50 settlement payout"
```

---

### Task 7: Daily BTC engine and knobs (starts after Tasks 4–6)

**Updated while building Tasks 4–6 (Claude, 2026-09-16).** The code below already uses what those tasks built:
- The daily ledger takes `mode=` as a keyword, as the hourly ledger does.
- The planned `_finalize_past_windows` (current mode only, open rows only) is gone. `settle_due` starts with `ledger.finalize_ended_windows(...)`, the hourly `finalize_ended_hours` rules, and sends one operator notice when an earlier window's attempt did not finish. `open_entries` no longer finalizes.
- `strategy_slot_entry.SlotWindow` has three fields (strategy, timeframe, window start). `advance` takes `mode=`.
- Entry notices now come from `strategy_slot_entry.notify`. The fixture's `engine.notify` mock sees only this engine's own notices (forecast unavailable, unfinished attempts of earlier windows). Entry notices go to the test database.

**Files:**
- Create: `polymarket_bot/daily_btc/engine.py`
- Modify: `polymarket_bot/runtime_knobs.py` (three knobs)
- Test: `tests/unit/test_daily_btc_engine.py`

**Interfaces:**
- Consumes:
  - Task 0: `market.current_window`, `market.discover`, `market.fetch_minute_close`, `market.outcome`, `market.payout`
  - Task 2: `forecast_input.fetch_forecast_candles`
  - Task 3: `tsinghua_kronos_btc_24h.request_for`, `decide`, `STRATEGY_ID`, `DISPLAY_NAME`
  - Task 4: `ledger.*`
  - Task 5: `strategy_slot_entry.advance`, `SlotWindow`, `UNFINISHED_ATTEMPT`
  - Task 6: `slot.record_settlement(..., payout=)`
  - From `polymarket_bot.paper`, imported lazily inside functions: `PaperSnapshot`, `_now`, `_fetch_clob_book`, `_log_tick`, `_close_position`, `_live_executor`, `connect`
- Produces:
  - `TIMEFRAME = "1d"`; `SETTLE_GRACE_S = 120`; `STRATEGIES`; `reset_caches()`
  - `async market_for(client, window) -> DayMarket`
  - `async build_snapshot(client, now: int | None = None) -> PaperSnapshot`
  - `async decide_window(client, snapshot, market, now) -> dict[str, dict]`
  - `async open_entries(snapshot, market, now, *, allow_entries: bool) -> None`
  - `async settle_due(client, snapshot, now: int) -> None` (needs no Gamma read)
  - `async tick(client, *, allow_entries: bool = True) -> PaperSnapshot`
  - Knobs:
    - `daily_btc_tsinghua_kronos_btc_24h_enabled` (bool, default True)
    - `daily_btc_tsinghua_kronos_btc_24h_edge_threshold` (float, default 0.05, range 0–0.5)
    - `daily_btc_entry_deadline_seconds` (int, default 300, range 30–3600, unit s)
    - All three in group "Daily BTC".

- [ ] **Step 1: Add the knobs** (append to `KNOBS` in `polymarket_bot/runtime_knobs.py`)

```python
    # --- Daily BTC strategies (polymarket_bot/daily_btc/engine.py) ----------
    # Tsinghua-Kronos BTC 24h: name Zayan (operator) 2026-09-16; defaults Claude 2026-09-16.
    "daily_btc_tsinghua_kronos_btc_24h_enabled": Knob(
        "runtime.daily_btc.tsinghua_kronos_btc_24h_enabled", True, "bool",
        "Tsinghua-Kronos BTC 24h enabled", group="Daily BTC",
    ),
    "daily_btc_tsinghua_kronos_btc_24h_edge_threshold": Knob(
        "runtime.daily_btc.tsinghua_kronos_btc_24h_edge_threshold", 0.05, "float",
        "Tsinghua-Kronos BTC 24h: minimum forecast-minus-price edge", 0.0, 0.5,
        group="Daily BTC",
    ),
    "daily_btc_entry_deadline_seconds": Knob(
        "runtime.daily_btc.entry_deadline_seconds", 300, "int",
        "Daily BTC entry deadline after the noon-ET window opens", 30, 3600, unit="s",
        group="Daily BTC",
    ),
```

- [ ] **Step 2: Write the failing engine tests** — `tests/unit/test_daily_btc_engine.py`

```python
"""Daily BTC engine: noon-ET decision, one entry per window, Binance 1-minute settlement, live slot."""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import pytest_asyncio

import config as _config
import db as _db
from polymarket_bot import paper
from polymarket_bot import runtime_knobs as _knobs
from polymarket_bot.daily_btc import engine, ledger
from polymarket_bot.daily_btc import market as dbm
from polymarket_bot.hourly import market as hm
from polymarket_bot.kronos_forecast import client as kronos
from polymarket_exec.execution.live import LiveOrderResult

WINDOW = dbm.window_for(date(2026, 9, 17))  # 2026-09-16 16:00 UTC -> 2026-09-17 16:00 UTC
S, E = WINDOW.reference_ts, WINDOW.settle_ts
SID = "tsinghua_kronos_btc_24h"
_MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august",
           "september", "october", "november", "december"]


def _kline(open_s: int, close: float = 100.5, *, seconds: int = 3600) -> list:
    return [open_s * 1000, "100", "101", "99", str(close), "10", open_s * 1000 + seconds * 1000 - 1,
            "1000", 5, "5", "0", "0"]


class _Venue:
    """httpx handler: Gamma daily markets, CLOB books, Binance ticker, 1h and 1m klines."""

    def __init__(self) -> None:
        self.now = S + 10
        self.minute_closes: dict[int, float] = {S: 100.0, E: 101.0}
        self.noon_candle_ready = True

    def _market_row(self, slug: str) -> dict:
        m = re.search(r"-on-([a-z]+)-(\d+)-(\d{4})$", slug)
        day = date(int(m.group(3)), _MONTHS.index(m.group(1)) + 1, int(m.group(2)))
        w = dbm.window_for(day)
        iso = lambda ts: datetime.utcfromtimestamp(ts).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
        return {"slug": slug, "question": f"Bitcoin Up or Down on {day:%B} {day.day}?",
                "eventStartTime": iso(w.reference_ts), "endDate": iso(w.settle_ts),
                "outcomes": json.dumps(["Up", "Down"]),
                "clobTokenIds": json.dumps([f"up-{w.reference_ts}", f"down-{w.reference_ts}"]),
                "feesEnabled": True, "feeSchedule": {"rate": 0.07, "exponent": 1}}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url, p = str(request.url), request.url.params
        if url.startswith(f"{_config.POLYMARKET_GAMMA_API}/markets"):
            return httpx.Response(200, json=[self._market_row(p["slug"])])
        if url.startswith(f"{_config.POLYMARKET_GAMMA_API}/events"):
            return httpx.Response(200, json=[])
        if url.startswith(f"{_config.POLYMARKET_CLOB_API}/book"):
            return httpx.Response(200, json={"bids": [{"price": "0.48", "size": "300"}],
                                             "asks": [{"price": "0.52", "size": "300"}]})
        if url.startswith(f"{_config.BINANCE_API_BASE}/api/v3/ticker/price"):
            return httpx.Response(200, json={"price": "100.7"})
        if p.get("interval") == "1m":
            ts = int(p["startTime"]) // 1000
            if ts in self.minute_closes and self.now >= ts + 60:
                return httpx.Response(200, json=[_kline(ts, self.minute_closes[ts], seconds=60)])
            return httpx.Response(200, json=[])
        if p.get("interval") == "1h":
            forming = self.now - self.now % 3600
            if not self.noon_candle_ready and forming == S:
                forming = S - 3600  # Binance has not opened the noon candle yet
            rows = [_kline(forming - k * 3600) for k in range(int(p["limit"]) - 1, -1, -1)]
            return httpx.Response(200, json=rows)
        return httpx.Response(404)


@pytest_asyncio.fixture
async def test_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "t.db")
    await _db.init_db()
    await _knobs.refresh_cache()
    engine.reset_caches()
    monkeypatch.setattr(paper, "_live_executor", None)
    monkeypatch.setattr(paper, "_risk_gate", None)
    monkeypatch.setattr(engine, "notify", AsyncMock())
    return _db


def _forecast(monkeypatch: pytest.MonkeyPatch, p: float = 0.8, ok: bool = True) -> AsyncMock:
    result = (kronos.ForecastResult(ok=True, upside_prob=p, last_close=100.5,
                                    final_closes=(101.0,) * 30, seconds=8.8)
              if ok else kronos.ForecastResult(ok=False, error="Kronos worker timed out after 90 s"))
    fake = AsyncMock(return_value=result)
    monkeypatch.setattr(kronos, "run_forecast", fake)
    return fake


async def _tick(monkeypatch: pytest.MonkeyPatch, venue: _Venue, now: int, *, allow: bool = True):
    venue.now = now
    monkeypatch.setattr(paper, "_now", lambda: now)
    async with httpx.AsyncClient(transport=httpx.MockTransport(venue)) as client:
        return await engine.tick(client, allow_entries=allow)


async def _positions() -> list[dict]:
    async with _db.connect() as conn:
        cur = await conn.execute("SELECT * FROM paper_positions ORDER BY position_id")
        return [dict(r) for r in await cur.fetchall()]


@pytest.mark.asyncio
async def test_noon_decision_enters_once_and_settles_net_of_the_fee(test_db, monkeypatch):
    venue, fake = _Venue(), _forecast(monkeypatch, p=0.8)
    snap = await _tick(monkeypatch, venue, S + 10)
    await _tick(monkeypatch, venue, S + 20)
    assert fake.await_count == 1
    request = fake.await_args.args[0]
    assert request.seed == S // 3600 and request.candles[-1][0] == (S - 3600) * 1000
    assert snap.window_slug == "bitcoin-up-or-down-on-september-17-2026"
    (pos,) = await _positions()
    assert (pos["side"], pos["entry_price"], pos["shares"], pos["market_timeframe"],
            pos["window_start_ts"], pos["strategy_id"], pos["mode"]) == (
        "Up", 0.52, 5.0, "1d", S, SID, "paper")
    row = await ledger.get_decision(S, SID, mode="paper")
    assert row["action"] == "ENTERED" and row["position_id"] == pos["position_id"]

    await _tick(monkeypatch, venue, E + engine.SETTLE_GRACE_S + 10)
    closed = (await _positions())[0]
    assert closed["state"] == "closed" and closed["exit_price"] == 1.0
    assert closed["realized_pnl_usd"] == pytest.approx(5 * (1 - 0.52) - 5 * 0.07 * 0.52 * 0.48)
    settled = await ledger.get_decision(S, SID, mode="paper")
    assert (settled["reference_close"], settled["settle_close"], settled["outcome"]) == (
        100.0, 101.0, "Up")


@pytest.mark.asyncio
async def test_exact_tie_settles_at_half(test_db, monkeypatch):
    venue = _Venue()
    venue.minute_closes = {S: 100.0, E: 100.0}
    _forecast(monkeypatch, p=0.8)
    await _tick(monkeypatch, venue, S + 10)
    await _tick(monkeypatch, venue, E + engine.SETTLE_GRACE_S + 10)
    closed = (await _positions())[0]
    assert closed["exit_price"] == 0.5
    assert closed["realized_pnl_usd"] == pytest.approx(5 * (0.5 - 0.52) - 5 * 0.07 * 0.52 * 0.48)
    assert (await ledger.get_decision(S, SID, mode="paper"))["outcome"] == "tie"


@pytest.mark.asyncio
async def test_unavailable_forecast_is_recorded_and_notified_once(test_db, monkeypatch):
    venue, _ = _Venue(), _forecast(monkeypatch, ok=False)
    await _tick(monkeypatch, venue, S + 10)
    await _tick(monkeypatch, venue, S + 20)
    assert await _positions() == []
    row = await ledger.get_decision(S, SID, mode="paper")
    assert row["action"] == "UNAVAILABLE" and "timed out" in row["decision_reason"]
    assert engine.notify.await_count == 1


@pytest.mark.asyncio
async def test_late_start_is_missed_without_running_the_model(test_db, monkeypatch):
    venue, fake = _Venue(), _forecast(monkeypatch)
    await _tick(monkeypatch, venue, S + 301)
    fake.assert_not_awaited()
    assert (await ledger.get_decision(S, SID, mode="paper"))["action"] == "MISSED"


@pytest.mark.asyncio
async def test_waits_until_binance_has_closed_the_hour_before_noon(test_db, monkeypatch):
    venue, fake = _Venue(), _forecast(monkeypatch)
    venue.noon_candle_ready = False
    await _tick(monkeypatch, venue, S + 2)
    fake.assert_not_awaited()
    assert await ledger.get_decision(S, SID, mode="paper") is None


@pytest.mark.asyncio
async def test_disabled_strategy_records_nothing(test_db, monkeypatch):
    await _knobs.set("daily_btc_tsinghua_kronos_btc_24h_enabled", False)
    venue, fake = _Venue(), _forecast(monkeypatch)
    await _tick(monkeypatch, venue, S + 10)
    fake.assert_not_awaited()
    assert await ledger.get_decision(S, SID, mode="paper") is None


@pytest.mark.asyncio
async def test_pending_decision_from_a_past_window_becomes_missed(test_db, monkeypatch):
    old = dbm.window_for(date(2026, 9, 16))
    old_market = dbm.DayMarket("bitcoin-up-or-down-on-september-16-2026", "q", old, "u", "d", 0.07, 1.0)
    await ledger.record_decision(strategy_id=SID, mode="paper", market=old_market, side="Up",
                                 reason="enter Up", signal={}, up_bid=None, up_ask=None,
                                 down_bid=None, down_ask=None, late=False, available=True)
    venue, _ = _Venue(), _forecast(monkeypatch, p=0.5)  # no bet today
    await _tick(monkeypatch, venue, S + 10)
    assert (await ledger.get_decision(old.reference_ts, SID, mode="paper"))["action"] == "MISSED"


def _live_account() -> MagicMock:
    slot = MagicMock()
    slot.resync_flat = AsyncMock(return_value=False)
    slot.submit_entry = AsyncMock(return_value=LiveOrderResult(
        ok=True, status="SUBMITTED", order_id="0xE", price=0.52, size=5.0, notional_usd=2.6))
    slot.record_settlement = AsyncMock(return_value=LiveOrderResult(
        ok=True, status="SETTLED", price=1.0, size=5.0, notional_usd=2.23))
    account = MagicMock()
    account.slot_executor = MagicMock(return_value=slot)
    account.slot = slot
    return account


@pytest.mark.asyncio
async def test_live_entry_and_settlement_go_through_the_strategy_slot(test_db, monkeypatch):
    account = _live_account()
    gate = MagicMock()
    gate.trade_shares = 5.0
    gate.block_reason = MagicMock(side_effect=AssertionError("live gates inside submit_entry"))
    monkeypatch.setattr(paper, "_live_executor", account)
    monkeypatch.setattr(paper, "_risk_gate", gate)
    venue, _ = _Venue(), _forecast(monkeypatch, p=0.8)
    await _tick(monkeypatch, venue, S + 10)
    account.slot_executor.assert_called_with(SID)
    account.slot.submit_entry.assert_awaited_once_with(
        token_id=f"up-{S}", side_price=0.52, notional_usd=pytest.approx(2.6),
        window_slug="bitcoin-up-or-down-on-september-17-2026")
    assert (await _positions())[0]["mode"] == "live"
    await _tick(monkeypatch, venue, E + engine.SETTLE_GRACE_S + 10)
    account.slot.record_settlement.assert_awaited_once_with(
        True, "bitcoin-up-or-down-on-september-17-2026", payout=1.0)
    closed = (await _positions())[0]
    assert closed["state"] == "closed" and closed["realized_pnl_usd"] == pytest.approx(2.23)
```

`datetime.utcfromtimestamp` is deprecated. If the test run warns, use `datetime.fromtimestamp(ts, UTC)` with `from datetime import UTC`. The helper name `hm` is only used by later tests; drop the import if ruff flags it.

- [ ] **Step 3: Run to verify they fail**

Run: `PYTHON_DOTENV_DISABLED=1 python3 -m pytest tests/unit/test_daily_btc_engine.py -q -p no:cacheprovider`
Expected: FAIL with `ImportError: cannot import name 'engine' from 'polymarket_bot.daily_btc'`

---
- [ ] **Step 4: Write the engine** — `polymarket_bot/daily_btc/engine.py`

```python
"""Daily BTC engine: one decision per noon-ET window per strategy, entries, Binance settlement.

Called by polymarket_bot.paper.paper_tick_once when the loop was started on the BTC 1d
selection. Runs Tsinghua-Kronos BTC 24h (name: Zayan, 2026-09-16) on Polymarket's daily BTC
Up/Down market in whichever mode was selected. Entries go through
polymarket_bot.strategy_slot_entry: one order attempt per window, one open position per
strategy, the same RiskGate. Settlement reads the two Binance 1-minute closes the market
resolves on; an exact tie pays 0.50 a share (market rules, read 2026-09-16).
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx

from db import notify
from logging_setup import get_logger
from polymarket_bot import runtime_knobs as _knobs
from polymarket_bot import strategy_slot_entry as slot_entry
from polymarket_bot.daily_btc import forecast_input, ledger, market, tsinghua_kronos_btc_24h
from polymarket_bot.hourly.market import fetch_spot
from polymarket_bot.kronos_forecast import client as kronos

if TYPE_CHECKING:
    from polymarket_bot.paper import PaperSnapshot

log = get_logger("daily_btc_engine")

TIMEFRAME = "1d"
# Settle once Binance has certainly published the settlement minute (Claude, 2026-09-16).
SETTLE_GRACE_S = 120
# (strategy_id, enable knob)
STRATEGIES: tuple[tuple[str, str], ...] = (
    (tsinghua_kronos_btc_24h.STRATEGY_ID, "daily_btc_tsinghua_kronos_btc_24h_enabled"),
)
DISPLAY_NAMES = {tsinghua_kronos_btc_24h.STRATEGY_ID: tsinghua_kronos_btc_24h.DISPLAY_NAME}

_market_cache: dict[int, market.DayMarket] = {}


def reset_caches() -> None:
    _market_cache.clear()


def _mode() -> str:
    from polymarket_bot import paper as P

    return "live" if P._live_executor is not None else "paper"


class _DailyDecision:
    """One btc_daily_market_decisions row as a strategy_slot_entry.DecisionHandle."""

    def __init__(self, reference_ts: int, strategy_id: str, mode: str) -> None:
        self.reference_ts, self.strategy_id, self.mode = reference_ts, strategy_id, mode

    async def reason(self) -> str | None:
        row = await ledger.get_decision(self.reference_ts, self.strategy_id, mode=self.mode)
        return (row or {}).get("decision_reason")

    async def set_action(self, action: str, position_id: int | None = None, *,
                         expected_action: str | None = None) -> bool:
        return await ledger.set_action(self.reference_ts, self.strategy_id, action, position_id,
                                       mode=self.mode, expected_action=expected_action)


async def market_for(client: httpx.AsyncClient, window: market.DayWindow) -> market.DayMarket:
    if window.reference_ts not in _market_cache:
        found = await market.discover(client, window)
        if found is None:
            raise RuntimeError(
                f"Could not find the BTC daily market for {window.market_date:%Y-%m-%d}")
        _market_cache.clear()
        _market_cache[window.reference_ts] = found
    return _market_cache[window.reference_ts]


async def _minute_close(client: httpx.AsyncClient, ts: int, now: int) -> float | None:
    try:
        return await market.fetch_minute_close(client, ts, now)
    except httpx.HTTPError as exc:
        log.warning("daily_btc_engine.minute_close_failed", minute_ts=ts, error=str(exc))
        return None


async def build_snapshot(client: httpx.AsyncClient, now: int | None = None) -> PaperSnapshot:
    from polymarket_bot import paper as P

    now = P._now() if now is None else now
    window = market.current_window(now)
    m = await market_for(client, window)
    up_book = await P._fetch_clob_book(client, m.up_token_id)
    down_book = await P._fetch_clob_book(client, m.down_token_id)
    reference = await _minute_close(client, window.reference_ts, now)
    spot = await fetch_spot(client)
    return P.PaperSnapshot(
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        window_slug=m.slug,
        market_question=m.question,
        remaining_seconds=max(0, window.settle_ts - now),
        spot_price=spot or 0.0,
        reference_price=reference or 0.0,
        sigma_per_second=0.0,
        market_up_price=up_book.best_ask,
        market_down_price=down_book.best_ask,
        fair_up_prob=0.5,
        edge=0.0,
        signal_side=None,
        confidence=0.0,
        notional_usd=0.0,
        reason="daily BTC: waiting",
        feed_source=(
            f"spot={'binance_rest' if spot else 'unavailable'};"
            f"ref={'binance_1m' if reference else 'pending'};vol=none;quotes=clob"
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
        feed_degraded=False,
    )


def _book(snapshot: PaperSnapshot) -> dict[str, float | None]:
    return {"up_bid": snapshot.up_best_bid, "up_ask": snapshot.up_best_ask,
            "down_bid": snapshot.down_best_bid, "down_ask": snapshot.down_best_ask}


async def decide_window(
    client: httpx.AsyncClient, snapshot: PaperSnapshot, m: market.DayMarket, now: int
) -> dict[str, dict[str, Any]]:
    """Record this window's decision for every enabled strategy (once per mode)."""
    from polymarket_bot import paper as P

    mode = _mode()
    window = m.window
    deadline = int(_knobs.cached("daily_btc_entry_deadline_seconds"))
    rows: dict[str, dict[str, Any]] = {}
    pending: list[str] = []
    for sid, knob in STRATEGIES:
        if not _knobs.cached(knob):
            continue
        row = await ledger.get_decision(window.reference_ts, sid, mode=mode)
        if row is None:
            pending.append(sid)
        else:
            rows[sid] = row
    if not pending:
        return rows
    elapsed = now - window.reference_ts
    if elapsed > deadline:
        for sid in pending:
            await ledger.record_decision(
                strategy_id=sid, mode=mode, market=m, side=None,
                reason=f"missed: the window opened {elapsed} s ago, past the {deadline} s deadline",
                signal={}, **_book(snapshot), late=True, available=True,
            )
            rows[sid] = await ledger.get_decision(window.reference_ts, sid, mode=mode) or {}
        return rows
    candles = await forecast_input.fetch_forecast_candles(client, window, now)
    if candles is None:
        log.info("daily_btc_engine.waiting_for_noon_candle", window_slug=m.slug)
        return rows
    for sid in pending:
        if sid != tsinghua_kronos_btc_24h.STRATEGY_ID:
            continue
        result = await kronos.run_forecast(
            tsinghua_kronos_btc_24h.request_for(candles, window.reference_ts))
        decision = tsinghua_kronos_btc_24h.decide(
            result, candles=candles, reference_ts=window.reference_ts,
            up_ask=snapshot.up_best_ask, down_ask=snapshot.down_best_ask,
            edge_threshold=float(
                _knobs.cached("daily_btc_tsinghua_kronos_btc_24h_edge_threshold")),
            # 23 or 25 on daylight-saving days; the forecast always covers 24 hours.
            window_hours=(m.window.settle_ts - m.window.reference_ts) / 3600,
        )
        late = P._now() - window.reference_ts > deadline  # the forecast takes ~9 s
        wrote = await ledger.record_decision(
            strategy_id=sid, mode=mode, market=m, side=decision.side, reason=decision.reason,
            signal=decision.signal, **_book(snapshot), late=late,
            available=decision.available,
        )
        if wrote and not decision.available:
            await notify(
                "daily_btc_forecast_unavailable",
                f"{DISPLAY_NAMES[sid]} could not forecast {m.slug}: {result.error}. "
                "No bet this window.",
                {"window_slug": m.slug, "strategy_id": sid},
            )
        log.info("daily_btc_engine.decided", strategy_id=sid, mode=mode, side=decision.side,
                 available=decision.available, window_slug=m.slug)
        rows[sid] = await ledger.get_decision(window.reference_ts, sid, mode=mode) or {}
    return rows


async def open_entries(
    snapshot: PaperSnapshot, m: market.DayMarket, now: int, *, allow_entries: bool
) -> None:
    mode = _mode()
    deadline = int(_knobs.cached("daily_btc_entry_deadline_seconds"))
    for sid, knob in STRATEGIES:
        if not _knobs.cached(knob):
            continue
        row = await ledger.get_decision(m.window.reference_ts, sid, mode=mode)
        if row is None:
            continue
        await slot_entry.advance(
            snapshot, slot_entry.SlotWindow(sid, TIMEFRAME, m.window.reference_ts),
            action=str(row["action"]), side=row["decision_side"], now=now, deadline_s=deadline,
            allow_entries=allow_entries, mode=mode,
            decision=_DailyDecision(m.window.reference_ts, sid, mode),
        )


async def settle_due(client: httpx.AsyncClient, snapshot: PaperSnapshot, now: int) -> None:
    """Settle daily positions and decision rows whose settlement minute has closed on Binance."""
    from polymarket_bot import paper as P

    # As in the hourly engine (Claude, 2026-09-15, branch-review finding
    # pending-row-never-finalized): every tick, before anything that can skip or fail,
    # close out decisions left open in windows that already ended, in every mode. The
    # current window is handled by open_entries.
    finalized = await ledger.finalize_ended_windows(
        market.current_window(now).reference_ts, slot_entry.UNFINISHED_ATTEMPT)
    if any(finalized.values()):
        log.info("daily_btc_engine.ended_windows_finalized", **finalized)
    if finalized["unfinished"]:
        # Same operator warning as the hourly engine's (branch-review finding
        # hourly-reentry-after-untraced-post): a live order may exist with no ledger row.
        await notify(
            "entry_attempt_unfinished",
            f"{finalized['unfinished']} daily BTC entry attempt(s) in an earlier window did "
            "not finish: the bot stopped or its tick failed mid-order. If the bot was LIVE, an "
            "order may be on Polymarket with no ledger row; check the account's open orders "
            "and trades.",
            finalized,
        )

    closes: dict[int, float | None] = {}

    async def close_at(ts: int) -> float | None:
        if ts not in closes:
            closes[ts] = await _minute_close(client, ts, now)
        return closes[ts]

    async with P.connect() as db:
        async with db.execute(
            "SELECT * FROM paper_positions WHERE state = 'open' AND market_timeframe = ? "
            "ORDER BY opened_at",
            (TIMEFRAME,),
        ) as cur:
            open_rows = [dict(r) for r in await cur.fetchall()]
    for pos in open_rows:
        live_row = pos.get("mode") == "live"
        if live_row and P._live_executor is None:
            continue  # real tokens: only the live executor may book them
        window = market.current_window(int(pos["window_start_ts"]))
        if now < window.settle_ts + SETTLE_GRACE_S:
            continue
        reference, final = await close_at(window.reference_ts), await close_at(window.settle_ts)
        if reference is None or final is None:
            continue
        result = market.outcome(reference, final)
        pay = market.payout(str(pos["side"]), result)
        held: float | None = None
        pnl: float | None = None
        if live_row:
            slot = P._live_executor.slot_executor(str(pos["strategy_id"]))
            settled = await slot.record_settlement(pay == 1.0, pos["window_slug"], payout=pay)
            if not settled.ok and settled.status != "SKIPPED":
                continue  # registration failed: keep the row open and retry next tick
            held, pnl = settled.size or 0.0, settled.notional_usd
        await P._close_position(pos, snapshot, pay, "SETTLED", settled=True,
                                settled_held=held, settled_pnl=pnl)
    for reference_ts, settle_ts in await ledger.unsettled_windows(now, SETTLE_GRACE_S):
        reference, final = await close_at(reference_ts), await close_at(settle_ts)
        if reference is not None and final is not None:
            await ledger.settle_window(reference_ts, reference, final,
                                       market.outcome(reference, final))


def _reason_line(rows: dict[str, dict[str, Any]]) -> str:
    if not rows:
        return "daily BTC: waiting for the noon-ET candle"
    return " | ".join(
        f"{DISPLAY_NAMES.get(sid, sid)}: {row.get('action')} ({row.get('decision_reason')})"
        for sid, row in rows.items()
    )


async def tick(client: httpx.AsyncClient, *, allow_entries: bool = True) -> PaperSnapshot:
    from polymarket_bot import paper as P

    now = P._now()  # one clock read per tick, as in the hourly engine
    snapshot = await build_snapshot(client, now)
    await settle_due(client, snapshot, now)
    m = await market_for(client, market.current_window(now))
    rows = await decide_window(client, snapshot, m, now)
    await open_entries(snapshot, m, now, allow_entries=allow_entries)
    mode = _mode()
    for sid in list(rows):
        rows[sid] = await ledger.get_decision(m.window.reference_ts, sid, mode=mode) or rows[sid]
    snapshot.reason = _reason_line(rows)
    entered = [r for r in rows.values() if r.get("action") == ledger.ENTERED]
    if entered:
        snapshot.signal_side = entered[0].get("decision_side")
    await P._log_tick(snapshot)
    return snapshot
```

`settle_due` finalizes ended windows first, as the hourly engine does, so the step also runs when a 1h or 5m run settles 1d rows (Task 8). The current window's PENDING and SUBMITTING rows are left to `open_entries`.

- [ ] **Step 5: Run the tests**

Run: `PYTHON_DOTENV_DISABLED=1 python3 -m pytest tests/unit/test_daily_btc_engine.py tests/unit/test_runtime_knobs.py tests/unit/test_strategy_slot_entry.py -q -p no:cacheprovider`
Expected: PASS. If `test_runtime_knobs.py` pins the knob list, add the three knobs there in this commit.

- [ ] **Step 6: Full unit suite, ruff, commit**

```bash
PYTHON_DOTENV_DISABLED=1 python3 -m pytest tests/unit -q -p no:cacheprovider 2>&1 | tail -3
python3 -m ruff check polymarket_exec/ polymarket_bot/ tests/ tools/
git add polymarket_bot/daily_btc/engine.py polymarket_bot/runtime_knobs.py tests/unit/test_daily_btc_engine.py tests/unit/test_runtime_knobs.py
git commit -m "feat(daily_btc): engine - Tsinghua-Kronos BTC 24h decision, entry and settlement, paper and live"
```

---

### Task 8: Loop wiring, dashboard glow, rules, docs, gates, smoke run

**Files:**
- Modify: `polymarket_bot/paper.py`, `polymarket_bot/controller.py`, `polymarket_bot/market_selection.py`
- Modify: `polymarket_exec/ops/dashboard/panels/market_selector.py`
- Modify: `AGENTS.md`, `docs/CODE_MAP.md`; regenerate `docs/FILE_MAP.md` and the generated blocks
- Test: `tests/unit/test_daily_btc_loop.py`; `tests/unit/test_market_selector.py` (append)

**Interfaces:**
- Consumes: `daily_btc.engine.TIMEFRAME`, `tick`, `build_snapshot`, `settle_due` (Task 7); the merged hourly `paper.py`, which already has `_timeframe`, the 1h routing, `_close_due_5m_rows_during_1h_run`, `hourly_engine.settle_due(...)` on the 5m path, `_LEGACY_ROWS_SQL`, `_NOT_LIVE_ROWS_SQL` and the hourly branch of `force_close_open_positions`.
- Produces:
  - `paper._STRATEGY_ENGINES: dict[str, ModuleType]` = `{"1h": hourly_engine, "1d": daily_btc_engine}`
  - `market_selection.LOOP_SUPPORTED` gains `("btc", "1d")`
  - `market_selector.open_market_pnl` maps `bitcoin-up-or-down-on-<month>-<day>-<year>` rows to `("btc", "1d")`

- [ ] **Step 1: Write the failing loop tests** — `tests/unit/test_daily_btc_loop.py`

```python
"""The loop trades BTC 1d when started on it, in the selected mode, and settles 1d rows from any run."""
from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

import db as _db
from polymarket_bot import controller, market_selection, paper
from polymarket_bot.daily_btc import engine as daily_engine
from polymarket_bot.daily_btc import market as dbm
from polymarket_bot.hourly import engine as hourly_engine

WINDOW = dbm.window_for(date(2026, 9, 17))
SLUG = "bitcoin-up-or-down-on-september-17-2026"


@pytest_asyncio.fixture
async def test_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "t.db")
    await _db.init_db()
    monkeypatch.setattr(paper, "_timeframe", "5m")
    monkeypatch.setattr(paper, "_risk_gate", None)
    monkeypatch.setattr(paper, "_live_executor", None)
    return _db


async def _insert_daily_row(mode: str = "paper") -> int:
    async with _db.connect() as conn:
        cur = await conn.execute(
            "INSERT INTO paper_positions(opened_at, window_slug, side, state, entry_price,"
            " notional_usd, shares, strategy_id, market_timeframe, window_start_ts, mode)"
            " VALUES ('x', ?, 'Up', 'open', 0.52, 2.6, 5, 'tsinghua_kronos_btc_24h', '1d', ?, ?)",
            (SLUG, WINDOW.reference_ts, mode),
        )
        await conn.commit()
        return int(cur.lastrowid)


def test_btc_1d_is_loop_supported() -> None:
    assert ("btc", "1d") in market_selection.LOOP_SUPPORTED


@pytest.mark.asyncio
async def test_tick_routes_to_the_daily_engine_and_settles_other_timeframes(test_db, monkeypatch):
    snap = MagicMock()
    daily_tick = AsyncMock(return_value=snap)
    hourly_settle = AsyncMock()
    monkeypatch.setattr(daily_engine, "tick", daily_tick)
    monkeypatch.setattr(hourly_engine, "settle_due", hourly_settle)
    monkeypatch.setattr(paper, "_timeframe", "1d")
    assert await paper.paper_tick_once() is snap
    assert daily_tick.await_args.kwargs == {"allow_entries": True}
    hourly_settle.assert_awaited_once()
    assert hourly_settle.await_args.args[1] is snap


@pytest.mark.asyncio
async def test_a_5m_tick_settles_due_daily_rows(test_db, monkeypatch):
    snap = MagicMock(window_slug="btc-updown-5m-1")
    monkeypatch.setattr(paper, "_build_snapshot", AsyncMock(return_value=snap))
    monkeypatch.setattr(paper, "_log_tick", AsyncMock())
    monkeypatch.setattr(paper, "_close_due_positions", AsyncMock())
    monkeypatch.setattr(paper, "_maybe_open_position", AsyncMock())
    monkeypatch.setattr(paper, "_record_and_settle_shadow", AsyncMock())
    monkeypatch.setattr(hourly_engine, "settle_due", AsyncMock())
    daily_settle = AsyncMock()
    monkeypatch.setattr(daily_engine, "settle_due", daily_settle)
    await paper.paper_tick_once()
    daily_settle.assert_awaited_once()


@pytest.mark.asyncio
async def test_five_minute_paths_ignore_daily_rows(test_db) -> None:
    await _insert_daily_row()
    assert await paper._open_legacy_position_exists() is False


@pytest.mark.asyncio
async def test_stop_sells_current_window_daily_rows_only_on_a_1d_run(test_db, monkeypatch):
    position_id = await _insert_daily_row()
    snap = SimpleNamespace(window_slug=SLUG, created_at="y", spot_price=1.0,
                           up_best_bid=0.50, down_best_bid=0.49)
    build = AsyncMock(return_value=snap)
    monkeypatch.setattr(daily_engine, "build_snapshot", build)
    closed = AsyncMock(return_value=True)
    monkeypatch.setattr(paper, "_close_position", closed)
    assert await paper.force_close_open_positions("STOP_REQUEST") == 0  # 5m run: left for settlement
    build.assert_not_awaited()
    monkeypatch.setattr(paper, "_timeframe", "1d")
    assert await paper.force_close_open_positions("STOP_REQUEST") == 1
    assert closed.await_args.args[0]["position_id"] == position_id
    assert closed.await_args.args[2] == 0.50


@pytest.mark.asyncio
async def test_start_pins_btc_1d(test_db, monkeypatch) -> None:
    await market_selection.set_selection("btc", "1d")
    started: list[tuple] = []
    monkeypatch.setattr(controller, "_ensure_runner_started", lambda force=False: started.append(
        (controller._mode_cache, controller._timeframe_cache)))
    monkeypatch.setattr(controller, "_ensure_watchdog_started", lambda: None)
    await controller.request_start()
    assert started == [("paper", "1d")]
    controller._desired_running = False
```

Append to `tests/unit/test_market_selector.py`:

```python
def test_daily_btc_slug_rows_glow_the_btc_1d_button() -> None:
    rows = [{"window_slug": "bitcoin-up-or-down-on-september-17-2026", "side": "Up",
             "entry_price": 0.52, "shares": 5.0}]
    assert msel.open_market_pnl(open_pos=rows, daily_open=[], tick=None) == {("btc", "1d"): None}
```

If the merged `open_market_pnl` returns a different shape for rows without a live price, copy the expectation from the neighbouring hourly-glow test.

- [ ] **Step 2: Run to verify they fail**

Run: `PYTHON_DOTENV_DISABLED=1 python3 -m pytest tests/unit/test_daily_btc_loop.py tests/unit/test_market_selector.py -q -p no:cacheprovider`
Expected: FAIL (`LOOP_SUPPORTED` lacks 1d; the tick is not routed; the daily slug does not glow).

- [ ] **Step 3: Implement in `polymarket_bot/paper.py`**

1. Import the engine and add a registry after the existing `hourly_engine` import:

```python
from polymarket_bot.daily_btc import engine as daily_btc_engine

# Strategy engines by timeframe (Claude, 2026-09-16). The loop runs the engine of the
# timeframe pinned at Start, and every run settles due rows of all the other engines.
_STRATEGY_ENGINES = {hourly_engine.TIMEFRAME: hourly_engine,
                     daily_btc_engine.TIMEFRAME: daily_btc_engine}
```

2. In `paper_tick_once`, replace the `if _timeframe == hourly_engine.TIMEFRAME:` block with:

```python
        engine = _STRATEGY_ENGINES.get(_timeframe)
        if engine is not None:
            await _close_due_5m_rows_during_1h_run(client)
            snapshot = await engine.tick(client, allow_entries=not kill_active)
            await _settle_other_engines(client, snapshot, skip=engine)
            return snapshot
```

   On the 5m path, replace `await hourly_engine.settle_due(client, snapshot, _now())` with `await _settle_other_engines(client, snapshot, skip=None)`.
   - Rename `_close_due_5m_rows_during_1h_run` to `_close_due_5m_rows_during_strategy_run` everywhere, in code, tests and docs.
   - Update its docstring's "1h run" to "1h or 1d run" and keep its source line.

3. Add the helper next to it:

```python
async def _settle_other_engines(
    client: httpx.AsyncClient, snapshot: PaperSnapshot, *, skip: object | None
) -> None:
    """Settle due rows of every strategy engine except ``skip`` (a failure never blocks the tick).

    Boot adopts open strategy rows into their live slots whatever timeframe runs, so every
    run settles them from Binance (Claude, 2026-09-15, branch-review finding
    other-timeframe-live-rows-never-settled; extended to 1d rows by Claude, 2026-09-16).
    """
    for engine in _STRATEGY_ENGINES.values():
        if engine is skip:
            continue
        try:
            await engine.settle_due(client, snapshot, _now())
        except Exception as e:  # noqa: BLE001
            log.warning("paper_tick.strategy_settlement_failed", timeframe=engine.TIMEFRAME,
                        error=f"{type(e).__name__}: {e}")
```

4. `_LEGACY_ROWS_SQL = "(market_timeframe IS NULL OR market_timeframe NOT IN ('1h', '1d'))"`.

5. In `force_close_open_positions`:
   - Group the non-legacy rows by timeframe using `_STRATEGY_ENGINES`.
   - Keep the merged hourly rule for each engine: rows are sold at the bid only when `_timeframe == engine.TIMEFRAME` and the row is in the snapshot's current window. Otherwise they are left for settlement and logged.
   - Generalize the existing hourly branch into a loop:

```python
        for timeframe, engine in _STRATEGY_ENGINES.items():
            rows = [p for p in positions if p.get("market_timeframe") == timeframe]
            if not rows:
                continue
            if _timeframe != timeframe:
                log.warning("force_close.strategy_rows_left_for_settlement", timeframe=timeframe,
                            count=len(rows))
                continue
            snapshot = await engine.build_snapshot(client)
            for pos in rows:
                bid = _current_price_for_side(snapshot, pos["side"])
                if pos["window_slug"] != snapshot.window_slug or bid is None:
                    log.warning("force_close.strategy_row_left_for_settlement",
                                timeframe=timeframe, position_id=pos["position_id"],
                                window_slug=pos["window_slug"])
                    continue
                if await _close_position(pos, snapshot, bid, exit_reason):
                    closed += 1
```

   `legacy` becomes the rows whose `market_timeframe` is not a key of `_STRATEGY_ENGINES`. Keep the merged error handling around the legacy block unchanged.

- [ ] **Step 4: Controller, selection, selector glow**

- `polymarket_bot/market_selection.py`: `LOOP_SUPPORTED: frozenset[tuple[str, str]] = frozenset({("btc", "5m"), ("btc", "1h"), ("btc", "1d")})`
- `polymarket_bot/controller.py`: the Start-refusal text ends with `Select BTC 5m, BTC 1h or BTC 1d.` If a test pins the old text, update that test.
- `polymarket_exec/ops/dashboard/panels/market_selector.py`, below `_HOURLY_SLUG`:

```python
_DAILY_SLUG = re.compile(
    r"^(bitcoin|ethereum|solana|xrp|dogecoin|bnb)-up-or-down-on-[a-z]+-\d+-\d{4}$"
)
```

  In `open_market_pnl`, extend the slug matching:

```python
        daily = None if (m or hourly) else _DAILY_SLUG.match(slug)
        if not m and not hourly and not daily:
            continue
        if m:
            key = (m.group(1), m.group(2))
        elif hourly:
            key = (_LONG_TO_ASSET[hourly.group(1)], "1h")
        else:
            key = (_LONG_TO_ASSET[daily.group(1)], "1d")
```

- [ ] **Step 5: Run the loop tests and the neighbours**

Run: `PYTHON_DOTENV_DISABLED=1 python3 -m pytest tests/unit/test_daily_btc_loop.py tests/unit/test_market_selector.py tests/unit/test_hourly_loop.py tests/unit/test_loop_watchdog.py tests/unit/test_mode_switch.py tests/unit/test_live_wiring.py tests/unit/test_runtime_config.py tests/unit/test_settle_style.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 6: Rules and routing** (hand-written parts only)

`AGENTS.md`:
1. In Active Scope's numbered strategy list, add:

```markdown
4. **Daily BTC strategies** (`polymarket_bot/daily_btc/engine.py`). The loop runs them
   when the operator selects **BTC 1d** and presses ▶ Start, in the mode selected (paper
   or live). Today: **Tsinghua-Kronos BTC 24h**. At noon New York time it runs the Kronos
   team's own Kronos-mini 24-hour BTCUSDT forecast in an isolated worker process, and buys
   Up or Down when the forecast beats the market price. One position of its own, held to
   the market's noon-ET Binance settlement. Strategy doc:
   `docs/strategies/tsinghua-kronos-btc-24h.md`.
```

2. In the Absolute Rules live-authorization bullet (renamed by the hourly session), name the BTC daily Up/Down market next to BTC hourly. Keep that bullet's existing wording and add:
   "and the BTC daily Up/Down market (`polymarket_bot/daily_btc/`, operator decision 2026-09-16: build Tsinghua-Kronos BTC 24h into the app; standing rule 2026-09-14: "we will run what we run when we select mode")".
   Apply the same change to the matching Scope Fence out-of-scope bullet.

`docs/CODE_MAP.md`, under "I want to change X → edit Y", add:

```markdown
| Daily BTC strategies (Tsinghua-Kronos BTC 24h: noon-ET decision, entries, settlement, per-window record) | `polymarket_bot/daily_btc/engine.py` (tick) + `tsinghua_kronos_btc_24h.py` (rule) + `forecast_input.py` + `market.py` (windows/discovery/Binance) + `ledger.py` (`btc_daily_market_decisions`); model worker `polymarket_bot/kronos_forecast/` (isolated process, vendored MIT Kronos code in `third_party/kronos_67b630e/`); shared entry `polymarket_bot/strategy_slot_entry.py`; strategy doc `docs/strategies/tsinghua-kronos-btc-24h.md` |
```

- [ ] **Step 7: Regenerate docs and run every gate**

```bash
PYTHON_DOTENV_DISABLED=1 DATA_DIR="$(mktemp -d)" python3 -m pytest tests/ -q -p no:cacheprovider 2>&1 | tail -3
python3 -m ruff check polymarket_exec/ polymarket_bot/ tests/ tools/
PYTHON_DOTENV_DISABLED=1 python3 tools/gen_docs.py && PYTHON_DOTENV_DISABLED=1 python3 tools/gen_docs.py --check
```

Also run the banned-strings grep from the `docs-drift` job in `.github/workflows/ci.yml`; it must print nothing.

Expected: all tests pass, ruff `All checks passed!`, and `--check` exits 0.

- [ ] **Step 8: Paper smoke run around noon ET** (the operator's machine; never click LIVE)

1. **Tsinghua-Kronos BTC 24h setup**, as in Task 1 Step 11. The interpreter that will run the worker (`KRONOS_PYTHON`, or the app's own) must have torch, einops and pandas. Either run `python3 -m pip install -e '.[kronos]'` with that interpreter from the repo root, or use an absolute `KRONOS_PYTHON`.
2. `python3 tools/fetch_kronos_mini_weights.py`. Expected: "Kronos weights ready."
3. Start the app from this worktree with an isolated DB and no `.env`. Use a preview config that sets `PYTHON_DOTENV_DISABLED=1 BOT_MODE=paper DATA_DIR=<scratch> DB_PATH=<scratch>/smoke.db DASHBOARD_SERVER_PORT=7873`.
   - Point `DATA_DIR` at the folder holding `kronos_models`, or copy it there.
   - `.env` is not read here, so if the worker needs `KRONOS_PYTHON`, set it in the preview config too.
4. Select **BTC 1d** in the header and press **Start** a few minutes before 12:00 America/New_York.
5. Before noon: `paper_ticks` rows carry the current `bitcoin-up-or-down-on-…` slug with both books.
6. Between 12:00:05 and 12:05 ET: `btc_daily_market_decisions` gets one `tsinghua_kronos_btc_24h` paper row.
   - Its `signal_json` **must** hold a numeric `p_up` between 0 and 1. It also holds `paths = 30`, the pinned model and tokenizer, `torch_version`, and `worker_seconds` under 60.
   - The action is `NO_SIGNAL`, or `PENDING` then `ENTERED`.
   - **A row that is `UNAVAILABLE`, or has no `p_up`, fails the smoke run.** Read its reason, fix the setup (items 1–3), and run the check again at the next noon.
7. `ps` shows no Kronos worker left running after the decision.
8. Press Stop, stop the smoke app, and remove the preview config entry.

- [ ] **Step 9: Commit**

```bash
git add polymarket_bot/paper.py polymarket_bot/controller.py polymarket_bot/market_selection.py polymarket_exec/ops/dashboard/panels/market_selector.py tests/unit/test_daily_btc_loop.py tests/unit/test_market_selector.py tests/unit/test_hourly_loop.py AGENTS.md docs/CODE_MAP.md docs/FILE_MAP.md
git commit -m "feat(daily_btc): run Tsinghua-Kronos BTC 24h from the loop on BTC 1d; settle 1d rows from every run; docs"
```

---

## Self-review notes (plan author, 2026-09-16)

- **Spec coverage:**

| Spec item | Task |
|---|---|
| Market rules | Task 0 reuse, Task 2 current window and tie payout, Task 7 settlement |
| Recipe and pinning | Tasks 1 and 3 |
| Worker safety | Task 1 minimal env, `-I`, import check |
| `UNAVAILABLE` | Tasks 3, 4 and 7 |
| Entry deadline and one attempt per window | Tasks 5 and 7 |
| Selected-mode running and live slots | Tasks 5, 6 and 7 |
| Per-window record with signal and book | Tasks 4 and 7 |
| Dashboard selection | Task 8 |
| Sources | module docstrings, knob comments, strategy doc |

- **Known dependency.** Tasks 4–8 need the hourly session's merged branch (Task 0). Tasks 1–3 do not.
- **Deliberately not built here.**
  - A dashboard panel for daily results. The hourly roadmap's per-strategy results panel covers both engines.
  - The fade-the-3-day strategy on `feature/btc-daily-fade-3d`. Its owner wires it; `STRATEGIES` takes a second entry.
