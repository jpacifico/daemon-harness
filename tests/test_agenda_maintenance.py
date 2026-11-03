from datetime import datetime
import os
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from daemon_core.agenda_maintenance import (  # noqa: E402
    apply_personal_agenda_migration,
    default_archive_path,
    plan_personal_agenda_migration,
)


NOW = datetime.fromisoformat("2026-08-23T12:00:00+02:00")


def test_migration_accounts_for_every_row_without_inferring_outcomes():
    crossed = "- 2026-08-20 | Alex | rendez-vous prévu"
    recent = "- 2026-08-22 | Alex | retour prévu"
    current = "- du 2026-08-21 au 2026-08-24 | Alex | garde de Sam"
    future = "- 2026-08-30 | Alex | prochain rendez-vous"
    recurring = "- Chaque vendredi | Sam | cours de dessin | 18h15"
    misplaced = "- En cours | Alex et Sam | aiment une brûlerie"
    source = (
        "# Agenda\n\n"
        f"{crossed}\n{recent}\n{current}\n{future}\n"
        f"{recurring}\n{misplaced}\n"
    )

    plan = plan_personal_agenda_migration(source, now=NOW)

    assert plan.complete is True
    assert plan.source_line_count == 6
    assert plan.retained_lines == (current, future, recurring)
    assert [(item.line, item.reason) for item in plan.archived_lines] == [
        (crossed, "crossed_horizon"),
        (recent, "crossed_horizon"),
        (misplaced, "outside_agenda_boundary"),
    ]
    assert "rendez-vous prévu" not in plan.active_md
    assert crossed in plan.archive_md
    assert recent in plan.archive_md
    assert misplaced in plan.archive_md
    assert "proves neither completion, cancellation nor outcome" in plan.archive_md


def test_migration_preserves_duplicate_rows_exactly():
    duplicate = "- 2026-08-20 | Alex | rendez-vous prévu"
    source = f"# Agenda\n\n{duplicate}\n{duplicate}\n"

    plan = plan_personal_agenda_migration(source, now=NOW)

    assert plan.complete is True
    assert len(plan.archived_lines) == 2
    assert plan.archive_md.count(duplicate) == 2


def test_migration_preserves_non_bullet_markdown_metadata():
    source = (
        "# Agenda\n\n"
        "Source : saisie personnelle.\n\n"
        "## Contraintes récurrentes\n"
        "- Chaque vendredi | Alex | contrainte récurrente\n"
    )

    plan = plan_personal_agenda_migration(source, now=NOW)

    assert "Source : saisie personnelle." in plan.active_md
    assert "## Contraintes récurrentes" in plan.active_md
    assert plan.source_line_count == 1
    assert plan.complete is True


def test_apply_writes_archive_then_active_agenda_with_private_modes(tmp_path):
    memory_root = tmp_path / "memory"
    agenda_path = memory_root / "users" / "garden" / "agenda.md"
    agenda_path.parent.mkdir(parents=True)
    crossed = "- 2026-08-20 | Alex | rendez-vous prévu"
    future = "- 2026-08-30 | Alex | prochain rendez-vous"
    source = f"# Agenda\n\n{crossed}\n{future}\n"
    agenda_path.write_text(source, encoding="utf-8")
    plan = plan_personal_agenda_migration(source, now=NOW)
    archive_path = default_archive_path(
        memory_root=memory_root,
        user_id="garden",
        plan=plan,
    )

    result = apply_personal_agenda_migration(
        agenda_path=agenda_path,
        archive_path=archive_path,
        plan=plan,
    )

    assert result.changed is True
    assert agenda_path.read_text(encoding="utf-8") == plan.active_md
    assert archive_path.read_text(encoding="utf-8") == plan.archive_md
    assert os.stat(agenda_path).st_mode & 0o777 == 0o600
    assert os.stat(archive_path).st_mode & 0o777 == 0o600


def test_apply_refuses_a_source_changed_after_planning(tmp_path):
    agenda_path = tmp_path / "memory" / "users" / "garden" / "agenda.md"
    agenda_path.parent.mkdir(parents=True)
    source = "# Agenda\n\n- 2026-08-20 | Alex | rendez-vous prévu\n"
    agenda_path.write_text(source, encoding="utf-8")
    plan = plan_personal_agenda_migration(source, now=NOW)
    agenda_path.write_text(
        source + "- 2026-08-30 | Alex | nouveau rendez-vous\n",
        encoding="utf-8",
    )
    archive_path = tmp_path / "memory" / "archive" / "agenda.md"

    with pytest.raises(ValueError, match="changed since migration planning"):
        apply_personal_agenda_migration(
            agenda_path=agenda_path,
            archive_path=archive_path,
            plan=plan,
        )

    assert archive_path.exists() is False


def test_apply_refuses_to_overwrite_an_unrelated_archive(tmp_path):
    agenda_path = tmp_path / "memory" / "users" / "garden" / "agenda.md"
    archive_path = tmp_path / "memory" / "archive" / "agenda.md"
    agenda_path.parent.mkdir(parents=True)
    archive_path.parent.mkdir(parents=True)
    source = "# Agenda\n\n- 2026-08-20 | Alex | rendez-vous prévu\n"
    agenda_path.write_text(source, encoding="utf-8")
    archive_path.write_text("unrelated\n", encoding="utf-8")
    plan = plan_personal_agenda_migration(source, now=NOW)

    with pytest.raises(ValueError, match="already contains other data"):
        apply_personal_agenda_migration(
            agenda_path=agenda_path,
            archive_path=archive_path,
            plan=plan,
        )

    assert agenda_path.read_text(encoding="utf-8") == source
    assert archive_path.read_text(encoding="utf-8") == "unrelated\n"
