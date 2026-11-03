"""Check a completed ``native.files.aggregate`` workspace."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from benchmarks.productivity_v1.checkers.native_files_aggregate import (
    check_workspace,
    load_control,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--control", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    outcome = check_workspace(
        workspace=args.workspace,
        control=load_control(args.control),
    )
    rendered = json.dumps(
        outcome,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    if args.output is None:
        print(rendered, end="")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    return 0 if outcome["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
