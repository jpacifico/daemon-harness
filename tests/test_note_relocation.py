import json
from pathlib import Path

import pytest

from daemon_core.memory import note_relocation as moves
from daemon_core.memory.library_layout import (
    FOLDERS, LibraryLayoutError, resolve_note, resolve_reference, with_identity,
)

NAME = "theme/work/harbor"
WORDS = "The Harbor workshop is delivered and finished. Keep its history."
SOURCE = "levels/L0_ground_zero/conversations/synthetic#2026-10-17T10:00:00 - user"


@pytest.fixture
def library(tmp_path, monkeypatch):
    root = tmp_path / ".daemon"
    root.mkdir()
    (root / "vault-layout.json").write_text(json.dumps({
        "schema": 1, "layout": "root-folders-v1", "vault_root": str(tmp_path),
        "runtime_root": str(root)}))
    (root / ".memory-structure-prototype.json").write_text(json.dumps({
        "schema": 1, "memory_root": str(root)}))
    for name in FOLDERS:
        (tmp_path / name).mkdir()
    monkeypatch.setenv("DAEMON_CORE_MEMORY_STRUCTURE", "v1")
    monkeypatch.setenv(moves.ENV, "v1")
    return root, tmp_path


def note(vault, relative="Projects/Harbor.md", name=NAME, body="# Harbor\n\nExact historical fact.\n"):
    p = vault / relative
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(with_identity(body, name))
    return p


def proposal(root, path, destination="Archives/Harbor.md", name=NAME, words=WORDS):
    context = moves.capture_context(root, {name: path.read_text()}, SOURCE)
    raw = {"from": path.relative_to(root.parent).as_posix(), "to": destination,
           "evidence_quote": words, "reason": "The user explicitly changed this note's purpose."}
    return moves.validate_proposal(root, name, raw, context, words), context, raw


@pytest.mark.parametrize("old,new", [
    ("Projects/Harbor.md", "Archives/Work/Harbor.md"),
    ("Garden/Harbor.md", "Projects/Work/Harbor.md"),
    ("Archives/Harbor.md", "Projects/Work/Harbor.md"),
])
def test_move_keeps_exact_bytes_identity_and_stable_reference(library, old, new):
    root, vault = library
    p = note(vault, old)
    raw = p.read_bytes()
    request, _, _ = proposal(root, p, new)
    assert moves.publish_relocation(root, request, raw) == "applied"
    assert not p.exists()
    assert (vault / new).read_bytes() == raw
    assert resolve_note(root, NAME) == vault / new
    assert resolve_reference(root, "capsules/" + NAME + ".md") == vault / new
    assert moves.publish_relocation(root, request, raw) == "applied"
    assert len(list(vault.glob("**/Harbor.md"))) == 1
    receipt, = (root / "levels/note_relocations").glob("*.json")
    assert "writes" not in json.loads(receipt.read_text())


def test_links_change_only_the_address_and_keep_sources_and_code(library):
    root, vault = library
    body = ("# Harbor\nExact historical fact.\n"
            "[Guide](../Resources/Guide.md#Process)\n"
            "[[../Resources/Guide#Process|the guide]]\n"
            "[Original](file:///exact/L0/source.md#evidence)\n")
    p = note(vault, body=body)
    note(vault, "Resources/Guide.md", "theme/docs/guide")
    other = note(vault, "Areas/Owner.md", "theme/people/owner", (
        "# Owner\n[[Projects/Harbor#History|the workshop]]\n"
        "![[Projects/Harbor.md#^block]]\n[[Harbor]]\n"
        "[Work](../Projects/Harbor.md#History \"Exact title\")\n"
        "[other][work]\n[work]: <../Projects/Harbor.md#History> \"Title\"\n"
        "`[[Projects/Harbor]]`\n``[[Projects/Harbor]] `nested` ``\n```md\n[[Projects/Harbor]]\n```\n"
        "    [[Projects/Harbor]]\nPlain Projects/Harbor is not a link.\n"))
    request, _, _ = proposal(root, p, "Archives/Work/Harbor.md")
    moves.publish_relocation(root, request, p.read_bytes())
    text = other.read_text()
    assert "[[Archives/Work/Harbor#History|the workshop]]" in text
    assert "![[Archives/Work/Harbor.md#^block]]" in text
    assert "[Work](../Archives/Work/Harbor.md#History \"Exact title\")" in text
    assert '[work]: <../Archives/Work/Harbor.md#History> "Title"' in text
    assert "`[[Projects/Harbor]]`" in text and "```md\n[[Projects/Harbor]]\n```" in text
    assert "``[[Projects/Harbor]] `nested` ``" in text
    assert "    [[Projects/Harbor]]\nPlain Projects/Harbor is not a link." in text
    moved = resolve_note(root, NAME).read_text()
    assert "[Guide](../../Resources/Guide.md#Process)" in moved
    assert "[[Resources/Guide#Process|the guide]]" in moved
    assert "[Original](file:///exact/L0/source.md#evidence)" in moved


@pytest.mark.parametrize("destination", [
    "../Harbor.md", "People/Harbor.md", "Archives/link/Harbor.md",
    "Archives/NUL/Harbor.md", "Archives/Changed name.md", "Projects/Harbor.md",
    "Archives/a/b/c/d/Harbor.md",
])
def test_invalid_destination_refused(library, destination):
    root, vault = library
    p = note(vault)
    (vault / "Archives/link").symlink_to(vault / "Areas", target_is_directory=True)
    with pytest.raises((ValueError, OSError)):
        proposal(root, p, destination)
    assert p.exists()


def test_collision_never_overwrites_and_bad_evidence_never_moves(library):
    root, vault = library
    p = note(vault)
    request, context, raw = proposal(root, p)
    other = note(vault, "Archives/Harbor.md", "theme/work/other")
    before = other.read_bytes()
    with pytest.raises(LibraryLayoutError, match="exists"):
        moves.publish_relocation(root, request, p.read_bytes())
    assert other.read_bytes() == before and p.exists()
    other.unlink()
    with pytest.raises(LibraryLayoutError, match="evidence"):
        moves.validate_proposal(root, NAME, {**raw, "evidence_quote": "Invented closure"}, context, WORDS)
    with pytest.raises(LibraryLayoutError, match="source"):
        moves.validate_proposal(root, NAME, raw, None, WORDS)


@pytest.mark.parametrize("mutation", ["edit", "move", "delete"])
def test_human_change_after_read_wins(library, mutation):
    root, vault = library
    p = note(vault)
    request, context, raw = proposal(root, p)
    if mutation == "edit":
        p.write_text(p.read_text() + "My manual addition.\n")
    elif mutation == "move":
        p.rename(vault / "Areas/My name.md")
    else:
        p.unlink()
    before = {str(x): x.read_bytes() for x in vault.rglob("*.md")}
    with pytest.raises((ValueError, FileNotFoundError)):
        moves.validate_proposal(root, NAME, raw, context, WORDS)
    with pytest.raises((ValueError, FileNotFoundError)):
        moves.publish_relocation(root, request, request.snapshot.content)
    assert {str(x): x.read_bytes() for x in vault.rglob("*.md")} == before


class Interrupted(BaseException):
    pass


@pytest.mark.parametrize("phase", ["journal", "link-write", "hardlink", "unlink", "receipt"])
def test_interruption_recovers_without_double_note_or_broken_links(library, monkeypatch, phase):
    root, vault = library
    p = note(vault)
    q = note(vault, "Areas/Owner.md", "theme/people/owner", "# Owner\n[[Projects/Harbor|work]]\n")
    request, _, _ = proposal(root, p)
    raw = p.read_bytes()
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
            def link(source, target):
                original(source, target)
                raise Interrupted()
            patch.setattr(moves.os, "link", link)
        else:
            original = Path.unlink
            def unlink(path, *args, **kwargs):
                original(path, *args, **kwargs)
                if path == p:
                    raise Interrupted()
            patch.setattr(Path, "unlink", unlink)
        with pytest.raises(Interrupted):
            moves.publish_relocation(root, request, raw)
    assert moves.recover_relocation(root) == "applied"
    assert not p.exists() and (vault / "Archives/Harbor.md").read_bytes() == raw
    assert "[[Archives/Harbor|work]]" in q.read_text()
    assert len(list(vault.rglob("Harbor.md"))) == 1
    assert moves.recover_relocation(root) == "none"


def test_concurrent_destination_creation_does_not_clobber(library, monkeypatch):
    root, vault = library
    p = note(vault)
    q = note(vault, "Areas/Owner.md", "theme/people/owner", "# Owner\n[[Projects/Harbor]]\n")
    before = q.read_bytes()
    request, _, _ = proposal(root, p)
    original = moves.os.link
    def link(source, target):
        target.write_text("A human file created just now.\n")
        return original(source, target)
    monkeypatch.setattr(moves.os, "link", link)
    with pytest.raises(FileExistsError):
        moves.publish_relocation(root, request, p.read_bytes())
    assert p.exists() and q.read_bytes() == before
    assert (vault / "Archives/Harbor.md").read_text() == "A human file created just now.\n"
    assert moves.recover_relocation(root) == "cancelled"


def test_human_edit_after_interruption_is_not_rolled_back(library, monkeypatch):
    root, vault = library
    p = note(vault)
    q = note(vault, "Areas/Owner.md", "theme/people/owner", "# Owner\n[[Projects/Harbor]]\n")
    request, _, _ = proposal(root, p)
    with monkeypatch.context() as patch:
        original = moves._bytes
        def write(path, content):
            original(path, content)
            raise Interrupted()
        patch.setattr(moves, "_bytes", write)
        with pytest.raises(Interrupted):
            moves.publish_relocation(root, request, p.read_bytes())
    q.write_text(q.read_text() + "A human addition after the crash.\n")
    manual = q.read_bytes()
    assert moves.recover_relocation(root) == "cancelled"
    assert q.read_bytes() == manual and p.exists()
    assert not (vault / "Archives/Harbor.md").exists()


def test_io_error_after_rename_recovers_receipt_without_rolling_links_back(library, monkeypatch):
    root, vault = library
    p = note(vault)
    q = note(vault, "Areas/Owner.md", "theme/people/owner", "# Owner\n[[Projects/Harbor]]\n")
    request, _, _ = proposal(root, p)
    with monkeypatch.context() as patch:
        patch.setattr(moves, "_receipt", lambda *a, **kw: (_ for _ in ()).throw(OSError("disk temporarily unavailable")))
        with pytest.raises(OSError):
            moves.publish_relocation(root, request, p.read_bytes())
    assert "[[Archives/Harbor]]" in q.read_text()
    moved = resolve_note(root, NAME)
    moved.write_text(moved.read_text() + "Manual edit after commit.\n")
    manual = moved.read_bytes()
    assert moves.recover_relocation(root) == "applied" and moved.read_bytes() == manual


def test_after_turn_moves_only_after_protected_merge_and_records_same_id(library, monkeypatch):
    from daemon_core import memory_agent as agent
    root, vault = library
    monkeypatch.setattr(agent, "MEMORY_ROOT", root)
    monkeypatch.setattr(agent, "load_user_agenda", lambda _: "")
    monkeypatch.setattr(agent, "prepare_souvenir_format", lambda content, name: content)
    p = note(vault)
    request, context, raw = proposal(root, p)
    monkeypatch.setattr(agent, "_merge_existing_souvenir", lambda **kw: agent.SouvenirMergeResult(
        content=kw["current"] + "\nThe workshop is delivered and finished."))
    result = agent._apply_memory_agent_result("synthetic", {"capsule_updates": [{
        "name": NAME, "content_md": "# Finished", "relocation": raw}]},
        user_message=WORDS, provider=object(), relocation_context=context)
    assert result.relocated_souvenirs == [NAME] and not result.skipped_souvenirs
    moved = resolve_note(root, NAME)
    assert moved == vault / "Archives/Harbor.md"
    assert "Exact historical fact." in moved.read_text() and "delivered" in moved.read_text()
    assert "capsules/" + NAME + ".md" in result.changed_paths


def test_reported_claim_and_disabled_flag_do_not_move(library, monkeypatch):
    from daemon_core import memory_agent as agent
    root, vault = library
    monkeypatch.setattr(agent, "MEMORY_ROOT", root)
    p = note(vault)
    _, context, raw = proposal(root, p)
    data = {"source_posture": "reported_memory_claim", "capsule_updates": [{
        "name": NAME, "content_md": "# Finished", "relocation": raw}]}
    result = agent._apply_memory_agent_result("synthetic", data, user_message=WORDS, relocation_context=context)
    assert not result.relocated_souvenirs and p.exists()
    monkeypatch.delenv(moves.ENV)
    with pytest.raises(LibraryLayoutError, match="not enabled"):
        moves.validate_proposal(root, NAME, raw, context, WORDS)


def test_user_note_with_invalid_yaml_is_not_ignored_for_link_repair(library):
    root, vault = library
    p = note(vault)
    q = vault / "Areas/Human.md"
    q.write_text('---\naliases: [unfinished\n---\n# My note\n[[Projects/Harbor|work]]\n')
    request, _, _ = proposal(root, p)
    moves.publish_relocation(root, request, p.read_bytes())
    assert q.read_text() == '---\naliases: [unfinished\n---\n# My note\n[[Archives/Harbor|work]]\n'


def test_recovery_refuses_a_journal_that_would_write_new_facts(library, monkeypatch):
    import base64
    root, vault = library
    p = note(vault)
    request, _, _ = proposal(root, p)
    with monkeypatch.context() as patch:
        original = moves._json
        def save(path, value):
            original(path, value)
            raise Interrupted()
        patch.setattr(moves, "_json", save)
        with pytest.raises(Interrupted):
            moves.publish_relocation(root, request, p.read_bytes())
    pending = root / 'levels/note_relocations/pending.json'
    journal = json.loads(pending.read_text())
    journal['writes'][0]['after'] = base64.b64encode(b'# Invented fact\n').decode()
    pending.write_text(json.dumps(journal))
    before = p.read_bytes()
    with pytest.raises(LibraryLayoutError, match='non-mechanical'):
        moves.recover_relocation(root)
    assert p.read_bytes() == before and not (vault / 'Archives/Harbor.md').exists()
