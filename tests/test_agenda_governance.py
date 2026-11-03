from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from daemon_core.agenda_governance import (  # noqa: E402
    assess_personal_agenda_line,
    invalid_new_agenda_lines,
)


def test_operational_horizons_accept_dates_bounds_and_recurrences():
    accepted = (
        "- 2026-08-21 | Alex | travailler pour Acme",
        "- du 2026-08-21 au 2026-08-23 | Alex | garde de Sam",
        "- Jusqu’à fin août 2026 | Alex | rythme client | jeudi et vendredi",
        "- Chaque samedi et dimanche | Alex | week-end",
        "- Vendredi | Sam | cours de dessin | 18h15-19h45",
    )

    assert all(
        assess_personal_agenda_line(line).operational for line in accepted
    )


def test_unbounded_memory_labels_are_not_operational_agenda_horizons():
    rejected = (
        "- En cours | Alex et Sam | brûlerie | chocolat viennois",
        "- En ce moment | Sam | joue très bien Le Héros",
        "- Récemment | Alex | a acheté une extension",
        "- À prévoir | Alex | renvoyer le fer à souder",
        "- Depuis 26 ans | Alex | porte ses lunettes",
        "- Déjà | Marie | a récupéré Sam",
        "- Cette année | Alex et Sam | sont allés à une convention",
    )

    assessments = [assess_personal_agenda_line(line) for line in rejected]

    assert all(not item.operational for item in assessments)
    assert {item.reason for item in assessments} == {"unbounded_horizon"}


def test_legacy_unbounded_rows_may_survive_but_cannot_be_added_or_rewritten():
    legacy = "- En cours | Alex et Sam | brûlerie | souvenir durable"
    current = f"# Agenda\n\n{legacy}\n"
    valid_addition = "- 2026-08-22 | Alex | rendez-vous | 10h00"
    invalid_addition = "- En cours | Alex et Sam | Warhammer | intérêt partagé"

    assert invalid_new_agenda_lines(
        current,
        f"# Agenda\n\n{legacy}\n{valid_addition}\n",
    ) == ()
    invalid = invalid_new_agenda_lines(
        current,
        f"# Agenda\n\n{legacy}\n{invalid_addition}\n",
    )

    assert tuple(item.line for item in invalid) == (invalid_addition,)


def test_an_extra_duplicate_of_a_legacy_row_counts_as_a_new_row():
    legacy = "- En cours | Alex | souvenir sans horizon"

    invalid = invalid_new_agenda_lines(
        f"# Agenda\n\n{legacy}\n",
        f"# Agenda\n\n{legacy}\n{legacy}\n",
    )

    assert tuple(item.line for item in invalid) == (legacy,)


def test_malformed_absolute_date_is_refused():
    assessment = assess_personal_agenda_line(
        "- 2026-02-30 | Alex | rendez-vous impossible"
    )

    assert assessment.operational is False
    assert assessment.reason == "invalid_absolute_date"
