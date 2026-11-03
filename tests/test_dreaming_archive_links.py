"""Dreaming's own decision uses the shared mechanical relocation publisher."""
import hashlib
import json
from dataclasses import asdict
from pathlib import Path

import pytest

from daemon_core import dreaming
from daemon_core.memory import note_relocation as moves, reader
from daemon_core.memory.library_layout import FOLDERS, resolve_note, with_identity
from daemon_core.single_pass_sovereign import load_searchable_memory_entries

NAME = "theme/work/atlas"
LOGICAL = f"memory/capsules/{NAME}.md"


@pytest.fixture
def library(tmp_path, monkeypatch):
    root = tmp_path / ".daemon"
    root.mkdir()
    (root / "vault-layout.json").write_text(json.dumps({
        "schema": 1, "layout": "root-folders-v1", "vault_root": str(tmp_path),
        "runtime_root": str(root)}))
    for folder in FOLDERS:
        (tmp_path / folder).mkdir()
    monkeypatch.setattr(dreaming, "MEMORY_ROOT", root)
    monkeypatch.setattr(reader, "MEMORY_ROOT", root)
    monkeypatch.setenv(moves.ENV, "off")  # Dreaming has its own existing authority.
    monkeypatch.setattr(dreaming, "_refresh_semantic_atlas_after_dreaming", lambda *a: None)
    monkeypatch.setattr(dreaming, "refresh_navigation_safely", lambda *a: None)
    monkeypatch.setattr("daemon_core.scene_model.invalidate_scene_models", lambda *a, **k: None)
    monkeypatch.setattr("daemon_core.memory.metadata_maintenance.maybe_run_facet_maintenance", lambda **k: None)
    monkeypatch.setattr(dreaming, "_run_relation_gardening_if_enabled", lambda **k: ())
    monkeypatch.setattr(dreaming, "load_memory_mutation_evidence", lambda **k: ())
    monkeypatch.setattr(dreaming, "_memory_mutation_receipt_paths", lambda: ())
    return root, tmp_path


def note(vault, path, name, body):
    target = vault / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(with_identity(body, name))
    return target


def seed(vault):
    source = note(vault, "Projects/Atlas.md", NAME,
        "# Atlas\nExact historical fact.\n[Guide](../Resources/Guide.md#Source)\n")
    incoming = note(vault, "Areas/Studio.md", "theme/studio",
        '# Studio\n[[Projects/Atlas#History|Atlas]]\n[Work](../Projects/Atlas.md "Title")\n')
    note(vault, "Resources/Guide.md", "theme/guide", "# Guide\nExact reference.\n")
    return source, incoming


def decision(name=NAME):
    return {"updates": [{"path": f"memory/capsules/{name}.md", "action": "archive",
        "reason": "The note has an explicit expired validity date."}]}


def apply(data=None, **kw):
    return dreaming.apply_dreaming_result("synthetic", decision() if data is None else data,
        dry_run=False, **kw)


def test_full_apply_keeps_links_exact_facts_and_identity(library):
    root, vault = library
    source, incoming = seed(vault)
    before_source, before_incoming = source.read_text(), incoming.read_text()
    result = apply()
    assert len(result.updates) == 1 and result.updates[0].applied
    assert not result.rejected_updates
    target = resolve_note(root, NAME)
    assert target == vault / "Archives/Projects/Atlas.md" and not source.exists()
    assert target.read_text() == before_source.replace("../Resources/Guide", "../../Resources/Guide")
    assert incoming.read_text() == before_incoming.replace("Projects/Atlas", "Archives/Projects/Atlas")
    assert reader.load_souvenir(NAME) == target.read_text()
    assert "Archives/Projects/Atlas.md" in result.updates[0].diff
    receipt, = (root / "levels/note_relocations").glob("*.json")
    data = json.loads(receipt.read_text())
    assert data["source_ref"].startswith("dreaming:") and data["evidence_quote"] == ""
    assert data["status"] == "applied" and "writes" not in data
    repeated = apply()
    assert not repeated.updates[0].changed and not repeated.updates[0].applied
    assert len(list((root / "levels/note_relocations").glob("*.json"))) == 1


@pytest.mark.parametrize("change", ["edit", "move", "delete", "forged_hash"])
def test_current_human_state_wins_over_stale_model_decision(library, change):
    root, vault = library
    source, _ = seed(vault)
    locations = {}
    revisions = dreaming._capture_dreaming_revisions("synthetic", locations=locations)
    if change == "move":
        source.rename(vault / "Garden/Atlas.md")
    elif change == "delete":
        source.unlink()
    else:
        source.write_text(source.read_text() + "Human correction.\n")
    data = decision()
    if change == "forged_hash":
        data["updates"][0]["base_sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
    before = {str(p): p.read_bytes() for p in vault.rglob("*.md")}
    result = apply(data, expected_revisions=revisions, expected_locations=locations)
    assert len(result.rejected_updates) == 1 and not result.updates
    assert {str(p): p.read_bytes() for p in vault.rglob("*.md")} == before


def test_run_binds_location_before_the_model_call(library):
    _, vault = library
    source, _ = seed(vault)
    class Provider:
        def complete(self, messages, **kw):
            assert "Exact historical fact." in str(messages)
            source.rename(vault / "Garden/Atlas.md")
            return json.dumps(decision())
    result = dreaming.run_dreaming("synthetic", Provider(), dry_run=False)
    assert len(result.rejected_updates) == 1
    assert (vault / "Garden/Atlas.md").exists()


def test_saved_dry_run_refuses_a_later_human_move(library):
    root, vault = library
    source, _ = seed(vault)
    result = dreaming.apply_dreaming_result("synthetic", decision(), dry_run=True)
    assert result.updates[0].base_location == "Projects/Atlas.md"
    assert not (root / "levels/note_relocations").exists()
    report = root / "report.json"
    report.write_text(json.dumps(asdict(result)))
    source.rename(vault / "Garden/Atlas.md")
    applied = dreaming.apply_dreaming_report_file("synthetic", report)
    assert len(applied.rejected_updates) == 1 and not applied.updates
    assert (vault / "Garden/Atlas.md").exists()


def test_collision_preserves_every_note(library):
    _, vault = library
    seed(vault)
    note(vault, "Archives/Projects/Atlas.md", "theme/other", "# Other\nHuman file.\n")
    before = {str(p): p.read_bytes() for p in vault.rglob("*.md")}
    result = apply()
    assert len(result.rejected_updates) == 1
    assert {str(p): p.read_bytes() for p in vault.rglob("*.md")} == before


def test_unregistered_human_note_is_not_silently_given_a_new_identity(library):
    root, vault = library
    human = vault / "Projects/Human.md"
    human.write_text("# User file\nNo daemon identity.\n")
    name = dreaming.name_for_path(root, human)
    result = apply(decision(name))
    assert len(result.rejected_updates) == 1 and human.exists()
    assert human.read_text() == "# User file\nNo daemon identity.\n"


class Interrupted(BaseException):
    pass


@pytest.mark.parametrize("phase", ["journal", "link-write", "hardlink", "unlink", "receipt"])
def test_dreaming_recovers_shared_publisher_interruption(library, monkeypatch, phase):
    root, vault = library
    source, incoming = seed(vault)
    with monkeypatch.context() as patch:
        if phase in {"journal", "receipt"}:
            original = moves._json
            def save(path, value):
                original(path, value)
                if (path.name == "pending.json") == (phase == "journal"):
                    raise Interrupted()
            patch.setattr(moves, "_json", save)
        elif phase == "link-write":
            original = moves._bytes
            def write(path, content):
                original(path, content)
                raise Interrupted()
            patch.setattr(moves, "_bytes", write)
        elif phase == "hardlink":
            original = moves.os.link
            def link(old, new):
                original(old, new)
                raise Interrupted()
            patch.setattr(moves.os, "link", link)
        else:
            original = Path.unlink
            def unlink(path, *a, **kw):
                original(path, *a, **kw)
                if path == source:
                    raise Interrupted()
            patch.setattr(Path, "unlink", unlink)
        with pytest.raises(Interrupted):
            apply()
    before = {str(p): p.read_bytes() for p in vault.rglob("*.md")}
    dreaming.apply_dreaming_result("synthetic", {"updates": []}, dry_run=True)
    assert {str(p): p.read_bytes() for p in vault.rglob("*.md")} == before
    class Provider:
        def complete(self, messages, **kw):
            # Recovery precedes the model's new source snapshot.
            assert not source.exists()
            assert "[[Archives/Projects/Atlas#History|Atlas]]" in incoming.read_text()
            return '{"updates": []}'
    dreaming.run_dreaming("synthetic", Provider(), dry_run=False)
    assert not source.exists() and not (root / "levels/note_relocations/pending.json").exists()
    assert "[Guide](../../Resources/Guide.md#Source)" in resolve_note(root, NAME).read_text()
    assert len(list(vault.rglob("Atlas.md"))) == 1


def test_rem_episode_is_searchable_and_protected_from_dreaming(library):
    root, vault = library
    name = "theme/scenes_episodiques/synthetic/2026-10-16_return"
    episode = note(vault, "Archives/Episodes/Notebook.md", name,
        "---\ntruth_scope: past_only\n---\n# Notebook\nAlex returned Nora's notebook.\n")
    original = episode.read_bytes()
    assert name in dict(load_searchable_memory_entries(root)[0])
    assert name in reader.load_souvenir_index()
    assert "Nora's notebook" in reader.load_souvenir(name)
    result = apply(decision(name))
    assert len(result.rejected_updates) == 1 and not result.updates
    assert episode.read_bytes() == original
