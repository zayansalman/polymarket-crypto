"""Kronos forecast worker: request checks, input frames, chunked sampling, isolation, subprocess run.

The worker process runs here with a fake torch and a fake model package; anything needing pandas
is skipped where pandas is not installed, so CI never needs torch (Claude, 2026-09-16; lc2004
recipe and chunks, Claude, 2026-09-22; ems.kronos_forecast and the ems package refused,
Claude, 2026-09-29).
"""
from __future__ import annotations

import ast
import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ems.kronos_forecast import worker

HOUR_MS = 3_600_000
WORKER = Path(worker.__file__).resolve()


def _candles(n: int, start_ms: int = 1_789_000_000_000 - 1_789_000_000_000 % HOUR_MS) -> list[list[float]]:
    return [[start_ms + i * HOUR_MS, 100.0, 101.0, 99.0, 100.0 + i, 10.0, 1000.0] for i in range(n)]


def _request(**overrides) -> dict:
    req = {"candles": _candles(5), "horizon": 3, "paths": 4, "temperature": 1.0, "top_p": 0.9,
           "top_k": 0, "seed": 42, "max_context": 512, "threads": 1, "chunk": 15,
           "code_dir": "/nonexistent", "model_dir": "/nonexistent/model",
           "tokenizer_dir": "/nonexistent/tokenizer"}
    req.update(overrides)
    return req


def test_validate_reports_missing_keys_bad_rows_and_gaps() -> None:
    assert worker.validate(_request()) is None
    for key in ("seed", "chunk"):
        bad = _request()
        del bad[key]
        assert key in worker.validate(bad)
    assert "candles" in worker.validate(_request(candles=[[1, 2, 3]]))
    assert "candles" in worker.validate(_request(candles=[]))
    gap = _candles(3)
    gap[2][0] += HOUR_MS
    assert "consecutive" in worker.validate(_request(candles=gap))
    for field in ("paths", "horizon", "chunk"):
        assert "positive" in worker.validate(_request(**{field: 0}))


def test_validate_wants_absolute_snapshot_folders() -> None:
    assert "model_dir" in worker.validate(_request(model_dir="models--lc2004/snapshots/eb51"))
    assert "tokenizer_dir" in worker.validate(_request(tokenizer_dir="tokenizer"))


@pytest.mark.parametrize(("paths", "chunk", "sizes"), [
    (30, 15, [15, 15]), (30, 30, [30]), (30, 50, [30]), (7, 3, [3, 3, 1]), (1, 15, [1]),
])
def test_chunk_sizes_draw_the_paths_chunk_at_a_time(paths: int, chunk: int, sizes: list[int]) -> None:
    assert worker.chunk_sizes(paths, chunk) == sizes


def test_build_frames_uses_naive_utc_open_times_and_next_hours() -> None:
    pd = pytest.importorskip("pandas")
    candles = _candles(4)
    df, x_ts, y_ts = worker.build_frames(candles, 3)
    assert list(df.columns) == ["open", "high", "low", "close", "volume", "amount"]
    assert df["amount"].iloc[-1] == candles[-1][6]  # quote volume
    assert x_ts.iloc[-1] == pd.Timestamp(candles[-1][0], unit="ms")
    assert x_ts.dt.tz is None
    assert list(y_ts) == [pd.Timestamp(candles[-1][0] + k * HOUR_MS, unit="ms") for k in (1, 2, 3)]


APP_MODULES = {"ems", "config", "db", "polymarket_bot", "polymarket_exec", "dotenv",
               "logging_setup", "tools"}


def test_worker_imports_nothing_from_the_app() -> None:
    tree = ast.parse(WORKER.read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    assert not names & APP_MODULES


def test_the_app_import_blocker_refuses_app_modules_only() -> None:
    assert worker.BLOCKED_IMPORTS == APP_MODULES
    blocker = worker.AppImportBlocker()
    for name in ("ems", "ems.config", "ems.kronos_forecast.client", "config", "db",
                 "polymarket_bot", "polymarket_bot.paper", "polymarket_exec", "dotenv",
                 "logging_setup", "tools", "tools.gen_docs"):
        with pytest.raises(worker.BlockedImportError):
            blocker.find_spec(name, None)
    for name in ("configparser", "sysconfig", "dbm", "toolz", "logging.config", "model",
                 "model.kronos", "torch", "pandas", "email", "email.message", "emsx"):
        assert blocker.find_spec(name, None) is None
    # Importing the worker module (as this test file does) must not install the blocker.
    assert not any(isinstance(f, worker.AppImportBlocker) for f in sys.meta_path)


# Records every call, in order, to events.txt next to it.
FAKE_TORCH = '''
import contextlib
from pathlib import Path
__version__ = "fake"
EVENTS = Path(__file__).with_name("events.txt")
def _event(text):
    with EVENTS.open("a") as f:
        f.write(text + "\\n")
def set_num_threads(n): _event(f"threads {n}")
def manual_seed(s): _event(f"seed {s}")
def no_grad(): return contextlib.nullcontext()
'''

# Every fourth path (0, 4, 8, ...) ends 1% below the last close, the rest 1% above.
FAKE_MODEL = '''
import pandas as pd
import torch
class _Loaded:
    in_eval_mode = False
    @classmethod
    def from_pretrained(cls, path):
        torch._event(f"load {cls.__name__} {path}")
        return cls()
    def eval(self):
        self.in_eval_mode = True
        return self
class Kronos(_Loaded): pass
class KronosTokenizer(_Loaded): pass
class KronosPredictor:
    def __init__(self, model, tokenizer, device=None, max_context=512, clip=5):
        assert isinstance(model, Kronos) and isinstance(tokenizer, KronosTokenizer)
        assert model.in_eval_mode and tokenizer.in_eval_mode, "model and tokenizer must be in eval mode"
        assert device == "cpu" and max_context == 512
        self.drawn = 0
    def predict_batch(self, df_list, x_timestamp_list, y_timestamp_list, pred_len, T, top_k, top_p,
                      sample_count, verbose):
        assert sample_count == 1 and (T, top_k, top_p) == (1.0, 0, 0.9) and verbose is False
        assert len(df_list) == len(x_timestamp_list) == len(y_timestamp_list)
        assert all(len(y) == pred_len for y in y_timestamp_list)
        torch._event(f"batch {len(df_list)} {pred_len}")
        last = float(df_list[0]["close"].iloc[-1])
        first, self.drawn = self.drawn, self.drawn + len(df_list)
        return [pd.DataFrame({"close": [last * (0.99 if (first + i) % 4 == 0 else 1.01)] * pred_len},
                             index=y_timestamp_list[i]) for i in range(len(df_list))]
'''


def _run_worker(stdin_text: str) -> tuple[subprocess.CompletedProcess, dict]:
    """Run the real worker script the way the client does; return the process and last line."""
    proc = subprocess.run([sys.executable, "-I", "-B", str(WORKER)], input=stdin_text,
                          capture_output=True, text=True, timeout=60,
                          env={"PATH": "/usr/bin:/bin", "PYTHON_DOTENV_DISABLED": "1"})
    return proc, json.loads(proc.stdout.strip().splitlines()[-1])


def _code_dir(tmp_path: Path, model_source: str) -> Path:
    """A stand-in for third_party/kronos_67b630e with a fake torch and a fake model package."""
    code = tmp_path / "code"
    (code / "model").mkdir(parents=True)
    (code / "torch.py").write_text(FAKE_TORCH)
    (code / "model" / "__init__.py").write_text(model_source)
    return code


def _snapshots(tmp_path: Path) -> dict[str, str]:
    """Absolute stand-ins for the model and tokenizer snapshot folders."""
    folders = {"model_dir": tmp_path / "snapshots" / "model",
               "tokenizer_dir": tmp_path / "snapshots" / "tokenizer"}
    for folder in folders.values():
        folder.mkdir(parents=True)
    return {key: str(folder) for key, folder in folders.items()}


def test_worker_process_runs_the_research_recipe_in_chunks(tmp_path: Path) -> None:
    pytest.importorskip("pandas")
    code = _code_dir(tmp_path, FAKE_MODEL)
    folders = _snapshots(tmp_path)
    req = _request(code_dir=str(code), threads=3, seed=497_000, paths=10, chunk=4, **folders)
    proc, out = _run_worker(json.dumps(req))
    assert proc.returncode == 0 and out["ok"] is True, (proc.stderr, out)
    assert out["last_close"] == req["candles"][-1][4]
    assert len(out["final_closes"]) == 10
    # Paths 0, 4 and 8 end below the last close.
    assert sum(close > out["last_close"] for close in out["final_closes"]) == 7
    assert out["torch"] == "fake"
    # Threads first; seeded once, after both loads and before the first chunk.
    assert (code / "events.txt").read_text().splitlines() == [
        "threads 3",
        f"load KronosTokenizer {folders['tokenizer_dir']}",
        f"load Kronos {folders['model_dir']}",
        "seed 497000",
        "batch 4 3",
        "batch 4 3",
        "batch 2 3",
    ]


def test_worker_reports_errors_as_json() -> None:
    proc, out = _run_worker("not json")
    assert proc.returncode == 0 and out["ok"] is False and "JSONDecodeError" in out["error"]


def test_worker_reports_a_bad_request_before_importing_anything(tmp_path: Path) -> None:
    code = _code_dir(tmp_path, FAKE_MODEL)
    proc, out = _run_worker(json.dumps(_request(code_dir=str(code), chunk=0)))
    assert proc.returncode == 0 and out == {
        "ok": False, "error": "paths, horizon and chunk must be positive"}
    assert not (code / "events.txt").exists()


def test_worker_reports_a_missing_snapshot_folder(tmp_path: Path) -> None:
    code = _code_dir(tmp_path, FAKE_MODEL)
    folders = _snapshots(tmp_path)
    missing = str(tmp_path / "snapshots" / "gone")
    proc, out = _run_worker(json.dumps(_request(code_dir=str(code), **{**folders,
                                                                        "model_dir": missing})))
    assert proc.returncode == 0 and out == {
        "ok": False, "error": f"model_dir {missing} is not a folder"}
    assert not (code / "events.txt").exists()


def test_worker_names_a_missing_library_and_how_to_install_it(tmp_path: Path) -> None:
    code = _code_dir(tmp_path, "import kronos_test_missing_library\n")
    proc, out = _run_worker(json.dumps(_request(code_dir=str(code), **_snapshots(tmp_path))))
    assert proc.returncode == 0 and out["ok"] is False
    assert out["error"] == (
        f"Kronos worker is missing kronos_test_missing_library in {sys.executable}; "
        "install the optional 'kronos' extra into that interpreter "
        "(python3 -m pip install -e '.[kronos]') or set KRONOS_PYTHON to an absolute path of an "
        "interpreter that has torch, einops, pandas, safetensors, huggingface_hub and tqdm "
        "outside the user site")


def test_worker_process_refuses_to_import_app_modules(tmp_path: Path) -> None:
    pytest.importorskip("pandas")
    imported = tmp_path / "app_config_was_imported"
    # A fake app config next to the model code, where an editable install could put the real one.
    code = _code_dir(tmp_path, "import config\n" + FAKE_MODEL)
    (code / "config.py").write_text(f"from pathlib import Path\nPath({str(imported)!r}).touch()\n")
    proc, out = _run_worker(json.dumps(_request(code_dir=str(code), **_snapshots(tmp_path))))
    assert proc.returncode == 0 and out["ok"] is False, out
    assert "refused to import the app module config" in out["error"]
    assert not imported.exists()


def test_worker_process_refuses_the_ems_package(tmp_path: Path) -> None:
    """ems.config loads the app's .env, which holds the wallet key (Claude, 2026-09-29)."""
    imported = tmp_path / "ems_config_was_imported"
    # A fake ems package next to the model code, where an editable install could put the real one.
    code = _code_dir(tmp_path, "import ems.config\n" + FAKE_MODEL)
    (code / "ems").mkdir()
    (code / "ems" / "__init__.py").write_text("")
    (code / "ems" / "config.py").write_text(
        f"from pathlib import Path\nPath({str(imported)!r}).touch()\n")
    proc, out = _run_worker(json.dumps(_request(code_dir=str(code), **_snapshots(tmp_path))))
    assert proc.returncode == 0 and out["ok"] is False, out
    assert "refused to import the app module ems" in out["error"]
    assert not imported.exists()


# A model package that marks the moment the model "runs", then sleeps like a long forecast.
SLOW_MODEL = '''
import time
from pathlib import Path
Path(RUNNING).write_text("running")
time.sleep(60)
'''

# The app stand-in: starts the real worker the way the client does (its own session, so a
# terminal's hang-up never reaches it), hands it a request, records its PID, then waits.
PARENT = '''
import json, subprocess, sys, time
from pathlib import Path
proc = subprocess.Popen([sys.executable, "-I", "-B", WORKER], stdin=subprocess.PIPE,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        env={"PATH": "/usr/bin:/bin"}, start_new_session=True)
proc.stdin.write(PAYLOAD.encode())
proc.stdin.close()
Path(PID_FILE).write_text(str(proc.pid))
time.sleep(60)
'''


def _gone(pid: int, within_s: float) -> bool:
    """True once ``pid`` no longer exists (an orphan is reaped by launchd/init, so poll)."""
    deadline = time.monotonic() + within_s
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def test_the_worker_dies_with_the_app_that_started_it(tmp_path: Path) -> None:
    """kill -9 on the app, or its terminal closing, must not leave the model running.

    The worker has its own session, so no hang-up or Ctrl-C reaches it; it watches its parent
    and kills its own process group once the parent is gone (reviewer finding, Claude,
    2026-09-29).
    """
    running = tmp_path / "running"
    code = _code_dir(tmp_path, f"RUNNING = {str(running)!r}\n" + SLOW_MODEL)
    payload = json.dumps(_request(code_dir=str(code), **_snapshots(tmp_path)))
    pid_file = tmp_path / "worker.pid"
    parent = subprocess.Popen([sys.executable, "-I", "-c", (
        f"WORKER = {str(WORKER)!r}\nPAYLOAD = {payload!r}\nPID_FILE = {str(pid_file)!r}\n"
        + PARENT)])
    worker_pid = None
    try:
        deadline = time.monotonic() + 20
        while not (running.exists() and pid_file.exists()) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert running.exists() and pid_file.exists(), "the worker never reached the model"
        worker_pid = int(pid_file.read_text())
        parent.send_signal(signal.SIGKILL)
        parent.wait(10)
        assert _gone(worker_pid, 5.0), "the worker outlived the app that started it"
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(10)
        if worker_pid is not None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(worker_pid, signal.SIGKILL)
