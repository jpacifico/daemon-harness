#!/usr/bin/env python3
"""Fail closed when an active host points at an incomplete or removable runtime."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import plistlib
import shlex
import sys
import tempfile


def _resolved(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _is_within(path: Path, parent: Path) -> bool:
    try:
        _resolved(path).relative_to(_resolved(parent))
    except ValueError:
        return False
    return True


def _env_values(path: Path, key: str) -> list[str]:
    if not path.is_file():
        return []
    values: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{key}="):
            value = line.split("=", 1)[1].strip()
            if value.startswith(("\"", "'")):
                try:
                    parsed = shlex.split(value, comments=True)
                    if len(parsed) == 1:
                        value = parsed[0]
                except ValueError:
                    pass  # Malformed quoting remains an invalid source path.
            values.append(value)
    return values


def _inspect_hermes(home: Path) -> tuple[list[tuple[str, Path]], list[str]]:
    references: list[tuple[str, Path]] = []
    failures: list[str] = []
    values = _env_values(home / ".env", "DAEMON_CORE_SRC")
    plugin_paths = tuple(home / "plugins" / name for name in ("daemon_core", "daemon_voice"))
    has_daemon_plugin = any(path.exists() or path.is_symlink() for path in plugin_paths)
    if len(values) > 1:
        failures.append("Hermes defines DAEMON_CORE_SRC more than once.")
    elif values:
        raw_source = Path(values[0]).expanduser()
        if not raw_source.is_absolute():
            failures.append("Hermes DAEMON_CORE_SRC is not an absolute path.")
        source = _resolved(raw_source)
        references.append(("Hermes source", source))
        for relative in (
            Path("daemon_core/__init__.py"),
            Path("daemon_core/loop.py"),
        ):
            if not (source / relative).is_file():
                failures.append(f"Hermes source is incomplete: missing {relative}.")
    elif has_daemon_plugin:
        failures.append("Hermes plugins are present but DAEMON_CORE_SRC is missing.")

    for name, link in zip(("daemon_core", "daemon_voice"), plugin_paths):
        if not link.exists() and not link.is_symlink():
            continue
        if not link.is_symlink():
            failures.append(f"Hermes plugin {name} is not a symbolic link.")
            continue
        target = _resolved(link)
        references.append((f"Hermes plugin {name}", target))
        if not (target / "__init__.py").is_file():
            failures.append(f"Hermes plugin {name} target is incomplete.")
    return references, failures


def _inspect_openclaw(home: Path) -> tuple[list[tuple[str, Path]], list[str]]:
    config = home / "openclaw.json"
    if not config.is_file():
        return [], []
    try:
        payload = json.loads(config.read_text(encoding="utf-8"))
        raw_paths = payload.get("plugins", {}).get("load", {}).get("paths", [])
    except (json.JSONDecodeError, OSError, AttributeError):
        return [], ["OpenClaw plugin configuration is invalid."]
    if not isinstance(raw_paths, list) or any(not isinstance(item, str) for item in raw_paths):
        return [], ["OpenClaw plugin path list is invalid."]
    references = [
        (f"OpenClaw plugin {index}", _resolved(Path(value)))
        for index, value in enumerate(raw_paths, start=1)
    ]
    failures: list[str] = []
    for _label, target in references:
        if target.name == "openclaw_plugin" and not (target / "index.js").is_file():
            failures.append("OpenClaw daemon plugin target is incomplete.")
    return references, failures


def _inspect_services(directory: Path) -> tuple[list[tuple[str, Path]], list[str]]:
    """Inspect the daemon-owned macOS jobs without printing their environment."""
    references: list[tuple[str, Path]] = []
    failures: list[str] = []
    for label in (
        "com.daemonharness.openclaw-api",
        "com.daemonharness.after-turn-worker",
        "com.daemonharness.dreaming",
        "com.daemonharness.memory-structure-worker",
        "com.daemonharness.memory-structure-openclaw-api",
    ):
        definition = directory / f"{label}.plist"
        if not definition.exists():
            continue
        try:
            payload = plistlib.loads(definition.read_bytes())
            root = Path(payload["WorkingDirectory"])
            arguments = payload["ProgramArguments"]
            environment = payload.get("EnvironmentVariables", {})
            if not root.is_absolute() or not isinstance(arguments, list) or not arguments:
                raise ValueError("Invalid service paths")
            paths = [root]
            paths.extend(Path(arg) for arg in arguments if isinstance(arg, str) and arg.startswith("/"))
            paths.extend(Path(part) for part in environment.get("PYTHONPATH", "").split(":") if part)
            for path in paths:
                references.append((label, _resolved(path)))
                if not path.is_absolute() or not path.exists():
                    failures.append(f"{label} has an unavailable or relative code path.")
            for relative in ("src/daemon_core/__init__.py", "src/daemon_core/loop.py"):
                if not (root / relative).is_file():
                    failures.append(f"{label} source is incomplete: missing {relative}.")
        except (OSError, ValueError, KeyError, TypeError, AttributeError, plistlib.InvalidFileException):
            failures.append(f"{label} definition is invalid.")
    return references, failures


def _is_temporary(path: Path) -> bool:
    return any(_is_within(path, Path(root)) for root in (
        "/tmp", "/private/tmp", "/var/tmp", "/private/var/tmp", tempfile.gettempdir(),
    ))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate active Daemon Harness runtime pointers.",
    )
    parser.add_argument(
        "--hermes-home",
        type=Path,
        default=Path.home() / ".hermes",
    )
    parser.add_argument(
        "--openclaw-home",
        type=Path,
        default=Path.home() / ".openclaw",
    )
    parser.add_argument(
        "--before-remove",
        type=Path,
        help="Fail if an active pointer is inside this worktree.",
    )
    parser.add_argument("--launchagents-dir", type=Path, default=Path.home() / "Library/LaunchAgents")
    parser.add_argument(
        "--allow-temporary", action="store_true",
        help="Only for disposable isolated tests, never a persistent host installation.",
    )
    args = parser.parse_args(argv)

    references: list[tuple[str, Path]] = []
    failures: list[str] = []
    for inspect, home in (
        (_inspect_hermes, args.hermes_home),
        (_inspect_openclaw, args.openclaw_home),
        (_inspect_services, args.launchagents_dir),
    ):
        discovered, problems = inspect(_resolved(home))
        references.extend(discovered)
        failures.extend(problems)

    # A same-bot canary changes the gateway's profile, not ~/.hermes. Inspect
    # that actual profile too, so removing its durable checkout fails closed.
    gateway = args.launchagents_dir / "ai.hermes.gateway.plist"
    if gateway.is_file():
        try:
            payload = plistlib.loads(gateway.read_bytes())
            active_home = payload.get("EnvironmentVariables", {}).get("HERMES_HOME")
            if active_home and _resolved(Path(active_home)) != _resolved(args.hermes_home):
                found, problems = _inspect_hermes(_resolved(Path(active_home)))
                references.extend(found)
                failures.extend(problems)
        except (OSError, ValueError, TypeError, plistlib.InvalidFileException):
            failures.append("Hermes gateway definition is invalid.")

    if not args.allow_temporary:
        for label, target in references:
            if _is_temporary(target):
                failures.append(f"{label} points into temporary storage; use a durable checkout or release.")

    if args.before_remove is not None:
        candidate = _resolved(args.before_remove)
        for label, target in references:
            if _is_within(target, candidate):
                failures.append(f"{label} still points inside the worktree to remove.")

    if failures:
        for failure in failures:
            print(f"ERROR: {failure}", file=sys.stderr)
        return 1
    print(f"Runtime pointer check passed ({len(references)} active references).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
