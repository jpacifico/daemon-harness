import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dotenv import dotenv_values

from daemon_core.onboarding import build_dreaming_cron_line, ensure_env_file, format_env_value, import_memory_bundle, latest_memory_bundle, mask_secret, parse_daily_time, parse_env_assignment, secret_preview, update_crontab_content, validate_memory_bundle_members, write_env_updates


def test_write_env_updates_preserves_comments_and_updates_values(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "# Provider\n"
        "LLM_PROVIDER=echo\n"
        "OPENAI_API_KEY=\n"
        "\n"
        "# Telegram\n"
        "TELEGRAM_BOT_TOKEN=old-token\n",
        encoding="utf-8",
    )

    write_env_updates(
        env_path,
        {
            "LLM_PROVIDER": "openai",
            "OPENAI_API_KEY": "sk-test",
            "TELEGRAM_BOT_TOKEN": "123:ABC&DEF",
            "DAEMON_CORE_USER_ID": "1234567890",
            "DAEMON_CORE_VOICE_CAPTURE_FILE": (
                "exports/private/provider-capture.json"
            ),
            "DREAMING_DAILY_TIME": "04:30",
        },
    )

    content = env_path.read_text(encoding="utf-8")
    values = dotenv_values(env_path)
    assert "# Provider" in content
    assert "# Telegram" in content
    assert values["LLM_PROVIDER"] == "openai"
    assert values["OPENAI_API_KEY"] == "sk-test"
    assert values["TELEGRAM_BOT_TOKEN"] == "123:ABC&DEF"
    assert values["DAEMON_CORE_USER_ID"] == "1234567890"
    assert values["DAEMON_CORE_VOICE_CAPTURE_FILE"] == (
        "exports/private/provider-capture.json"
    )
    assert values["DREAMING_DAILY_TIME"] == "04:30"


def test_format_env_value_quotes_values_with_spaces_or_shell_chars():
    assert format_env_value("simple-token_123") == "simple-token_123"
    assert format_env_value("123:ABC&DEF") == '"123:ABC&DEF"'
    assert format_env_value("value with space") == '"value with space"'
    assert format_env_value("") == ""


def test_parse_env_assignment_splits_key_value():
    assert parse_env_assignment("OPENAI_API_KEY=sk-test=value") == ("OPENAI_API_KEY", "sk-test=value")


def test_parse_env_assignment_rejects_invalid_input():
    for value in ("OPENAI_API_KEY", "1_BAD=value"):
        try:
            parse_env_assignment(value)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{value} should be invalid")


def test_mask_secret_keeps_only_edges():
    assert mask_secret("") == "empty"
    assert mask_secret("short") == "********"
    assert mask_secret("sk-1234567890") == "sk-1...7890"


def test_secret_preview_shows_edges_and_length():
    preview = secret_preview("sk-proj-abcdefghijklmnopqrstuvwxyz")

    assert preview == "sk-proj...wxyz (34 caractères)"


def test_ensure_env_file_copies_example(tmp_path):
    env_path = tmp_path / ".env"
    example = tmp_path / ".env.example"
    example.write_text("LLM_PROVIDER=echo\n", encoding="utf-8")

    ensure_env_file(env_path)

    assert env_path.read_text(encoding="utf-8") == "LLM_PROVIDER=echo\n"


def test_parse_daily_time_accepts_hour_or_hhmm():
    assert parse_daily_time("4") == (4, 0)
    assert parse_daily_time("04:30") == (4, 30)
    assert parse_daily_time("23:59") == (23, 59)


def test_parse_daily_time_rejects_invalid_values():
    for value in ("24:00", "12:60", "abc", "10:5"):
        try:
            parse_daily_time(value)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{value} should be invalid")


def test_build_dreaming_cron_line_translates_time_and_quotes_project_root(tmp_path):
    root = tmp_path / "Project With Spaces"
    line = build_dreaming_cron_line(
        user_id="1234567890",
        since_hours="24",
        daily_time="04:30",
        project_root=root,
    )

    assert line.startswith("30 4 * * * ")
    assert "cd " in line
    assert "Project With Spaces" in line
    assert "DAEMON_CORE_USER_ID=1234567890" in line
    assert "DREAMING_SINCE_HOURS=24" in line
    assert "./scripts/run_dreaming_daily.sh" in line


def test_update_crontab_content_replaces_existing_dreaming_line():
    existing = (
        "MAILTO=test@example.com\n"
        "0 4 * * * cd /old && ./scripts/run_dreaming_daily.sh\n"
        "# Daemon Harness dreaming daily\n"
        "15 5 * * * cd /also-old && ./scripts/run_dreaming_daily.sh\n"
    )
    cron_line = "30 6 * * * cd /new && ./scripts/run_dreaming_daily.sh"

    updated = update_crontab_content(existing, cron_line)

    assert "MAILTO=test@example.com" in updated
    assert "/old" not in updated
    assert "/also-old" not in updated
    assert updated.count("scripts/run_dreaming_daily.sh") == 1
    assert "# Daemon Harness dreaming daily" in updated
    assert cron_line in updated


def test_import_memory_bundle_extracts_memory_and_creates_backup(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    memory = project / "memory"
    (memory / "users" / "old").mkdir(parents=True)
    (memory / "users" / "old" / "context.md").write_text("old", encoding="utf-8")
    source = tmp_path / "source"
    (source / "memory" / "users" / "new").mkdir(parents=True)
    (source / "memory" / "users" / "new" / "context.md").write_text("new", encoding="utf-8")
    bundle = tmp_path / "memory.tar.gz"
    with tarfile.open(bundle, "w:gz") as archive:
        archive.add(source / "memory", arcname="memory")

    backup = import_memory_bundle(bundle, project_root=project)

    assert (project / "memory" / "users" / "new" / "context.md").read_text(encoding="utf-8") == "new"
    assert backup.exists()
    with tarfile.open(backup, "r:gz") as archive:
        assert "memory/users/old/context.md" in archive.getnames()


def test_latest_memory_bundle_returns_newest_tar_gz(tmp_path):
    old = tmp_path / "daemon-core-memory-alice-20260515-100000.tar.gz"
    new = tmp_path / "daemon-core-memory-alice-20260515-110000.tar.gz"
    ignored = tmp_path / "notes.txt"
    old.write_text("old", encoding="utf-8")
    new.write_text("new", encoding="utf-8")
    ignored.write_text("ignored", encoding="utf-8")

    assert latest_memory_bundle(tmp_path) == new


def test_validate_memory_bundle_rejects_unsafe_paths():
    unsafe = tarfile.TarInfo("../outside")
    try:
        validate_memory_bundle_members([unsafe])
    except ValueError as exc:
        assert "Chemin dangereux" in str(exc)
    else:
        raise AssertionError("Unsafe path should be rejected")


def test_validate_memory_bundle_rejects_paths_outside_memory():
    outside = tarfile.TarInfo("README.md")
    try:
        validate_memory_bundle_members([outside])
    except ValueError as exc:
        assert "sous memory/" in str(exc)
    else:
        raise AssertionError("Path outside memory should be rejected")
