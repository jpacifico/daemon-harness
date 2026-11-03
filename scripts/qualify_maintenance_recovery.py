#!/usr/bin/env python3
"""Bounded native write/read checks for maintenance recovery, synthetic only."""
from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import shutil
import sys
import time
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]


def disposable_access(source: Path, target: Path) -> None:
    """Use a fresh short-lived access token; never duplicate its refresh token."""
    payload = json.loads((source / "auth.json").read_text())
    encoded = payload["tokens"]["access_token"].split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    if claims["exp"] - time.time() < 3600:
        raise RuntimeError("A fresh native login is required before disposable qualification")
    payload["tokens"]["refresh_token"] = "disabled-for-disposable-qualification"
    target.mkdir(parents=True)
    (target / "auth.json").write_text(json.dumps(payload))
    (target / "auth.json").chmod(0o600)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", choices=("hermes", "openclaw"), required=True)
    for name in ("output", "source-env", "auth-home", "archive"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--previous", type=Path)
    parser.add_argument("--codex-plugin", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    root = args.output.resolve()
    if not root.is_relative_to(Path("/private/tmp")) or root.exists():
        raise ValueError("A fresh disposable root is required")
    sys.path[:0] = [str(REPO), str(REPO / "src")]
    from benchmarks.productivity_v1.runner import common
    from scripts import qualify_memory_root_folders as native

    common.prepare_codex_cli_home = lambda target, **kw: disposable_access(args.auth_home, target)
    original_env = common.safe_dotenv

    def fixture_environment(path):
        values = original_env(path)
        values["DAEMON_CORE_ATLAS_EMBEDDING_URL"] = "http://127.0.0.1:11435"
        return values

    common.safe_dotenv = fixture_environment
    fixture = json.loads(native.FIXTURE.read_text())
    convert = native.convert_fixture
    if args.previous:
        previous = args.previous.resolve()
        if not previous.is_relative_to(Path("/private/tmp")):
            raise ValueError("Only the preceding synthetic runtime may be read")
        proof_path = previous / "recovery-result.json"
        if not proof_path.exists() and args.host == "openclaw":
            # A fixture may have resumed its original L0 after a configuration
            # error. Preserve that first result instead of rewriting it as a pass.
            proof_path = previous / "alpha-after-turn-recovery.json"
        proof = json.loads(proof_path.read_text())
        if not proof["passed"] or proof["host"] != args.host:
            raise ValueError("The preceding native write must have passed")
        fixture["prompt"] = (
            "Please read the exact saved Harbor Workshop and Cedar radio workshop memories. "
            "Which timber did I deliver, what is the Cedar project's deliverable and deadline, "
            "and which component have I already bought?"
        )

        def convert_previous(destination):
            convert(destination)
            (destination / "memory").rename(destination / "unused-seed")
            shutil.copytree(previous / "memory", destination / "memory")
            memory = destination / "memory/.daemon"
            for filename in ("vault-layout.json", ".memory-structure-prototype.json"):
                marker = memory / filename
                data = json.loads(marker.read_text())
                for key in ("vault_root", "runtime_root", "memory_root"):
                    if key in data:
                        data[key] = str(destination / "memory" if key == "vault_root" else memory)
                native.save(marker, data)
            return memory, destination / "memory/Areas/Work/Harbor Workshop renamed.md"

        native.convert_fixture = convert_previous
    fixture_path = root.with_suffix(".fixture.json")
    native.save(fixture_path, fixture)
    native.FIXTURE = fixture_path
    began = time.monotonic()
    if args.host == "hermes":
        # Hermes owns its native auth store. Initialize that disposable store
        # through its own writer, with no usable rotating credential.
        sys.path.insert(0, str(native.HERMES))
        from benchmarks.productivity_v1.runner import hermes

        prepare_profile = hermes._prepare_profile

        def prepare_disposable_profile(**kwargs):
            prepare_profile(**kwargs)
            assert Path(os.environ["HERMES_HOME"]) == kwargs["profile"]
            from hermes_cli.auth_codex import _save_codex_tokens

            auth = json.loads((root / "codex-home/auth.json").read_text())
            _save_codex_tokens(auth["tokens"], last_refresh=auth.get("last_refresh"))

        hermes._prepare_profile = prepare_disposable_profile
        native.hermes_child(SimpleNamespace(output=root, source=REPO,
            source_env=args.source_env, arm="B", hermes_home=root / "empty-auth-source",
            transition_case=None, transition_mode="off"))
    else:
        from scripts import qualify_openclaw_memory_structure as openclaw

        sys.argv = [str(openclaw.__file__), "--arm", "B", "--source", str(REPO),
            "--source-env", str(args.source_env), "--output", str(root), "--archive", str(args.archive),
            "--root-folders", "--fixture", str(fixture_path)]
        if args.codex_plugin:
            sys.argv += ["--codex-plugin", str(args.codex_plugin)]
        openclaw.main()
    result = json.loads((root / "result.json").read_text())
    checks = native.assess(root, result, REPO)
    summary = {"host": args.host, "phase": "read" if args.previous else "write",
        "passed": all(checks.values()), "checks": checks,
        "source_revision": result.get("source_revision"), "duration_s": time.monotonic() - began,
        "voice_seconds": result.get("voice_seconds"), "first_delta_seconds": result.get("first_delta_seconds"),
        "after_turn_wait_seconds": result.get("after_turn_wait_seconds"),
        "error": result.get("error"), "refresh_token_copied": False}
    native.save(root / "recovery-result.json", summary)
    print(json.dumps(summary), flush=True)
    if not summary["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
