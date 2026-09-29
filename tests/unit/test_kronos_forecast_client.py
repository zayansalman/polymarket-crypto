"""Kronos forecast client: pinned lc2004 sources, Hugging Face cache, isolated worker, timeouts.

Every worker here is a stand-in script: nothing loads torch or the real weights
(Claude, 2026-09-16; lc2004, Hugging Face cache, chunked sampling, cross-loop lock,
Claude, 2026-09-22; ems.kronos_forecast, Claude, 2026-09-29).
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from ems.kronos_forecast import client as kc

SPEC = kc.LC2004_BTCUSDT_1H
REQUEST = kc.ForecastRequest(candles=[[0, 1, 1, 1, 1, 1, 1]], horizon=1, paths=2,
                             temperature=1.0, top_p=0.9, top_k=0, seed=1)
# Stands in for the wallet key. Distinctive, so any leak into the worker is unmistakable.
FAKE_KEY = "0xDEADBEEF_TEST_KEY_NOT_REAL"
HF_ENV = ("HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE", "HF_HOME", "XDG_CACHE_HOME")
FETCH_HINT = "; run python3 tools/fetch_lc2004_kronos_weights.py"


def _fill_snapshot(folder: Path) -> None:
    folder.mkdir(parents=True)
    (folder / "config.json").write_text("{}")
    (folder / "model.safetensors").write_bytes(b"x")


@pytest.fixture
def models(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A Hugging Face cache in tmp_path/hub holding both pinned snapshots."""
    monkeypatch.setattr(kc._config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(kc._config, "KRONOS_PYTHON", "")  # the test's own interpreter
    for name in HF_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "hub"))
    for repo, rev in ((SPEC.model_repo, SPEC.model_revision),
                      (SPEC.tokenizer_repo, SPEC.tokenizer_revision)):
        _fill_snapshot(kc.snapshot_dir(repo, rev))
    return tmp_path


def _fake_worker(tmp_path: Path, body: str, **constants: object) -> Path:
    """A stand-in worker script; ``constants`` become module-level names in it."""
    script = tmp_path / "fake_worker.py"
    preamble = "".join(f"{name} = {value!r}\n" for name, value in constants.items())
    script.write_text(preamble + textwrap.dedent(body))
    return script


def test_pinned_sources() -> None:
    assert SPEC == kc.ModelSpec(
        "lc2004/kronos_base_model_BTCUSDT_1h_finetune", "eb51e682c8194a1ba7254cc4357c75819683fbaf",
        "lc2004/kronos_tokenizer_base_BTCUSDT_1h_finetune",
        "b8f1c795b80231f5bdeb69dfe71fc1542d2111fc")
    assert kc.KRONOS_CODE_COMMIT == "67b630e67f6a18c9e9be918d9b4337c960db1e9a"
    assert kc.CODE_DIR == kc.REPO_ROOT / "third_party" / "kronos_67b630e"
    # ems/kronos_forecast/client.py sits two folders below the repository root.
    assert (kc.REPO_ROOT / "pyproject.toml").is_file()
    assert kc.WORKER_SCRIPT == kc.REPO_ROOT / "ems" / "kronos_forecast" / "worker.py"
    for name in ("__init__.py", "kronos.py", "module.py"):
        assert (kc.CODE_DIR / "model" / name).is_file()


def test_defaults_follow_the_research_recipe_and_the_lc2004_spec() -> None:
    assert kc.DEFAULT_TIMEOUT_S == 900.0
    params = inspect.signature(kc.run_forecast).parameters
    assert params["spec"].default is kc.LC2004_BTCUSDT_1H
    assert params["timeout_s"].default == 900.0
    assert inspect.signature(kc.missing_weights).parameters["spec"].default is SPEC
    assert (REQUEST.max_context, REQUEST.threads, REQUEST.chunk) == (512, 4, 15)


def test_the_app_side_never_imports_torch_or_huggingface_hub(tmp_path: Path) -> None:
    env = {**os.environ, "PYTHON_DOTENV_DISABLED": "1", "DATA_DIR": str(tmp_path)}
    env.pop("POLYMARKET_PRIVATE_KEY", None)
    probe = ("import sys; import ems.kronos_forecast.client; "
             "print(sorted(m for m in ('torch', 'huggingface_hub', 'pandas') if m in sys.modules))")
    out = subprocess.run([sys.executable, "-c", probe], cwd=kc.REPO_ROOT, env=env,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]"


def test_hf_cache_dir_follows_the_hugging_face_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in HF_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert kc.hf_cache_dir() == tmp_path / "home" / ".cache" / "huggingface" / "hub"
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert kc.hf_cache_dir() == tmp_path / "xdg" / "huggingface" / "hub"
    monkeypatch.setenv("HF_HOME", "~/hf")
    assert kc.hf_cache_dir() == tmp_path / "home" / "hf" / "hub"
    monkeypatch.setenv("HUGGINGFACE_HUB_CACHE", str(tmp_path / "legacy"))
    assert kc.hf_cache_dir() == tmp_path / "legacy"
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "hub"))
    assert kc.hf_cache_dir() == tmp_path / "hub"
    monkeypatch.setenv("HF_HUB_CACHE", "  ")  # blank counts as unset
    assert kc.hf_cache_dir() == tmp_path / "legacy"


def test_snapshot_dir_is_the_standard_cache_layout(models: Path) -> None:
    assert kc.snapshot_dir(SPEC.model_repo, SPEC.model_revision) == (
        models / "hub" / "models--lc2004--kronos_base_model_BTCUSDT_1h_finetune" / "snapshots"
        / "eb51e682c8194a1ba7254cc4357c75819683fbaf")
    assert kc.snapshot_dir(SPEC.tokenizer_repo, SPEC.tokenizer_revision) == (
        models / "hub" / "models--lc2004--kronos_tokenizer_base_BTCUSDT_1h_finetune"
        / "snapshots" / "b8f1c795b80231f5bdeb69dfe71fc1542d2111fc")


def test_missing_weights_names_the_folder_and_the_fetch_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "hub"))
    problem = kc.missing_weights()
    assert problem is not None and problem.endswith(FETCH_HINT)
    assert "lc2004/kronos_base_model_BTCUSDT_1h_finetune@eb51e682c819" in problem
    model = kc.snapshot_dir(SPEC.model_repo, SPEC.model_revision)
    _fill_snapshot(model)
    problem = kc.missing_weights()  # the tokenizer is still missing
    assert problem is not None and "lc2004/kronos_tokenizer_base_BTCUSDT_1h_finetune" in problem
    tokenizer = kc.snapshot_dir(SPEC.tokenizer_repo, SPEC.tokenizer_revision)
    tokenizer.mkdir(parents=True)
    (tokenizer / "config.json").write_text("{}")
    assert kc.missing_weights() is not None  # config.json alone is not enough
    (tokenizer / "model.safetensors").write_bytes(b"x")
    assert kc.missing_weights() is None


def test_cache_snapshots_that_are_symlinks_into_blobs_count_as_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """snapshot_download writes blobs/<hash> and links snapshots/<rev>/<file> to them."""
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "hub"))
    for repo, rev in ((SPEC.model_repo, SPEC.model_revision),
                      (SPEC.tokenizer_repo, SPEC.tokenizer_revision)):
        folder = kc.snapshot_dir(repo, rev)
        blobs = folder.parents[1] / "blobs"
        blobs.mkdir(parents=True)
        folder.mkdir(parents=True)
        for name in ("config.json", "model.safetensors"):
            blob = blobs / f"hash-{name}"
            blob.write_bytes(b"x")
            (folder / name).symlink_to(os.path.relpath(blob, folder))
    assert kc.missing_weights() is None


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
    assert env["OMP_NUM_THREADS"] == "4" and kc.worker_env(2)["OMP_NUM_THREADS"] == "2"
    assert not any(FAKE_KEY in value for value in env.values())


async def _boom(*args, **kwargs):
    raise AssertionError("no worker process may start")


@pytest.mark.asyncio
async def test_missing_weights_are_reported_without_starting_a_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "empty"))
    monkeypatch.setattr(kc.asyncio, "create_subprocess_exec", _boom)
    result = await kc.run_forecast(REQUEST)
    assert result.ok is False
    assert result.error.endswith(FETCH_HINT)


@pytest.mark.asyncio
async def test_missing_model_code_is_reported_without_starting_a_process(
    models: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(kc, "CODE_DIR", models / "no_code")
    monkeypatch.setattr(kc.asyncio, "create_subprocess_exec", _boom)
    result = await kc.run_forecast(REQUEST)
    assert result == kc.ForecastResult(
        ok=False, error=f"the vendored Kronos model code is missing from {models / 'no_code'}")


@pytest.mark.asyncio
@pytest.mark.parametrize("setting", ["python3", "venv/bin/python", "~/venv/bin/python"])
async def test_a_kronos_python_that_is_not_an_absolute_path_is_refused(
    models: Path, monkeypatch: pytest.MonkeyPatch, setting: str
) -> None:
    monkeypatch.setattr(kc._config, "KRONOS_PYTHON", setting)
    monkeypatch.setattr(kc.asyncio, "create_subprocess_exec", _boom)
    result = await kc.run_forecast(REQUEST)
    assert result == kc.ForecastResult(
        ok=False, error="KRONOS_PYTHON must be an absolute path to a Python interpreter")


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
        if sys.flags.dont_write_bytecode != 1:
            print("the worker was not started with -B", file=sys.stderr)
            sys.exit(4)
        run_dir = os.path.realpath(os.path.join(os.environ["HOME"], "run"))
        if os.path.realpath(os.getcwd()) != run_dir or os.listdir(run_dir):
            print(f"the worker runs in {os.getcwd()}, not the empty {run_dir}", file=sys.stderr)
            sys.exit(5)
        if os.environ.get("HF_HUB_OFFLINE") != "1" or os.environ.get("OMP_NUM_THREADS") != "4":
            print("the worker is not offline with 4 threads", file=sys.stderr)
            sys.exit(6)
        print("torch warning line")
        print(json.dumps({"ok": True, "last_close": 100.0,
                          "final_closes": [101.0, 99.0], "seconds": 0.5, "torch": "2.10.0"}))
    """, FAKE_KEY=FAKE_KEY))
    result = await kc.run_forecast(REQUEST)
    assert result == kc.ForecastResult(ok=True, last_close=100.0,
                                       final_closes=(101.0, 99.0), seconds=0.5,
                                       torch_version="2.10.0")


GOOD_RESULT = {"ok": True, "last_close": 100.0, "final_closes": [101.0, 99.0], "seconds": 0.5}


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
    "null_seconds": _result_line(seconds=None),
    "true_as_last_close": _result_line(last_close=True),
    "text_last_close": _result_line(last_close="100.0"),
    "infinite_final_close": _result_line(final_closes=[101.0, float("inf")]),
    "nan_final_close": _result_line(final_closes=[101.0, float("nan")]),
    "text_final_close": _result_line(final_closes=[101.0, "99.0"]),
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
async def test_payload_carries_the_request_and_absolute_snapshot_folders(
    models: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = models / "payload.json"
    monkeypatch.setattr(kc, "WORKER_SCRIPT", _fake_worker(models, """
        import sys
        from pathlib import Path
        Path(SEEN).write_text(sys.stdin.read())
        print(LINE)
    """, SEEN=str(seen), LINE=json.dumps(GOOD_RESULT)))
    assert (await kc.run_forecast(REQUEST)).ok
    payload = json.loads(seen.read_text())
    assert payload["model_dir"] == str(
        models / "hub" / "models--lc2004--kronos_base_model_BTCUSDT_1h_finetune" / "snapshots"
        / "eb51e682c8194a1ba7254cc4357c75819683fbaf")
    assert payload["tokenizer_dir"] == str(
        models / "hub" / "models--lc2004--kronos_tokenizer_base_BTCUSDT_1h_finetune"
        / "snapshots" / "b8f1c795b80231f5bdeb69dfe71fc1542d2111fc")
    assert Path(payload["model_dir"]).is_absolute() and Path(payload["tokenizer_dir"]).is_absolute()
    assert payload["code_dir"] == str(kc.CODE_DIR)
    assert payload["candles"] == [[0, 1, 1, 1, 1, 1, 1]]
    assert {k: payload[k] for k in ("horizon", "paths", "temperature", "top_p", "top_k", "seed",
                                    "max_context", "threads", "chunk")} == {
        "horizon": 1, "paths": 2, "temperature": 1.0, "top_p": 0.9, "top_k": 0, "seed": 1,
        "max_context": 512, "threads": 4, "chunk": 15}


@pytest.mark.asyncio
async def test_the_request_threads_set_the_worker_threads(
    models: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(kc, "WORKER_SCRIPT", _fake_worker(models, """
        import json, os, sys
        request = json.loads(sys.stdin.read())
        if os.environ["OMP_NUM_THREADS"] != str(request["threads"]):
            print(json.dumps({"ok": False, "error": "threads differ"}))
            sys.exit(0)
        print(LINE)
    """, LINE=json.dumps(GOOD_RESULT)))
    request = kc.ForecastRequest(candles=[[0, 1, 1, 1, 1, 1, 1]], horizon=1, paths=2,
                                 temperature=1.0, top_p=0.9, top_k=0, seed=1, threads=2)
    result = await kc.run_forecast(request)
    assert result.ok, result.error


@pytest.mark.parametrize(("torch_field", "expected"), [
    ({}, None), ({"torch": None}, None), ({"torch": 2.1}, None), ({"torch": " "}, None),
    ({"torch": "2.10.0+cpu"}, "2.10.0+cpu"), ({"torch": "v" * 100}, "v" * 40),
])
def test_the_torch_version_is_optional_and_short(torch_field: dict, expected: str | None) -> None:
    result = kc.parse_worker_result({**GOOD_RESULT, **torch_field}, paths=2)
    assert result is not None and result.ok is True
    assert result.torch_version == expected


@pytest.mark.asyncio
async def test_worker_error_is_reported(models: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kc, "WORKER_SCRIPT", _fake_worker(models, """
        import json
        print(json.dumps({"ok": False, "error": "ModuleNotFoundError: No module named 'torch'"}))
    """))
    failed = await kc.run_forecast(REQUEST)
    assert failed.ok is False and "torch" in failed.error


@pytest.mark.asyncio
async def test_a_kronos_python_that_does_not_exist_is_a_failure_not_a_crash(
    models: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(kc._config, "KRONOS_PYTHON", str(models / "no" / "python"))
    failed = await kc.run_forecast(REQUEST)
    assert failed.ok is False and failed.error.startswith("could not start the Kronos worker: ")


async def _process_ends(pid: int, within_s: float = 5.0) -> bool:
    """True once ``pid`` is gone. A killed grandchild is reaped by init, not by us: poll."""
    deadline = time.monotonic() + within_s
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            return True
        await asyncio.sleep(0.02)
    return False


async def _read_pids(path: Path, count: int, within_s: float = 10.0) -> list[int]:
    deadline = time.monotonic() + within_s
    while time.monotonic() < deadline:
        words = path.read_text().split() if path.exists() else []
        if len(words) == count:
            return [int(w) for w in words]
        await asyncio.sleep(0.01)
    raise AssertionError(f"the fake worker never wrote {count} PID(s) to {path}")


# Starts a helper process (it joins the worker's process group), records both PIDs, sleeps.
SLEEPING_WORKER = """
    import os, subprocess, sys, time
    from pathlib import Path
    helper = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(20)"])
    Path(PID_FILE).write_text(f"{os.getpid()} {helper.pid}")
    time.sleep(20)
"""


class _LogRecorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def warning(self, event: str, **fields: object) -> None:
        self.events.append((event, fields))

    info = warning


@pytest.mark.asyncio
async def test_error_text_is_cut_to_400_characters_in_results_and_logs(
    models: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _LogRecorder()
    monkeypatch.setattr(kc, "log", recorder)
    monkeypatch.setattr(kc, "WORKER_SCRIPT", _fake_worker(models, """
        import json
        print(json.dumps({"ok": False, "error": "E" * 1000}))
    """))
    assert await kc.run_forecast(REQUEST) == kc.ForecastResult(ok=False, error="E" * 400)

    monkeypatch.setattr(kc, "WORKER_SCRIPT", _fake_worker(models, """
        import sys
        sys.stderr.write("S" * 1000)
        sys.exit(1)
    """))
    assert await kc.run_forecast(REQUEST) == kc.ForecastResult(ok=False, error="S" * 400)

    async def cannot_start(*args, **kwargs):
        raise OSError("O" * 1000)

    monkeypatch.setattr(kc.asyncio, "create_subprocess_exec", cannot_start)
    failed_start = await kc.run_forecast(REQUEST)
    assert failed_start.ok is False and len(failed_start.error) == 400
    assert failed_start.error.startswith("could not start the Kronos worker: OOO")

    logged = [fields["error"] for _, fields in recorder.events if "error" in fields]
    assert len(logged) >= 2 and all(len(text) <= 400 for text in logged)


@pytest.mark.asyncio
async def test_timeout_kills_the_worker_and_the_processes_it_started(
    models: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_file = models / "worker.pids"
    monkeypatch.setattr(kc, "WORKER_SCRIPT",
                        _fake_worker(models, SLEEPING_WORKER, PID_FILE=str(pid_file)))
    started = time.monotonic()
    slow = await kc.run_forecast(REQUEST, timeout_s=1.0)
    assert slow == kc.ForecastResult(ok=False, error="Kronos worker timed out after 1 s")
    assert time.monotonic() - started < 10
    worker_pid, helper_pid = await _read_pids(pid_file, 2)
    with pytest.raises(ProcessLookupError):
        os.kill(worker_pid, 0)
    assert await _process_ends(helper_pid), "a process the worker started outlived the timeout"
    assert not kc._WORKER_LOCK.locked()


@pytest.mark.asyncio
async def test_cancelling_a_forecast_kills_the_worker(
    models: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_file = models / "worker.pids"
    monkeypatch.setattr(kc, "WORKER_SCRIPT",
                        _fake_worker(models, SLEEPING_WORKER, PID_FILE=str(pid_file)))
    task = asyncio.create_task(kc.run_forecast(REQUEST, timeout_s=60))
    worker_pid, helper_pid = await _read_pids(pid_file, 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ProcessLookupError):
        os.kill(worker_pid, 0)
    assert await _process_ends(helper_pid), "a process the worker started outlived the cancel"
    assert not kc._WORKER_LOCK.locked()


# Fails if another fake worker is running at the same moment (an exclusive marker file).
EXCLUSIVE_WORKER = """
    import json, os, sys, time
    sys.stdin.read()
    try:
        marker = os.open(MARKER, os.O_CREAT | os.O_EXCL)
    except FileExistsError:
        print(json.dumps({"ok": False, "error": "another Kronos worker was running"}))
        sys.exit(0)
    time.sleep(0.3)
    os.close(marker)
    os.remove(MARKER)
    print(LINE)
"""


@pytest.mark.asyncio
async def test_only_one_worker_runs_at_a_time(
    models: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(kc, "_WORKER_LOCK", threading.Lock())
    monkeypatch.setattr(kc, "WORKER_SCRIPT", _fake_worker(
        models, EXCLUSIVE_WORKER, MARKER=str(models / "running"), LINE=json.dumps(GOOD_RESULT)))
    first, second = await asyncio.gather(kc.run_forecast(REQUEST), kc.run_forecast(REQUEST))
    assert first.ok and second.ok, (first.error, second.error)


def test_only_one_worker_runs_at_a_time_across_event_loops(
    models: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dashboard's loop and the strategy loop's own thread share one worker slot."""
    monkeypatch.setattr(kc, "_WORKER_LOCK", threading.Lock())
    monkeypatch.setattr(kc, "WORKER_SCRIPT", _fake_worker(
        models, EXCLUSIVE_WORKER, MARKER=str(models / "running"), LINE=json.dumps(GOOD_RESULT)))
    results: list[kc.ForecastResult | BaseException | None] = [None, None]

    def forecast_on_its_own_loop(slot: int) -> None:
        try:
            results[slot] = asyncio.run(kc.run_forecast(REQUEST, timeout_s=30))
        except BaseException as exc:  # noqa: BLE001 - reported by the assertion below
            results[slot] = exc

    threads = [threading.Thread(target=forecast_on_its_own_loop, args=(i,)) for i in (0, 1)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert all(isinstance(r, kc.ForecastResult) and r.ok for r in results), results
    # A later loop can still use it (an asyncio.Lock would stay bound to the first loop).
    assert asyncio.run(kc.run_forecast(REQUEST, timeout_s=30)).ok


@pytest.mark.asyncio
async def test_waiting_for_the_worker_lock_counts_toward_the_timeout(
    models: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two queued calls never add up past the caller's limit (Claude, 2026-09-16)."""
    lock = threading.Lock()
    monkeypatch.setattr(kc, "_WORKER_LOCK", lock)
    monkeypatch.setattr(kc.asyncio, "create_subprocess_exec", _boom)
    loop = asyncio.get_running_loop()
    lock.acquire()  # a worker is already running
    try:
        started = loop.time()
        # The outer guard turns waiting on the lock forever into a failure, not a hung run.
        queued = await asyncio.wait_for(kc.run_forecast(REQUEST, timeout_s=0.5), 5.0)
        waited = loop.time() - started
    finally:
        lock.release()
    assert queued == kc.ForecastResult(
        ok=False,
        error="Kronos worker timed out after 0.5 s waiting for another forecast to finish")
    assert waited < 2.0


@pytest.mark.asyncio
async def test_a_call_cancelled_while_queued_holds_nothing(
    models: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = threading.Lock()
    monkeypatch.setattr(kc, "_WORKER_LOCK", lock)
    monkeypatch.setattr(kc, "WORKER_SCRIPT", _fake_worker(models, """
        import sys
        sys.stdin.read()
        print(LINE)
    """, LINE=json.dumps(GOOD_RESULT)))
    lock.acquire()  # a worker is already running
    queued = asyncio.create_task(kc.run_forecast(REQUEST, timeout_s=60))
    await asyncio.sleep(0.2)
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    lock.release()
    assert not lock.locked()
    assert (await kc.run_forecast(REQUEST, timeout_s=30)).ok


# --- one worker across app processes (review finding, Claude, 2026-09-29) --------------------

# Another process holding the lock file: an app whose worker is still running.
HOLD_LOCK = """
import fcntl, os, sys, time
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
print("locked", flush=True)
time.sleep(30)
"""


@pytest.mark.asyncio
async def test_a_worker_held_by_another_app_process_blocks_a_new_one(
    models: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(kc, "_WORKER_LOCK", threading.Lock())
    monkeypatch.setattr(kc, "WORKER_SCRIPT", _fake_worker(models, """
        import sys
        sys.stdin.read()
        print(LINE)
    """, LINE=json.dumps(GOOD_RESULT)))
    holder = subprocess.Popen([sys.executable, "-I", "-c", HOLD_LOCK, str(kc.worker_lock_path())],
                              stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "locked"
        busy = await kc.run_forecast(REQUEST, timeout_s=0.5)
        assert busy == kc.ForecastResult(
            ok=False,
            error="Kronos worker timed out after 0.5 s waiting for another forecast to finish")
        assert not kc._WORKER_LOCK.locked()  # a busy slot holds nothing in this process
    finally:
        holder.kill()
        holder.wait(10)
    assert (await kc.run_forecast(REQUEST, timeout_s=30)).ok


# The app stand-in: runs one forecast through the real client with a sleeping stand-in worker
# (one that does not watch its parent), then gets killed with -9 by the test.
APP = """
import asyncio, sys
from pathlib import Path
sys.path.insert(0, ROOT)
from ems.kronos_forecast import client as kc
kc._config.DATA_DIR = Path(DATA_DIR)
kc._config.KRONOS_PYTHON = ""
kc.WORKER_SCRIPT = Path(SCRIPT)
request = kc.ForecastRequest(candles=[[0, 1, 1, 1, 1, 1, 1]], horizon=1, paths=2,
                             temperature=1.0, top_p=0.9, top_k=0, seed=1)
asyncio.run(kc.run_forecast(request, timeout_s=60))
"""


@pytest.mark.asyncio
async def test_a_worker_orphaned_by_a_killed_app_still_holds_the_slot(
    models: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """kill -9 on the app leaves its worker holding the lock, so a restarted app waits."""
    monkeypatch.setattr(kc, "_WORKER_LOCK", threading.Lock())
    pid_file = models / "worker.pids"
    script = _fake_worker(models, SLEEPING_WORKER, PID_FILE=str(pid_file))
    root = str(Path(kc.__file__).resolve().parents[2])
    env = {"PATH": "/usr/bin:/bin", "PYTHON_DOTENV_DISABLED": "1", "DATA_DIR": str(models),
           "HF_HUB_CACHE": str(models / "hub")}
    app = subprocess.Popen([sys.executable, "-c", (
        f"ROOT = {root!r}\nDATA_DIR = {str(models)!r}\nSCRIPT = {str(script)!r}\n" + APP)],
        env=env, cwd=str(models))
    worker_pid = None
    try:
        worker_pid, _helper_pid = await _read_pids(pid_file, 2, within_s=30)
        app.kill()
        app.wait(10)
        os.kill(worker_pid, 0)  # the orphan is still running
        busy = await kc.run_forecast(REQUEST, timeout_s=0.5)
        assert busy.ok is False and "waiting for another forecast to finish" in busy.error
    finally:
        if app.poll() is None:
            app.kill()
            app.wait(10)
        if worker_pid is not None:
            try:
                os.killpg(worker_pid, 9)
            except (ProcessLookupError, PermissionError):
                pass
    assert await _process_ends(worker_pid)
    monkeypatch.setattr(kc, "WORKER_SCRIPT", _fake_worker(models, """
        import sys
        sys.stdin.read()
        print(LINE)
    """, LINE=json.dumps(GOOD_RESULT)))
    assert (await kc.run_forecast(REQUEST, timeout_s=30)).ok  # freed once the orphan is gone
