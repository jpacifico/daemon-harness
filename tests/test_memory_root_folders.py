import json

import pytest

from daemon_core.memory.library_layout import (
    LibraryLayoutError, FOLDERS, checked_note_path, link_target,
    name_from_vault_link, note_locations, resolve_note, vault_root, with_identity,
)


@pytest.fixture
def library(tmp_path):
    root = tmp_path / '.daemon'
    root.mkdir()
    (root / 'vault-layout.json').write_text(json.dumps({
        'schema': 1, 'layout': 'root-folders-v1', 'vault_root': str(tmp_path),
        'runtime_root': str(root)}))
    for name in FOLDERS:
        (tmp_path / name).mkdir()
    return root, tmp_path


def make_note(vault, relative, identity='theme/work/atlas'):
    p = vault / relative
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(with_identity('# Project\n\n- An exact source fact.\n', identity))
    return p


def test_actual_files_and_identity_survive_user_move_and_unicode_rename(library):
    root, vault = library
    old = make_note(vault, 'Projects/Atlas.md')
    original = old.read_bytes()
    moved = vault / 'Archives/Atlas terminé.md'
    old.rename(moved)
    assert vault_root(root) == vault
    assert resolve_note(root, 'theme/work/atlas') == moved
    assert moved.read_bytes() == original and not old.exists()
    assert len(note_locations(root)) == 1
    assert link_target(root, 'theme/work/atlas') == 'Archives/Atlas terminé'
    assert name_from_vault_link(root, 'Archives/Atlas terminé', source='theme/work/atlas') == 'theme/work/atlas'


def test_new_human_file_is_readable_without_inventing_metadata(library):
    root, vault = library
    p = vault / 'Resources/My article.md'
    p.write_text('# My article\nExact user text.\n')
    before = p.read_bytes()
    entry, = note_locations(root)
    assert not entry.registered
    assert resolve_note(root, entry.name) == p
    assert p.read_bytes() == before
    p.rename(vault / 'Resources/Renamed.md')
    with pytest.raises(LibraryLayoutError):
        resolve_note(root, entry.name, create=True, storage_path='Resources/My article.md')
    assert not p.exists()


def test_duplicate_ids_never_select_a_winner_or_overwrite_a_file(library):
    root, vault = library
    a = make_note(vault, 'Projects/Atlas.md')
    b = make_note(vault, 'Archives/Atlas copy.md')
    with pytest.raises(LibraryLayoutError, match='Duplicate'):
        resolve_note(root, 'theme/work/atlas')
    assert a.exists() and b.exists()


def test_deleted_known_note_is_not_recreated_by_stale_proposal(library):
    root, vault = library
    p = make_note(vault, 'Projects/Atlas.md')
    (root / 'capsules').mkdir()
    (root / 'capsules/index.md').write_text('- theme/work/atlas: exact existing note\n')
    p.unlink()
    with pytest.raises(LibraryLayoutError, match='removed'):
        resolve_note(root, 'theme/work/atlas', create=True, storage_path='Projects/Atlas.md')
    assert not p.exists()


def test_new_placement_is_explicit_and_never_overwrites_another_identity(library):
    root, vault = library
    with pytest.raises(LibraryLayoutError, match='explicit'):
        resolve_note(root, 'theme/work/new', create=True)
    assert resolve_note(root, 'theme/work/new', create=True, storage_path='Projects/New.md') == vault/'Projects/New.md'
    make_note(vault, 'Projects/New.md', 'theme/work/existing')
    with pytest.raises(LibraryLayoutError, match='another note'):
        resolve_note(root, 'theme/work/new', create=True, storage_path='Projects/New.md')


@pytest.mark.parametrize('relative', ['../escape.md', '/tmp/escape.md', 'Projects/../../escape.md',
    'People/Person.md', 'Areas/no.txt', 'Projects/a|b.md', 'Projects//file.md'])
def test_path_boundaries(library, relative):
    _, vault = library
    with pytest.raises(LibraryLayoutError):
        checked_note_path(vault, relative)


def test_symlink_does_not_escape_or_appear_as_a_second_note(library, tmp_path):
    root, vault = library
    outside = tmp_path / 'outside.md'
    outside.write_text('# Not a note')
    (vault / 'Projects/link.md').symlink_to(outside)
    with pytest.raises(LibraryLayoutError):
        note_locations(root)


def test_identity_changes_are_rejected_and_aliases_preserved():
    text = '---\naliases: [Atlas, Lumen]\n---\n# Project\n\nAn exact source.\n'
    result = with_identity(text, 'theme/work/atlas')
    assert 'aliases:' in result and 'Lumen' in result and result.endswith('An exact source.\n')
    with pytest.raises(LibraryLayoutError, match='immutable'):
        with_identity(result, 'theme/work/different', current=result)


def test_legacy_root_has_no_implicit_migration(tmp_path):
    assert vault_root(tmp_path) is None
    assert resolve_note(tmp_path, 'theme/work/atlas') == tmp_path/'capsules/theme/work/atlas.md'


def bind_root(monkeypatch, root):
    from daemon_core import paths, memory_agent, dreaming
    from daemon_core.memory import reader
    for module in (paths, memory_agent, dreaming, reader):
        monkeypatch.setattr(module, 'MEMORY_ROOT', root)
    monkeypatch.setattr(memory_agent, 'load_user_agenda', lambda _: '')
    monkeypatch.setenv('DAEMON_CORE_OBSIDIAN_FORMAT', '1')
    return memory_agent, dreaming, reader


def test_after_turn_updates_renamed_note_and_publishes_new_explicit_placement(library, monkeypatch):
    root, vault = library
    agent, _, reader = bind_root(monkeypatch, root)
    old = make_note(vault, 'Projects/Atlas.md')
    moved = vault / 'Areas/Atlas renamed.md'
    old.rename(moved)
    monkeypatch.setattr(agent, '_merge_existing_souvenir', lambda **kw:
        agent.SouvenirMergeResult(content=kw['current'] + '\n- A newly entrusted fact.'))
    result = agent._apply_memory_agent_result('isolated', {'capsule_updates': [
        {'name': 'theme/work/atlas', 'content_md': '# Project\n- A newly entrusted fact.'},
        {'name': 'theme/work/new', 'storage_path': 'Projects/New.md', 'content_md': '# New project\n- A source fact.'}
    ]}, provider=object())
    assert len(result.updated_souvenirs) == 2
    assert not old.exists() and 'entrusted' in moved.read_text()
    assert reader.load_souvenir('theme/work/atlas') == moved.read_text()
    assert resolve_note(root, 'theme/work/new') == vault / 'Projects/New.md'
    assert 'capsules/theme/work/atlas.md' in result.changed_paths
    assert not list((root / 'capsules/theme').rglob('*.md'))


@pytest.mark.parametrize('action', ['rename', 'delete', 'edit'])
def test_user_mutation_during_model_merge_is_never_overwritten(library, monkeypatch, action):
    root, vault = library
    agent, _, _ = bind_root(monkeypatch, root)
    old = make_note(vault, 'Projects/Atlas.md')
    moved = vault / 'Archives/Atlas.md'
    original = old.read_text()
    def merge(**kw):
        if action == 'rename':
            old.rename(moved)
        elif action == 'delete':
            old.unlink()
        else:
            old.write_text(original + '\nUser edit wins.\n')
        return agent.SouvenirMergeResult(content=kw['current'] + '\nStale model proposal.')
    monkeypatch.setattr(agent, '_merge_existing_souvenir', merge)
    result = agent._apply_memory_agent_result('isolated', {'capsule_updates': [
        {'name': 'theme/work/atlas', 'content_md': '# Changed'}]}, provider=object())
    assert result.skipped_souvenirs == ['theme/work/atlas']
    if action != 'edit':
        assert not old.exists()
    else:
        assert old.read_text().endswith('User edit wins.\n')
    if action == 'rename':
        assert moved.read_text() == original


def test_dreaming_uses_identity_and_preserves_body_when_archiving(library, monkeypatch):
    root, vault = library
    _, dreaming, reader = bind_root(monkeypatch, root)
    old = make_note(vault, 'Projects/Atlas.md')
    raw = old.read_bytes()
    update = dreaming.DreamingUpdate('memory/capsules/theme/work/atlas.md', 'archive', 'explicit closure', '')
    target, rejected = dreaming._validate_update_target('isolated', update)
    assert target == old and rejected is None
    assert dreaming._validate_archive_target(target, update) is None
    dreaming._archive_souvenir(target)
    new = resolve_note(root, 'theme/work/atlas')
    assert new.relative_to(vault).parts[0] == 'Archives'
    assert new.read_bytes() == raw and not old.exists()
    assert reader.load_souvenir('theme/work/atlas') == raw.decode()


def test_migration_is_mechanical_and_preserves_every_exact_source(tmp_path, monkeypatch):
    from daemon_core.memory.root_migration import migrate_copy, verify_migration, hashes
    from daemon_core.memory.graph import build_memory_graph
    from daemon_core.memory.context_pages import refresh_navigation_safely, rebuild_navigation
    source = tmp_path / 'before'
    p = source / 'capsules/theme/work/atlas.md'
    p.parent.mkdir(parents=True)
    p.write_text('---\naliases: [Atlas]\n---\n# Atlas\nExact fact.\n\n## Links\n- [[capsules/theme/person/sam]]: participant\n')
    q = source / 'capsules/theme/person/sam.md'
    q.parent.mkdir(parents=True)
    q.write_text('# Sam\nExact second fact.\n')
    l0 = source / 'levels/L0_ground_zero/conversations/test.md'
    l0.parent.mkdir(parents=True)
    l0.write_text('# Raw source\nUnchanged original dialogue.\n')
    before = hashes(source)
    destination = tmp_path / 'after'
    manifest = migrate_copy(source, destination, {'theme/work/atlas': 'Projects/Atlas.md', 'theme/person/sam': 'Areas/Sam.md'})
    assert hashes(source) == before
    assert verify_migration(source, destination, manifest)['verified']
    assert set(p.name for p in destination.iterdir()) == set(FOLDERS) | {'.daemon'}
    root = destination / '.daemon'
    bind_root(monkeypatch, root)
    assert (root / 'levels/L0_ground_zero/conversations/test.md').read_bytes() == l0.read_bytes()
    graph = build_memory_graph(root / 'capsules', memory_root=root)
    assert graph.index.souvenirs['theme/work/atlas'].relations[0].target == 'theme/person/sam'
    moved = destination / 'Archives/New title.md'
    (destination / 'Projects/Atlas.md').rename(moved)
    monkeypatch.setenv('DAEMON_CORE_MEMORY_STRUCTURE', 'v1')
    refresh_navigation_safely(root)
    assert not rebuild_navigation(root)['enabled']
    assert not (destination / 'Home.md').exists()
    assert resolve_note(root, 'theme/work/atlas') == moved
    assert 'Exact fact.' in moved.read_text()


def test_hidden_source_links_keep_exact_provenance_without_broken_wikilinks(library, monkeypatch):
    from daemon_core.memory.library_layout import render_vault_links, internal_source_reference
    from daemon_core.memory.graph import normalize_source_ref, source_ref_exists
    root, vault = library
    bind_root(monkeypatch, root)
    source = root / 'levels/L0_ground_zero/conversations/synthetic.md'
    source.parent.mkdir(parents=True)
    anchor = '2026-10-11T10:00:00 — user'
    source.write_text('## ' + anchor + '\nExact words.\n')
    reference = 'levels/L0_ground_zero/conversations/synthetic#' + anchor
    text = '[[%s|Original words]]' % reference
    rendered = render_vault_links(root, text)
    assert '[Original words](file://' in rendered
    assert internal_source_reference(root, rendered) == reference
    assert normalize_source_ref(rendered) == reference
    assert source_ref_exists(root, rendered)
    assert render_vault_links(root, '```md\n' + text + '\n```') == '```md\n' + text + '\n```'


def test_human_short_links_are_exact_and_ambiguous_links_are_refused(library):
    root, vault = library
    make_note(vault, 'Projects/Atlas.md')
    assert name_from_vault_link(root, 'Atlas', source='theme/work/atlas') == 'theme/work/atlas'
    make_note(vault, 'Resources/Atlas.md', 'theme/books/atlas')
    assert name_from_vault_link(root, 'Atlas', source='theme/work/atlas') is None


def test_offline_identity_insertion_preserves_historical_nested_rem_properties():
    from daemon_core.memory.root_migration import identify_original
    from daemon_core.memory.metadata import parse_souvenir_frontmatter
    original = '---\nmemory_type: episodic_scene\nclaims:\n- text: Exact original fact\n  source_refs: [original-source]\n---\n# Episode\nExact original body.\n'
    result = identify_original(original, 'theme/scenes_episodiques/test')
    old_metadata, old_body = parse_souvenir_frontmatter(original)
    metadata, body = parse_souvenir_frontmatter(result)
    assert metadata.pop('memory_id') == 'theme/scenes_episodiques/test'
    assert metadata == old_metadata and body == old_body
    assert result.replace('memory_id: "theme/scenes_episodiques/test"\n', '') == original


def test_new_project_needs_no_fabricated_or_empty_history(library, monkeypatch):
    root, vault = library
    agent, _, _ = bind_root(monkeypatch, root)
    monkeypatch.setenv('DAEMON_CORE_MEMORY_STRUCTURE', 'v1')
    (root / '.memory-structure-prototype.json').write_text(json.dumps(
        {'schema': 1, 'memory_root': str(root)}))
    proposed = "---\nkind: project\nmemory_views: [Projects]\n---\n# Receiver\n\n## Scope\n- Build a receiver.\n\n## Last confirmed state\n- The user bought its circuit board.\n"
    result = agent._apply_memory_agent_result('isolated', {'capsule_updates': [
        {'name': 'theme/electronics/receiver', 'storage_path': 'Projects/Receiver.md',
         'content_md': proposed}]}, provider=object())
    assert result.updated_souvenirs == ['theme/electronics/receiver']
    assert 'History' not in (vault / 'Projects/Receiver.md').read_text()
    assert not (vault / 'Home.md').exists()


def test_workspace_follows_real_notes_without_exposing_internal_files():
    from daemon_core.memory.root_migration import remap_workspace
    before = {'main': {'state': {'file': 'capsules/theme/work/atlas.md', 'mode': 'source'}},
              'lastOpenFiles': ['home.md', 'views/projects.md',
                  'capsules/theme/work/atlas.md', 'levels/L0_ground_zero/raw.md'],
              'tabs': [{'type': 'leaf', 'state': {'type': 'markdown',
                       'state': {'file': 'home.md', 'mode': 'source'},
                       'icon': 'lucide-file', 'title': 'Home'}}],
              'left': {'width': 283}, 'active': 'user-tab'}
    after = json.loads(remap_workspace(json.dumps(before),
        {'capsules/theme/work/atlas.md': 'Projects/Atlas.md'}))
    assert after['main']['state'] == {'file': 'Projects/Atlas.md', 'mode': 'source'}
    assert after['lastOpenFiles'] == ['Projects/Atlas.md']
    assert after['tabs'][0]['state'] == {'type': 'empty', 'state': {}}
    assert after['left'] == before['left'] and after['active'] == before['active']


def test_one_graph_build_scans_the_vault_once_and_resolves_the_same_links(library, monkeypatch):
    from daemon_core.memory import library_layout
    from daemon_core.memory.graph import build_memory_graph_from_entries
    root, vault = library
    bind_root(monkeypatch, root)
    make_note(vault, 'Projects/Atlas.md')
    make_note(vault, 'Resources/Book.md', 'theme/books/book')
    entries = [('theme/source%d' % i, '# Source\n\n## Liens proches\n'
                '- [[Projects/Atlas]] : atlas\n- [[Resources/Book]] : book\n') for i in range(20)]
    scans = []
    real = library_layout._scan_note_locations
    monkeypatch.setattr(library_layout, '_scan_note_locations', lambda r: scans.append(r) or real(r))
    build = build_memory_graph_from_entries(entries, memory_root=root)
    targets = {(r.target) for record in build.index.souvenirs.values() for r in record.relations}
    assert targets == {'theme/work/atlas', 'theme/books/book'}
    assert len(scans) == 1
    # Outside a build every lookup still scans, so a new human file is seen at once.
    make_note(vault, 'Garden/New.md', 'theme/garden/new')
    assert name_from_vault_link(root, 'Garden/New', source='theme/source0') == 'theme/garden/new'
    assert name_from_vault_link(root, 'Garden/New', source='theme/source0') == 'theme/garden/new'
    assert len(scans) == 3
