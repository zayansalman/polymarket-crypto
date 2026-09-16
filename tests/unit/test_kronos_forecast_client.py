"""Kronos forecast client: minimal worker environment, weights check, timeout, error handling."""
from __future__ import annotations

import json
import os
import textwrap
import time
from pathlib import Path

import pytest

from polymarket_bot.kronos_forecast import client as kc

SPEC = kc.KRONOS_MINI_WITH_TOKENIZER_2K
REQUEST = kc.ForecastRequest(candles=[[0, 1, 1, 1, 1, 1, 1]], horizon=1, paths=2,
                             temperature=1.0, top_p=0.95, top_k=0, seed=1)
# Stands in for the wallet key. Distinctive, so any leak into the worker is unmistakable.
FAKE_KEY = "0xDEADBEEF_TEST_KEY_NOT_REAL"


@pytest.fixture
def models(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(kc._config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(kc, "models_dir", lambda: tmp_path / "models")
    for repo, rev in ((SPEC.model_repo, SPEC.model_revision),
                      (SPEC.tokenizer_repo, SPEC.tokenizer_revision)):
        d = kc.snapshot_dir(repo, rev)
        d.mkdir(parents=True)
        (d / "config.json").write_text("{}")
        (d / "model.safetensors").write_bytes(b"x")
    return tmp_path


def _fake_worker(tmp_path: Path, body: str, **constants: object) -> Path:
    """A stand-in worker script; ``constants`` become module-level names in it."""
    script = tmp_path / "fake_worker.py"
    preamble = "".join(f"{name} = {value!r}\n" for name, value in constants.items())
    script.write_text(preamble + textwrap.dedent(body))
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
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", FAKE_KEY)
    monkeypatch.setattr(kc._config, "DATA_DIR", tmp_path)
    env = kc.worker_env()
    assert set(env) == {"PATH", "HOME", "HF_HUB_OFFLINE", "HF_HUB_DISABLE_TELEMETRY",
                        "TRANSFORMERS_OFFLINE", "OMP_NUM_THREADS", "PYTHON_DOTENV_DISABLED",
                        "PYTHONDONTWRITEBYTECODE"}
    assert env["HOME"] == str(tmp_path / "kronos_worker_home")
    assert env["HF_HUB_OFFLINE"] == "1" and env["PYTHON_DOTENV_DISABLED"] == "1"
    assert not any(FAKE_KEY in value for value in env.values())


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
async def test_success_line_is_parsed_from_an_isolated_worker_that_cannot_see_the_key(
    models: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", FAKE_KEY)
    monkeypatch.setattr(kc, "WORKER_SCRIPT", _fake_worker(models, """
        import json, os, sys
        sys.stdin.read()
        if "POLYMARKET_PRIVATE_KEY" in os.environ or any(
                FAKE_KEY in value for value in os.environ.values()):
            print("the wallet key reached the worker", file=sys.stderr)
            sys.exit(2)
        if sys.flags.isolated != 1:
            print("the worker was not started with -I", file=sys.stderr)
            sys.exit(3)
        print("torch warning line")
        print(json.dumps({"ok": True, "upside_prob": 0.5, "last_close": 100.0,
                          "final_closes": [101.0, 99.0], "seconds": 0.5}))
    """, FAKE_KEY=FAKE_KEY))
    result = await kc.run_forecast(REQUEST)
    assert result == kc.ForecastResult(ok=True, upside_prob=0.5, last_close=100.0,
                                       final_closes=(101.0, 99.0), seconds=0.5)


GOOD_RESULT = {"ok": True, "upside_prob": 0.5, "last_close": 100.0,
               "final_closes": [101.0, 99.0], "seconds": 0.5}


def _result_line(**changes: object) -> str:
    return json.dumps({**GOOD_RESULT, **changes})


# Last stdout lines that must never pass as a forecast (REQUEST asks for 2 paths).
UNREADABLE_LINES = {
    "a_list": "[1,2]",
    "null": "null",
    "a_string": '"done"',
    "no_output": "",
    "only_ok": '{"ok": true}',
    "ok_false_without_an_error": '{"ok": false}',
    "ok_is_the_string_false": _result_line(ok="false"),
    "ok_is_one": _result_line(ok=1),
    "null_probability": _result_line(upside_prob=None),
    "null_seconds": _result_line(seconds=None),
    "nan_probability": _result_line(upside_prob=float("nan")),
    "probability_above_one": _result_line(upside_prob=1.5),
    "probability_below_zero": _result_line(upside_prob=-0.2),
    "true_as_probability": _result_line(upside_prob=True),
    "text_last_close": _result_line(last_close="100.0"),
    "infinite_final_close": _result_line(final_closes=[101.0, float("inf")]),
    "too_few_final_closes": _result_line(final_closes=[101.0]),
    "too_many_final_closes": _result_line(final_closes=[101.0, 99.0, 98.0]),
    "long_line": '{"ok": true, "padding": "' + "x" * 500 + '"}',
}


@pytest.mark.asyncio
@pytest.mark.parametrize("line", list(UNREADABLE_LINES.values()), ids=list(UNREADABLE_LINES))
async def test_an_unreadable_last_line_is_a_failure(
    models: Path, monkeypatch: pytest.MonkeyPatch, line: str
) -> None:
    monkeypatch.setattr(kc, "WORKER_SCRIPT", _fake_worker(models, """
        import sys
        sys.stdin.read()
        print(LINE)
    """, LINE=line))
    result = await kc.run_forecast(REQUEST)
    assert result == kc.ForecastResult(
        ok=False, error="Kronos worker returned an unreadable result: " + repr(line)[:200])


@pytest.mark.asyncio
async def test_worker_error_is_reported(models: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kc, "WORKER_SCRIPT", _fake_worker(models, """
        import json
        print(json.dumps({"ok": False, "error": "ModuleNotFoundError: No module named 'torch'"}))
    """))
    failed = await kc.run_forecast(REQUEST)
    assert failed.ok is False and "torch" in failed.error


@pytest.mark.asyncio
async def test_timeout_is_reported_and_the_worker_process_is_gone(
    models: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_file = models / "worker.pid"
    monkeypatch.setattr(kc, "WORKER_SCRIPT", _fake_worker(models, """
        import os, time
        from pathlib import Path
        Path(PID_FILE).write_text(str(os.getpid()))
        time.sleep(20)
    """, PID_FILE=str(pid_file)))
    started = time.monotonic()
    slow = await kc.run_forecast(REQUEST, timeout_s=1.0)
    assert slow == kc.ForecastResult(ok=False, error="Kronos worker timed out after 1 s")
    assert time.monotonic() - started < 10
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)
