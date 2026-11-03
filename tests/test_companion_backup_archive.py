"""Memory archives, with synthetic vaults only."""

import hashlib
import json
from pathlib import Path
import zipfile

import pytest

from daemons_companion.backup import archive as backup
from daemons_companion.desktop.memory import initialize_empty_vault
from daemon_core.memory.library_layout import unique_note_locations, vault_root


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source"
    initialize_empty_vault(root)
    data = {
        ".daemon/core/daemon_identity.json": json.dumps(
            {"name": "Nova", "gender": "feminine"}
        ),
        ".daemon/core/persona.md": "# Synthetic persona\n",
        ".daemon/users/12345/context.md": "# Current context\nA bicycle needs repair.\n",
        ".daemon/users/12345/agenda.md": "2026-10-20: repair appointment\n",
        ".daemon/levels/L0_ground_zero/conversations/12345_2026-10-19.md": "USER: My bicycle needs repair.\n",
        ".daemon/levels/rem_state_12345.json": '{"watermark":"2026-10-19T12:00:00"}',
        ".daemon/levels/rem_episodic_state_12345.json": '{"schema":1,"assessed_through":"2026-10-19T12:00:00"}',
        "Projects/Bicycle.md": "---\nmemory_id: theme/project/bicycle\n---\n# Bicycle\nSee [[Areas/Transport]].\n",
        "Areas/Transport.md": "---\nmemory_id: theme/area/transport\n---\n# Transport\n",
        "Attachments/receipt.txt": "Synthetic receipt\n",
        ".obsidian/plugins/untrusted/main.js": "This must not execute or restore.",
        ".daemon/followups/old.json": '{"action":"never-run-again"}',
        ".daemon/users/12345/first_meeting.json": '{"complete":true}',
        ".daemon/capsules/index.md": "# Capsules\n",
        ".daemon/root-migration.json": '{"schema":1}',
        ".daemon/levels/common_ground_shadow/12345/events.jsonl": '{"event":1}\n',
        ".daemon/levels/memory_relation_shadow/12345/events.jsonl": '{"event":1}\n',
        ".daemon/levels/L0_ground_zero/idempotency/12345/a.json": "{}",
        ".daemon/levels/single_pass_trace_12345.json": "{}",
        ".daemon/levels/memory_graph.json": "{}",
        ".daemon/levels/dreaming/reports/2026-10-19.md": "report\n",
        "Garden/Sketch.canvas": "{}",
        "Resources/manual.epub": "binary",
        ".trash/old.md": "deleted note\n",
        "Areas/.DS_Store": "finder",
    }
    for name, text in data.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def test_portable_round_trip_preserves_exact_sources_and_canonical_lookup(
    source, tmp_path
):
    archive, restored = tmp_path / "backup.zip", tmp_path / "restored"
    manifest = backup.create_archive(source, archive)
    backup.restore_archive(archive, restored)
    for relative in manifest["files"]:
        assert (restored / relative).read_bytes() == (source / relative).read_bytes()
    assert vault_root(restored / ".daemon") == restored
    assert {n.name for n in unique_note_locations(restored / ".daemon")} == {
        "theme/project/bicycle",
        "theme/area/transport",
    }
    reasons = {name: entry["reason"] for name, entry in manifest["excluded"].items()}
    assert reasons[".daemon/levels/L0_ground_zero/idempotency/12345/a.json"] == "channel-state"
    assert reasons[".daemon/levels/single_pass_trace_12345.json"] == "diagnostic"
    assert reasons[".daemon/levels/memory_graph.json"] == "rebuild"
    assert reasons[".trash/old.md"] == "editor-settings"
    for kept in (".daemon/users/12345/first_meeting.json", ".daemon/capsules/index.md",
                 ".daemon/levels/common_ground_shadow/12345/events.jsonl",
                 "Garden/Sketch.canvas", "Resources/manual.epub"):
        assert kept in manifest["files"]
    assert not (restored / ".obsidian").exists()
    assert not (restored / ".daemon/followups").exists()
    assert (source / ".daemon/followups/old.json").exists()
    assert (
        manifest["excluded"][".daemon/followups/old.json"]["reason"]
        == "external-actions-not-restored"
    )
    with pytest.raises(backup.BackupError, match="destination-exists"):
        backup.restore_archive(archive, restored)
    with pytest.raises(backup.BackupError, match="new-external-file-required"):
        backup.create_archive(source, archive)


@pytest.mark.parametrize(
    "name",
    [
        "../escape",
        "/escape",
        "Areas/CON.md",
        "Areas/a:b.md",
        "Areas/a\\b.md",
        "Areas/a./note.md",
        "Areas//a.md",
        "Areas/./a.md",
        "Areas/a\x00.md",
    ],
)
def test_nonportable_paths_refused(name):
    with pytest.raises(backup.BackupError):
        backup.checked_relative(name)


def test_unknown_technical_state_and_secrets_block_export(source, tmp_path):
    for name in (".daemon/core/provider.json", ".daemon/users/12345/token.json",
                 ".daemon/levels/new_feature_state.json", ".daemon/credentials.json"):
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('{"api_key":"synthetic-test-secret"}')
        with pytest.raises(backup.BackupError, match="unknown-technical"):
            backup.create_archive(source, tmp_path / "backup.zip")
        assert not (tmp_path / "backup.zip").exists()
        target.unlink()


def test_case_collision_and_symlink_refused(source, tmp_path):
    target = source / "Areas/link.md"
    target.symlink_to(source / "Areas/Transport.md")
    with pytest.raises(backup.BackupError, match="indirect-path"):
        backup.create_archive(source, tmp_path / "backup.zip")
    target.unlink()
    # Unicode composition collides even on a case-sensitive Linux/Windows disk.
    checked = backup.checked_relative("Areas/transport.md")
    assert checked


def test_source_mutation_during_export_never_publishes(source, tmp_path, monkeypatch):
    original = zipfile.ZipFile.write

    def racing_write(self, file, *args, **kwargs):
        result = original(self, file, *args, **kwargs)
        if str(file).endswith("Bicycle.md"):
            Path(file).write_text("concurrent edit")
        return result

    monkeypatch.setattr(zipfile.ZipFile, "write", racing_write)
    with pytest.raises(backup.BackupError, match="source-changed"):
        backup.create_archive(source, tmp_path / "backup.zip")
    assert not (tmp_path / "backup.zip").exists()
    assert not list(tmp_path.glob(".backup-*"))


def rewrite(original, target, mutate):
    with zipfile.ZipFile(original) as archive:
        items = {n: archive.read(n) for n in archive.namelist()}
    mutate(items)
    with zipfile.ZipFile(target, "w") as archive:
        for name, raw in items.items():
            archive.writestr(name, raw)


def test_tampered_member_and_unlisted_payload_fail_before_restore(source, tmp_path):
    archive = tmp_path / "valid.zip"
    backup.create_archive(source, archive)
    for mutation in [
        lambda items: items.update({"memory/Projects/Bicycle.md": b"fake"}),
        lambda items: items.update({"../escape": b"unlisted"}),
    ]:
        corrupted = tmp_path / "corrupt.zip"
        rewrite(archive, corrupted, mutation)
        with pytest.raises(backup.BackupError):
            backup.restore_archive(corrupted, tmp_path / "restored")
        assert not (tmp_path / "restored").exists()


def test_manifest_cannot_import_jobs_or_unknown_version(source, tmp_path):
    archive = tmp_path / "valid.zip"
    backup.create_archive(source, archive)

    def inject(items):
        manifest = json.loads(items["manifest.json"])
        name = ".daemon/followups/replay.json"
        raw = b'{"action":"run"}'
        manifest["files"][name] = {
            "size": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
        items["memory/" + name] = raw
        items["manifest.json"] = json.dumps(manifest).encode()

    rewrite(archive, tmp_path / "jobs.zip", inject)
    with pytest.raises(backup.BackupError):
        backup.restore_archive(tmp_path / "jobs.zip", tmp_path / "restored")

    def unknown(items):
        manifest = json.loads(items["manifest.json"])
        manifest["schema"] = "future-format"
        items["manifest.json"] = json.dumps(manifest).encode()

    rewrite(archive, tmp_path / "future.zip", unknown)
    with pytest.raises(backup.BackupError, match="unsupported-version"):
        backup.inspect_archive(tmp_path / "future.zip")


def test_compressed_and_duplicate_members_are_refused(source, tmp_path):
    good = tmp_path / "good.zip"
    backup.create_archive(source, good)
    with zipfile.ZipFile(good) as archive:
        items = {n: archive.read(n) for n in archive.namelist()}
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in items.items():
            archive.writestr(name, value)
    with pytest.raises(backup.BackupError, match="unsafe-member"):
        backup.inspect_archive(bad)
    with zipfile.ZipFile(bad, "w") as archive:
        for name, value in items.items():
            archive.writestr(name, value)
        with pytest.warns(UserWarning):
            archive.writestr("manifest.json", items["manifest.json"])
    with pytest.raises(backup.BackupError, match="duplicate"):
        backup.inspect_archive(bad)


def test_case_collision_and_file_directory_overlap_in_manifest(source, tmp_path):
    good = tmp_path / "good.zip"
    backup.create_archive(source, good)

    def collide(items):
        manifest = json.loads(items["manifest.json"])
        manifest["files"]["Areas/transport.md"] = manifest["files"][
            "Areas/Transport.md"
        ]
        items["memory/Areas/transport.md"] = items["memory/Areas/Transport.md"]
        items["manifest.json"] = json.dumps(manifest).encode()

    rewrite(good, tmp_path / "collision.zip", collide)
    with pytest.raises(backup.BackupError, match="path-collision"):
        backup.inspect_archive(tmp_path / "collision.zip")


def test_hardlinks_and_indirect_destination_refused(source, tmp_path):
    import os

    os.link(source / "Areas/Transport.md", source / "Areas/Alias.md")
    with pytest.raises(backup.BackupError, match="unsafe-member"):
        backup.create_archive(source, tmp_path / "archive.zip")
    (source / "Areas/Alias.md").unlink()
    (tmp_path / "redirect").symlink_to(source, target_is_directory=True)
    with pytest.raises(backup.BackupError, match="indirect-path"):
        backup.create_archive(source, tmp_path / "redirect/backup.zip")


def test_central_directory_is_bounded_before_zip_allocation(source, tmp_path):
    import struct

    good = tmp_path / "good.zip"
    backup.create_archive(source, good)
    data = bytearray(good.read_bytes())
    end = data.rfind(b"PK\x05\x06")
    struct.pack_into("<L", data, end + 12, backup.MAX_MANIFEST * 3)
    (tmp_path / "huge.zip").write_bytes(data)
    with pytest.raises(backup.BackupError, match="central-directory-bound"):
        backup.inspect_archive(tmp_path / "huge.zip")


def test_encrypted_cross_language_roundtrip_and_canonical_read(source, tmp_path):
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node required for the desktop encryption integration")
    archive, encrypted, decrypted = (
        tmp_path / name
        for name in ("plain.zip", "encrypted.daemonbackup", "decrypted.zip")
    )
    backup.create_archive(source, archive)
    module = (
        Path(__file__).resolve().parents[1] / "apps/companion/desktop/backup-crypto.cjs"
    )
    subprocess.run(
        [
            node,
            "-e",
            """
const [modulePath,source,encrypted,decrypted]=process.argv.slice(1);
const crypto=require(modulePath);
(async()=>{await crypto.encrypt(source,encrypted,'synthetic-cross-language-password');
await crypto.decrypt(encrypted,decrypted,'synthetic-cross-language-password');})().catch(()=>process.exit(1));
""",
            str(module),
            str(archive),
            str(encrypted),
            str(decrypted),
        ],
        check=True,
        timeout=30,
    )
    assert archive.read_bytes() == decrypted.read_bytes()
    restored = tmp_path / "restored"
    backup.restore_archive(decrypted, restored)
    notes = {
        n.name: n.path.read_bytes() for n in unique_note_locations(restored / ".daemon")
    }
    assert (
        notes["theme/project/bicycle"] == (source / "Projects/Bicycle.md").read_bytes()
    )


def test_whole_memory_export_keeps_unknown_files_in_place_and_leaves_credentials(source, tmp_path):
    # An older daemon's memory (the owner's own, 2026-10-29) holds archives and
    # technical state this contract does not know; exported whole, they travel
    # unchanged to the same place, except files named like credential stores.
    unknown = {
        ".daemon/archive/agenda/12345/2026-09-01_old.md": "2026-08-30: past appointment\n",
        ".daemon/archive/capsules/theme/retired.md": "# Retired souvenir\n",
        ".daemon/templates/users/persona.md": "# Old template\n",
        ".daemon/views/areas/work.md": "generated view\n",
        ".daemon/levels/replay_backups/run-1/state.json": '{"turn": 1}',
        ".daemon/users/12345/notes.rtf": "{\\rtf1 synthetic}",
    }
    credentials = {
        ".daemon/levels/replay_backups/run-1/replay.env": "SYNTHETIC_KEY=never-copied\n",
        ".daemon/users/12345/token.json": '{"token":"synthetic-test-secret"}',
    }
    # A hidden file is atomic-write staging: never read, so never carried either.
    hidden = ".daemon/levels/replay_backups/run-1/.env"
    for name, text in {**unknown, **credentials, hidden: "SYNTHETIC_KEY=never-copied\n"}.items():
        (source / name).parent.mkdir(parents=True, exist_ok=True)
        (source / name).write_text(text)
    with pytest.raises(backup.BackupError):
        backup.create_archive(source, tmp_path / "refused.zip")

    manifest = backup.create_archive(source, tmp_path / "whole.zip", preserve_unknown=True)

    assert manifest["schema"] == backup.SCHEMA_PRESERVING
    assert set(manifest["preserved"]) == set(unknown)
    assert {name: manifest["excluded"][name]["reason"] for name in credentials} == {
        name: "credential" for name in credentials}
    assert backup.inspect_archive(tmp_path / "whole.zip")["preserved"] == manifest["preserved"]
    backup.restore_archive(tmp_path / "whole.zip", tmp_path / "restored")
    for name, text in unknown.items():
        assert (tmp_path / "restored" / name).read_text() == text
    assert not any((tmp_path / "restored" / name).exists() for name in [*credentials, hidden])
    assert "synthetic-test-secret" not in (tmp_path / "whole.zip").read_bytes().decode("latin-1")


def test_whole_memory_manifest_cannot_smuggle_known_or_credential_files(source, tmp_path):
    backup.create_archive(source, tmp_path / "whole.zip", preserve_unknown=True)
    with zipfile.ZipFile(tmp_path / "whole.zip") as original:
        manifest = json.loads(original.read("manifest.json"))
        members = {name: original.read(name) for name in original.namelist()}
    for smuggled in (".daemon/users/12345/token.json", ".daemon/core/persona.md"):
        altered = json.loads(json.dumps(manifest))
        body = b"synthetic"
        altered["preserved"][smuggled] = {"size": len(body), "sha256": hashlib.sha256(body).hexdigest()}
        altered["files"].pop(smuggled, None)
        target = tmp_path / ("smuggled-" + smuggled.rsplit("/", 1)[-1] + ".zip")
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_STORED) as archive:
            for name, data in members.items():
                if name not in {"manifest.json", "memory/" + smuggled}:
                    archive.writestr(name, data)
            archive.writestr("memory/" + smuggled, body)
            archive.writestr("manifest.json", json.dumps(altered))
        with pytest.raises(backup.BackupError):
            backup.inspect_archive(target)


def test_whole_memory_export_supplies_a_missing_identity_only_in_the_backup(source, tmp_path):
    # The owner's personal daemon never recorded an identity file (2026-10-30).
    (source / ".daemon/core/daemon_identity.json").unlink()
    with pytest.raises(backup.BackupError, match="missing-identity") as raised:
        backup.create_archive(source, tmp_path / "refused.zip", preserve_unknown=True)
    assert raised.value.area == "identity"
    identity = backup.supplied_identity("Iris", "feminine")

    manifest = backup.create_archive(source, tmp_path / "whole.zip", preserve_unknown=True, identity=identity)

    assert not (source / ".daemon/core/daemon_identity.json").exists()
    assert manifest["files"][".daemon/core/daemon_identity.json"]["size"] == len(identity)
    backup.restore_archive(tmp_path / "whole.zip", tmp_path / "restored")
    restored = json.loads((tmp_path / "restored/.daemon/core/daemon_identity.json").read_text())
    assert restored == {"name": "Iris", "gender": "feminine"}
    (source / ".daemon/core/daemon_identity.json").write_text('{"name": "Nova", "gender": "feminine"}')
    with pytest.raises(backup.BackupError, match="identity-exists"):
        backup.create_archive(source, tmp_path / "again.zip", preserve_unknown=True, identity=identity)
    with pytest.raises(backup.BackupError, match="invalid-identity"):
        backup.supplied_identity("Iris", "neutral")
