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
