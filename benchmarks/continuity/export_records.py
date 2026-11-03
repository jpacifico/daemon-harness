"""Copy a campaign's records into a publishable directory, without runtimes, credentials or personal paths.

Kept: the plan, runs, per-episode records and evidence, the simulated person's raw
answers, blinded packets, raw judge answers, verdicts, the blind keys (opened
after judging) and the reports. Left out: every runtime directory (Hermes and
Codex homes, sign-ins, the bench daemon's vault, logs and caches). The local
home directory is written as `~` in every kept file. A scan refuses the export
if a forbidden pattern remains. Protocol: docs/continuity-benchmark.md,
"Publication and reproducibility".
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil

ROOT_FILES = ("plan.json", "runs.jsonl", "campaign.log", "judging.log")
ROOT_GLOBS = ("runner-revision-*.json",)
TREES = ("episodes", "evidence")
JUDGING_PARTS = ("INSTRUCTIONS.md", "packets", "raw", "verdicts", "merged.json")
FORBIDDEN = (
    r"refresh_token", r"access_token", r"id_token", r"\beyJ[A-Za-z0-9_-]{20,}", r"\bsk-[A-Za-z0-9_-]{20,}",
    r"Bearer\s+[A-Za-z0-9._-]{10,}", r"/Users/[^/\s\"']+", r"/home/[^/\s\"']+", r"/private/var/", r"/var/folders/",
)


def selected(campaign: Path) -> list[Path]:
    """Every kept file, relative to the campaign directory."""
    files = [Path(name) for name in ROOT_FILES if (campaign / name).is_file()]
    files += sorted(p.relative_to(campaign) for pattern in ROOT_GLOBS for p in campaign.glob(pattern))
    for tree in TREES:
        files += sorted(p.relative_to(campaign) for p in (campaign / tree).rglob("*") if p.is_file())
    for stage in sorted(p for p in campaign.iterdir() if p.is_dir() and p.name.endswith("judging")):
        for part in JUDGING_PARTS:
            target = stage / part
            items = [target] if target.is_file() else sorted(p for p in target.rglob("*") if p.is_file())
            files += [p.relative_to(campaign) for p in items]
    for report in sorted(p for p in campaign.iterdir() if p.is_dir() and p.name.endswith("report")):
        files += sorted(p.relative_to(campaign) for p in report.rglob("*") if p.is_file())
    private = campaign / "private"
    if private.is_dir():
        files += sorted(p.relative_to(campaign) for p in private.glob("*-blind-key.json"))
        files += sorted(p.relative_to(campaign) for p in private.glob("*/simulator/*.json"))
    return files


def scrub(text: str, home: str) -> str:
    return text.replace(home, "~")


def forbidden(text: str, extra: tuple[str, ...] = ()) -> list[str]:
    return [pattern for pattern in (*FORBIDDEN, *extra) if re.search(pattern, text)]


def export(campaign: Path, out: Path, *, home: str, extra: tuple[str, ...] = ()) -> dict:
    target = out / campaign.name
    if target.exists():
        shutil.rmtree(target)
    hashes: dict[str, str] = {}
    refused: dict[str, list[str]] = {}
    for relative in selected(campaign):
        text = scrub((campaign / relative).read_text(), home)
        found = forbidden(text, extra)
        if found:
            refused[str(relative)] = found
            continue
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text)
        hashes[str(relative)] = hashlib.sha256(text.encode()).hexdigest()
    if refused:
        shutil.rmtree(target, ignore_errors=True)
        raise SystemExit(f"Refused, forbidden patterns remain: {json.dumps(refused, indent=2)}")
    manifest = {"campaign": campaign.name, "files": hashes}
    (target / "MANIFEST.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--forbid", action="append", default=[],
                        help="an extra pattern that must not appear, such as an e-mail address; never stored")
    args = parser.parse_args()
    manifest = export(args.campaign.resolve(), args.out.resolve(), home=str(Path.home()),
                      extra=tuple(re.escape(item) for item in args.forbid))
    print(json.dumps({"campaign": manifest["campaign"], "files": len(manifest["files"])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
