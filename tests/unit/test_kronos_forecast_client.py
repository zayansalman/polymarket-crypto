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
