"""Materialize one registered synthetic fixture in a disposable workspace."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from benchmarks.productivity_v1.fixtures.native_files_aggregate import (
    FIXTURE_VARIANTS,
    OFFICIAL_SEEDS,
    materialize_fixture,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", required=True, type=int, choices=OFFICIAL_SEEDS)
    parser.add_argument(
        "--variant",
        required=True,
        choices=FIXTURE_VARIANTS,
    )
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--control", required=True, type=Path)
    args = parser.parse_args()

    result = materialize_fixture(
        seed=args.seed,
        variant=args.variant,
        workspace=args.workspace,
        control_path=args.control,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
