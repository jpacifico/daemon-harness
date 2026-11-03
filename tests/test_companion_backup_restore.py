"""Restoring a backup as a new daemon's memory, with synthetic data only."""

import io
import json
from types import SimpleNamespace

import pytest

from daemon_core import first_meeting as meeting
from daemons_companion.backup import archive, restore
from daemons_companion.desktop.memory import initialize_empty_vault
from daemons_companion.desktop.protocol import DesktopUI
from daemons_companion.hermes.first_install import initialize_private_memory
from daemons_companion.setup import telegram

OWNER = "1234567"


def companion_vault(root, owner=OWNER, language="fr", phase="complete"):
    """The shape a Companion daemon writes: per-user voice, identity in core."""
    initialize_empty_vault(root)
    files = {
        ".daemon/core/daemon_identity.json": json.dumps({"name": "Nova", "gender": "masculine"}),
        f".daemon/users/{owner}/persona.md": "Synthetic persona\n",
        f".daemon/users/{owner}/conversation.md": "Synthetic manner\n",
        f".daemon/users/{owner}/contexte_actif.md": "Repairing a bicycle.\n",
        f".daemon/users/{owner}/first_meeting.json": json.dumps(
            {"schema": meeting.SCHEMA, "user_id": owner, "language": language, "phase": phase}),
        f".daemon/levels/L0_ground_zero/conversations/{owner}_2026-10-27.md": "## user\n\nHello\n",
        "Projects/Bicycle.md": "# Bicycle\n",
    }
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


@pytest.fixture
def backup(tmp_path):
    source = companion_vault(tmp_path / "source")
    archive.create_archive(source, tmp_path / "backup.zip")
    return tmp_path / "backup.zip"


@pytest.fixture
def bundle(tmp_path):
    template = tmp_path / "bundle/memory/templates/users/conversation.md"
    template.parent.mkdir(parents=True)
    template.write_text("New template manner")
    template.with_name("persona.md").write_text("New template persona")
    return tmp_path / "bundle"


def test_setup_reads_identity_owner_and_language_from_the_backup(backup):
    source = restore.read_source(backup)
    assert (source.name, source.gender, source.owner, source.language) == ("Nova", "masculine", OWNER, "fr")
    assert len(source.manifest_sha256) == 64


def test_a_memory_of_several_accounts_is_not_restored_automatically(tmp_path):
    source = companion_vault(tmp_path / "source")
    companion_vault_owner = source / ".daemon/users/7654321/persona.md"
    companion_vault_owner.parent.mkdir(parents=True)
    companion_vault_owner.write_text("Other\n")
    archive.create_archive(source, tmp_path / "backup.zip")
    with pytest.raises(archive.BackupError, match="owner-ambiguous"):
        restore.read_source(tmp_path / "backup.zip")


def test_new_installation_keeps_the_restored_voice_and_does_not_replay_the_meeting(
        backup, bundle, tmp_path, monkeypatch):
    monkeypatch.setenv(restore.ENVIRONMENT, str(backup))
    scope = SimpleNamespace(home=tmp_path / "home", attempt=tmp_path / "attempt", validate_home=lambda _: None)
    scope.attempt.mkdir()
    initialize_private_memory(scope, bundle, OWNER, root_folder_memory=True, conversation_language="en")
    root = scope.home / "data/memory"
    internal = root / ".daemon"
    assert (internal / f"users/{OWNER}/persona.md").read_text() == "Synthetic persona\n"
    assert (internal / f"users/{OWNER}/contexte_actif.md").read_text() == "Repairing a bicycle.\n"
    assert meeting.read_state(internal, OWNER)["phase"] == "complete"
    assert (root / "Projects/Bicycle.md").read_text() == "# Bicycle\n"
    # A resumed setup keeps the memory it restored instead of refusing or replacing it.
    (internal / f"users/{OWNER}/contexte_actif.md").write_text("A new turn.\n")
    initialize_private_memory(scope, bundle, OWNER, root_folder_memory=True, conversation_language="en")
    assert (internal / f"users/{OWNER}/contexte_actif.md").read_text() == "A new turn.\n"


def test_restore_refuses_another_account_an_existing_memory_and_the_old_layout(
        backup, bundle, tmp_path, monkeypatch):
    monkeypatch.setenv(restore.ENVIRONMENT, str(backup))
    scope = SimpleNamespace(home=tmp_path / "home", attempt=tmp_path / "attempt", validate_home=lambda _: None)
    scope.attempt.mkdir()
    with pytest.raises(archive.BackupError, match="other-telegram-account"):
        initialize_private_memory(scope, bundle, "7654321", root_folder_memory=True)
    with pytest.raises(ValueError, match="root-folder"):
        initialize_private_memory(scope, bundle, OWNER, root_folder_memory=False)
    existing = scope.home / "data/memory"
    existing.mkdir(parents=True)
    (existing / "keep.md").write_text("An existing memory.")
    with pytest.raises(archive.BackupError, match="destination-exists"):
        initialize_private_memory(scope, bundle, OWNER, root_folder_memory=True)
    assert (existing / "keep.md").read_text() == "An existing memory."


def test_telegram_step_accepts_only_the_account_the_memory_belongs_to(backup, tmp_path, monkeypatch):
    monkeypatch.setenv(restore.ENVIRONMENT, str(backup))
    answers = iter([1, "7654321", OWNER])
    said = []
    ui = SimpleNamespace(
        say=said.append,
        choose=lambda *args, **kwargs: next(answers),
        field=lambda *args, **kwargs: next(answers),
        open_link=lambda url: True,
    )
    class Stop(Exception):
        pass

    def create_bot(ui):
        raise Stop

    monkeypatch.setattr(telegram, "create_bot", create_bot)
    with pytest.raises(Stop):
        telegram.prepare_telegram(ui, tmp_path)
    assert any(f"belongs to Telegram account {OWNER}" in line for line in said)
    assert sum("This backup belongs" in line for line in said) == 1


def test_desktop_conversation_is_unchanged_without_a_backup(monkeypatch):
    monkeypatch.delenv(restore.ENVIRONMENT, raising=False)
    assert restore.pending_restore() is None
    assert DesktopUI(io.StringIO(), io.StringIO()) is not None


def test_restored_setup_keeps_no_plain_copy_of_the_backup(backup, bundle, tmp_path, monkeypatch):
    copy = tmp_path / "setup/restore/backup.zip"
    copy.parent.mkdir(parents=True)
    copy.write_bytes(backup.read_bytes())
    monkeypatch.setenv(restore.ENVIRONMENT, str(copy))
    scope = SimpleNamespace(home=tmp_path / "home", attempt=tmp_path / "attempt", validate_home=lambda _: None)
    scope.attempt.mkdir()
    initialize_private_memory(scope, bundle, OWNER, root_folder_memory=True)
    assert not copy.exists()
    assert (scope.home / "data/memory/Projects/Bicycle.md").exists()


def test_desktop_commands_return_counts_and_codes_only(backup, tmp_path):
    from daemons_companion.backup import command

    identity = command.handle_request({"action": "restore-inspect", "archive": str(backup)})
    assert identity == {"name": "Nova", "gender": "masculine", "language": "fr"}
    source = companion_vault(tmp_path / "running")
    result = command.handle_request({"action": "backup", "vault": str(source), "spool": None,
                                     "destination": str(tmp_path / "new.zip")})
    assert set(result) == {"files", "bytes", "attempts", "failed_turns"} and result["files"] >= 6
    with pytest.raises(archive.BackupError, match="invalid-request"):
        command.handle_request({"action": "backup", "vault": "relative", "spool": None, "destination": "x"})
    with pytest.raises(archive.BackupError, match="invalid-request"):
        command.handle_request({"action": "delete-everything"})


def _reminders(source, now_plus_days=3):
    from daemon_core import followup

    intents = source / ".daemon/followups/intents"
    intents.mkdir(parents=True)
    future = "2099-01-01T10:00"
    for name, state in (("aaaa1111", "pending"), ("bbbb2222", "delivered")):
        (intents / f"{name}.json").write_text(json.dumps({
            "id": name, "user_id": OWNER, "fire_at": future, "topic": "Bicycle", "evidence": "said so",
            "angle": "check-in", "origin": {"platform": "telegram", "chat_id": OWNER}, "job_id": "old-host-job",
            "state": state, "created_at": "2026-10-27T10:00", "fired_at": None, "delivered_at": None,
            "outcome_reason": None, "reschedule_count": 0}))
    (source / f".daemon/followups/users/{OWNER}").mkdir(parents=True)
    (source / f".daemon/followups/users/{OWNER}/disabled").write_text("")
    return followup


def test_reminders_travel_only_with_a_move(tmp_path):
    source = companion_vault(tmp_path / "source")
    _reminders(source)
    archive.create_archive(source, tmp_path / "backup.zip")
    manifest = archive.inspect_archive(tmp_path / "backup.zip")
    assert set(manifest["moving"]) == {".daemon/followups/intents/aaaa1111.json", ".daemon/followups/intents/bbbb2222.json"}
    # The user's "no reminders" preference is part of the memory either way.
    assert f".daemon/followups/users/{OWNER}/disabled" in manifest["files"]
    archive.restore_archive(tmp_path / "backup.zip", tmp_path / "copy")
    assert not (tmp_path / "copy/.daemon/followups/intents").exists()
    archive.restore_archive(tmp_path / "backup.zip", tmp_path / "moved", move=True)
    assert (tmp_path / "moved/.daemon/followups/intents/aaaa1111.json").exists()
    assert json.loads((tmp_path / "moved/.daemon/followups/moved.json").read_text()) == ["aaaa1111"]


def test_moved_reminders_take_the_new_hosts_channel_at_its_first_turn(tmp_path, monkeypatch):
    source = companion_vault(tmp_path / "source")
    followup = _reminders(source)
    archive.create_archive(source, tmp_path / "backup.zip")
    archive.restore_archive(tmp_path / "backup.zip", tmp_path / "moved", move=True)
    monkeypatch.setattr("daemon_core.paths.MEMORY_ROOT", tmp_path / "moved/.daemon")
    origin = {"platform": "openclaw", "channel": "telegram", "to": OWNER}
    assert followup.adopt_moved(OWNER, {}, lambda fu: "never") == []
    assert followup.adopt_moved(OWNER, origin, lambda fu: None) == []
    assert json.loads((tmp_path / "moved/.daemon/followups/moved.json").read_text()) == ["aaaa1111"]
    scheduled = []

    def schedule(fu):
        scheduled.append((fu.id, fu.origin, fu.job_id))
        fu.job_id = "new-host-job"
        followup.save_intent(fu)
        return fu.job_id

    assert followup.adopt_moved(OWNER, origin, schedule) == ["aaaa1111"]
    assert scheduled == [("aaaa1111", origin, None)]
    assert followup.load_intent("aaaa1111").job_id == "new-host-job"
    assert not (tmp_path / "moved/.daemon/followups/moved.json").exists()


def test_a_move_is_chosen_by_the_app_not_by_the_backup(backup, monkeypatch):
    monkeypatch.setenv(restore.ENVIRONMENT, str(backup))
    monkeypatch.delenv(restore.MOVE_ENVIRONMENT, raising=False)
    assert restore.pending_restore().move is False
    monkeypatch.setenv(restore.MOVE_ENVIRONMENT, "1")
    assert restore.pending_restore().move is True


def test_backup_command_reports_only_the_error_class_and_numbers(monkeypatch, capsys):
    from daemons_companion.backup import command

    def failing(request):
        raise PermissionError(13, "denied", "/private/secret/path")

    monkeypatch.setattr(command, "handle_request", failing)
    monkeypatch.setattr("sys.stdin", SimpleNamespace(buffer=io.BytesIO(b"{}")))
    command.main()
    printed = capsys.readouterr().out
    result = json.loads(printed)
    assert result == {"ok": False, "code": "backup.unavailable",
                      "diagnostic": {"type": "PermissionError", "errno": 13, "winerror": None}}
    assert "secret" not in printed
