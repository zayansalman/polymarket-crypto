"""Keep each strategy's doc in step with its code.

    python tools/strategy_docs.py check            # list stale or missing docs, exit 1 if any
    python tools/strategy_docs.py stamp KEY "note" # after changing a strategy: refresh + log it
    python tools/strategy_docs.py new KEY          # scaffold a doc for a new family

Docs live in docs/strategies/<key>.md; the rules are in polymarket_bot/strategy_docs.py.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from polymarket_bot import strategy_docs as sd  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    st = sub.add_parser("stamp")
    st.add_argument("key")
    st.add_argument("note")
    nw = sub.add_parser("new")
    nw.add_argument("key")
    args = parser.parse_args(argv)

    if args.cmd == "check":
        report = sd.all_problems()
        for key, issues in report.items():
            for issue in issues:
                print(f"{key}: {issue}")
        if not report:
            print(f"all {len(sd._inv.FAMILIES)} strategy docs are in step with their code")
        return 1 if report else 0

    if args.cmd == "stamp":
        path = sd.stamp(args.key, args.note)
        print(f"stamped {path.relative_to(sd.ROOT)}")
        return 0

    path = sd.doc_path(args.key)
    if path.exists():
        print(f"{path.relative_to(sd.ROOT)} already exists", file=sys.stderr)
        return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(sd.scaffold(args.key), encoding="utf-8")
    print(f"created {path.relative_to(sd.ROOT)} — fill in every section before committing")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
