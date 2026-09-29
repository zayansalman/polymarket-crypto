"""Run one Kronos forecast in an isolated worker process and return the parsed result.

Used by the lc2004-Kronos BTC 24h forecast card, which shows the model's chance that
Polymarket's daily BTC Up/Down market settles Up so the operator can trade it by hand ("this
doesnt need to trade for me actually just show its prediction", Zayan (operator),
2026-09-29). Nothing here places, cancels or simulates orders. The model is the lc2004 BTCUSDT
1h fine-tune of Kronos, at the revisions pinned below, read from the standard Hugging Face
cache (fill it once with ``python3 tools/fetch_lc2004_kronos_weights.py``).

The worker (worker.py) starts as ``python -I -B worker.py`` with an explicit minimal
environment, so it never sees the app's environment (which holds the wallet key). ``-I``
ignores every PYTHON* variable, so ``-B`` is what stops it writing bytecode. It runs in an
empty folder of its own and reads the pinned weights from their snapshot folders with the
Hugging Face hub offline. This process never imports torch or huggingface_hub.

Only one worker runs at a time, whichever event loop or app process asks: a thread lock in
this process, then an exclusive lock on ``worker.lock`` in the worker's home that the worker
inherits and holds until it exits. If a call times out, is cancelled or fails, the worker is
killed together with every process it started; if the app dies first, the worker notices
and kills itself (worker.py ``watch_parent``).

Ported from the Tsinghua-Kronos BTC 24h branch (Claude, 2026-09-16), adapted to the lc2004
fine-tune, the Hugging Face cache and chunked sampling (Claude, 2026-09-22), and moved into
``ems/`` for the forecast card (Claude, 2026-09-29).
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import math
import os
import signal
import sys
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ems import config as _config  # type: ignore[import-untyped]
from ems.logging_setup import get_logger

log = get_logger("kronos_forecast.client")

# ems/kronos_forecast/client.py -> parents[2] is the repository root.
REPO_ROOT = Path(__file__).resolve().parents[2]
WORKER_SCRIPT = Path(__file__).resolve().with_name("worker.py")
# github.com/shiyu-coder/Kronos model/ at this commit, MIT, vendored unchanged.
KRONOS_CODE_COMMIT = "67b630e67f6a18c9e9be918d9b4337c960db1e9a"
CODE_DIR = REPO_ROOT / "third_party" / "kronos_67b630e"
# 30 paths x 24 hourly steps of the lc2004 model (about 102M parameters, a full 512-token
# forward pass per step, no KV cache) is estimated at 2-4 minutes on the operator's M2 at 4
# threads; unmeasured, so the limit leaves wide room. Callers run the forecast off any loop's
# tick path, so it never counts toward a loop watchdog's stall limit (Claude, 2026-09-22).
DEFAULT_TIMEOUT_S = 900.0
# CPU threads for one forecast: torch.set_num_threads and OMP_NUM_THREADS, as in the research
# run (research/kronos_lc2004_btcusdt_1h_finetune_24h_horizon, --threads 4) (Claude, 2026-09-22).
WORKER_THREADS = 4
# Paths sampled per predict_batch call, as in the research run (--chunk 15). It also halves
# peak activation memory against one batch of 30 (Claude, 2026-09-22).
DEFAULT_CHUNK = 15
UNREADABLE_RESULT = "Kronos worker returned an unreadable result: "
# Longest error text kept in a result or a log line (Claude, 2026-09-16).
MAX_ERROR_CHARS = 400
FETCH_TOOL = "tools/fetch_lc2004_kronos_weights.py"
# How often a queued call checks whether the running worker has finished (Claude, 2026-09-22).
LOCK_POLL_S = 0.05
# One worker at a time in this process: the operator's machine has 8 GB of memory and a
# 10-process job crashed it on 2026-09-15. A thread lock, not an asyncio.Lock, because callers
# on more than one event loop can ask (the dashboard's loop, or a loop on another thread), and
# an asyncio.Lock is bound to the first loop that waits on it. Queued calls poll it without
# blocking their loop (Claude, 2026-09-22).
_WORKER_LOCK = threading.Lock()
# The same rule across app processes: an exclusive flock on this file in worker_home(). The
# worker inherits the locked descriptor, so the lock is held for the worker's whole life, even
# if the app that started it dies first and a new app starts (Claude, 2026-09-29, for a review
# finding).
WORKER_LOCK_FILE = "worker.lock"


@dataclass(frozen=True)
class ModelSpec:
    model_repo: str
    model_revision: str
    tokenizer_repo: str
    tokenizer_revision: str


# huggingface.co/lc2004, the BTCUSDT 1h fine-tune of Kronos-base and its tokenizer, pinned to
# the revisions the research pre-registration used (research/
# kronos_lc2004_btcusdt_1h_finetune_24h_horizon/PREREG.md on the branch
# research/kronos-lc2004-btcusdt-1h-finetune-24h-horizon) (Claude, 2026-09-22).
LC2004_BTCUSDT_1H = ModelSpec(
    "lc2004/kronos_base_model_BTCUSDT_1h_finetune", "eb51e682c8194a1ba7254cc4357c75819683fbaf",
    "lc2004/kronos_tokenizer_base_BTCUSDT_1h_finetune", "b8f1c795b80231f5bdeb69dfe71fc1542d2111fc",
)


@dataclass(frozen=True)
class ForecastRequest:
    # Rows of [open_time_ms, open, high, low, close, volume, quote_volume]: consecutive
    # closed 1h candles, oldest first.
    candles: list[list[float]]
    horizon: int
    paths: int
    temperature: float
    top_p: float
    top_k: int
    seed: int
    max_context: int = 512
    threads: int = WORKER_THREADS
    chunk: int = DEFAULT_CHUNK


@dataclass(frozen=True)
class ForecastResult:
    ok: bool
    last_close: float | None = None
    final_closes: tuple[float, ...] = ()  # each path's close at step ``horizon``
    seconds: float | None = None
    error: str | None = None
    torch_version: str | None = None  # the worker's torch.__version__, when it reports one


def _failed(error: str) -> ForecastResult:
    return ForecastResult(ok=False, error=error[:MAX_ERROR_CHARS])


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name, "").strip()
    return Path(os.path.expandvars(os.path.expanduser(value))) if value else None


def hf_cache_dir() -> Path:
    """The Hugging Face hub cache folder, found from the environment as huggingface_hub does.

    HF_HUB_CACHE (or its legacy name HUGGINGFACE_HUB_CACHE), else $HF_HOME/hub, else
    $XDG_CACHE_HOME/huggingface/hub, else ~/.cache/huggingface/hub. Read from the environment
    only, because the app process never imports huggingface_hub.
    """
    for name in ("HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE"):
        path = _env_path(name)
        if path is not None:
            return path.absolute()
    home = _env_path("HF_HOME")
    if home is None:
        xdg = _env_path("XDG_CACHE_HOME")
        home = (xdg if xdg is not None else Path.home() / ".cache") / "huggingface"
    return (home / "hub").absolute()


def snapshot_dir(repo: str, revision: str) -> Path:
    """The folder that holds ``repo`` at ``revision`` in the Hugging Face cache."""
    return hf_cache_dir() / f"models--{repo.replace('/', '--')}" / "snapshots" / revision


def missing_weights(spec: ModelSpec = LC2004_BTCUSDT_1H) -> str | None:
    """None when both snapshot folders hold config.json and model.safetensors, else the fix."""
    for repo, revision in ((spec.model_repo, spec.model_revision),
                           (spec.tokenizer_repo, spec.tokenizer_revision)):
        folder = snapshot_dir(repo, revision)
        # is_file follows symlinks: cache snapshots are symlinks into the cache's blobs/.
        if not (folder / "config.json").is_file() or not (folder / "model.safetensors").is_file():
            return (f"Kronos weights for {repo}@{revision[:12]} are not in {folder}; "
                    f"run python3 {FETCH_TOOL}")
    return None


def worker_home() -> Path:
    return Path(_config.DATA_DIR) / "kronos_worker_home"


def worker_lock_path() -> Path:
    """The lock file that allows one worker at a time across every app process."""
    home = worker_home()
    home.mkdir(parents=True, exist_ok=True)
    return home / WORKER_LOCK_FILE


def worker_run_dir() -> Path:
    """The worker's working folder: empty, and used for nothing else."""
    folder = worker_home() / "run"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def worker_env(threads: int = WORKER_THREADS) -> dict[str, str]:
    """The worker's whole environment. Nothing is inherited from this process."""
    home = worker_home()
    home.mkdir(parents=True, exist_ok=True)
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(home),
        "HF_HUB_OFFLINE": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "OMP_NUM_THREADS": str(threads),
        "PYTHON_DOTENV_DISABLED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def worker_python() -> str:
    return _config.KRONOS_PYTHON.strip() or sys.executable


async def _poll(take: Callable[[], bool], loop: asyncio.AbstractEventLoop,
                deadline: float) -> bool:
    """Call ``take`` until it returns True or ``deadline`` (loop time) passes; never blocks."""
    while not take():
        remaining = deadline - loop.time()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(LOCK_POLL_S, remaining))
    return True


def _try_flock(fd: int) -> bool:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


async def _acquire_worker_slot(loop: asyncio.AbstractEventLoop, deadline: float) -> int | None:
    """Take the one-worker slot by ``deadline``; the locked file's descriptor, or None.

    None means another forecast held the slot until then: one in this process (the thread
    lock) or in another app process, or a worker whose app has died (the lock file). Never
    blocks the loop, and holds nothing if the caller is cancelled while waiting. The caller
    passes the descriptor to the worker and closes its own copy once the worker has exited;
    it must never unlock it, which would unlock the worker's copy too.
    """
    if not await _poll(lambda: _WORKER_LOCK.acquire(blocking=False), loop, deadline):
        return None
    try:
        fd = os.open(worker_lock_path(), os.O_RDWR | os.O_CREAT, 0o600)
    except BaseException:
        _WORKER_LOCK.release()
        raise
    try:
        locked = await _poll(lambda: _try_flock(fd), loop, deadline)
    except BaseException:
        os.close(fd)
        _WORKER_LOCK.release()
        raise
    if locked:
        return fd
    os.close(fd)
    _WORKER_LOCK.release()
    return None


async def run_forecast(
    request: ForecastRequest,
    *,
    spec: ModelSpec = LC2004_BTCUSDT_1H,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> ForecastResult:
    """One forecast. Worker failures come back as ``ok=False``, never raised.

    A cancellation kills the worker's process group and is then re-raised.
    """
    missing = missing_weights(spec)
    if missing:
        return _failed(missing)
    if not (CODE_DIR / "model" / "kronos.py").is_file():
        return _failed(f"the vendored Kronos model code is missing from {CODE_DIR}")
    # A bare name would be looked up on the worker's PATH (/usr/bin:/bin) and a relative
    # path from the worker's own folder, neither of which is what the operator meant.
    setting = _config.KRONOS_PYTHON.strip()
    if setting and not Path(setting).is_absolute():
        return _failed("KRONOS_PYTHON must be an absolute path to a Python interpreter")
    payload = {
        **asdict(request),
        "code_dir": str(CODE_DIR),
        "model_dir": str(snapshot_dir(spec.model_repo, spec.model_revision)),
        "tokenizer_dir": str(snapshot_dir(spec.tokenizer_repo, spec.tokenizer_revision)),
    }
    loop = asyncio.get_running_loop()
    # The timeout covers waiting for the one-worker lock too, so queued calls never add up
    # past the caller's limit (Claude, 2026-09-16).
    deadline = loop.time() + timeout_s
    try:
        lock_fd = await _acquire_worker_slot(loop, deadline)
    except OSError as exc:
        return _failed(f"could not open the Kronos worker lock file: {exc}")
    if lock_fd is None:
        log.warning("kronos_forecast.worker_lock_timeout", timeout_s=timeout_s)
        return _failed(f"Kronos worker timed out after {timeout_s:g} s "
                       "waiting for another forecast to finish")
    try:
        try:
            # A new session makes the worker the leader of its own process group, so
            # everything it starts can be killed with it. It inherits the locked descriptor
            # and so holds the one-worker lock until it exits.
            proc = await asyncio.create_subprocess_exec(
                worker_python(), "-I", "-B", str(WORKER_SCRIPT),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, env=worker_env(request.threads),
                cwd=str(worker_run_dir()), start_new_session=True, pass_fds=(lock_fd,),
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
        # The worker has exited (or never started): closing this copy frees the file lock.
        os.close(lock_fd)
        _WORKER_LOCK.release()
    result = read_worker_output(out, err, proc.returncode, request.paths)
    if result.ok:
        log.info("kronos_forecast.finished", seconds=result.seconds, paths=request.paths,
                 horizon=request.horizon, torch=result.torch_version)
    return result


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

    Accepted only: ``ok`` exactly true, ``final_closes`` a list of ``paths`` finite numbers,
    ``last_close`` and ``seconds`` finite numbers. The optional ``torch`` version is kept only
    as a non-blank string, cut to 40 characters.
    """
    if not isinstance(data, dict) or data.get("ok") is not True:
        return None
    closes = data.get("final_closes")
    if not (isinstance(closes, list) and len(closes) == paths
            and all(_finite_number(v) for v in closes)):
        return None
    if not (_finite_number(data.get("last_close")) and _finite_number(data.get("seconds"))):
        return None
    torch_version = data.get("torch")
    return ForecastResult(
        ok=True,
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
