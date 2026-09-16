"""Run one Kronos forecast in an isolated worker process and return the parsed result.

The worker (worker.py) starts as ``python -I -B worker.py`` with an explicit minimal
environment, so it never sees the app's environment (which holds the wallet key). ``-I``
ignores every PYTHON* variable, so ``-B`` is what stops it writing bytecode. It runs in an
empty folder of its own and reads pinned weights from a local folder with the Hugging Face
hub offline.

Only one worker runs at a time. If a call times out, is cancelled or fails, the worker is
killed together with every process it started. Pinned sources:
docs/strategies/tsinghua-kronos-btc-24h.md.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import signal
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import config as _config
from logging_setup import get_logger

log = get_logger("kronos_forecast")

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKER_SCRIPT = Path(__file__).resolve().with_name("worker.py")
# github.com/shiyu-coder/Kronos model/ at this commit, MIT, vendored unchanged.
KRONOS_CODE_COMMIT = "67b630e67f6a18c9e9be918d9b4337c960db1e9a"
CODE_DIR = REPO_ROOT / "third_party" / "kronos_67b630e"
# Under the loop watchdog's 180 s stall threshold (controller.WATCHDOG_STALL_SECONDS)
# (Claude, 2026-09-16).
DEFAULT_TIMEOUT_S = 90.0
# CPU threads for one forecast: torch.set_num_threads and OMP_NUM_THREADS (Claude, 2026-09-16).
WORKER_THREADS = 4
UNREADABLE_RESULT = "Kronos worker returned an unreadable result: "
# Longest error text kept in a result or a log line (Claude, 2026-09-16).
MAX_ERROR_CHARS = 400
# One worker at a time: each uses about 1.5 GB of memory and the operator's machine has
# 8 GB (Claude, 2026-09-16).
_WORKER_LOCK = asyncio.Lock()


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
    threads: int = WORKER_THREADS


@dataclass(frozen=True)
class ForecastResult:
    ok: bool
    upside_prob: float | None = None
    last_close: float | None = None
    final_closes: tuple[float, ...] = ()
    seconds: float | None = None
    error: str | None = None
    torch_version: str | None = None  # the worker's torch.__version__, when it reports one


def _failed(error: str) -> ForecastResult:
    return ForecastResult(ok=False, error=error[:MAX_ERROR_CHARS])


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
                    "run python3 tools/fetch_kronos_mini_weights.py")
    return None


def worker_home() -> Path:
    return Path(_config.DATA_DIR) / "kronos_worker_home"


def worker_run_dir() -> Path:
    """The worker's working folder: empty, and used for nothing else."""
    folder = worker_home() / "run"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def worker_env() -> dict[str, str]:
    home = worker_home()
    home.mkdir(parents=True, exist_ok=True)
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(home),
        "HF_HUB_OFFLINE": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "OMP_NUM_THREADS": str(WORKER_THREADS),
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
        return _failed(missing)
    # A bare name would be looked up on the worker's PATH (/usr/bin:/bin) and a relative
    # path from the worker's own folder, neither of which is what the operator meant.
    if _config.KRONOS_PYTHON and not Path(_config.KRONOS_PYTHON).is_absolute():
        return _failed("KRONOS_PYTHON must be an absolute path to a Python interpreter")
    payload = {
        **asdict(request),
        "code_dir": str(CODE_DIR),
        "model_dir": str(snapshot_dir(spec.model_repo, spec.model_revision)),
        "tokenizer_dir": str(snapshot_dir(spec.tokenizer_repo, spec.tokenizer_revision)),
    }
    loop = asyncio.get_running_loop()
    # The timeout covers waiting for the one-worker lock too, so queued calls can never add
    # up past the loop watchdog's stall limit (Claude, 2026-09-16).
    deadline = loop.time() + timeout_s
    try:
        await asyncio.wait_for(_WORKER_LOCK.acquire(), timeout_s)
    except TimeoutError:
        log.warning("kronos_forecast.worker_lock_timeout", timeout_s=timeout_s)
        return _failed(f"Kronos worker timed out after {timeout_s:g} s "
                       "waiting for another forecast to finish")
    try:
        try:
            # A new session makes the worker the leader of its own process group, so
            # everything it starts can be killed with it.
            proc = await asyncio.create_subprocess_exec(
                worker_python(), "-I", "-B", str(WORKER_SCRIPT),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, env=worker_env(), cwd=str(worker_run_dir()),
                start_new_session=True,
            )
        except OSError as exc:
            return _failed(f"could not start the Kronos worker: {exc}")
        finished = False
        try:
            remaining = max(0.0, deadline - loop.time())
            out, err = await asyncio.wait_for(proc.communicate(json.dumps(payload).encode()),
                                              remaining)
            finished = True
        except TimeoutError:
            log.warning("kronos_forecast.worker_timeout", timeout_s=timeout_s)
            return _failed(f"Kronos worker timed out after {timeout_s:g} s")
        finally:
            if not finished:  # timeout, cancellation or any other exception
                await _kill_process_group(proc)
    finally:
        _WORKER_LOCK.release()
    return read_worker_output(out, err, proc.returncode, request.paths)


async def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    """Kill the worker and every process it started, then reap the worker.

    ``proc.wait()`` returns only once the worker has exited and its pipes are closed, and a
    process the worker started holds those pipes too, so the whole group is killed.
    """
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    await proc.wait()


def _finite_number(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def parse_worker_result(data: Any, paths: int) -> ForecastResult | None:
    """The worker's success line as a result, or None unless every field is present and valid.

    Accepted only: ``ok`` exactly true, ``upside_prob`` a finite number in [0, 1],
    ``final_closes`` a list of ``paths`` finite numbers, ``last_close`` and ``seconds``
    finite numbers. The optional ``torch`` version is kept only as a non-blank string, cut
    to 40 characters.
    """
    if not isinstance(data, dict) or data.get("ok") is not True:
        return None
    prob, closes = data.get("upside_prob"), data.get("final_closes")
    if not (_finite_number(prob) and 0.0 <= prob <= 1.0):
        return None
    if not (isinstance(closes, list) and len(closes) == paths
            and all(_finite_number(v) for v in closes)):
        return None
    if not (_finite_number(data.get("last_close")) and _finite_number(data.get("seconds"))):
        return None
    torch_version = data.get("torch")
    return ForecastResult(
        ok=True,
        upside_prob=float(prob),
        last_close=float(data["last_close"]),
        final_closes=tuple(float(v) for v in closes),
        seconds=float(data["seconds"]),
        torch_version=((torch_version.strip()[:40] or None)
                       if isinstance(torch_version, str) else None),
    )


def _reported_error(data: Any) -> str | None:
    """The worker's own failure report, ``{"ok": false, "error": "<text>"}``."""
    if isinstance(data, dict) and data.get("ok") is False:
        error = data.get("error")
        if isinstance(error, str) and error.strip():
            return error.strip()
    return None


def read_worker_output(out: bytes, err: bytes, returncode: int | None,
                       paths: int) -> ForecastResult:
    """The result from the worker's last stdout line, its stderr and its exit code."""
    lines = out.decode("utf-8", "replace").strip().splitlines()
    last = lines[-1] if lines else ""
    try:
        data: Any = json.loads(last)
    except (ValueError, RecursionError):  # ValueError covers json.JSONDecodeError
        data = None
    reported = _reported_error(data)
    if returncode != 0 or reported:
        # The end of stderr holds the exception; the worker's own report is cut from its start.
        detail = (reported or err.decode("utf-8", "replace").strip()[-MAX_ERROR_CHARS:]
                  or f"worker exited with code {returncode}")[:MAX_ERROR_CHARS]
        log.warning("kronos_forecast.worker_failed", returncode=returncode, error=detail)
        return _failed(detail)
    result = parse_worker_result(data, paths)
    if result is None:
        error = UNREADABLE_RESULT + repr(last)[:200]
        log.warning("kronos_forecast.worker_unreadable_result", error=error)
        return _failed(error)
    return result
