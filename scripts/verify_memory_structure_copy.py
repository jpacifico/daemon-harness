"""Offline integrity audit for the authorized copied-memory format run."""
import collections
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

repo = Path(__file__).resolve().parents[1]
root = Path(sys.argv[1]).resolve()
sys.path[:0] = [str(repo), str(repo / 'src')]
spec = importlib.util.spec_from_file_location('preparation', repo / 'scripts/prepare_memory_structure.py')
prep = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prep)
# Set private paths before importing modules that bind MEMORY_ROOT.
prep.configure(type('Args', (), {'output': root, 'codex_bin': None})())
from daemon_core.memory.context_page_formatting import render_format_decision  # noqa: E402
from daemon_core.memory.context_pages import GENERATED, PROPERTIES, exact_body_lines, validate_context_page  # noqa: E402
from daemon_core.memory.metadata import parse_souvenir_frontmatter  # noqa: E402
from daemon_core.memory.obsidian import prepare_souvenir_format  # noqa: E402
from daemon_core.memory.graph import parse_souvenir_graph  # noqa: E402

manifest = json.loads((root / 'manifest.json').read_text())
original = root / 'original-memory'
memory = root / 'memory'
failures = []
retained_legacy = []
expected_changed = set()
classifications = collections.Counter()
for record_path in sorted((root / 'format-batches').glob('*.json')):
    record = json.loads(record_path.read_text())
    recovery_path = root / 'format-recovery' / record_path.name
    if not record.get('complete') and recovery_path.exists():
        recovery = json.loads(recovery_path.read_text())
        assert recovery['original_response_sha256'] == hashlib.sha256(record['response'].encode()).hexdigest()
        retained_legacy.extend(recovery['refused'])
        record = recovery
    if not record.get('complete'):
        failures.append({'batch': record_path.name, 'error': record.get('error', 'incomplete')})
        continue
    decisions = json.loads(record['response'])['notes']
    for decision in decisions:
        name = decision['name']
        relative = 'capsules/' + name + '.md'
        before = (original / relative).read_bytes()
        after = (memory / relative).read_text()
        assert hashlib.sha256(before).hexdigest() == record['source_hashes'][name]
        expected = render_format_decision(name, before, decision)
        # YAML key order is presentation; values and the exact body are invariant.
        assert parse_souvenir_frontmatter(expected) == parse_souvenir_frontmatter(after), relative
        validate_context_page(after)
        # Independent guards: every factual line and every personal property.
        normalized_before = prepare_souvenir_format(before.decode(), name)
        assert exact_body_lines(normalized_before) == exact_body_lines(after), relative
        old_meta, _ = parse_souvenir_frontmatter(normalized_before)
        new_meta, _ = parse_souvenir_frontmatter(after)
        assert {k:v for k,v in old_meta.items() if k not in PROPERTIES} == {k:v for k,v in new_meta.items() if k not in PROPERTIES}, relative
        old_graph = parse_souvenir_graph(name, normalized_before)
        new_graph = parse_souvenir_graph(name, after)
        def relations(g):
            return collections.Counter((r.source, r.target, r.kind, r.description, r.source_refs) for r in g.relations)
        assert relations(old_graph) == relations(new_graph), relative + ' graph changed'
        assert old_graph.collection == new_graph.collection, relative + ' collection changed'
        expected_changed.add(relative)
        classifications[decision['kind']] += 1

actual = prep.hashes(memory)
original_hashes = prep.hashes(original)
assert original_hashes == manifest['source_hashes'], 'Frozen original copy changed'
missing = sorted(set(original_hashes) - set(actual))
assert not missing, missing
changed = {n for n in original_hashes if original_hashes[n] != actual[n]}
unexpected_changes = sorted(changed - expected_changed)
assert not unexpected_changes, unexpected_changes
added = sorted(set(actual) - set(original_hashes))
for name in added:
    if name == '.memory-structure-prototype.json' or name.endswith('.lock'):
        continue
    assert name == 'home.md' or name.startswith('views/'), name
    assert (memory / name).read_text().startswith(GENERATED + '\n'), name

summary = {'complete': not failures, 'source_files': len(original_hashes),
           'formatted_notes': len(expected_changed), 'unchanged_original_files': len(original_hashes) - len(changed),
           'modified_paths': sorted(changed), 'added_paths': added,
           'classification_counts': dict(classifications), 'failures': failures, 'retained_legacy_notes': retained_legacy,
           'checks': ['snapshot hashes unchanged', 'no original file removed', 'all source block IDs exactly once',
                      'every normalized factual line preserved with multiplicity', 'personal metadata preserved',
                      'all parsed graph relations and collections preserved',
                      'only declared notes changed', 'only managed navigation added']}
output = root / 'content-integrity.json'
output.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
output.chmod(0o600)
print(json.dumps({k:v for k,v in summary.items() if k not in {'modified_paths','added_paths'}}, ensure_ascii=False))
