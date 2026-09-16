"""Run one Kronos forecast in an isolated worker process and return the parsed result.

The worker (worker.py) starts as ``python -I worker.py`` with an explicit minimal
environment, so it never sees the app's environment (which holds the wallet key). It
reads pinned weights from a local folder with the Hugging Face hub offline. Pinned
sources: docs/strategies/tsinghua-kronos-btc-24h.md.
"""
from __future__ import annotations

import asyncio
import json
import math
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
# Under the loop watchdog's 180 s stall threshold (controller.WATCHDOG_STALL_SECONDS).
DEFAULT_TIMEOUT_S = 90.0
UNREADABLE_RESULT = "Kronos worker returned an unreadable result: "


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
    return read_worker_output(out, err, proc.returncode, request.paths)


def _finite_number(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def parse_worker_result(data: Any, paths: int) -> ForecastResult | None:
    """The worker's success line as a result, or None unless every field is present and valid.

    Accepted only: ``ok`` exactly true, ``upside_prob`` a finite number in [0, 1],
    ``final_closes`` a list of ``paths`` finite numbers, ``last_close`` and ``seconds``
    finite numbers.
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
    return ForecastResult(
        ok=True,
        upside_prob=float(prob),
        last_close=float(data["last_close"]),
        final_closes=tuple(float(v) for v in closes),
        seconds=float(data["seconds"]),
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
        detail = (reported or err.decode("utf-8", "replace").strip()[-400:]
                  or f"worker exited with code {returncode}")
        log.warning("kronos_forecast.worker_failed", returncode=returncode, error=detail)
        return ForecastResult(ok=False, error=detail)
    result = parse_worker_result(data, paths)
    if result is None:
        error = UNREADABLE_RESULT + repr(last)[:200]
        log.warning("kronos_forecast.worker_unreadable_result", error=error)
        return ForecastResult(ok=False, error=error)
    return result
