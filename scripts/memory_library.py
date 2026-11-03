#!/usr/bin/env python3
"""Bounded read-only access to an isolated active-memory view."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from daemon_core.memory.collection_read import (  # noqa: E402
    CollectionReadError,
    MAX_COLLECTION_PAGE_MEMBERS,
    read_collection_page,
)
from daemon_core.memory.graph import load_memory_graph_index  # noqa: E402


EVENT_PREFIX = "DAEMON_LIBRARY_EVENT "
MAX_READ_SOUVENIRS = 8
MAX_READ_CHARS = 50_000
MAX_BROWSE_PATHS = 200
LINK_PATTERN = re.compile(r"(?<![\w/])(theme/[A-Za-z0-9_./-]+)")


def _catalog(root: Path) -> dict[str, Path]:
    return {
        path.relative_to(root).with_suffix("").as_posix(): path
        for path in sorted(root.rglob("*.md"))
        if path.is_file()
    }


def _clean_path(value: str) -> str | None:
    candidate = value.strip().removesuffix(".md")
    if not candidate or candidate.startswith("/") or "\\" in candidate:
        return None
    parts = Path(candidate).parts
    if any(part in {"", ".", ".."} for part in parts):
        return None
    return Path(*parts).as_posix()


def _emit_event(payload: dict) -> None:
    print(EVENT_PREFIX + json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def _browse(catalog: dict[str, Path], prefix: str) -> int:
    clean = _clean_path(prefix) if prefix else ""
    if prefix and clean is None:
        _emit_event({"operation": "browse", "prefix": prefix, "paths": [], "error": "invalid_prefix"})
        return 2
    paths = [name for name in catalog if not clean or name == clean or name.startswith(clean + "/")]
    truncated = len(paths) > MAX_BROWSE_PATHS
    paths = paths[:MAX_BROWSE_PATHS]
    _emit_event(
        {
            "operation": "browse",
            "prefix": clean,
            "paths": paths,
            "truncated": truncated,
        }
    )
    for name in paths:
        print(name)
    return 0


def _read(
    catalog: dict[str, Path],
    requested: list[str],
    *,
    established: list[str],
    uncertain: list[str],
    need: str,
    handling_mode: str,
    handling_plan: str,
) -> int:
    scene_assessment = {
        "established": established,
        "uncertain": uncertain,
        "need": need,
        "handling": {
            "mode": handling_mode,
            "plan": handling_plan,
        },
    }
    cleaned = [_clean_path(value) for value in requested]
    if len(requested) > MAX_READ_SOUVENIRS:
        _emit_event(
            {
                "operation": "read",
                "scene_assessment": scene_assessment,
                "requested_paths": requested,
                "read_paths": [],
                "rejected_paths": requested,
                "error": "too_many_paths",
            }
        )
        return 2
    valid = []
    rejected = []
    seen = set()
    for original, clean in zip(requested, cleaned):
        if clean is None or clean not in catalog:
            rejected.append(original)
        elif clean not in seen:
            seen.add(clean)
            valid.append(clean)
    contents = [(name, catalog[name].read_text(encoding="utf-8").strip()) for name in valid]
    rendered_chars = sum(len(content) for _name, content in contents)
    if rendered_chars > MAX_READ_CHARS:
        _emit_event(
            {
                "operation": "read",
                "scene_assessment": scene_assessment,
                "requested_paths": requested,
                "read_paths": [],
                "rejected_paths": requested,
                "error": "read_too_large",
                "rendered_chars": rendered_chars,
            }
        )
        return 2
    _emit_event(
        {
            "operation": "read",
            "scene_assessment": scene_assessment,
            "requested_paths": requested,
            "read_paths": valid,
            "rejected_paths": rejected,
            "sha256": {
                name: hashlib.sha256(content.encode("utf-8")).hexdigest()
                for name, content in contents
            },
            "rendered_chars": rendered_chars,
        }
    )
    for name, content in contents:
        print(f"\n# [[{name}]]\n\n{content}")
    if uncertain and handling_mode == "clarify":
        commitment = (
            f'If the unknown remains after this read, neither choose nor commit to any branch: then ask this single natural question you had considered: "{handling_plan}", then wait without composing the choices that depend on it. If the exact bodies resolve it, respond directly from them.'
        )
    elif uncertain and handling_mode == "condition":
        commitment = (
            f'If the unknown remains but a useful response can proceed, then begin with this condition you had considered: "{handling_plan}". If the exact bodies resolve the unknown, respond directly without artificially retaining the condition.'
        )
    elif uncertain:
        commitment = (
            f'The conduct `{handling_mode}` proposed before reading does not resolve the declared unknowns. If the exact bodies resolve them, respond directly; otherwise now choose clarify or condition according to their actual effect on the response.'
        )
    elif handling_mode == "offer":
        commitment = (
            f'Now respond with exactly this proposal you chose yourself: "{handling_plan}". Add no elaboration before the user responds.'
        )
    elif handling_mode in {"clarify", "condition"}:
        commitment = (
            f'No material unknown was declared before reading: the proposed precaution `{handling_mode}` was provisional. Ask a question or state a condition only if the exact bodies still reveal an unknown that genuinely changes the response; otherwise respond directly.'
        )
    else:
        commitment = 'No material unknown was declared: respond directly.'
    print(
        '\nNow reassess the scene from the exact bodies: pre-reading conduct was provisional. '
        + commitment
        + ' A plausible suggestion never suffices to resolve a material unknown. With no material unknown, remain direct and do not request confirmation as a precaution.'
    )
    return 0


def _neighbors(catalog: dict[str, Path], requested: str) -> int:
    clean = _clean_path(requested)
    if clean is None or clean not in catalog:
        _emit_event(
            {
                "operation": "neighbors",
                "path": requested,
                "paths": [],
                "error": "unknown_path",
            }
        )
        return 2
    content = catalog[clean].read_text(encoding="utf-8")
    paths = []
    seen = set()
    for match in LINK_PATTERN.finditer(content):
        name = match.group(1).rstrip("./-")
        if name in catalog and name != clean and name not in seen:
            seen.add(name)
            paths.append(name)
    _emit_event({"operation": "neighbors", "path": clean, "paths": paths})
    for name in paths:
        print(name)
    return 0


def _members(
    catalog: dict[str, Path],
    requested: str,
    *,
    graph_index: Path | None,
    memory_root: Path | None,
    offset: int,
    limit: int,
) -> int:
    clean = _clean_path(requested)
    if clean is None or clean not in catalog:
        _emit_event(
            {
                "operation": "members",
                "path": requested,
                "members": [],
                "error": "unknown_path",
            }
        )
        return 2
    if graph_index is None or memory_root is None:
        _emit_event(
            {
                "operation": "members",
                "path": clean,
                "members": [],
                "error": "graph_unavailable",
            }
        )
        return 2
    try:
        index = load_memory_graph_index(graph_index.expanduser().resolve())
        page = read_collection_page(
            index,
            clean,
            memory_root=memory_root.expanduser().resolve(),
            offset=offset,
            limit=limit,
        )
    except (CollectionReadError, OSError, ValueError, json.JSONDecodeError) as exc:
        _emit_event(
            {
                "operation": "members",
                "path": clean,
                "members": [],
                "error": "collection_unavailable",
                "detail": str(exc)[:300],
            }
        )
        return 2
    members = [
        {"path": member.path, "role": member.role}
        for member in page.members
        if member.path in catalog
    ]
    unavailable = [
        member.path for member in page.members if member.path not in catalog
    ]
    event = {
        "operation": "members",
        "path": clean,
        "coverage": page.coverage,
        "known_member_count": page.known_member_count,
        "offset": page.offset,
        "members": members,
        "unavailable_paths": unavailable,
        "next_offset": page.next_offset,
        "known_members_complete": page.known_members_complete,
        "claimed_exhaustive": page.claimed_exhaustive and not unavailable,
    }
    _emit_event(event)
    print(
        f"Collection [[{clean}]]: coverage={page.coverage}, "
        f"known_members={page.known_member_count}, next_offset={page.next_offset}."
    )
    for member in members:
        print(f"- [[{member['path']}]] : {member['role']}")
    if unavailable:
        print("Unavailable active members: " + ", ".join(unavailable))
    print(
        "Ces appartenances sont sourcees, mais les corps des membres ne sont "
        "pas encore lus. Utilise read sur les chemins utiles avant de repondre."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    subparsers = parser.add_subparsers(dest="operation", required=True)
    browse = subparsers.add_parser("browse")
    browse.add_argument("prefix", nargs="?", default="")
    read = subparsers.add_parser("read")
    read.add_argument("--established", action="append", default=[])
    read.add_argument("--uncertain", action="append", default=[])
    read.add_argument("--need", required=True)
    read.add_argument(
        "--handling-mode",
        choices=("direct", "clarify", "condition", "offer"),
        required=True,
    )
    read.add_argument("--handling-plan", required=True)
    read.add_argument("paths", nargs="+")
    neighbors = subparsers.add_parser("neighbors")
    neighbors.add_argument("path")
    members = subparsers.add_parser("members")
    members.add_argument("path")
    members.add_argument("--graph-index", type=Path, required=True)
    members.add_argument("--memory-root", type=Path, required=True)
    members.add_argument("--offset", type=int, default=0)
    members.add_argument(
        "--limit",
        type=int,
        default=MAX_COLLECTION_PAGE_MEMBERS,
    )
    args = parser.parse_args()

    root = args.root.resolve()
    if not root.is_dir():
        _emit_event({"operation": args.operation, "error": "library_unavailable"})
        return 2
    catalog = _catalog(root)
    if args.operation == "browse":
        return _browse(catalog, args.prefix)
    if args.operation == "read":
        return _read(
            catalog,
            args.paths,
            established=args.established,
            uncertain=args.uncertain,
            need=args.need.strip(),
            handling_mode=args.handling_mode,
            handling_plan=args.handling_plan.strip(),
        )
    if args.operation == "members":
        return _members(
            catalog,
            args.path,
            graph_index=args.graph_index,
            memory_root=args.memory_root,
            offset=args.offset,
            limit=args.limit,
        )
    return _neighbors(catalog, args.path)


if __name__ == "__main__":
    raise SystemExit(main())
