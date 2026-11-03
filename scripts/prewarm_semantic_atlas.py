#!/usr/bin/env python3
"""Precompute the local derived embedding cache without touching Markdown."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from daemon_core.semantic_atlas import prewarm_semantic_atlas_cache  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--memory-root", type=Path, required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            prewarm_semantic_atlas_cache(args.memory_root),
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
