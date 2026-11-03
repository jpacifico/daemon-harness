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
from benchmarks.continuity.corpus import ELSEWHERE, SCENARIOS, Scenario
from benchmarks.continuity import simulate
from benchmarks.continuity.judge import INSTRUCTIONS

DATASET = Path(__file__).resolve().parent / "dataset"
CORPUS = Path(__file__).resolve().parent / "corpus.py"


def record(scenario: Scenario) -> dict:
    sessions = [{"stage": "telling", "messages": list(scenario.telling)},
                {"stage": "elsewhere", "messages": list(ELSEWHERE[scenario.elsewhere])}]
    if scenario.change:
        sessions.append({"stage": "change", "messages": list(scenario.change)})
    sessions.append({"stage": "final", "messages": [scenario.asking]})
    return {
        "id": scenario.id, "version": VERSION, "language": "en", "family": scenario.family,
        "counter_case": scenario.counter,
        "sessions": sessions, "person": scenario.person,
        "criteria": {"must_use": list(scenario.must_use), "must_not": list(scenario.must_not),
                     "no_repeat": list(scenario.no_repeat), "unknown": list(scenario.unknown),
                     "outcome": scenario.outcome},
    }


def lines() -> dict[str, str]:
    scenarios = "".join(json.dumps(record(scenario), ensure_ascii=False, sort_keys=True) + "\n" for scenario in SCENARIOS)
    return {"scenarios.jsonl": scenarios, "judge_instructions.md": INSTRUCTIONS + "\n",
            "simulator_instructions.md": simulate.INSTRUCTIONS + "\n"}


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
