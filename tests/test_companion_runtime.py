from __future__ import annotations

import json
import os
from pathlib import Path
import stat

import pytest

import daemons_companion.runtime.release as companion_runtime
from daemons_companion.setup.plan import HostKind, HostProbe, PlanStep, SetupPlan
from daemons_companion.runtime.release import (
    CompanionRuntimeAdapter,
    CompanionRuntimeLayout,
    RUNTIME_LAYOUT_STEP,
    create_disposable_companion_marker,
    resolve_current_release,
)
from daemons_companion.setup.transaction import (
    TransactionApplyError,
    TransactionEngine,
    TransactionRecoveryError,
    public_plan_digest,
)


RUNTIME_PLAN_STEP = PlanStep(
    RUNTIME_LAYOUT_STEP,
    "Install an isolated runtime.",
    ("Versioned release", "Shared memory", "Shared state"),
)


def _plan(step: PlanStep = RUNTIME_PLAN_STEP) -> SetupPlan:
    return SetupPlan(
        selected_host=HostProbe(
            HostKind.HERMES,
            detected=True,
            usable=True,
            executable="/synthetic/hermes",
            version="synthetic",
            version_status="verified",
        ),
        daemon_name="Iris",
        daemon_gender="feminine",
        provider={"kind": "synthetic", "settings": {}, "required_secret_names": []},
        steps=(step,),
    )


def _bundle(root: Path, *, variant: str = "one") -> Path:
    files = {
        "requirements-runtime.lock": (
            f"example=={variant} \\\n"
            "    --hash=sha256:"
            + ("0" * 64)
            + "\n"
        ),
        "requirements.txt": f"example=={variant}\n",
        "src/daemon_core/__init__.py": f'VERSION = "{variant}"\n',
        "integrations/hermes_plugin/__init__.py": "# memory\n",
        "integrations/hermes_voice_plugin/__init__.py": "# voice\n",
        "integrations/openclaw_plugin/index.js": "export default {};\n",
        "integrations/after_turn_service/service.sh": "#!/bin/sh\nexit 0\n",
        "integrations/dreaming_service/service.sh": "#!/bin/sh\nexit 0\n",
        "src/daemons_companion/runtime/service_entry.py": "# entry\n",
        "src/daemon_core/nightly_maintenance.py": "# nightly\n",
        "src/daemons_companion/runtime/services.py": "# services\n",
        "assets/empty/.keep": "",
    }
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755 if path.name == "service.sh" else 0o644)
    return root.absolute()


def _layout(tmp_path: Path, *, variant: str = "one") -> CompanionRuntimeLayout:
    home = (tmp_path / "disposable-companion").absolute()
    create_disposable_companion_marker(home)
    return CompanionRuntimeLayout(home, _bundle(tmp_path / f"bundle-{variant}", variant=variant))


def _execute(
    tmp_path: Path,
    adapter: CompanionRuntimeAdapter,
    *,
    transaction_id: str = "runtime-test",
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


def test_materializes_private_release_and_portable_pointer(tmp_path: Path) -> None:
    layout = _layout(tmp_path)

    result = _execute(tmp_path, CompanionRuntimeAdapter(layout))

    assert result.status == "committed"
    release = resolve_current_release(layout)
    assert release.parent == layout.releases_dir
    assert (release / "src/daemon_core/__init__.py").is_file()
    assert (release / ".daemon-harness-release.json").is_file()
    assert layout.memory_root.is_dir()
    assert layout.state_root.is_dir()
    pointer = json.loads(layout.current_pointer.read_text(encoding="utf-8"))
    assert set(pointer) == {"schema", "release_id"}
    assert str(layout.companion_home) not in layout.current_pointer.read_text()
    assert stat.S_IMODE(layout.current_pointer.stat().st_mode) == 0o600


def test_same_bundle_is_idempotent(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    adapter = CompanionRuntimeAdapter(layout)
    _execute(tmp_path, adapter, transaction_id="first")
    before = _tree_state(layout.companion_home)

    _execute(tmp_path, CompanionRuntimeAdapter(layout), transaction_id="second")

    assert _tree_state(layout.companion_home) == before
    assert len(list(layout.releases_dir.iterdir())) == 1


def test_upgrade_keeps_old_release_and_shared_roots(tmp_path: Path) -> None:
    layout_one = _layout(tmp_path, variant="one")
    _execute(tmp_path, CompanionRuntimeAdapter(layout_one), transaction_id="one")
    first = resolve_current_release(layout_one)
    (layout_one.memory_root / "personal.md").write_text("keep", encoding="utf-8")
    (layout_one.state_root / "queue.json").write_text("keep", encoding="utf-8")

    layout_two = CompanionRuntimeLayout(
        layout_one.companion_home,
        _bundle(tmp_path / "bundle-two", variant="two"),
    )
    _execute(tmp_path, CompanionRuntimeAdapter(layout_two), transaction_id="two")
    second = resolve_current_release(layout_two)

    assert second != first
    assert first.is_dir() and second.is_dir()
    assert (layout_two.memory_root / "personal.md").read_text() == "keep"
    assert (layout_two.state_root / "queue.json").read_text() == "keep"


@pytest.mark.parametrize("boundary", ["staging", "release", "shared_roots", "current"])
def test_failure_boundaries_restore_exact_home(tmp_path: Path, boundary: str) -> None:
    layout = _layout(tmp_path)
    before = _tree_state(layout.companion_home)

    def fail(artifact: str) -> None:
        if artifact == boundary:
            raise RuntimeError("synthetic failure containing provider-secret")

    with pytest.raises(TransactionApplyError):
        _execute(tmp_path, CompanionRuntimeAdapter(layout, mutation_hook=fail))

    assert _tree_state(layout.companion_home) == before
    journal = (tmp_path / "transactions/runtime-test/journal.json").read_text()
    assert "provider-secret" not in journal


def test_rollback_restores_preexisting_shared_root_mode(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    layout.memory_root.mkdir(parents=True)
    layout.memory_root.chmod(0o755)
    (layout.memory_root / "persona.md").write_text("keep", encoding="utf-8")
    before = _tree_state(layout.companion_home)

    def fail(artifact: str) -> None:
        if artifact == "current":
            raise RuntimeError("synthetic failure")

    with pytest.raises(TransactionApplyError):
        _execute(tmp_path, CompanionRuntimeAdapter(layout, mutation_hook=fail))

    assert _tree_state(layout.companion_home) == before


@pytest.mark.parametrize("boundary", ["staging", "release", "shared_roots", "current"])
def test_interruptions_are_recovered_by_fresh_adapter(tmp_path: Path, boundary: str) -> None:
    layout = _layout(tmp_path)
    before = _tree_state(layout.companion_home)

    def interrupt(artifact: str) -> None:
        if artifact == boundary:
            raise KeyboardInterrupt("synthetic interruption")

    with pytest.raises(KeyboardInterrupt):
        _execute(tmp_path, CompanionRuntimeAdapter(layout, mutation_hook=interrupt))

    plan = _plan()
    result = TransactionEngine(tmp_path / "transactions").recover(
        plan,
        CompanionRuntimeAdapter(layout),
        transaction_id="runtime-test",
        confirmed_plan_digest=public_plan_digest(plan),
    )
    assert result.status == "rolled_back"
    assert _tree_state(layout.companion_home) == before


def test_concurrent_pointer_change_is_preserved_and_rollback_fails(tmp_path: Path) -> None:
    layout = _layout(tmp_path)

    def conflict(artifact: str) -> None:
        if artifact == "current":
            layout.current_pointer.write_text('{"third_party":true}\n', encoding="utf-8")
            raise RuntimeError("force rollback")

    with pytest.raises(TransactionRecoveryError):
        _execute(tmp_path, CompanionRuntimeAdapter(layout, mutation_hook=conflict))

    assert json.loads(layout.current_pointer.read_text()) == {"third_party": True}
    result = TransactionEngine(tmp_path / "transactions").inspect("runtime-test")
    assert result.status == "rollback_failed"


def test_concurrent_shared_root_content_is_preserved(tmp_path: Path) -> None:
    layout = _layout(tmp_path)

    def conflict(artifact: str) -> None:
        if artifact == "shared_roots":
            (layout.memory_root / "third-party.md").write_text("keep", encoding="utf-8")
            raise RuntimeError("force rollback")

    with pytest.raises(TransactionApplyError):
        _execute(tmp_path, CompanionRuntimeAdapter(layout, mutation_hook=conflict))

    assert (layout.memory_root / "third-party.md").read_text() == "keep"
    result = TransactionEngine(tmp_path / "transactions").inspect("runtime-test")
    assert result.status == "rolled_back"


def test_runtime_generated_shared_data_survives_later_install_failure(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)

    def fail(artifact: str) -> None:
        if artifact == "current":
            (layout.memory_root / "dialogue.md").write_text("durable", encoding="utf-8")
            (layout.state_root / "heartbeat.json").write_text("{}\n", encoding="utf-8")
            raise RuntimeError("later owner failed")

    with pytest.raises(TransactionApplyError):
        _execute(tmp_path, CompanionRuntimeAdapter(layout, mutation_hook=fail))

    assert (layout.memory_root / "dialogue.md").read_text() == "durable"
    assert (layout.state_root / "heartbeat.json").read_text() == "{}\n"
    assert not layout.current_pointer.exists()
    assert not layout.releases_dir.exists()


@pytest.mark.parametrize("boundary", ["staging", "release"])
def test_concurrent_runtime_tree_change_is_preserved(
    tmp_path: Path,
    boundary: str,
) -> None:
    layout = _layout(tmp_path)

    def conflict(artifact: str) -> None:
        if artifact != boundary:
            return
        if boundary == "staging":
            candidates = list(layout.runtime_root.glob(".staging-*"))
        else:
            candidates = list(layout.releases_dir.iterdir())
        assert len(candidates) == 1
        (candidates[0] / "third-party.txt").write_text("keep", encoding="utf-8")
        raise RuntimeError("force rollback")

    with pytest.raises(TransactionRecoveryError):
        _execute(tmp_path, CompanionRuntimeAdapter(layout, mutation_hook=conflict))

    preserved = list(layout.runtime_root.rglob("third-party.txt"))
    assert len(preserved) == 1
    assert preserved[0].read_text() == "keep"


def test_same_identifier_with_modified_release_is_refused(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    _execute(tmp_path, CompanionRuntimeAdapter(layout), transaction_id="first")
    release = resolve_current_release(layout)
    (release / "requirements.txt").write_text("tampered\n", encoding="utf-8")

    with pytest.raises(TransactionApplyError):
        _execute(
            tmp_path,
            CompanionRuntimeAdapter(layout),
            transaction_id="collision",
        )


def test_unmarked_home_and_wrong_step_are_refused_without_write(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path / "bundle")
    home = (tmp_path / "ordinary-home").absolute()
    home.mkdir()
    layout = CompanionRuntimeLayout(home, bundle)
    with pytest.raises(TransactionApplyError):
        _execute(tmp_path, CompanionRuntimeAdapter(layout), transaction_id="unmarked")
    assert list(home.iterdir()) == []

    marked = _layout(tmp_path / "marked")
    wrong = _plan(PlanStep("identity", "Wrong step", ("identity",)))
    with pytest.raises(TransactionApplyError):
        _execute(
            tmp_path / "marked",
            CompanionRuntimeAdapter(marked),
            transaction_id="wrong-step",
            plan=wrong,
        )
    assert not marked.data_root.exists()


def test_bundle_symlink_and_incomplete_bundle_are_refused(tmp_path: Path) -> None:
    layout = _layout(tmp_path / "symlink")
    target = layout.bundle_root / "target.txt"
    target.write_text("target", encoding="utf-8")
    (layout.bundle_root / "bad-link").symlink_to(target)
    with pytest.raises(TransactionApplyError):
        _execute(tmp_path / "symlink", CompanionRuntimeAdapter(layout))
    assert not layout.data_root.exists()

    home = (tmp_path / "incomplete/disposable").absolute()
    create_disposable_companion_marker(home)
    bundle = (tmp_path / "incomplete/bundle").absolute()
    bundle.mkdir(parents=True)
    (bundle / "requirements.txt").write_text("empty\n", encoding="utf-8")
    incomplete = CompanionRuntimeLayout(home, bundle)
    with pytest.raises(TransactionApplyError):
        _execute(tmp_path / "incomplete", CompanionRuntimeAdapter(incomplete))
    assert not incomplete.data_root.exists()


def test_bundle_root_and_managed_root_must_not_be_symlinks(tmp_path: Path) -> None:
    real_bundle = _bundle(tmp_path / "real-bundle")
    bundle_link = (tmp_path / "bundle-link").absolute()
    bundle_link.symlink_to(real_bundle, target_is_directory=True)
    home = (tmp_path / "disposable").absolute()
    create_disposable_companion_marker(home)
    with pytest.raises(TransactionApplyError):
        _execute(
            tmp_path,
            CompanionRuntimeAdapter(CompanionRuntimeLayout(home, bundle_link)),
            transaction_id="bundle-link",
        )
    assert not (home / "data").exists()

    real_home = (tmp_path / "real-home").absolute()
    create_disposable_companion_marker(real_home)
    home_link = (tmp_path / "home-link").absolute()
    home_link.symlink_to(real_home, target_is_directory=True)
    with pytest.raises(TransactionApplyError):
        _execute(
            tmp_path,
            CompanionRuntimeAdapter(CompanionRuntimeLayout(home_link, real_bundle)),
            transaction_id="home-link",
        )
    assert not (real_home / "data").exists()


def test_bundle_size_is_bounded_before_any_runtime_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _layout(tmp_path)
    monkeypatch.setattr(companion_runtime, "_MAX_TOTAL_BYTES", 4)

    with pytest.raises(TransactionApplyError):
        _execute(tmp_path, CompanionRuntimeAdapter(layout), transaction_id="too-large")

    assert not layout.data_root.exists()


def test_bundle_file_count_is_bounded_before_any_runtime_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _layout(tmp_path)
    monkeypatch.setattr(companion_runtime, "_MAX_FILES", 2)

    with pytest.raises(TransactionApplyError):
        _execute(tmp_path, CompanionRuntimeAdapter(layout), transaction_id="too-many")

    assert not layout.data_root.exists()


@pytest.mark.skipif(os.name == "nt", reason="Windows test users cannot create FIFOs")
def test_special_bundle_file_is_refused(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    os.mkfifo(layout.bundle_root / "unsupported.pipe")

    with pytest.raises(TransactionApplyError):
        _execute(tmp_path, CompanionRuntimeAdapter(layout), transaction_id="special")

    assert not layout.data_root.exists()


def test_public_setup_plan_remains_non_executable(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    public_plan = SetupPlan(
        selected_host=_plan().selected_host,
        daemon_name="Iris",
        daemon_gender="feminine",
        provider={"kind": "synthetic", "settings": {}, "required_secret_names": []},
        steps=(
            PlanStep("review", "Review", ("No mutation",)),
            PlanStep("runtime", "Runtime and services", ("Runtime", "Services")),
            PlanStep("identity", "Identity", ("Metadata",)),
            PlanStep("provider", "Provider", ("Protected access",)),
            PlanStep("hermes-plugin", "Hermes", ("Plugin",)),
            PlanStep("verify", "Verify", ("Readiness",)),
        ),
    )

    with pytest.raises(TransactionApplyError):
        _execute(
            tmp_path,
            CompanionRuntimeAdapter(layout),
            transaction_id="public-plan",
            plan=public_plan,
        )

    assert not layout.data_root.exists()
