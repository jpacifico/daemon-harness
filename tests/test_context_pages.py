import json

import pytest

from daemon_core.memory import context_pages as pages
from daemon_core.memory import context_page_formatting as formatting
from daemon_core.memory.metadata import parse_souvenir_frontmatter, preserve_existing_frontmatter
from daemon_core.memory.graph import parse_souvenir_graph


@pytest.fixture
def vault(tmp_path, monkeypatch):
    root = tmp_path.resolve() / "memory"
    root.mkdir()
    (root / pages.MARKER).write_text(json.dumps({"schema": 1, "memory_root": str(root)}))
    monkeypatch.setenv("DAEMON_CORE_MEMORY_STRUCTURE", "v1")
    monkeypatch.setenv("DAEMON_CORE_OBSIDIAN_FORMAT", "1")
    from daemon_core import paths
    monkeypatch.setattr(paths, "MEMORY_ROOT", root)
    return root


def note(root, name, content):
    path = root / "capsules" / (name + ".md")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def test_flag_alone_never_activates_unmarked_root(vault, monkeypatch):
    assert pages.structure_enabled(vault)
    (vault / pages.MARKER).unlink()
    assert not pages.structure_enabled(vault)
    assert pages.context_page_prompt("unchanged") == "unchanged"


def test_marker_bound_to_exact_root(vault, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    (other / pages.MARKER).write_bytes((vault / pages.MARKER).read_bytes())
    assert not pages.structure_enabled(other)
    link = tmp_path / "link"
    link.symlink_to(vault)
    assert not pages.structure_enabled(link)


def test_legacy_prompt_replaced_only_in_prototype(vault, monkeypatch):
    system = "prefix\n- New thematic souvenir names must follow legacy\nlegacy format\n- Perishability: keep\n"
    changed = pages.context_page_prompt(system)
    assert "legacy format" not in changed
    assert "- Perishability: keep" in changed
    assert "Last confirmed state" in changed
    monkeypatch.delenv("DAEMON_CORE_MEMORY_STRUCTURE")
    assert pages.context_page_prompt(system) == system


def test_model_membership_drives_views_without_facts_or_path_inference(vault):
    note(vault, "theme/project/closed", "---\nkind: project\nmemory_views: [Archives]\n---\n# Closed\n## Scope\n## Last confirmed state\n- Done, 2026-01-01.\n## History\n")
    note(vault, "theme/health/unclassified", "# Note\n\n- Private fact.\n")
    result = pages.rebuild_navigation(vault)
    assert result["counts"]["Projects"] == 0
    assert result["counts"]["Archives"] == 1
    assert result["counts"]["All notes"] == 2
    assert "Private fact" not in (vault / "views/all_notes.md").read_text()
    assert not pages.rebuild_navigation(vault)["changed"]


def test_navigation_preserves_user_page_and_rejects_escaping_symlink(vault, tmp_path):
    (vault / "home.md").write_text("# My personal home\n")
    assert "home.md" in pages.rebuild_navigation(vault)["protected"]
    assert (vault / "home.md").read_text() == "# My personal home\n"
    outside = tmp_path / "outside.md"
    outside.write_text("private")
    (vault / "capsules").mkdir(exist_ok=True)
    (vault / "capsules/escape.md").symlink_to(outside)
    with pytest.raises(ValueError):
        pages.rebuild_navigation(vault)


def test_managed_views_can_change_while_personal_aliases_survive(vault):
    current = "---\nkind: project\nmemory_views: [Projects]\naliases: [Mine]\ntags: [custom]\n---\nold"
    proposed = "---\nkind: project\nmemory_views: [Archives]\naliases: [Wrong]\n---\nnew"
    fields, body = parse_souvenir_frontmatter(preserve_existing_frontmatter(current, proposed))
    assert fields["memory_views"] == ["Archives"]
    assert fields["aliases"] == ["Mine"]
    assert fields["tags"] == ["custom"]
    assert body == "new"


def test_english_links_are_real_graph_edges():
    graph = parse_souvenir_graph("theme/project/demo", "## Links\n- [[capsules/theme/people/alex]]: project reviewer\n")
    assert not graph.issues
    assert graph.relations[0].target == "theme/people/alex"


@pytest.mark.parametrize("kind,views", [("invented", []), ("person", "Areas"), ("person", ["Inbox"]), ("person", ["Areas", "Areas"])])
def test_bad_structure_properties_refused(kind, views):
    import yaml
    with pytest.raises(ValueError):
        pages.validate_context_page("---\n" + yaml.safe_dump({"kind": kind, "memory_views": views}) + "---\n# Note")


def test_offline_formatter_cannot_invent_or_drop_source_blocks(vault):
    name = "theme/work/harbor"
    path = note(vault, name, "# Harbor\n\n- 2026-01-01: initial decision.\n\n- The limit is still unknown.\n")
    messages, snapshots = formatting.prepare_format_batch(vault, [name])
    assert "initial decision" in messages[1]["content"]
    decision = {"name": name, "kind": "project", "memory_views": ["Projects"],
                "sections": {"Scope": [0], "History": [1], "Last confirmed state": [2]}}
    original = path.read_bytes()
    result = formatting.publish_format_batch(vault, snapshots, json.dumps({"notes": [decision]}))
    assert result["changed"] == [name]
    assert pages.exact_body_lines(path.read_text()) == pages.exact_body_lines(original.decode())
    for invalid_ids in ([1, 2], [0, 1, 1, 2], [0, 1, 2, 3]):
        decision["sections"] = {"Scope": invalid_ids, "History": [], "Last confirmed state": []}
        with pytest.raises(ValueError):
            formatting.render_format_decision(name, original, decision)


def test_offline_formatter_refuses_external_edit_and_protected_rem(vault):
    name = "theme/work/harbor"
    path = note(vault, name, "# Harbor\n\n- Original.")
    _, snapshots = formatting.prepare_format_batch(vault, [name])
    path.write_text("# User changed this")
    decision = {"name": name, "kind": "resource", "memory_views": ["Resources"], "sections": {"Scope": [0, 1]}}
    assert formatting.publish_format_batch(vault, snapshots, json.dumps({"notes": [decision]}))["conflicted"] == [name]
    assert path.read_text() == "# User changed this"
    for name in ("theme/scenes_episodiques/2026/test", "theme/chroniques/test", "theme/../../escape"):
        with pytest.raises(ValueError):
            formatting.prepare_format_batch(vault, [name])


def test_context_history_is_protected_by_real_merge_guard():
    from daemon_core.memory_agent import _merge_existing_souvenir
    class Lossy:
        def complete(self, **kwargs):
            return json.dumps({"content_md": "# Example\n## History\n- New fact.", "removed_existing_lines": []})
    with pytest.raises(ValueError, match="perte d'information"):
        _merge_existing_souvenir(provider=Lossy(), name="theme/work/example",
            current="# Example\n## History\n- Old fact.", proposed="# Example\n## History\n- New fact.", user_message="New fact")


def test_frozen_batch_recovery_keeps_incompatible_source_and_publishes_valid_note(vault, monkeypatch):
    import hashlib
    import importlib.util
    from pathlib import Path
    script = Path(__file__).resolve().parents[1] / 'scripts/prepare_memory_structure.py'
    spec = importlib.util.spec_from_file_location('prepare_structure', script)
    prep = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(prep)
    from benchmarks.productivity_v1.runner import common
    monkeypatch.setattr(common, 'source_revision', lambda repo: {'commit': 'test', 'dirty': False})
    names = ['theme/work/valid', 'theme/work/legacy']
    good = note(vault, names[0], '# Valid\n\n- An exact source fact.\n')
    bad = note(vault, names[1], '---\nsubjects:\n- {Game: suffix}\n---\n# Legacy\n\n- Another exact fact.\n')
    _, snapshots = formatting.prepare_format_batch(vault, names)
    decisions = [{'name': name, 'kind': 'resource', 'memory_views': ['Resources'],
                  'sections': {'Scope': [0], 'History': [1]}} for name in names]
    response = json.dumps({'notes': decisions})
    with pytest.raises(ValueError, match='nested'):
        formatting.publish_format_batch(vault, snapshots, response)
    assert good.read_bytes() == snapshots[names[0]]
    root = vault.parent
    for name, raw in snapshots.items():
        note(root / 'original-memory', name, raw.decode())
    (root / 'format-batches').mkdir()
    (root / 'format-batches/001.json').write_text(json.dumps({'complete': False,
        'response': response, 'source_hashes': {n: hashlib.sha256(b).hexdigest() for n,b in snapshots.items()}}))
    prep.recover_valid_notes(root)
    assert bad.read_bytes() == snapshots[names[1]]
    assert 'kind: resource' in good.read_text()
    assert pages.exact_body_lines(good.read_text()) == pages.exact_body_lines(snapshots[names[0]].decode())
    record = json.loads((root / 'format-recovery/001.json').read_text())
    assert record['model_calls'] == 0
    assert record['publication']['changed'] == [names[0]]
    assert record['refused'][0]['name'] == names[1]
    before = good.read_bytes()
    prep.recover_valid_notes(root)
    assert good.read_bytes() == before

    # The global auditor must accept only the recorded partial recovery.
    import subprocess
    import sys
    (root / 'manifest.json').write_text(json.dumps({'schema': 1, 'root': str(root),
        'source': str(root / 'original-memory'), 'snapshot_complete': True,
        'source_hashes': {'capsules/' + n + '.md': hashlib.sha256(b).hexdigest() for n,b in snapshots.items()}}))
    audit = subprocess.run([sys.executable, '-B', str(script.with_name('verify_memory_structure_copy.py')), str(root)],
                           capture_output=True, text=True, timeout=20)
    assert audit.returncode == 0, audit.stderr
    audit_result = json.loads(audit.stdout)
    assert audit_result['complete'] and audit_result['formatted_notes'] == 1
    assert len(audit_result['retained_legacy_notes']) == 1
