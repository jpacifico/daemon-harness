from datetime import datetime

import pytest

from daemon_core.memory_dates import MemoryDateError, normalize_date_text, normalize_memory_dates
from daemon_core.agenda_governance import assess_personal_agenda_line
from daemon_core.near_term_timeline import compile_near_term_timeline, timeline_group_key


@pytest.mark.parametrize('source, expected', [
    ('14 November 2026', '2026-11-14'),
    ('November 14, 2026', '2026-11-14'),
    ('14 novembre 2026', '2026-11-14'),
    ('1er février 2024', '2024-02-01'),
    ('February 29th, 2024', '2024-02-29'),
    ('2026-11-14', '2026-11-14'),
    ('November 2026', '2026-11'),
    ('novembre 2026', '2026-11'),
    ('2026-11', '2026-11'),
    ('2026', '2026'),
    ('14–16 November 2026', '2026-11-14–2026-11-16'),
    ('November 14 - 16, 2026', '2026-11-14 - 2026-11-16'),
    ('Du 3 au 6 août 2026', 'Du 2026-08-03 au 2026-08-06'),
    ('14 through 16 November 2026', '2026-11-14 through 2026-11-16'),
    ('from 2026-11-14 to 2026-11-16', 'from 2026-11-14 to 2026-11-16'),
    ('around 14 November 2026, not confirmed', 'around 2026-11-14, not confirmed'),
    ('18 juillet 1998', '1998-07-18'),
    ('2026-11-14T09:30:00+01:00', '2026-11-14T09:30:00+01:00'),
])
def test_calendar_values_are_locale_independent_and_idempotent(source, expected):
    actual = normalize_date_text(source)
    assert actual == expected
    assert normalize_date_text(actual) == actual


@pytest.mark.parametrize('source', ['30 February 2026', '31 avril 2026',
                                    '2026-02-29', '2026-13', '2026-11-14T99:30:00+01:00', '16–14 November 2026',
                                    '28 October to 2 November 2026'])
def test_impossible_or_incomplete_ranges_are_not_silently_reinterpreted(source):
    with pytest.raises(MemoryDateError):
        normalize_date_text(source)
    assert not assess_personal_agenda_line('- ' + source + ' | Alex | event').operational


@pytest.mark.parametrize('source', ['14 November', '11/12/2026', '03/04/26',
                                    'next Friday', 'tomorrow', 'around 1998'])
def test_missing_precision_and_ambiguous_dates_are_never_invented(source):
    assert normalize_date_text(source) == source


def test_exact_sources_aliases_links_quotes_code_and_existing_history_survive():
    old = '- Began on 14 novembre 2026.'
    content = '---\naliases: [14 November 2026]\ncreated: "14 November 2026"\nspan_start: 2026-11-14T09:30:00+01:00\n---\n# 14 November 2026\n## History\n- Began on 14 novembre 2026.\n- New fact, 15 novembre 2026.\n- He said "14 November 2026"; date confirmed 16 November 2026.\n- Exact « 14 novembre 2026 » and `14 November 2026`.\n- [[capsules/14 November 2026|14 November 2026]]\n- [14 November 2026](notes/source.md)\n> Source: 14 November 2026\n```\n14 November 2026\n```\n    14 November 2026\n## Sources\n- Quotation: 14 November 2026\n## Last confirmed state\n- Confirmed on November 17, 2026.\n'
    actual = normalize_memory_dates(content, current=old)
    assert 'created: "2026-11-14"' in actual
    assert '- New fact, 2026-11-15.' in actual
    assert '- He said "14 November 2026"; date confirmed 2026-11-16.' in actual
    assert '- Confirmed on 2026-11-17.' in actual
    expected = content.replace('created: "14 November 2026"', 'created: "2026-11-14"').replace(
        '- New fact, 15 novembre 2026.', '- New fact, 2026-11-15.').replace(
        'date confirmed 16 November 2026.', 'date confirmed 2026-11-16.').replace(
        '- Confirmed on November 17, 2026.', '- Confirmed on 2026-11-17.')
    assert actual == expected


@pytest.mark.parametrize('horizon, start, end', [
    ('14 November 2026', '2026-11-14', '2026-11-14'),
    ('2026-11-14T09:30:00+01:00', '2026-11-14', '2026-11-14'),
    ('14 novembre 2026', '2026-11-14', '2026-11-14'),
    ('2026-11', '2026-11-01', '2026-11-30'),
    ('November 2026', '2026-11-01', '2026-11-30'),
    ('November 2026 through December 2026', '2026-11-01', '2026-12-31'),
    ('until end of November 2026', '', '2026-11-30'),
    ('14–16 November 2026', '2026-11-14', '2026-11-16'),
    ('week of 16 November 2026', '2026-11-16', '2026-11-22'),
])
def test_agenda_projects_real_day_or_entire_period_and_keeps_exact_source(horizon, start, end):
    raw = horizon + ' | Alex | Room Cedar booking'
    timeline = compile_near_term_timeline(now=datetime(2026, 11, 14), personal_agenda='- ' + raw)
    assert timeline.complete and len(timeline.entries) == 1
    entry = timeline.entries[0]
    assert (entry.starts_on, entry.ends_on) == (start, end)
    assert entry.raw == raw
    assert timeline_group_key(entry, datetime(2027, 1, 10).date()) == 'earlier'


@pytest.mark.parametrize('horizon', ['Every Friday', 'Fridays', 'Chaque vendredi',
                                    'Monthly', 'Each Saturday and Sunday'])
def test_explicit_english_and_french_recurrences_are_retained(horizon):
    assert assess_personal_agenda_line('- ' + horizon + ' | Alex | practice').operational


def test_generated_dates_do_not_trigger_migration_of_unchanged_lines():
    old = '- Legacy: 14 novembre 2026.'
    assert normalize_memory_dates(old, current=old) == old
    assert normalize_memory_dates(old + '\n- New: 15 November 2026.', current=old) == (
        old + '\n- New: 2026-11-15.')


@pytest.mark.parametrize('obsidian', [False, True])
def test_new_note_and_short_context_use_iso_without_altering_source_or_l0(tmp_path, monkeypatch, obsidian):
    from daemon_core import memory_agent
    from daemon_core.memory import reader, writer
    from daemon_core.memory.metadata import parse_souvenir_frontmatter
    for module in (memory_agent, reader, writer):
        monkeypatch.setattr(module, 'MEMORY_ROOT', tmp_path)
    monkeypatch.setenv('DAEMON_CORE_OBSIDIAN_FORMAT', '1' if obsidian else '')
    exact = 'The trial on October 10, 2026 showed birch plywood is more durable.'
    reference = writer.append_ground_zero('dates', 'user', exact, turn_id='date-fixture', return_reference=True)
    before = writer.read_ground_zero_reference(reference)
    result = memory_agent._apply_memory_agent_result('dates', {
        'source_posture': 'direct_fact',
        'user_context_md': '# Context\n- Decided on 10 octobre 2026.',
        'capsule_updates': [{
            'name': 'theme/workshop/material',
            'content_md': '---\ncreated: October 10, 2026\naliases: [October 10]\n---\n# Material\n\n- Chosen on October 10, 2026.\n> '+exact,
        }],
    }, user_message=exact)
    assert not result.rejected_paths
    saved = (tmp_path / 'capsules/theme/workshop/material.md').read_text()
    metadata, body = parse_souvenir_frontmatter(saved)
    assert str(metadata['created']) == '2026-10-10'
    assert metadata['aliases'] == ['October 10']
    assert '- Chosen on 2026-10-10.' in body
    assert '> '+exact in body
    assert '2026-10-10' in (tmp_path / 'users/dates/context.md').read_text()
    assert writer.read_ground_zero_reference(reference) == before


def test_dreaming_applies_same_date_contract_and_preserves_legacy(tmp_path, monkeypatch):
    from daemon_core import dreaming
    monkeypatch.setattr(dreaming, 'MEMORY_ROOT', tmp_path)
    target = tmp_path / 'capsules/theme/workshop/material.md'
    target.parent.mkdir(parents=True)
    old = '# Material\n\n- Trial held on October 10, 2026.\n'
    target.write_text(old)
    result = dreaming.apply_dreaming_result('dates', {'updates': [{
        'path': 'memory/capsules/theme/workshop/material.md', 'action': 'update',
        'reason': 'New exact evidence.',
        'content_md': old + '- Decision on 14 novembre 2026.\n',
    }]}, dry_run=False)
    assert not result.rejected_updates
    assert target.read_text() == old + '- Decision on 2026-11-14.\n'
