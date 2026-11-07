#!/usr/bin/env python3
"""Fixed synthetic PARGA cases for the existing two native qualification runners."""
from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "docs/evaluations/fixtures/parga_note_transitions_en.json"
GARDEN_FIXTURE = REPO / "docs/evaluations/fixtures/parga_garden_placement_en.json"
FIXTURES = (FIXTURE, GARDEN_FIXTURE)
CASES = tuple(c['id'] for f in FIXTURES for c in json.loads(f.read_text())['cases'])


def fixture_for(case_id):
    """The fixture file that defines one case, for its checksum in results."""
    return next(f for f in FIXTURES if any(c['id'] == case_id for c in json.loads(f.read_text())['cases']))


def case_fixture(case_id):
    case = next(c for c in json.loads(fixture_for(case_id).read_text())['cases'] if c['id'] == case_id)
    first = case['notes'][0]
    return {**case, 'structured_note': first['text'], 'legacy_note': first['text'],
            'index': '- ' + first['name'] + ': ' + Path(first['path']).stem + '\n'}


def seed_case(root, case_id):
    """Replace only the synthetic seed before any runtime or source turn starts."""
    from daemon_core.memory.library_layout import with_identity
    from daemon_core.memory.root_migration import migrate_copy
    case = case_fixture(case_id)
    legacy = root / 'memory'
    for p in (legacy / 'capsules').rglob('*.md'):
        p.unlink()
    for item in case['notes']:
        p = legacy / 'capsules' / (item['name'] + '.md')
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(with_identity(item['text'], item['name']))
    (legacy / 'capsules/index.md').write_text(''.join('- ' + n['name'] + ': ' + Path(n['path']).stem + '\n' for n in case['notes']))
    legacy.rename(root / 'synthetic-seed')
    migrate_copy(root / 'synthetic-seed', root / 'memory', {n['name']: n['path'] for n in case['notes']})
    return root / 'memory/.daemon', root / 'memory' / case['notes'][0]['path']


def assess_case(root, result, case_id, mode):
    from daemon_core.memory.library_layout import note_locations, resolve_note
    from daemon_core.memory.graph import build_memory_graph
    case = case_fixture(case_id)
    memory, vault = root / 'memory/.daemon', root / 'memory'
    locations = note_locations(memory)
    checks = {
        'native_response_completed': bool(result.get('complete') or (result.get('returncode') == 0 and result.get('answer'))),
        'after_turn_complete': bool(result.get('lifecycle', {}).get('complete')),
        'one_cycle_completed': result.get('lifecycle', {}).get('cycle_status_counts', {}).get('completed') == 1,
        'no_failed_or_pending_work': not any(result.get('lifecycle', {}).get('spool_counts', {'missing': 1}).values()),
        'unique_identities': len({p.name for p in locations}) == len(locations),
        'no_pending_relocation': not (memory / 'levels/note_relocations/pending.json').exists(),
    }
    states = []
    for item in case['notes']:
        path = resolve_note(memory, item['name'])
        text = path.read_text()
        expected = item['expected_folder'] if mode == 'v1' else item['path'].split('/')[0]
        checks[item['name'] + ':folder'] = path.relative_to(vault).parts[0] == expected
        checks[item['name'] + ':history'] = all(line in text for line in item['preserve'])
        if item.get('grows_with_any'):
            checks[item['name'] + ':grew'] = any(term in text.lower() for term in item['grows_with_any'])
        states.append({'name': item['name'], 'path': path.relative_to(vault).as_posix(), 'text': text})
    if case.get('new_project_term'):
        checks['distinct_project_created'] = any(p.path.relative_to(vault).parts[0] == 'Projects' and
            case['new_project_term'].lower() in p.path.read_text().lower() for p in locations)
    if case_id == 'finished_project':
        graph = build_memory_graph(memory / 'capsules', memory_root=memory)
        checks['backlink_resolves'] = any(r.target == 'theme/work/harbor-workshop'
            for r in graph.index.souvenirs['theme/work/studio'].relations)
    checks.update(_garden_checks(case, vault, locations))
    receipts = [json.loads(p.read_text()) for p in (memory / 'levels/note_relocations').glob('*.json')]
    (root / 'relocation-inspection.json').write_text(json.dumps({'checks': checks, 'notes': states, 'receipts': receipts}, indent=2))
    return checks


def _garden_checks(case, vault, locations):
    """Placement expectations for notes created by the turn, matched by lowercase terms."""
    seeded = {item['name'] for item in case['notes']}
    def matching(spec, *, new_only=True):
        found = []
        for item in locations:
            if new_only and item.name in seeded:
                continue
            if all(term in item.path.read_text().lower() for term in spec['terms']):
                found.append(item.path.relative_to(vault).parts[0])
        return found
    checks = {}
    for index, spec in enumerate(case.get('expect_new', ())):
        folders, label = matching(spec), 'new:' + ('+'.join(spec['terms']) or 'any') + f':{index}'
        if 'folder' in spec:
            checks[label] = bool(folders) and all(f == spec['folder'] for f in folders)
        elif 'not_folder' in spec:
            checks[label] = bool(folders) and spec['not_folder'] not in folders
        else:
            checks[label] = spec['any_in_folder'] in folders
    for index, spec in enumerate(case.get('expect_absent', ())):
        folders = matching(spec, new_only=spec.get('new_only', False))
        checks['absent:' + '+'.join(spec['terms']) + f':{index}'] = spec['folder'] not in folders
    return checks
