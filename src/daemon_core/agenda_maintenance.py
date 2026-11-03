"""Lossless maintenance for the operational personal agenda.

The active file keeps only current, future and recurring horizons.  Rows that
are historical or outside the agenda boundary move byte-for-byte to a dated
Markdown archive.  Moving a row never claims that its event occurred.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from daemon_core.agenda_governance import assess_personal_agenda_line
from daemon_core.memory.integrity import atomic_write_text, memory_file_lock
from daemon_core.memory.writer import safe_memory_user_id
from daemon_core.near_term_timeline import (
    compile_near_term_timeline,
    timeline_group_key,
)
from daemon_core.paths import MEMORY_ROOT

AGENDA_ARCHIVE_SCHEMA = "daemon.personal-agenda-archive.v1"
ACTIVE_GROUPS = frozenset(
    {"current", "today", "tomorrow", "near", "later", "undated"}
)


@dataclass(frozen=True)
class ArchivedAgendaLine:
    line: str
    reason: str


@dataclass(frozen=True)
class AgendaMigrationPlan:
    planned_at: str
    source_sha256: str
    active_sha256: str
    active_md: str
    archive_md: str
    retained_lines: tuple[str, ...]
    archived_lines: tuple[ArchivedAgendaLine, ...]
    source_line_count: int

    @property
    def complete(self) -> bool:
        return self.source_line_count == (
            len(self.retained_lines) + len(self.archived_lines)
        )


@dataclass(frozen=True)
class AgendaMigrationApplyResult:
    agenda_path: Path
    archive_path: Path
    source_sha256: str
    active_sha256: str
    retained_count: int
    archived_count: int
    changed: bool


def plan_personal_agenda_migration(
    content: str,
    *,
    now: datetime,
) -> AgendaMigrationPlan:
    """Split one agenda without deleting or semantically completing a row."""

    timeline = compile_near_term_timeline(now=now, personal_agenda=content)
    if not timeline.complete:
        raise ValueError("agenda migration refuses an incomplete source scan")

    groups_by_raw: dict[str, deque[str]] = defaultdict(deque)
    for entry in timeline.entries:
        groups_by_raw[entry.raw].append(
            timeline_group_key(entry, now.date())
        )

    retained: list[str] = []
    archived: list[ArchivedAgendaLine] = []
    source_bullets = [
        line.strip()
        for line in content.splitlines()
        if line.strip().startswith("- ")
    ]
    for line in source_bullets:
        assessment = assess_personal_agenda_line(line)
        if not assessment.operational:
            archived.append(
                ArchivedAgendaLine(line=line, reason="outside_agenda_boundary")
            )
            continue
        raw = line[2:].strip()
        if not groups_by_raw[raw]:
            raise ValueError("agenda migration lost an operational row")
        group = groups_by_raw[raw].popleft()
        if group in ACTIVE_GROUPS:
            retained.append(line)
        else:
            archived.append(
                ArchivedAgendaLine(line=line, reason="crossed_horizon")
            )

    metadata = _agenda_metadata(content)
    active_parts = ["# Agenda"]
    if metadata:
        active_parts.append("\n".join(metadata))
    if retained:
        active_parts.append("\n".join(retained))
    active_md = "\n\n".join(active_parts).rstrip() + "\n"
    planned_at = now.isoformat(timespec="seconds")
    source_sha256 = _sha256(content)
    archive_md = _render_archive(
        planned_at=planned_at,
        source_sha256=source_sha256,
        archived=archived,
    )
    plan = AgendaMigrationPlan(
        planned_at=planned_at,
        source_sha256=source_sha256,
        active_sha256=_sha256(active_md),
        active_md=active_md,
        archive_md=archive_md,
        retained_lines=tuple(retained),
        archived_lines=tuple(archived),
        source_line_count=len(source_bullets),
    )
    if not plan.complete:
        raise ValueError("agenda migration does not account for every source row")
    return plan


def apply_personal_agenda_migration(
    *,
    agenda_path: Path,
    archive_path: Path,
    plan: AgendaMigrationPlan,
) -> AgendaMigrationApplyResult:
    """Apply one precomputed plan with a source hash guard and exact archive."""

    agenda_path = agenda_path.expanduser().resolve()
    archive_path = archive_path.expanduser().resolve()
    with memory_file_lock(agenda_path):
        current = agenda_path.read_text(encoding="utf-8")
        if _sha256(current) != plan.source_sha256:
            raise ValueError("agenda changed since migration planning")
        if archive_path.exists():
            existing_archive = archive_path.read_text(encoding="utf-8")
            if existing_archive != plan.archive_md:
                raise ValueError("agenda archive path already contains other data")
        else:
            atomic_write_text(archive_path, plan.archive_md)
            os.chmod(archive_path, 0o600)
        changed = current != plan.active_md
        if changed:
            atomic_write_text(agenda_path, plan.active_md)
            os.chmod(agenda_path, 0o600)
    return AgendaMigrationApplyResult(
        agenda_path=agenda_path,
        archive_path=archive_path,
        source_sha256=plan.source_sha256,
        active_sha256=plan.active_sha256,
        retained_count=len(plan.retained_lines),
        archived_count=len(plan.archived_lines),
        changed=changed,
    )


def default_archive_path(
    *,
    memory_root: Path,
    user_id: str,
    plan: AgendaMigrationPlan,
) -> Path:
    stamp = datetime.fromisoformat(plan.planned_at).strftime("%Y-%m-%d")
    return (
        memory_root
        / "archive"
        / "agenda"
        / safe_memory_user_id(user_id)
        / f"{stamp}_{plan.source_sha256[:12]}.md"
    )


def _agenda_metadata(content: str) -> list[str]:
    preserved: list[str] = []
    root_heading_seen = False
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("- "):
            continue
        if stripped.casefold() == "# agenda" and not root_heading_seen:
            root_heading_seen = True
            continue
        preserved.append(line.rstrip())
    return preserved


def _render_archive(
    *,
    planned_at: str,
    source_sha256: str,
    archived: list[ArchivedAgendaLine],
) -> str:
    groups = (
        (
            "crossed_horizon",
            "Past horizons — outcome not inferred",
        ),
        (
            "outside_agenda_boundary",
            "Facts preserved outside the operational agenda",
        ),
    )
    parts = [
        (
            "---\n"
            f"schema: {AGENDA_ARCHIVE_SCHEMA}\n"
            f"archived_at: {planned_at}\n"
            f"source_sha256: {source_sha256}\n"
            f"archived_rows: {len(archived)}\n"
            "---"
        ),
        "# Archive de l’agenda personnel",
        (
            "These lines are preserved exactly. Their presence here "
            "proves neither completion, cancellation nor outcome."
        ),
    ]
    for reason, heading in groups:
        lines = [item.line for item in archived if item.reason == reason]
        if lines:
            parts.append(f"## {heading}\n\n" + "\n".join(lines))
    return "\n\n".join(parts).rstrip() + "\n"


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _summary(
    *,
    plan: AgendaMigrationPlan,
    result: AgendaMigrationApplyResult | None = None,
) -> dict[str, object]:
    return {
        "schema": "daemon.personal-agenda-migration-summary.v1",
        "planned_at": plan.planned_at,
        "source_sha256": plan.source_sha256,
        "active_sha256": plan.active_sha256,
        "source_rows": plan.source_line_count,
        "retained_rows": len(plan.retained_lines),
        "archived_rows": len(plan.archived_lines),
        "crossed_rows": sum(
            item.reason == "crossed_horizon" for item in plan.archived_lines
        ),
        "outside_boundary_rows": sum(
            item.reason == "outside_agenda_boundary"
            for item in plan.archived_lines
        ),
        "complete": plan.complete,
        "applied": bool(result and result.changed),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Plan or apply a lossless personal agenda migration."
    )
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--memory-root", type=Path, default=MEMORY_ROOT)
    parser.add_argument(
        "--now",
        default="",
        help="ISO timestamp; defaults to the current local time.",
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-sha256", default="")
    args = parser.parse_args()

    memory_root = args.memory_root.expanduser().resolve()
    agenda_path = (
        memory_root / "users" / safe_memory_user_id(args.user_id) / "agenda.md"
    )
    content = agenda_path.read_text(encoding="utf-8")
    plan = plan_personal_agenda_migration(
        content,
        now=(
            datetime.fromisoformat(args.now)
            if args.now
            else datetime.now().astimezone()
        ),
    )
    result = None
    if args.expected_sha256 and args.expected_sha256 != plan.source_sha256:
        parser.error("--expected-sha256 does not match the current agenda")
    if args.apply and plan.archived_lines:
        result = apply_personal_agenda_migration(
            agenda_path=agenda_path,
            archive_path=default_archive_path(
                memory_root=memory_root,
                user_id=args.user_id,
                plan=plan,
            ),
            plan=plan,
        )
    print(json.dumps(_summary(plan=plan, result=result), indent=2))


if __name__ == "__main__":
    main()


__all__ = [
    "ACTIVE_GROUPS",
    "AGENDA_ARCHIVE_SCHEMA",
    "AgendaMigrationApplyResult",
    "AgendaMigrationPlan",
    "ArchivedAgendaLine",
    "apply_personal_agenda_migration",
    "default_archive_path",
    "plan_personal_agenda_migration",
]
