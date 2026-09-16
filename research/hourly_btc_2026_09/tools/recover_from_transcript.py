"""Rebuild the 2026-09-13..14 hourly BTC research files from the Claude Code session transcript.

The research was run in a temporary scratch folder that was wiped on 2026-09-16. Every file
in it was written by a tool call that the session transcript still holds, so this script
replays those calls in order:

- ``Write`` calls whose path is inside the scratch folder give the file content;
- ``Edit`` calls on those files are applied in order;
- ``Bash`` heredocs (``cat > $B/NAME <<'EOF' ... EOF``) give the file content;
- every ``Bash`` command that touched the scratch folder is saved with its printed output,
  so data downloads, inline analyses and printed results stay traceable.

Usage: python3 recover_from_transcript.py <transcript.jsonl> <output_dir>
Written by Claude, 2026-09-16, after the scratch folder was wiped.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

SCRATCH = "/scratchpad/kronos-test"
MAX_OUTPUT_CHARS = 200_000
# The research ran 2026-09-13..14. Later commands only searched the transcript or ran app tests.
RESEARCH_BEFORE = "2026-09-15"
NOT_RESEARCH = ("/.claude/projects/", "pytest")
HEREDOC = re.compile(
    r"cat\s*>\s*\"?(?P<target>[^\s\"<]+)\"?\s*<<-?\s*(?P<q>['\"]?)(?P<tag>\w+)(?P=q)[^\n]*\n"
    r"(?P<body>.*?)\n(?P=tag)[ \t]*(?:\n|$)",
    re.S,
)
ASSIGN = re.compile(r"(?m)(?:^|[;&\s])(?P<var>[A-Za-z_]\w*)=(?P<val>/[^\s;]*kronos-test)\b")


def _text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
    return ""


def _resolve(target: str, variables: dict[str, str]) -> str | None:
    for var, val in variables.items():
        target = target.replace("${" + var + "}", val).replace("$" + var, val)
    return target if SCRATCH in target else None


def main(transcript: Path, out: Path) -> None:
    events = [json.loads(line) for line in transcript.open() if line.strip()]
    results: dict[str, str] = {}
    for ev in events:
        for block in (ev.get("message") or {}).get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                results[block.get("tool_use_id", "")] = _text(block)

    files: dict[str, str] = {}
    origin: dict[str, list[str]] = {}
    commands: list[tuple[str, str, str, str]] = []
    for ev in events:
        if ev.get("type") != "assistant":
            continue
        stamp = ev.get("timestamp", "")
        for block in (ev.get("message") or {}).get("content") or []:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name, inp, tool_id = block.get("name"), block.get("input") or {}, block.get("id", "")
            path = str(inp.get("file_path", ""))
            if name == "Write" and SCRATCH in path:
                files[path] = inp.get("content", "")
                origin.setdefault(path, []).append(f"{stamp} Write {tool_id}")
            elif name == "Edit" and SCRATCH in path and path in files:
                old, new = inp.get("old_string", ""), inp.get("new_string", "")
                count = -1 if inp.get("replace_all") else 1
                if old in files[path]:
                    files[path] = files[path].replace(old, new, count)
                    origin[path].append(f"{stamp} Edit {tool_id}")
                else:
                    origin[path].append(f"{stamp} Edit {tool_id} NOT APPLIED (old text missing)")
            elif name == "Bash" and "kronos-test" in inp.get("command", ""):
                cmd = inp["command"]
                if stamp >= RESEARCH_BEFORE or any(marker in cmd for marker in NOT_RESEARCH):
                    continue
                variables = {m["var"]: m["val"] for m in ASSIGN.finditer(cmd)}
                for m in HEREDOC.finditer(cmd):
                    target = _resolve(m["target"], variables)
                    if target is None:
                        continue
                    exact = "byte-exact" if m["q"] else "unquoted heredoc: shell may have expanded $"
                    files[target] = m["body"] + "\n"
                    origin.setdefault(target, []).append(f"{stamp} Bash heredoc {tool_id} ({exact})")
                commands.append((stamp, tool_id, cmd, results.get(tool_id, "")))

    recovered = out / "recovered_verbatim"
    recovered.mkdir(parents=True, exist_ok=True)
    for path, content in sorted(files.items()):
        (recovered / Path(path).name).write_text(content)

    log_dir = out / "session_commands"
    log_dir.mkdir(parents=True, exist_ok=True)
    for i, (stamp, tool_id, cmd, output) in enumerate(commands, 1):
        stem = f"{i:03d}_{stamp[:19].replace(':', '')}_{tool_id[-6:]}"
        (log_dir / f"{stem}.sh").write_text(cmd + "\n")
        clipped = output[:MAX_OUTPUT_CHARS]
        if len(output) > MAX_OUTPUT_CHARS:
            clipped += f"\n[clipped: {len(output)} chars in the transcript]\n"
        (log_dir / f"{stem}.output.txt").write_text(clipped)

    lines = ["# Recovery log", "", f"Transcript: `{transcript.name}`", "",
             "| File | Source tool calls |", "|---|---|"]
    for path in sorted(files):
        lines.append(f"| `{Path(path).name}` | {'<br>'.join(origin[path])} |")
    lines += ["", f"Session commands saved: {len(commands)} (`session_commands/`).", ""]
    (out / "RECOVERY_LOG.md").write_text("\n".join(lines))
    print(f"files={len(files)} commands={len(commands)}")


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
