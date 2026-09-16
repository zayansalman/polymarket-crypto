"""Extract every forecast the Kronos team published in shiyu-coder/Kronos-demo's git history.

Source: github.com/shiyu-coder/Kronos-demo (hourly auto-commits of index.html by
update_predictions.py). Each commit's index.html holds the update time (UTC), the
"upside probability" (share of 30 sampled 24-hour Kronos-mini paths whose final close
is above the last closed hourly close) and the "volatility amplification probability".
Output: data/published_forecasts.csv, one row per commit that changed index.html.
"""
from __future__ import annotations

import csv
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
REPO = HERE / "data" / "kronos-demo-repo"
OUT = HERE / "data" / "published_forecasts.csv"

PATTERNS = {
    "update_time": re.compile(r'id="update-time">([^<]*)<'),
    "upside_prob": re.compile(r'id="upside-prob">([^<]*)<'),
    "vol_amp_prob": re.compile(r'id="vol-amp-prob">([^<]*)<'),
}


def git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(REPO), *args], check=True,
                          capture_output=True, text=True).stdout


def main() -> int:
    lines = git("log", "--reverse", "--format=%H %cI %s", "--", "index.html").splitlines()
    specs = [f"{line.split(' ', 1)[0]}:index.html" for line in lines]
    proc = subprocess.run(["git", "-C", str(REPO), "cat-file", "--batch"],
                          input=("\n".join(specs) + "\n").encode(), capture_output=True, check=True)
    buf = proc.stdout
    rows, pos = [], 0
    for line in lines:
        sha, committed, subject = line.split(" ", 2)
        header_end = buf.index(b"\n", pos)
        header = buf[pos:header_end].decode()
        if header.endswith("missing"):
            pos = header_end + 1
            rows.append({"commit": sha, "committed_at": committed, "subject": subject,
                         "update_time": "", "upside_prob": "", "vol_amp_prob": ""})
            continue
        size = int(header.split()[2])
        body = buf[header_end + 1: header_end + 1 + size].decode("utf-8", "replace")
        pos = header_end + 1 + size + 1
        row = {"commit": sha, "committed_at": committed, "subject": subject}
        for key, pat in PATTERNS.items():
            m = pat.search(body)
            row[key] = m.group(1).strip() if m else ""
        rows.append(row)
    with OUT.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"{len(rows)} commits touching index.html -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
