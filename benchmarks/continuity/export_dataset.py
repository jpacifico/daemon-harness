"""Write the Continuity Benchmark scenarios as a reusable dataset (JSON Lines, Hugging Face layout).

The dataset is generated from the preregistered corpus and checked against it,
so the file and the scored corpus can never differ. Run from the repository root:
    PYTHONPATH=src:. python -m benchmarks.continuity.export_dataset
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from benchmarks.continuity import VERSION
from benchmarks.continuity.corpus import ELSEWHERE, LIVES, Life
from benchmarks.continuity.judge import INSTRUCTIONS
from benchmarks.continuity.run import NATIVE

DATASET = Path(__file__).resolve().parent / "dataset"
CORPUS = Path(__file__).resolve().parent / "corpus.py"


def record(life: Life) -> dict:
    sessions = [{"stage": "telling", "messages": list(life.telling)},
                {"stage": "elsewhere", "messages": list(ELSEWHERE[life.elsewhere])}]
    if life.change:
        sessions.append({"stage": "change", "messages": list(life.change)})
    sessions.append({"stage": "asking", "messages": [life.asking]})
    return {
        "id": life.id, "version": VERSION, "language": "en", "family": life.family,
        "counter_case": life.counter,
        "sessions": sessions,
        "criteria": {"must_use": list(life.must_use), "must_not": list(life.must_not),
                     "no_repeat": list(life.no_repeat), "unknown": list(life.unknown),
                     "next_step": life.next_step},
    }


def lines() -> dict[str, str]:
    lives = "".join(json.dumps(record(life), ensure_ascii=False, sort_keys=True) + "\n" for life in LIVES)
    native = "".join(json.dumps({
        "id": f"{scenario}.{seed}", "version": VERSION, "scenario": scenario, "seed": seed,
        "generator": f"benchmarks/productivity_v1/fixtures/{scenario.replace('.', '_')}.py",
        "checker": f"benchmarks/productivity_v1/checkers/{scenario.replace('.', '_')}.py",
    }, sort_keys=True) + "\n" for scenario, seeds in NATIVE for seed in seeds)
    return {"lives.jsonl": lives, "native_tasks.jsonl": native,
            "judge_instructions.md": INSTRUCTIONS + "\n"}


def manifest(files: dict[str, str]) -> str:
    return json.dumps({
        "version": VERSION,
        "corpus_sha256": hashlib.sha256(CORPUS.read_bytes()).hexdigest(),
        "files": {name: hashlib.sha256(text.encode()).hexdigest() for name, text in sorted(files.items())},
    }, indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="fail if the published files differ")
    args = parser.parse_args()
    files = lines()
    files["manifest.json"] = manifest(files)
    for name, text in files.items():
        path = DATASET / name
        if args.check:
            if not path.is_file() or path.read_text() != text:
                raise SystemExit(f"{name} differs from the corpus; regenerate it")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
