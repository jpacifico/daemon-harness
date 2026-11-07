"""Deterministic checks of the Garden placement bench: seeding and scoring, no model call."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from scripts import qualify_parga_note_transitions as bench  # noqa: E402
from daemon_core.memory.library_layout import NoteLocation  # noqa: E402

GARDEN_CASES = [c['id'] for c in json.loads(bench.GARDEN_FIXTURE.read_text())['cases']]


def test_garden_cases_are_registered_beside_the_transition_cases():
    assert set(GARDEN_CASES) <= set(bench.CASES)
    assert len(set(bench.CASES)) == len(bench.CASES)
    assert all(bench.fixture_for(case) == bench.GARDEN_FIXTURE for case in GARDEN_CASES)
    assert bench.fixture_for('finished_project') == bench.FIXTURE


@pytest.mark.parametrize('case_id', GARDEN_CASES)
def test_every_garden_case_seeds_into_root_folders(tmp_path, case_id):
    (tmp_path / 'memory/capsules').mkdir(parents=True)
    memory, note = bench.seed_case(tmp_path, case_id)
    assert (memory / 'vault-layout.json').is_file()
    assert note.is_file()
    case = bench.case_fixture(case_id)
    assert note.relative_to(tmp_path / 'memory').as_posix() == case['notes'][0]['path']


def _write(vault, relative, text):
    path = vault / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_scoring_rejects_a_life_fact_filed_in_garden_and_accepts_it_in_areas(tmp_path):
    case = bench.case_fixture('garden_life_relationship')
    seed = NoteLocation('theme/health/morning-runs', _write(tmp_path, 'Areas/Health/Morning Runs.md', 'runs'), True)
    wrong = NoteLocation('theme/family/lena', _write(tmp_path, 'Garden/Family/Lena.md', 'Lena matters'), True)
    right = NoteLocation('theme/family/lena', _write(tmp_path, 'Areas/Family/Lena.md', 'Lena matters'), True)
    assert not any(bench._garden_checks(case, tmp_path, [seed, wrong]).values())
    assert all(bench._garden_checks(case, tmp_path, [seed, right]).values())
    # A turn that kept nothing about Lena fails too: absence is not a correct placement.
    assert not any(bench._garden_checks(case, tmp_path, [seed]).values())


def test_scoring_of_growth_and_unadopted_suggestion(tmp_path):
    grows = bench.case_fixture('garden_idea_grows')
    seed = NoteLocation('theme/knowledge/small-batches',
                        _write(tmp_path, 'Garden/Work/Small Batches.md', 'small batch grocery'), True)
    duplicate = NoteLocation('theme/knowledge/small-batches-home',
                             _write(tmp_path, 'Garden/Home/Small Batches at home.md', 'batch at home'), True)
    assert all(bench._garden_checks(grows, tmp_path, [seed]).values())
    assert not all(bench._garden_checks(grows, tmp_path, [seed, duplicate]).values())

    declined = bench.case_fixture('garden_suggestion_not_adopted')
    runs = NoteLocation('theme/health/morning-runs', _write(tmp_path, 'Areas/Health/Morning Runs.md', 'runs'), True)
    kept = NoteLocation('theme/ideas/journal', _write(tmp_path, 'Garden/Habits/Morning journal.md', 'journal'), True)
    assert all(bench._garden_checks(declined, tmp_path, [runs]).values())
    assert not all(bench._garden_checks(declined, tmp_path, [runs, kept]).values())
