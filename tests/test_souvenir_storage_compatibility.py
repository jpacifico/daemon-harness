"""The public rename must never migrate exact memory or queued provenance."""
from daemon_core.after_turn_spool import AfterTurnJob, SceneProjectionJob
from daemon_core.memory.graph import GraphIssue
from daemon_core.memory.metadata import parse_souvenir_frontmatter
from daemon_core.memory.reader import souvenir_path
from daemon_core.memory.writer import souvenir_recalls_path
from daemon_core.scene_model import AUTHORITATIVE_SCENE_SOURCE_KINDS


def test_legacy_turn_and_projection_fields_round_trip():
    def reference(role):
        return {'path': 'levels/L0/synthetic.md', 'offset': 0, 'length': 5,
                'sha256': 'a' * 64, 'role': role, 'turn_id': 'synthetic-turn'}
    raw = {
        'schema': 'daemon.after-turn-job.v1', 'host': 'hermes',
        'user_id': 'synthetic', 'turn_id': 'synthetic-turn',
        'source_turn_started_at': 1.0, 'scene_scope': 'telegram__synthetic',
        'user_l0': reference('user'), 'assistant_l0': reference('assistant'),
        'selected_capsules': ['theme/synthetic'],
        'read_capsules': ['theme/synthetic'],
        'tool_read_capsules': ['theme/synthetic'],
        'library_active': True, 'origin': {},
        'created_at': '2026-01-01T00:00:00.000+00:00', 'attempts': 0,
        'available_at': 0.0, 'last_error': '', 'lease_pid': 0,
    }
    job = AfterTurnJob.from_dict(raw)
    assert job.read_souvenirs == ('theme/synthetic',)
    assert job.as_dict() == raw
    projection_raw = SceneProjectionJob.from_turn(job).as_dict()
    assert projection_raw['read_capsules'] == ['theme/synthetic']
    assert projection_raw['capsule_read_sources'][0]['paths'] == ['theme/synthetic']
    assert SceneProjectionJob.from_dict(projection_raw).as_dict() == projection_raw


def test_legacy_graph_source_and_paths_are_not_renamed():
    raw = {'code': 'synthetic-issue', 'capsule': 'theme/synthetic'}
    assert GraphIssue.from_dict(raw).as_dict() == raw
    assert 'capsule' in AUTHORITATIVE_SCENE_SOURCE_KINDS
    assert 'souvenir' not in AUTHORITATIVE_SCENE_SOURCE_KINDS
    assert souvenir_path('theme/synthetic').parts[-3:] == ('capsules', 'theme', 'synthetic.md')
    assert souvenir_recalls_path().name == 'capsule_recalls.md'


def test_stored_markdown_body_and_headings_are_exact():
    body = '# Capsule: theme/synthetic\n\n## Sous-capsules\n- [[theme/child]]\n'
    metadata, exact = parse_souvenir_frontmatter('---\nsubjects: [Alex]\n---\n' + body)
    assert metadata == {'subjects': ['Alex']}
    assert exact == body
