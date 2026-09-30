"""Kronos forecast worker: one Kronos forecast per process, JSON in on stdin, JSON out on stdout.

Started by client.py as ``python -I -B worker.py`` with a minimal environment. It imports
nothing from the app, and its import blocker refuses app modules (``ems`` included) if
anything else tries. It kills itself if the app that started it dies (``watch_parent``). It
only forecasts: nothing here places, cancels or simulates orders.

The recipe is the lc2004 24h research run (research/kronos_lc2004_btcusdt_1h_finetune_24h_horizon/
scripts/run_lc2004_24h_forecasts_at_noon_et.py on the branch
research/kronos-lc2004-btcusdt-1h-finetune-24h-horizon) with the MIT Kronos code vendored in
third_party/kronos_67b630e: torch seeded once, then ``predict_batch`` over ``chunk`` identical
copies of the input with ``sample_count=1``, repeated until ``paths`` paths exist, so each copy
is one independently sampled path. The weights load from the absolute snapshot folders the
client names (Claude, 2026-09-16; chunked sampling and lc2004 folders, Claude, 2026-09-22;
moved into ``ems/`` for the forecast card, Claude, 2026-09-29).
"""
from __future__ import annotations

import importlib.abc
import json
import os
import signal
import sys
import threading
import time
from typing import Any

# App modules can load the app's .env, which holds the wallet key. The worker refuses them,
# even when an editable install (pip install -e) puts the app on sys.path (Claude, 2026-09-16;
# ``ems``, the app's one package since 2026-09-26, Claude, 2026-09-29). The old top-level
# names stay refused in case a stale install still provides them.
BLOCKED_IMPORTS = frozenset({"ems", "config", "db", "polymarket_bot", "polymarket_exec",
                             "dotenv", "logging_setup", "tools"})


class BlockedImportError(ImportError):
    """An import the worker process refuses."""


class AppImportBlocker(importlib.abc.MetaPathFinder):
    """A ``sys.meta_path`` finder that refuses the app's top-level modules and packages."""

    def find_spec(self, fullname: str, path: Any, target: Any = None) -> None:
        if fullname.partition(".")[0] in BLOCKED_IMPORTS:
            raise BlockedImportError(
                f"Kronos worker refused to import the app module {fullname}", name=fullname)
        return None


if __name__ == "__main__":  # the worker process only, before anything else is imported
    sys.meta_path.insert(0, AppImportBlocker())

# How often the worker checks that the app that started it is still alive (Claude, 2026-09-29).
PARENT_POLL_S = 1.0

REQUIRED_KEYS = ("candles", "horizon", "paths", "temperature", "top_p", "top_k", "seed",
                 "max_context", "threads", "chunk", "code_dir", "model_dir", "tokenizer_dir")
COLUMNS = ["open", "high", "low", "close", "volume", "amount"]
HOUR_MS = 3_600_000
# Operator setup for the worker's interpreter (pyproject.toml extra "kronos"). ``-I`` ignores
# the user site, so a library installed with ``pip install --user`` is not seen here.
SETUP_HINT = ("install the optional 'kronos' extra into that interpreter "
              "(python3 -m pip install -e '.[kronos]') or set KRONOS_PYTHON to an absolute path "
              "of an interpreter that has torch, einops, pandas, safetensors, huggingface_hub "
              "and tqdm outside the user site")


def validate(request: dict[str, Any]) -> str | None:
    missing = [k for k in REQUIRED_KEYS if k not in request]
    if missing:
        return f"request is missing {', '.join(missing)}"
    candles = request["candles"]
    if not candles or any(len(row) != 7 for row in candles):
        return ("candles must be non-empty rows of "
                "[open_time_ms, open, high, low, close, volume, quote_volume]")
    times = [int(row[0]) for row in candles]
    if any(b - a != HOUR_MS for a, b in zip(times, times[1:])):
        return "candles must be consecutive 1h candles, oldest first"
    if int(request["paths"]) < 1 or int(request["horizon"]) < 1 or int(request["chunk"]) < 1:
        return "paths, horizon and chunk must be positive"
    for key in ("model_dir", "tokenizer_dir"):
        if not os.path.isabs(str(request[key])):
            return f"{key} must be an absolute path to a weights snapshot folder"
    return None


def build_frames(candles: list[list[float]], horizon: int):
    import pandas as pd

    df = pd.DataFrame([row[1:] for row in candles], columns=COLUMNS, dtype="float64")
    x_ts = pd.Series(pd.to_datetime([int(row[0]) for row in candles], unit="ms"))
    first_future = pd.Timestamp(int(candles[-1][0]) + HOUR_MS, unit="ms")
    y_ts = pd.Series(pd.date_range(first_future, periods=horizon, freq="h"))
    return df, x_ts, y_ts


def chunk_sizes(paths: int, chunk: int) -> list[int]:
    """Batch sizes for ``paths`` paths drawn ``chunk`` at a time: 30 by 15 is [15, 15]."""
    sizes = []
    while sum(sizes) < paths:
        sizes.append(min(chunk, paths - sum(sizes)))
    return sizes


def missing_library_error(exc: ImportError) -> str:
    """What is missing, in which interpreter, and the two ways to fix it."""
    missing = exc.name or str(exc)[:60]
    return f"Kronos worker is missing {missing} in {sys.executable}; {SETUP_HINT}"


def run(request: dict[str, Any]) -> dict[str, Any]:
    problem = validate(request)
    if problem:
        return {"ok": False, "error": problem}
    for key in ("model_dir", "tokenizer_dir"):
        if not os.path.isdir(str(request[key])):
            return {"ok": False, "error": f"{key} {request[key]} is not a folder"}
    sys.path.insert(0, str(request["code_dir"]))
    try:
        import torch
        from model import Kronos, KronosPredictor, KronosTokenizer
    except BlockedImportError as exc:
        return {"ok": False, "error": str(exc)}
    except ImportError as exc:
        return {"ok": False, "error": missing_library_error(exc)}

    started = time.monotonic()
    torch.set_num_threads(int(request["threads"]))
    tokenizer = KronosTokenizer.from_pretrained(str(request["tokenizer_dir"]))
    model = Kronos.from_pretrained(str(request["model_dir"]))
    tokenizer.eval()
    model.eval()
    predictor = KronosPredictor(model, tokenizer, device="cpu",
                                max_context=int(request["max_context"]))
    horizon = int(request["horizon"])
    paths = int(request["paths"])
    df, x_ts, y_ts = build_frames(request["candles"], horizon)
    # Seeded once, after the weights load and before the first batch, as in the research run.
    torch.manual_seed(int(request["seed"]))
    finals: list[float] = []
    with torch.no_grad():
        for size in chunk_sizes(paths, int(request["chunk"])):
            preds = predictor.predict_batch(
                df_list=[df] * size, x_timestamp_list=[x_ts] * size,
                y_timestamp_list=[y_ts] * size, pred_len=horizon,
                T=float(request["temperature"]), top_k=int(request["top_k"]),
                top_p=float(request["top_p"]), sample_count=1, verbose=False,
            )
            finals += [float(frame["close"].iloc[-1]) for frame in preds]
    return {
        "ok": True,
        "last_close": float(request["candles"][-1][4]),
        "final_closes": finals,
        "seconds": round(time.monotonic() - started, 3),
        "torch": str(getattr(torch, "__version__", "")),
    }


def _stop_worker() -> None:
    """Kill this worker and everything it started; exit on its own if it leads no group."""
    try:
        if os.getpgrp() == os.getpid():  # started in its own session, as the client does
            os.killpg(os.getpid(), signal.SIGKILL)
    finally:
        os._exit(1)


def _watch_parent(parent_pid: int) -> None:
    while os.getppid() == parent_pid:
        time.sleep(PARENT_POLL_S)
    _stop_worker()


def watch_parent() -> None:
    """Stop the worker once the app that started it has gone.

    The client starts the worker in a session of its own, so a terminal's hang-up or a Ctrl-C
    never reaches it, and the client's timeout lives in the app. If the app dies without
    cleaning up (kill -9, its terminal closed, a crash, macOS ending it for memory), the
    orphan is re-parented and this thread kills the worker's process group within about
    ``PARENT_POLL_S``, so it never runs the model beside a restarted app's worker on the
    8 GB machine (Claude, 2026-09-29, for a review finding).
    """
    parent = os.getppid()
    if parent == 1:  # the app was already gone before the worker got here
        _stop_worker()
    threading.Thread(target=_watch_parent, args=(parent,), name="kronos-parent-watch",
                     daemon=True).start()


def main() -> int:
    watch_parent()
    try:
        result = run(json.loads(sys.stdin.read()))
    except Exception as exc:  # noqa: BLE001 - every failure is reported as JSON
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    sys.stdout.write(json.dumps(result) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
