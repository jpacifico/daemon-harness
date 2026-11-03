from __future__ import annotations

import json
import os
from pathlib import Path
import stat

import pytest

from daemons_companion.setup.plan import HostKind, HostProbe, PlanStep, SetupPlan
from daemons_companion.desktop.identity import (
    CompanionIdentityAdapter,
    CompanionIdentityLayout,
    IDENTITY_STEP,
)
from daemons_companion.runtime.release import create_disposable_companion_marker
from daemons_companion.setup.transaction import (
    TransactionApplyError,
    TransactionEngine,
    TransactionRecoveryError,
    public_plan_digest,
)


IDENTITY_PLAN_STEP = PlanStep(
    IDENTITY_STEP,
    "Store daemon identity separately from voice prompts.",
    ("daemon_identity.json", "No voice prompt change"),
)


def _plan(step: PlanStep = IDENTITY_PLAN_STEP) -> SetupPlan:
    return SetupPlan(
        selected_host=HostProbe(
            HostKind.HERMES,
            detected=True,
            usable=True,
            executable="/synthetic/hermes",
        ),
        daemon_name="Noé",
        daemon_gender="masculine",
        provider={"kind": "synthetic", "settings": {}, "required_secret_names": []},
        steps=(step,),
    )


def _layout(tmp_path: Path) -> CompanionIdentityLayout:
    home = (tmp_path / "disposable-companion").absolute()
    create_disposable_companion_marker(home)
    layout = CompanionIdentityLayout(home)
    layout.memory_root.mkdir(parents=True, mode=0o700)
    return layout


def _execute(
    tmp_path: Path,
    adapter: CompanionIdentityAdapter,
    *,
    transaction_id: str = "identity-test",
    plan: SetupPlan | None = None,
):
    chosen = plan or _plan()
    return TransactionEngine(tmp_path / "transactions").execute(
        chosen,
        adapter,
        transaction_id=transaction_id,
        confirmed_plan_digest=public_plan_digest(chosen),
    )


def _tree_state(root: Path) -> dict[str, tuple[object, ...]]:
    state: dict[str, tuple[object, ...]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            state[relative] = ("symlink", os.readlink(path))
        elif path.is_dir():
            state[relative] = ("directory", stat.S_IMODE(path.stat().st_mode))
        else:
            state[relative] = (
                "file",
                path.read_bytes(),
                stat.S_IMODE(path.stat().st_mode),
            )
    return state


def _write_voice_files(layout: CompanionIdentityLayout) -> dict[Path, bytes]:
    files = {
        layout.memory_root / "core/persona.md": b"Canonical persona\n",
        layout.memory_root / "users/test/conversation.md": b"Personal manner\n",
        layout.memory_root / "users/test/nested/persona.md": b"Nested portrait\n",
    }
    for path, content in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return files


def test_creates_unicode_identity_without_touching_voice_files(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    voice = _write_voice_files(layout)
    before = {path: path.read_bytes() for path in voice}

    result = _execute(
        tmp_path,
        CompanionIdentityAdapter(layout, name="Noé", gender="masculine"),
    )

    assert result.status == "committed"
    assert json.loads(layout.identity_file.read_text()) == {
        "name": "Noé",
        "gender": "masculine",
    }
    assert stat.S_IMODE(layout.identity_file.stat().st_mode) == 0o600
    assert {path: path.read_bytes() for path in voice} == before


def test_existing_identity_update_changes_only_two_metadata_values(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    _write_voice_files(layout)
    layout.identity_file.parent.mkdir(parents=True, exist_ok=True)
    layout.identity_file.write_text(
        '{\n  "name": "Iris",\n  "gender": "feminine"\n}\n',
        encoding="utf-8",
    )
    layout.identity_file.chmod(0o640)
    before = _tree_state(layout.memory_root)

    _execute(
        tmp_path,
        CompanionIdentityAdapter(layout, name="Noé", gender="masculine"),
    )

    after = _tree_state(layout.memory_root)
    changed = {path for path in set(before) | set(after) if before.get(path) != after.get(path)}
    assert changed == {"core/daemon_identity.json"}


def test_same_identity_is_byte_idempotent(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    _execute(
        tmp_path,
        CompanionIdentityAdapter(layout, name="Noé", gender="masculine"),
        transaction_id="first",
    )
    before = _tree_state(layout.memory_root)

    _execute(
        tmp_path,
        CompanionIdentityAdapter(layout, name="Noé", gender="masculine"),
        transaction_id="second",
    )

    assert _tree_state(layout.memory_root) == before


def test_same_identity_in_another_layout_is_rewritten_in_the_canonical_form(tmp_path: Path) -> None:
    # A backup of an older daemon carries a compact identity file; installing
    # it under the same name must not fail on the bytes alone.
    layout = _layout(tmp_path)
    layout.identity_file.parent.mkdir(parents=True, exist_ok=True)
    layout.identity_file.write_text('{"name": "Noé", "gender": "masculine"}\n', encoding="utf-8")
    layout.identity_file.chmod(0o644)

    result = _execute(
        tmp_path,
        CompanionIdentityAdapter(layout, name="Noé", gender="masculine"),
    )

    assert result.status == "committed"
    assert layout.identity_file.read_text(encoding="utf-8") == (
        '{\n  "name": "Noé",\n  "gender": "masculine"\n}\n'
    )
    assert stat.S_IMODE(layout.identity_file.stat().st_mode) == 0o600


def test_failure_restores_absence_and_does_not_leak_error_text(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    before = _tree_state(layout.memory_root)

    def fail(_artifact: str) -> None:
        raise RuntimeError("synthetic failure containing provider-secret")

    with pytest.raises(TransactionApplyError):
        _execute(
            tmp_path,
            CompanionIdentityAdapter(
                layout,
                name="Noé",
                gender="masculine",
                mutation_hook=fail,
            ),
        )

    assert _tree_state(layout.memory_root) == before
    journal = (tmp_path / "transactions/identity-test/journal.json").read_text()
    assert "provider-secret" not in journal
    assert "Noé" not in journal


def test_interruption_is_recovered_by_fresh_adapter(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    layout.identity_file.parent.mkdir(parents=True)
    layout.identity_file.write_text(
        '{"name":"Iris","gender":"feminine"}\n',
        encoding="utf-8",
    )
    layout.identity_file.chmod(0o640)
    before = _tree_state(layout.memory_root)

    def interrupt(_artifact: str) -> None:
        raise KeyboardInterrupt("synthetic interruption")

    with pytest.raises(KeyboardInterrupt):
        _execute(
            tmp_path,
            CompanionIdentityAdapter(
                layout,
                name="Noé",
                gender="masculine",
                mutation_hook=interrupt,
            ),
        )

    plan = _plan()
    result = TransactionEngine(tmp_path / "transactions").recover(
        plan,
        CompanionIdentityAdapter(layout, name="Noé", gender="masculine"),
        transaction_id="identity-test",
        confirmed_plan_digest=public_plan_digest(plan),
    )
    assert result.status == "rolled_back"
    assert _tree_state(layout.memory_root) == before


def test_concurrent_identity_change_is_preserved(tmp_path: Path) -> None:
    layout = _layout(tmp_path)

    def conflict(_artifact: str) -> None:
        layout.identity_file.write_text(
            '{"name":"Third Party","gender":"feminine"}\n',
            encoding="utf-8",
        )
        raise RuntimeError("force rollback")

    with pytest.raises(TransactionRecoveryError):
        _execute(
            tmp_path,
            CompanionIdentityAdapter(
                layout,
                name="Noé",
                gender="masculine",
                mutation_hook=conflict,
            ),
        )

    assert json.loads(layout.identity_file.read_text())["name"] == "Third Party"


def test_concurrent_voice_change_is_preserved(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    voice = _write_voice_files(layout)
    target = next(iter(voice))

    def conflict(_artifact: str) -> None:
        target.write_text("Third-party voice edit\n", encoding="utf-8")
        raise RuntimeError("force rollback")

    with pytest.raises(TransactionRecoveryError):
        _execute(
            tmp_path,
            CompanionIdentityAdapter(
                layout,
                name="Noé",
                gender="masculine",
                mutation_hook=conflict,
            ),
        )

    assert target.read_text() == "Third-party voice edit\n"


@pytest.mark.parametrize(
    "name,gender",
    [("", "feminine"), ("Robert", "neutral"), ("A/B", "masculine")],
)
def test_invalid_identity_is_rejected_before_transaction(
    tmp_path: Path,
    name: str,
    gender: str,
) -> None:
    layout = _layout(tmp_path)
    with pytest.raises(ValueError):
        CompanionIdentityAdapter(layout, name=name, gender=gender)
    assert not layout.identity_file.exists()


def test_malformed_existing_identity_fails_closed_and_is_preserved(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    layout.identity_file.parent.mkdir(parents=True)
    layout.identity_file.write_text('{"unexpected":true}\n', encoding="utf-8")
    before = layout.identity_file.read_bytes()

    with pytest.raises(TransactionApplyError):
        _execute(
            tmp_path,
            CompanionIdentityAdapter(layout, name="Noé", gender="masculine"),
        )

    assert layout.identity_file.read_bytes() == before


@pytest.mark.parametrize("target", ["memory", "core", "identity"])
def test_symlinked_managed_paths_are_refused(tmp_path: Path, target: str) -> None:
    home = (tmp_path / "disposable").absolute()
    create_disposable_companion_marker(home)
    layout = CompanionIdentityLayout(home)
    external = tmp_path / "external"
    external.mkdir()
    if target == "memory":
        layout.memory_root.parent.mkdir(parents=True)
        layout.memory_root.symlink_to(external, target_is_directory=True)
    else:
        layout.memory_root.mkdir(parents=True)
        if target == "core":
            layout.identity_file.parent.symlink_to(external, target_is_directory=True)
        else:
            layout.identity_file.parent.mkdir()
            external_file = external / "identity.json"
            external_file.write_text("keep", encoding="utf-8")
            layout.identity_file.symlink_to(external_file)

    with pytest.raises(TransactionApplyError):
        _execute(
            tmp_path,
            CompanionIdentityAdapter(layout, name="Noé", gender="masculine"),
            transaction_id=f"symlink-{target}",
        )

    if target == "identity":
        assert (external / "identity.json").read_text() == "keep"


def test_voice_symlink_is_refused_before_identity_write(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    external = tmp_path / "persona.md"
    external.write_text("keep", encoding="utf-8")
    voice = layout.memory_root / "users/test/persona.md"
    voice.parent.mkdir(parents=True)
    voice.symlink_to(external)

    with pytest.raises(TransactionApplyError):
        _execute(
            tmp_path,
            CompanionIdentityAdapter(layout, name="Noé", gender="masculine"),
        )

    assert not layout.identity_file.exists()
    assert external.read_text() == "keep"


@pytest.mark.parametrize("marked", [False, True])
def test_unmarked_home_or_missing_shared_memory_is_refused(
    tmp_path: Path,
    marked: bool,
) -> None:
    home = (tmp_path / "companion").absolute()
    home.mkdir()
    if marked:
        create_disposable_companion_marker(home)
    else:
        (home / "data/memory").mkdir(parents=True)
    layout = CompanionIdentityLayout(home)

    with pytest.raises(TransactionApplyError):
        _execute(
            tmp_path,
            CompanionIdentityAdapter(layout, name="Noé", gender="masculine"),
            transaction_id=f"prerequisite-{marked}",
        )

    assert not layout.identity_file.exists()


def test_complete_public_plan_remains_non_executable(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    public_plan = SetupPlan(
        selected_host=_plan().selected_host,
        daemon_name="Noé",
        daemon_gender="masculine",
        provider={"kind": "synthetic", "settings": {}, "required_secret_names": []},
        steps=(
            PlanStep("review", "Review", ("No mutation",)),
            PlanStep("runtime", "Runtime and services", ("Runtime", "Services")),
            IDENTITY_PLAN_STEP,
            PlanStep("provider", "Provider", ("Protected access",)),
            PlanStep("hermes-plugin", "Hermes", ("Plugin",)),
            PlanStep("verify", "Verify", ("Readiness",)),
        ),
    )

    with pytest.raises(TransactionApplyError):
        _execute(
            tmp_path,
            CompanionIdentityAdapter(layout, name="Noé", gender="masculine"),
            transaction_id="public-plan",
            plan=public_plan,
        )

    assert not layout.identity_file.exists()


def test_the_name_prompt_accepts_accents_and_keeps_a_refused_name():
    from daemons_companion.desktop.terminal import ask_daemon_name

    class UI:
        def __init__(self, answers):
            self.answers, self.fields, self.messages = list(answers), [], []

        def field(self, title, previous="", maximum=80):
            self.fields.append(previous)
            return self.answers.pop(0)

        def say(self, message):
            self.messages.append(message)

    ui = UI(["Aurélie"])
    assert ask_daemon_name(ui) == "Aurélie" and not ui.messages
    ui = UI(["Hélène / Iris", "Hélène"])
    assert ask_daemon_name(ui, "Aster") == "Hélène"
    assert ui.fields == ["Aster", "Hélène / Iris"]
    assert ui.messages == ["Daemon name must be 1–40 letters, numbers, spaces, dots, apostrophes or hyphens."]
