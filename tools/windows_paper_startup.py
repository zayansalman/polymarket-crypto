"""Install and launch the user-scoped Windows paper-trading dashboard."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
APP_HOME = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "PolymarketCrypto"
INSTALL_ROOT = APP_HOME / "repository"
DATA_ROOT = APP_HOME / "data"
LOG_ROOT = APP_HOME / "logs"
TASK_NAME = "PolymarketCryptoPaper"
SHORTCUT_NAME = "Start Polymarket Paper Trading.lnk"
PORT = 7860
URL = f"http://127.0.0.1:{PORT}"


def _python_paths(root: Path) -> tuple[Path, Path]:
    scripts = root / ".venv" / "Scripts"
    return scripts / "python.exe", scripts / "pythonw.exe"


def _run(
    command: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=env or _sanitized_environment(),
        check=True,
        capture_output=True,
        text=True,
    )


def _git(root: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = _run(["git", "-C", str(root), *args], env=env)
    return result.stdout.strip()


def _is_copilot_worktree(root: Path) -> bool:
    return "copilot-worktrees" in {part.lower() for part in root.parts}


def _public_pr_head_repository(
    root: Path, remote: str, branch: str
) -> str | None:
    if "://" in remote:
        parsed = urlsplit(remote)
        host = parsed.hostname
        parts = parsed.path.strip("/").removesuffix(".git").split("/")
    else:
        match = re.fullmatch(r"git@([^:]+):(.+)", remote)
        if match is None:
            return None
        host = match.group(1)
        parts = match.group(2).removesuffix(".git").split("/")
    if host != "github.com" or len(parts) != 2:
        return None
    upstream = "/".join(parts)
    head_sha = _git(root, "rev-parse", "HEAD")
    request = urllib.request.Request(
        f"https://api.github.com/repos/{upstream}/pulls?state=open&per_page=100",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "polymarket-crypto-paper-startup",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15.0) as response:
            pulls = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not inspect public pull-request refs: {exc}") from exc
    if not isinstance(pulls, list):
        return None
    for pull in pulls:
        if not isinstance(pull, dict):
            continue
        head = pull.get("head") or {}
        base = pull.get("base") or {}
        base_repo = base.get("repo") or {}
        head_repo = head.get("repo") or {}
        full_name = head_repo.get("full_name")
        if (
            head.get("ref") == branch
            and head.get("sha") == head_sha
            and base_repo.get("full_name", "").lower() == upstream.lower()
            and isinstance(full_name, str)
            and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", full_name)
        ):
            return full_name
    return None


def _published_branch_remote(root: Path, branch: str) -> str:
    remote = _git(root, "remote", "get-url", "origin")
    refs = _run(
        ["git", "ls-remote", "--heads", remote, f"refs/heads/{branch}"],
        cwd=root,
    ).stdout.strip()
    if refs:
        return remote

    repo = _public_pr_head_repository(root, remote, branch)
    if repo is None:
        raise RuntimeError(
            f"Branch {branch!r} is not published to origin or an open PR head."
        )
    return f"https://github.com/{repo}.git"


def _sanitized_environment() -> dict[str, str]:
    env = os.environ.copy()
    for key in list(env):
        if (
            key in {"GH_TOKEN", "GH_HOST", "GITHUB_TOKEN"}
            or key.startswith("COPILOT_GH_ACCOUNT_")
            or key.startswith("POLYMARKET_")
        ):
            env.pop(key)
    env.update(
        {
            "BOT_MODE": "paper",
            "BTC_PAPER_AUTOSTART": "1",
            "PYTHON_DOTENV_DISABLED": "1",
            "POLYMARKET_PRIVATE_KEY": "",
            "POLYMARKET_FUNDER": "",
            "DATA_DIR": str(DATA_ROOT),
            "DB_PATH": str(DATA_ROOT / "btc_5m_binary_fair_value.db"),
            "DASHBOARD_SERVER_NAME": "127.0.0.1",
            "DASHBOARD_SERVER_PORT": str(PORT),
            # Lets the app restart itself after an update, only when no trade is
            # open (polymarket_exec/ops/dashboard/self_restart.py).
            "PAPER_SELF_RESTART": "1",
        }
    )
    return env


def _ensure_durable_repository() -> Path:
    if not _is_copilot_worktree(REPOSITORY_ROOT):
        return REPOSITORY_ROOT

    branch = _git(REPOSITORY_ROOT, "branch", "--show-current")
    remote = _published_branch_remote(REPOSITORY_ROOT, branch)
    if not branch or not remote:
        raise RuntimeError("Cannot install: the source checkout has no branch or origin.")

    if INSTALL_ROOT.exists():
        installed_remote = _git(INSTALL_ROOT, "remote", "get-url", "origin")
        installed_branch = _git(INSTALL_ROOT, "branch", "--show-current")
        changes = _git(INSTALL_ROOT, "status", "--porcelain")
        if installed_remote != remote or installed_branch != branch:
            raise RuntimeError(
                f"Refusing to replace existing installation at {INSTALL_ROOT}; "
                "its repository or branch differs."
            )
        if changes:
            raise RuntimeError(
                f"Refusing to update modified installation at {INSTALL_ROOT}; "
                "preserve or clean its local changes first."
            )
        _git(INSTALL_ROOT, "pull", "--ff-only", "origin", branch)
    else:
        INSTALL_ROOT.parent.mkdir(parents=True, exist_ok=True)
        git_env = _sanitized_environment()
        git_env["GIT_TERMINAL_PROMPT"] = "0"
        _run(
            [
                "git",
                "-c",
                "credential.helper=",
                "-c",
                "http.extraheader=",
                "clone",
                "--single-branch",
                "--branch",
                branch,
                remote,
                str(INSTALL_ROOT),
            ],
            env=git_env,
        )
    return INSTALL_ROOT


def _register_task(root: Path) -> None:
    if os.name != "nt":
        raise RuntimeError("The on-logon task installer is supported on Windows only.")
    _python, pythonw = _python_paths(root)
    script = root / "tools" / "windows_paper_startup.py"

    def ps_quote(value: str | Path) -> str:
        return "'" + str(value).replace("'", "''") + "'"

    task_arguments = f'"{script}" run'
    powershell = "\n".join(
        [
            "$ErrorActionPreference = 'Stop'",
            f"$action = New-ScheduledTaskAction -Execute {ps_quote(pythonw)} "
            f"-Argument {ps_quote(task_arguments)} "
            f"-WorkingDirectory {ps_quote(root)}",
            "$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name",
            "$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user",
            "$principal = New-ScheduledTaskPrincipal -UserId $user "
            "-LogonType Interactive -RunLevel Limited",
            "$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable "
            "-MultipleInstances IgnoreNew",
            f"Register-ScheduledTask -TaskName {ps_quote(TASK_NAME)} "
            "-Action $action -Trigger $trigger -Principal $principal "
            "-Settings $settings -Force | Out-Null",
        ]
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", powershell],
        env=_sanitized_environment(),
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise RuntimeError(
            "Could not register the user-level logon task: "
            + (result.stderr.strip() or result.stdout.strip())
        )


def _create_desktop_shortcut(root: Path) -> Path:
    """Create or replace the current user's paper-only desktop launcher."""
    if os.name != "nt":
        raise RuntimeError("The desktop launcher is supported on Windows only.")
    python, _pythonw = _python_paths(root)
    script = root / "tools" / "windows_paper_startup.py"

    def ps_quote(value: str | Path) -> str:
        return "'" + str(value).replace("'", "''") + "'"

    arguments = f'"{script}" run'
    powershell = "\n".join(
        [
            "$ErrorActionPreference = 'Stop'",
            "$desktop = [Environment]::GetFolderPath('Desktop')",
            "if ([string]::IsNullOrWhiteSpace($desktop)) { "
            "throw 'Could not resolve the current user desktop.' }",
            f"$shortcutPath = Join-Path $desktop {ps_quote(SHORTCUT_NAME)}",
            "$shell = New-Object -ComObject WScript.Shell",
            "$shortcut = $shell.CreateShortcut($shortcutPath)",
            f"$shortcut.TargetPath = {ps_quote(python)}",
            f"$shortcut.Arguments = {ps_quote(arguments)}",
            f"$shortcut.WorkingDirectory = {ps_quote(root)}",
            "$shortcut.Description = 'Start the local Polymarket BTC paper dashboard'",
            "$shortcut.Save()",
            "Write-Output $shortcutPath",
        ]
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", powershell],
        env=_sanitized_environment(),
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise RuntimeError(
            "Could not create the current-user desktop launcher: "
            + (result.stderr.strip() or result.stdout.strip())
        )
    path = result.stdout.strip()
    if not path:
        raise RuntimeError("Windows did not return the desktop launcher path.")
    return Path(path)


def _dashboard_data(timeout: float = 2.0) -> dict[str, Any] | None:
    request = urllib.request.Request(f"{URL}/api/data")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) and "runtime" in payload else None


def _dashboard_listener_pids() -> set[int]:
    powershell = (
        "$ErrorActionPreference = 'Stop'; "
        f"Get-NetTCPConnection -LocalPort {PORT} -State Listen "
        "-ErrorAction SilentlyContinue | "
        "Select-Object -ExpandProperty OwningProcess -Unique"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", powershell],
        env=_sanitized_environment(),
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise RuntimeError(
            "Could not verify which process owns the dashboard port: "
            + (result.stderr.strip() or result.stdout.strip())
        )
    try:
        return {int(line.strip()) for line in result.stdout.splitlines() if line.strip()}
    except ValueError as exc:
        raise RuntimeError("Could not parse the dashboard listener process ID.") from exc


def _dashboard_log_matches_process(pid: int) -> bool:
    log_path = LOG_ROOT / "dashboard.log"
    try:
        log_file = log_path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return False

    expected_db_path = str(DATA_ROOT / "btc_5m_binary_fair_value.db").lower()
    latest_boot_uses_install = False
    with log_file:
        for line in log_file:
            lowered = line.lower()
            if "app.boot" in lowered:
                latest_boot_uses_install = f"db_path={expected_db_path}" in lowered
            match = re.search(r"started server process \[(\d+)\]", lowered)
            if match and int(match.group(1)) == pid and latest_boot_uses_install:
                return True
    return False


def _is_our_dashboard() -> bool:
    return any(
        _dashboard_log_matches_process(pid) for pid in _dashboard_listener_pids()
    )


def _start_existing_dashboard(payload: dict[str, Any]) -> None:
    runtime = payload.get("runtime") or {}
    if runtime.get("mode") != "paper":
        raise RuntimeError(
            "A dashboard is already using port 7860 in non-paper mode; "
            "the startup task did not interact with it."
        )
    if not _is_our_dashboard():
        raise RuntimeError(
            "Port 7860 belongs to a different dashboard installation; "
            "the startup task did not interact with it."
        )
    if runtime.get("state") != "running":
        request = urllib.request.Request(f"{URL}/api/start", method="POST")
        with urllib.request.urlopen(request, timeout=15.0) as response:
            result = json.loads(response.read().decode("utf-8"))
        if result.get("status") != "running":
            raise RuntimeError(f"Paper loop start failed: {result.get('detail', result)}")


def _has_current_btc_contract(payload: dict[str, Any], now: int | None = None) -> bool:
    html = str(payload.get("execution_view", "")).lower()
    selector = str(payload.get("market_selector", "")).lower()
    current_time = int(time.time()) if now is None else now
    windows = [
        (match.group(1), int(match.group(2)))
        for match in re.finditer(r"btc-updown-(5m|15m)-(\d+)", html)
    ]
    if re.search(
        r"""class=['"]mkt-btn active['"]\s+data-timeframe=['"]5m['"]""", selector
    ):
        windows.extend(
            ("5m", int(match.group(1)))
            for match in re.finditer(r"#(\d{9,10})", html)
        )
    for timeframe, start in windows:
        duration = 300 if timeframe == "5m" else 900
        current_start = current_time - current_time % duration
        if abs(start - current_start) <= duration:
            return True
    return False


def _edge_path() -> Path | None:
    for base in (os.environ.get("PROGRAMFILES(X86)"), os.environ.get("PROGRAMFILES"),
                 os.environ.get("LOCALAPPDATA")):
        if base:
            path = Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
            if path.is_file():
                return path
    return None


def _open_app_window() -> None:
    """Show the dashboard in its own window (Edge app mode: no tabs or address
    bar, its own taskbar icon); fall back to the default browser."""
    edge = _edge_path()
    if edge is not None:
        try:
            subprocess.Popen([str(edge), f"--app={URL}/", "--window-size=1280,900"])
            return
        except OSError:
            pass
    webbrowser.open_new(URL)


def _wait_for_exit(pid: int, timeout: float = 60.0) -> None:
    """Wait for the old app process to end (Windows) before starting a new one."""
    import ctypes

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if handle:
        try:
            kernel32.WaitForSingleObject(handle, int(timeout * 1000))
        finally:
            kernel32.CloseHandle(handle)
    deadline = time.monotonic() + timeout
    while _dashboard_data(timeout=1.0) is not None and time.monotonic() < deadline:
        time.sleep(1)  # port still answering: give the old server a moment


def _run_service(root: Path, *, open_window: bool = True) -> None:
    if os.name != "nt":
        raise RuntimeError("The Windows startup task can only run on Windows.")
    _python, _pythonw = _python_paths(root)
    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    log_path = LOG_ROOT / "dashboard.log"

    payload = _dashboard_data()
    if payload is None:
        env = _sanitized_environment()
        with log_path.open("a", encoding="utf-8") as log_file:
            subprocess.Popen(
                [str(_python), "main.py"],
                cwd=root,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )

    deadline = time.monotonic() + 120
    payload = None
    started = False
    while time.monotonic() < deadline:
        payload = _dashboard_data()
        if payload is not None:
            runtime = payload.get("runtime") or {}
            if runtime.get("mode") != "paper":
                raise RuntimeError("The dashboard did not start in paper mode.")
            if not started:
                _start_existing_dashboard(payload)
                started = True
            if _has_current_btc_contract(payload):
                break
        time.sleep(2)
    if payload is None or not _has_current_btc_contract(payload):
        raise RuntimeError(
            "The paper dashboard did not display a current BTC 5m/15m contract "
            f"within 120 seconds; inspect {log_path} for details."
        )

    if open_window:
        _open_app_window()
    print(f"Paper BTC dashboard is running at {URL}. Logs: {log_path}")


def install() -> None:
    if os.name != "nt":
        raise RuntimeError("The on-logon task installer is supported on Windows only.")
    root = _ensure_durable_repository()
    python, _pythonw = _python_paths(root)
    if not python.is_file():
        _run([sys.executable, "-m", "venv", str(root / ".venv")], cwd=root)
    env = _sanitized_environment()
    _run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--quiet",
            "-e",
            ".",
        ],
        cwd=root,
        env=env,
    )
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    _register_task(root)
    shortcut = _create_desktop_shortcut(root)
    _run_service(root)
    print(
        f"Installed {TASK_NAME} for the current Windows user. "
        f"Desktop launcher: {shortcut}; repository: {root}; data: {DATA_ROOT}"
    )


def uninstall() -> None:
    if os.name != "nt":
        raise RuntimeError("The on-logon task uninstaller is supported on Windows only.")
    powershell = (
        "$ErrorActionPreference = 'Stop'; "
        f"Unregister-ScheduledTask -TaskName '{TASK_NAME}' "
        "-Confirm:$false -ErrorAction SilentlyContinue; "
        "$desktop = [Environment]::GetFolderPath('Desktop'); "
        f"Remove-Item -LiteralPath (Join-Path $desktop '{SHORTCUT_NAME}') "
        "-Force -ErrorAction SilentlyContinue"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", powershell],
        env=_sanitized_environment(),
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise RuntimeError(
            "Could not remove the user-level logon task: "
            + (result.stderr.strip() or result.stdout.strip())
        )
    print(
        f"Removed the {TASK_NAME} logon task. Any dashboard already running "
        "was left open; press Stop in that dashboard to halt paper entries. "
        f"Repository and data were preserved at {APP_HOME}."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "run", "restart", "uninstall"))
    parser.add_argument("--after-pid", type=int, default=None,
                        help="restart: wait for this app process to exit first")
    args = parser.parse_args()
    action = args.action
    if action == "install":
        install()
    elif action == "run":
        _run_service(REPOSITORY_ROOT)
    elif action == "restart":
        # Started by the app itself after an update (self_restart.py): no new window.
        if args.after_pid:
            _wait_for_exit(args.after_pid)
        time.sleep(2)
        _run_service(REPOSITORY_ROOT, open_window=False)
    else:
        uninstall()


if __name__ == "__main__":
    main()
