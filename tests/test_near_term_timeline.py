from datetime import datetime
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from daemon_core.near_term_timeline import (
    compile_near_term_timeline,
    render_near_term_timeline,
)


NOW = datetime(2026, 8, 4, 18, 30, tzinfo=ZoneInfo("Europe/Paris"))


def test_timeline_keeps_preparation_deadline_and_meeting_distinct():
    personal = """# Agenda

- 2026-08-04 soir | Alex | préparer la présentation Acme | le soir | vingt slides à reprendre.
- 2026-08-05 | Alex | présenter son backlog | horaire non précisé | encore incomplet dans le bus.
- Vendredi | Sam | cours de dessin | 18h15-19h45 | hors vacances scolaires.
"""
    professional = """# Agenda professionnel (lecture seule)

Source : calendrier Outlook synchronisé (Calendrier).
Dernière synchro : mardi 4 août 2026, 18:27.
Fenêtre : 14 prochains jours. 1 événement(s).

## jeudi 6 août
- 12:00–12:30 sync Acme (Microsoft Teams Meeting)
"""

    timeline = compile_near_term_timeline(
        now=NOW,
        personal_agenda=personal,
        professional_agenda=professional,
    )
    rendered = render_near_term_timeline(timeline)

    assert timeline.complete is True
    assert timeline.source_bullet_count == 4
    assert len(timeline.entries) == 4
    assert len({entry.entry_id for entry in timeline.entries}) == 4
    assert "Today — 2026-08-04 (Tuesday)" in rendered
    assert "2026-08-04 (soir) | Alex | préparer la présentation Acme" in rendered
    assert "Tomorrow — 2026-08-05 (Wednesday)" in rendered
    assert "2026-08-05 | Alex | présenter son backlog" in rendered
    assert "2026-08-06 | 12:00–12:30 sync Acme" in rendered
    assert rendered.index("préparer la présentation Acme") < rendered.index(
        "présenter son backlog"
    )
    assert rendered.index("présenter son backlog") < rendered.index("sync Acme")


def test_timeline_withholds_unbounded_memory_but_keeps_recurrence_once():
    personal = """# Agenda

- En cours | Alex | éviter les repas lourds | durée non précisée | décision actuelle.
- Chaque samedi et dimanche | Alex | week-end | durée non précisée | ne travaille pas.
"""

    timeline = compile_near_term_timeline(now=NOW, personal_agenda=personal)
    rendered = render_near_term_timeline(timeline)

    assert timeline.complete is True
    assert (
        "## Recurring or bounded horizons without a resolved calendar date"
        in rendered
    )
    assert "éviter les repas lourds" not in rendered
    assert rendered.count("Chaque samedi et dimanche") == 1
    assert len(timeline.entries) == 1
    assert len(timeline.withheld_entries) == 1
    assert timeline.withheld_entries[0].reason == "unbounded_horizon"
    assert timeline.withheld_entries[0].sha256


def test_professional_calendar_uses_sync_year_across_a_year_boundary():
    now = datetime(2026, 12, 29, 9, 0, tzinfo=ZoneInfo("Europe/Paris"))
    professional = """# Agenda professionnel (lecture seule)
Dernière synchro : mardi 29 décembre 2026, 08:59.

## samedi 2 janvier
- 09:00–09:30 revue annuelle
"""

    timeline = compile_near_term_timeline(
        now=now,
        professional_agenda=professional,
    )

    assert timeline.entries[0].starts_on == "2027-01-02"


def test_render_can_reuse_an_existing_unambiguous_turn_clock():
    timeline = compile_near_term_timeline(
        now=NOW,
        personal_agenda="- 2026-08-04 | Alex | finir le dossier",
    )

    rendered = render_near_term_timeline(timeline, include_now=False)

    assert "## Now" not in rendered
    assert "Today — 2026-08-04 (Tuesday)" in rendered


def test_timeline_keeps_approximate_and_open_ended_date_statuses_visible():
    personal = """# Agenda
- Vers le 2026-08-17 | Alex | reprise possible | horaire non précisé
- À compter du 2026-08-03 | Alex | travail chez le client | trois jours
"""

    timeline = compile_near_term_timeline(now=NOW, personal_agenda=personal)
    rendered = render_near_term_timeline(timeline)

    assert timeline.entries[0].approximate is True
    assert timeline.entries[1].open_ended is True
    assert "around 2026-08-17 | Alex | reprise possible" in rendered
    assert "from 2026-08-03 | Alex | travail chez le client" in rendered


def test_expired_dated_entries_remain_auditable_but_leave_the_voice_timeline():
    personal = """# Agenda
- 2026-07-20 | Alex | ancien rendez-vous
- 2026-08-04 | Alex | rendez-vous actuel
"""

    timeline = compile_near_term_timeline(now=NOW, personal_agenda=personal)
    rendered = render_near_term_timeline(timeline)

    assert len(timeline.entries) == 2
    assert any("ancien rendez-vous" in entry.raw for entry in timeline.entries)
    assert "ancien rendez-vous" not in rendered
    assert "rendez-vous actuel" in rendered


def test_recently_crossed_entries_remain_visible_without_asserting_their_outcome():
    personal = """# Agenda
- 2026-07-20 | Alex | ancien rendez-vous
- 2026-08-01 | Alex | dépôt récent | horaire non précisé | prévu.
- 2026-08-03 | parents d'Alex | déménagement | horaire non précisé | échéance familiale.
- 2026-08-04 | Alex | rendez-vous actuel
"""

    timeline = compile_near_term_timeline(now=NOW, personal_agenda=personal)
    rendered = render_near_term_timeline(timeline)

    assert "## Recently crossed dated horizons" in rendered
    assert "unless the row itself gives an outcome, its outcome remains unknown" in rendered
    assert "[agenda personnel; date crossed 2026-08-02] 2026-08-01 | Alex | dépôt récent" in rendered
    assert "[agenda personnel; date crossed 2026-08-04] 2026-08-03 | parents d'Alex | déménagement" in rendered
    assert "ancien rendez-vous" not in rendered
    assert rendered.count("dépôt récent") == 1
    assert rendered.count("déménagement") == 1


def test_recently_crossed_window_uses_the_end_date_and_stops_after_seven_days():
    now = datetime(2026, 8, 15, 9, 0, tzinfo=ZoneInfo("Europe/Paris"))
    personal = """# Agenda
- du 2026-08-06 au 2026-08-08 | Alex | plage à la borne incluse
- 2026-08-07 | Alex | échéance trop ancienne
"""

    timeline = compile_near_term_timeline(now=now, personal_agenda=personal)
    rendered = render_near_term_timeline(timeline)

    assert "plage à la borne incluse" in rendered
    assert "[agenda personnel; date crossed 2026-08-09]" in rendered
    assert "échéance trop ancienne" not in rendered


def test_crossed_projection_preserves_an_explicit_source_outcome_verbatim():
    timeline = compile_near_term_timeline(
        now=NOW,
        personal_agenda=(
            "- 2026-08-03 | Alex | estimation des coûts | réalisée | terminée."
        ),
    )

    rendered = render_near_term_timeline(timeline)

    assert "estimation des coûts | réalisée | terminée" in rendered
    assert rendered.count("estimation des coûts") == 1


def test_bounded_month_is_current_then_leaves_the_active_timeline():
    personal = (
        "- Jusqu’à fin août 2026 | Alex | rythme temporaire | "
        "jeudi et vendredi"
    )
    august = datetime(
        2026,
        8,
        23,
        12,
        0,
        tzinfo=ZoneInfo("Europe/Paris"),
    )
    september = datetime(
        2026,
        9,
        10,
        12,
        0,
        tzinfo=ZoneInfo("Europe/Paris"),
    )

    active = compile_near_term_timeline(now=august, personal_agenda=personal)
    expired = compile_near_term_timeline(
        now=september,
        personal_agenda=personal,
    )

    assert active.entries[0].starts_on == ""
    assert active.entries[0].ends_on == "2026-08-31"
    assert "rythme temporaire" in render_near_term_timeline(active)
    assert "rythme temporaire" not in render_near_term_timeline(expired)


def test_week_horizon_uses_the_whole_seven_day_window():
    sunday = datetime(
        2026,
        8,
        23,
        12,
        0,
        tzinfo=ZoneInfo("Europe/Paris"),
    )
    personal = "- Semaine du 2026-08-17 | Alex | organisation temporaire"

    timeline = compile_near_term_timeline(
        now=sunday,
        personal_agenda=personal,
    )

    assert timeline.entries[0].starts_on == "2026-08-17"
    assert timeline.entries[0].ends_on == "2026-08-23"
    assert "organisation temporaire" in render_near_term_timeline(timeline)
