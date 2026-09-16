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


APP_MODULES = {"config", "db", "polymarket_bot", "polymarket_exec", "dotenv", "logging_setup",
               "tools"}


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
    for name in ("config", "db", "polymarket_bot", "polymarket_bot.paper", "polymarket_exec",
                 "dotenv", "logging_setup", "tools", "tools.gen_docs"):
        with pytest.raises(worker.BlockedImportError):
            blocker.find_spec(name, None)
    for name in ("configparser", "sysconfig", "dbm", "toolz", "logging.config", "model",
                 "model.kronos", "torch", "pandas"):
        assert blocker.find_spec(name, None) is None
    # Importing the worker module (as this test file does) must not install the blocker.
    assert not any(isinstance(f, worker.AppImportBlocker) for f in sys.meta_path)


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
    in_eval_mode = False
    @classmethod
    def from_pretrained(cls, path): return cls()
    def eval(self):
        self.in_eval_mode = True
        return self
class Kronos(_Loaded): pass
class KronosTokenizer(_Loaded): pass
class KronosPredictor:
    def __init__(self, model, tokenizer, device=None, max_context=512, clip=5):
        assert model.in_eval_mode and tokenizer.in_eval_mode, "model and tokenizer must be in eval mode"
        assert device == "cpu" and max_context == 512
    def predict_batch(self, df_list, x_timestamp_list, y_timestamp_list, pred_len, T, top_k, top_p,
                      sample_count, verbose):
        assert sample_count == 1 and (T, top_k, top_p) == (1.0, 0, 0.95)
        last = float(df_list[0]["close"].iloc[-1])
        return [pd.DataFrame({"close": [last * (0.99 if i % 4 == 0 else 1.01)] * pred_len},
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


def test_worker_process_runs_the_recipe_with_fake_torch_and_model(tmp_path: Path) -> None:
    pytest.importorskip("pandas")
    code = _code_dir(tmp_path, FAKE_MODEL)
    req = _request(code_dir=str(code), threads=3, seed=497_000)
    proc, out = _run_worker(json.dumps(req))
    assert proc.returncode == 0 and out["ok"] is True, proc.stderr
    assert out["upside_prob"] == pytest.approx(0.75)  # 3 of 4 paths close above the last close
    assert out["last_close"] == req["candles"][-1][4]
    assert len(out["final_closes"]) == 4
    assert (code / "seed.txt").read_text() == "497000"
    assert (code / "threads.txt").read_text() == "3"


def test_worker_reports_errors_as_json() -> None:
    proc, out = _run_worker("not json")
    assert proc.returncode == 0 and out["ok"] is False and "JSONDecodeError" in out["error"]


def test_worker_names_a_missing_library_and_how_to_install_it(tmp_path: Path) -> None:
    code = _code_dir(tmp_path, "import kronos_test_missing_library\n")
    proc, out = _run_worker(json.dumps(_request(code_dir=str(code))))
    assert proc.returncode == 0 and out["ok"] is False
    assert out["error"] == (
        f"Kronos worker is missing kronos_test_missing_library in {sys.executable}; "
        "install the optional 'kronos' extra into that interpreter "
        "(python3 -m pip install -e '.[kronos]') or set KRONOS_PYTHON to an absolute path of an "
        "interpreter that has torch, einops and pandas")


def test_worker_process_refuses_to_import_app_modules(tmp_path: Path) -> None:
    pytest.importorskip("pandas")
    imported = tmp_path / "app_config_was_imported"
    # A fake app config next to the model code, where an editable install could put the real one.
    code = _code_dir(tmp_path, "import config\n" + FAKE_MODEL)
    (code / "config.py").write_text(f"from pathlib import Path\nPath({str(imported)!r}).touch()\n")
    proc, out = _run_worker(json.dumps(_request(code_dir=str(code))))
    assert proc.returncode == 0 and out["ok"] is False, out
    assert "refused to import the app module config" in out["error"]
    assert not imported.exists()
