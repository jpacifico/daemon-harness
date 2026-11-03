from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess

import pytest

from daemons_companion.setup.plan import HostKind, HostProbe, PlanStep, SetupPlan
from daemons_companion.runtime.release import (
    CompanionRuntimeAdapter,
    CompanionRuntimeLayout,
    RUNTIME_LAYOUT_STEP,
    create_disposable_companion_marker,
    resolve_current_release,
)
from daemons_companion.runtime.environment import (
    CommandResult,
    CompanionRuntimeEnvironmentAdapter,
    CompanionRuntimeEnvironmentLayout,
    RUNTIME_ENVIRONMENT_STEP,
    SUPPORTED_UV_VERSION,
)
from daemons_companion.setup.transaction import (
    TransactionApplyError,
    TransactionEngine,
    TransactionRecoveryError,
    public_plan_digest,
)


LAYOUT_STEP = PlanStep(RUNTIME_LAYOUT_STEP, "layout", ("release",))
ENVIRONMENT_STEP = PlanStep(
    RUNTIME_ENVIRONMENT_STEP,
    "environment",
    ("managed Python", "locked dependencies"),
)


def _plan(step: PlanStep = ENVIRONMENT_STEP) -> SetupPlan:
    return SetupPlan(
        selected_host=HostProbe(
            HostKind.HERMES,
            detected=True,
            usable=True,
            executable="/synthetic/hermes",
            version="synthetic",
            version_status="verified",
        ),
        daemon_name="Lyra",
        daemon_gender="feminine",
        provider={"kind": "synthetic", "settings": {}, "required_secret_names": []},
        steps=(step,),
    )


def _bundle(root: Path, *, variant: str = "one") -> Path:
    lock = (
        f"example=={variant} \\\n"
        "    --hash=sha256:"
        + ("0" * 64)
        + "\n"
    )
    files = {
        "requirements-runtime.lock": lock,
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
    }
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755 if path.name == "service.sh" else 0o644)
    return root.absolute()


def _runtime(
    tmp_path: Path,
    *,
    variant: str = "one",
    real_lock: bool = False,
) -> CompanionRuntimeLayout:
    home = (tmp_path / "companion").absolute()
    create_disposable_companion_marker(home)
    bundle = _bundle(tmp_path / f"bundle-{variant}", variant=variant)
    if real_lock:
        (bundle / "requirements-runtime.lock").write_bytes(
            Path("requirements-runtime.lock").read_bytes()
        )
    layout = CompanionRuntimeLayout(home, bundle)
    plan = _plan(LAYOUT_STEP)
    TransactionEngine(tmp_path / "layout-transactions").execute(
        plan,
        CompanionRuntimeAdapter(layout),
        transaction_id=f"layout-{variant}",
        confirmed_plan_digest=public_plan_digest(plan),
    )
    return layout


class FakeRunner:
    def __init__(self, target_os: str = "macos") -> None:
        self.target_os = target_os
        self.calls: list[tuple[list[str], dict[str, str]]] = []

    def __call__(self, command, environment) -> CommandResult:
        command = list(command)
        environment = dict(environment)
        self.calls.append((command, environment))
        if command[1:] == ["--version"]:
            return CommandResult(0, f"uv {SUPPORTED_UV_VERSION} (synthetic)\n")
        if command[1:3] == ["python", "install"]:
            root = Path(command[command.index("--install-dir") + 1])
            folder = root / ("cpython-3.12.13-windows-x86_64-none" if self.target_os == "windows" else "cpython")
            folder.mkdir(parents=True)
            (folder / ("python.exe" if self.target_os == "windows" else "python")).write_text("managed", encoding="utf-8")
            return CommandResult(0)
        if command[1:2] == ["venv"]:
            venv = Path(command[2])
            interpreter = venv / (
                "Scripts/python.exe" if self.target_os == "windows" else "bin/python"
            )
            interpreter.parent.mkdir(parents=True)
            interpreter.write_text("python", encoding="utf-8")
            interpreter.chmod(0o700)
            if self.target_os == "windows":
                home = Path(command[command.index("--python") + 1]).parent
                (venv / "pyvenv.cfg").write_text(f"home = {home}\nrelocatable = true\n", encoding="utf-8")
            return CommandResult(0)
        if command[1:3] == ["pip", "sync"]:
            interpreter = Path(command[command.index("--python") + 1])
            marker = interpreter.parent.parent / "site-packages/locked.txt"
            marker.parent.mkdir(parents=True)
            marker.write_text("locked", encoding="utf-8")
            cache = Path(environment["UV_CACHE_DIR"])
            cache.mkdir(parents=True, exist_ok=True)
            (cache / "wheel").write_text("temporary", encoding="utf-8")
            return CommandResult(0)
        if command[0].endswith(("/python", "python.exe")):
            return CommandResult(0, "daemon-runtime-ok\n")
        return CommandResult(1, stderr="unexpected command")


def _environment_layout(
    tmp_path: Path,
    runtime: CompanionRuntimeLayout,
    *,
    target_os: str = "macos",
) -> tuple[CompanionRuntimeEnvironmentLayout, FakeRunner]:
    uv = (tmp_path / ("uv.exe" if target_os == "windows" else "uv")).absolute()
    uv.write_bytes(b"synthetic uv 0.12.16")
    uv.chmod(0o700)
    layout = CompanionRuntimeEnvironmentLayout(
        runtime=runtime,
        uv_executable=uv,
        uv_sha256=hashlib.sha256(uv.read_bytes()).hexdigest(),
        target_os=target_os,
    )
    return layout, FakeRunner(target_os)


def _execute(
    tmp_path: Path,
    adapter: CompanionRuntimeEnvironmentAdapter,
    *,
    transaction_id: str = "environment-test",
    plan: SetupPlan | None = None,
):
    chosen = plan or _plan()
    return TransactionEngine(tmp_path / "environment-transactions").execute(
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


@pytest.mark.parametrize("target_os", ["macos", "windows"])
def test_builds_private_release_owned_environment(
    tmp_path: Path,
    target_os: str,
) -> None:
    runtime = _runtime(tmp_path)
    layout, runner = _environment_layout(tmp_path, runtime, target_os=target_os)

    result = _execute(tmp_path, CompanionRuntimeEnvironmentAdapter(layout, runner=runner))

    assert result.status == "committed"
    release = resolve_current_release(runtime)
    environment = layout.environment_path(release.name)
    manifest = json.loads(
        (environment / ".daemon-harness-environment.json").read_text(encoding="utf-8")
    )
    assert manifest["release_id"] == release.name
    assert manifest["target_os"] == target_os
    assert not (environment / "cache").exists()
    interpreter = environment / (
        "venv/Scripts/python.exe" if target_os == "windows" else "venv/bin/python"
    )
    assert interpreter.is_file()

    install = next(call for call in runner.calls if call[0][1:3] == ["python", "install"])
    assert "--managed-python" in install[0]
    assert "--no-bin" in install[0]
    assert "--no-registry" in install[0]
    assert install[1]["UV_PYTHON_INSTALL_DIR"].startswith(str(runtime.runtime_root))
    assert "UV_PYTHON_PREFERENCE" not in install[1]
    assert "VIRTUAL_ENV" not in install[1]
    sync = next(call for call in runner.calls if call[0][1:3] == ["pip", "sync"])
    assert "--require-hashes" in sync[0]
    assert "--only-binary" in sync[0]
    assert "--no-python-downloads" in sync[0]


def test_same_release_is_idempotent(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    layout, first_runner = _environment_layout(tmp_path, runtime)
    _execute(
        tmp_path,
        CompanionRuntimeEnvironmentAdapter(layout, runner=first_runner),
        transaction_id="first",
    )
    before = _tree_state(layout.environments_dir)
    second_runner = FakeRunner()

    _execute(
        tmp_path,
        CompanionRuntimeEnvironmentAdapter(layout, runner=second_runner),
        transaction_id="second",
    )

    assert _tree_state(layout.environments_dir) == before
    assert not any(call[0][1:3] == ["python", "install"] for call in second_runner.calls)


def test_new_release_gets_separate_environment(tmp_path: Path) -> None:
    runtime_one = _runtime(tmp_path, variant="one")
    layout_one, runner_one = _environment_layout(tmp_path, runtime_one)
    _execute(
        tmp_path,
        CompanionRuntimeEnvironmentAdapter(layout_one, runner=runner_one),
        transaction_id="environment-one",
    )
    first_release = resolve_current_release(runtime_one).name
    first_environment = layout_one.environments_dir / first_release
    first_state = _tree_state(first_environment)

    runtime_two = CompanionRuntimeLayout(
        runtime_one.companion_home,
        _bundle(tmp_path / "bundle-two", variant="two"),
    )
    layout_plan = _plan(LAYOUT_STEP)
    TransactionEngine(tmp_path / "layout-transactions").execute(
        layout_plan,
        CompanionRuntimeAdapter(runtime_two),
        transaction_id="layout-two",
        confirmed_plan_digest=public_plan_digest(layout_plan),
    )
    layout_two, runner_two = _environment_layout(tmp_path, runtime_two)
    _execute(
        tmp_path,
        CompanionRuntimeEnvironmentAdapter(layout_two, runner=runner_two),
        transaction_id="environment-two",
    )

    second_release = resolve_current_release(runtime_two).name
    assert second_release != first_release
    assert _tree_state(first_environment) == first_state
    assert sorted(path.name for path in layout_two.environments_dir.iterdir()) == sorted(
        [first_release, second_release]
    )


@pytest.mark.parametrize("boundary", ["python", "venv", "sync", "verified", "published"])
def test_failure_boundaries_restore_exact_runtime(tmp_path: Path, boundary: str) -> None:
    runtime = _runtime(tmp_path)
    layout, runner = _environment_layout(tmp_path, runtime)
    before = _tree_state(runtime.companion_home)

    def fail(artifact: str) -> None:
        if artifact == boundary:
            raise RuntimeError("synthetic provider-secret")

    with pytest.raises(TransactionApplyError):
        _execute(
            tmp_path,
            CompanionRuntimeEnvironmentAdapter(layout, runner=runner, mutation_hook=fail),
        )

    assert _tree_state(runtime.companion_home) == before
    journal = (
        tmp_path / "environment-transactions/environment-test/journal.json"
    ).read_text(encoding="utf-8")
    assert "provider-secret" not in journal


@pytest.mark.parametrize("boundary", ["python", "venv", "sync", "verified", "published"])
def test_interruptions_recover_with_fresh_adapter(tmp_path: Path, boundary: str) -> None:
    runtime = _runtime(tmp_path)
    layout, runner = _environment_layout(tmp_path, runtime)
    before = _tree_state(runtime.companion_home)

    def interrupt(artifact: str) -> None:
        if artifact == boundary:
            raise KeyboardInterrupt("synthetic interruption")

    with pytest.raises(KeyboardInterrupt):
        _execute(
            tmp_path,
            CompanionRuntimeEnvironmentAdapter(
                layout,
                runner=runner,
                mutation_hook=interrupt,
            ),
        )

    plan = _plan()
    result = TransactionEngine(tmp_path / "environment-transactions").recover(
        plan,
        CompanionRuntimeEnvironmentAdapter(layout, runner=FakeRunner()),
        transaction_id="environment-test",
        confirmed_plan_digest=public_plan_digest(plan),
    )
    assert result.status == "rolled_back"
    assert _tree_state(runtime.companion_home) == before


@pytest.mark.parametrize("boundary", ["sync", "published"])
def test_concurrent_environment_change_is_preserved(
    tmp_path: Path,
    boundary: str,
) -> None:
    runtime = _runtime(tmp_path)
    layout, runner = _environment_layout(tmp_path, runtime)

    def conflict(artifact: str) -> None:
        if artifact != boundary:
            return
        release_id = resolve_current_release(runtime).name
        root = (
            layout.environments_dir / release_id
            if boundary == "published"
            else runtime.runtime_root / f".environment-{release_id}"
        )
        (root / "third-party.txt").write_text("preserve", encoding="utf-8")
        raise RuntimeError("force rollback")

    with pytest.raises(TransactionRecoveryError):
        _execute(
            tmp_path,
            CompanionRuntimeEnvironmentAdapter(
                layout,
                runner=runner,
                mutation_hook=conflict,
            ),
        )

    preserved = list(runtime.runtime_root.rglob("third-party.txt"))
    assert len(preserved) == 1
    assert preserved[0].read_text(encoding="utf-8") == "preserve"


def test_rejects_public_step_real_home_and_bad_uv(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    layout, runner = _environment_layout(tmp_path, runtime)
    public_plan = _plan(PlanStep("runtime", "public", ("services",)))
    with pytest.raises(TransactionApplyError):
        _execute(
            tmp_path,
            CompanionRuntimeEnvironmentAdapter(layout, runner=runner),
            plan=public_plan,
            transaction_id="public-step",
        )

    (runtime.companion_home / ".daemon-harness-disposable-companion").unlink()
    with pytest.raises(TransactionApplyError):
        _execute(
            tmp_path,
            CompanionRuntimeEnvironmentAdapter(layout, runner=runner),
            transaction_id="real-home",
        )

    create_disposable_companion_marker(runtime.companion_home)
    bad_layout = CompanionRuntimeEnvironmentLayout(
        runtime,
        layout.uv_executable,
        "f" * 64,
        "macos",
    )
    with pytest.raises(TransactionApplyError):
        _execute(
            tmp_path,
            CompanionRuntimeEnvironmentAdapter(bad_layout, runner=runner),
            transaction_id="bad-uv",
        )


def test_rejects_wrong_uv_version_and_symlink(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    layout, _runner = _environment_layout(tmp_path, runtime)

    def wrong_version(command, environment):
        return CommandResult(0, "uv 9.9.9 (synthetic)\n")

    with pytest.raises(TransactionApplyError):
        _execute(
            tmp_path,
            CompanionRuntimeEnvironmentAdapter(layout, runner=wrong_version),
            transaction_id="wrong-version",
        )

    link = (tmp_path / "uv-link").absolute()
    link.symlink_to(layout.uv_executable)
    linked_layout = CompanionRuntimeEnvironmentLayout(
        runtime,
        link,
        layout.uv_sha256,
        "macos",
    )
    with pytest.raises(TransactionApplyError):
        _execute(
            tmp_path,
            CompanionRuntimeEnvironmentAdapter(linked_layout, runner=FakeRunner()),
            transaction_id="linked-uv",
        )


def test_tampered_release_is_rejected_before_environment_write(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    layout, runner = _environment_layout(tmp_path, runtime)
    release = resolve_current_release(runtime)
    (release / "requirements-runtime.lock").write_text("tampered\n", encoding="utf-8")

    with pytest.raises(TransactionApplyError):
        _execute(tmp_path, CompanionRuntimeEnvironmentAdapter(layout, runner=runner))

    assert not layout.environments_dir.exists()
    assert not any(call[0][1:3] == ["python", "install"] for call in runner.calls)


def test_repository_runtime_lock_is_universal_hashed_and_complete() -> None:
    lock = Path("requirements-runtime.lock").read_text(encoding="utf-8")

    assert "--universal --generate-hashes --python-version 3.12" in lock.splitlines()[1]
    assert "sys_platform == 'win32'" in lock
    for package in ("fastapi", "openai", "pydantic", "pyyaml", "uvicorn"):
        assert f"{package}==" in lock.lower()
    assert lock.count("--hash=sha256:") > 28


def test_command_environment_does_not_forward_provider_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    monkeypatch.setenv("LOCAL_LLM_API_KEY", "must-not-leak")
    monkeypatch.setenv("VIRTUAL_ENV", "/host/venv")
    runtime = _runtime(tmp_path)
    layout, runner = _environment_layout(tmp_path, runtime)

    _execute(tmp_path, CompanionRuntimeEnvironmentAdapter(layout, runner=runner))

    for _command, environment in runner.calls:
        assert "OPENAI_API_KEY" not in environment
        assert "LOCAL_LLM_API_KEY" not in environment
        assert "VIRTUAL_ENV" not in environment


@pytest.mark.skipif(
    not os.environ.get("DAEMON_TEST_REAL_UV"),
    reason="requires an explicitly verified uv 0.12.16 binary and network",
)
def test_real_uv_build_survives_atomic_publication(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, real_lock=True)
    uv = Path(os.environ["DAEMON_TEST_REAL_UV"]).absolute()
    layout = CompanionRuntimeEnvironmentLayout(
        runtime=runtime,
        uv_executable=uv,
        uv_sha256=hashlib.sha256(uv.read_bytes()).hexdigest(),
        target_os="windows" if os.name == "nt" else "macos",
    )

    result = _execute(
        tmp_path,
        CompanionRuntimeEnvironmentAdapter(layout),
        transaction_id="real-uv",
    )

    assert result.status == "committed"
    release = resolve_current_release(runtime)
    interpreter = layout.environment_path(release.name) / ("venv/Scripts/python.exe" if os.name == "nt" else "venv/bin/python")
    completed = subprocess.run(
        [
            str(interpreter),
            "-I",
            "-c",
            "import fastapi,openai,pydantic,uvicorn,yaml; print('ok')",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.stdout.strip() == "ok"


@pytest.mark.parametrize("boundary", ["python", "venv", "import-paths", "sync", "verified", "python-home", "published", "published-verified"])
def test_windows_failure_boundaries_restore_runtime(tmp_path, boundary):
    runtime = _runtime(tmp_path)
    layout, runner = _environment_layout(tmp_path, runtime, target_os="windows")
    before = _tree_state(runtime.companion_home)

    def fail(where):
        if where == boundary:
            raise RuntimeError("injected Windows setup failure")

    with pytest.raises(TransactionApplyError):
        _execute(tmp_path, CompanionRuntimeEnvironmentAdapter(layout, runner=runner, mutation_hook=fail))
    assert _tree_state(runtime.companion_home) == before


def test_windows_uses_short_paths_direct_python_and_probes_published_venv(tmp_path):
    runtime = _runtime(tmp_path)
    layout, runner = _environment_layout(tmp_path, runtime, target_os="windows")
    _execute(tmp_path, CompanionRuntimeEnvironmentAdapter(layout, runner=runner))
    release = resolve_current_release(runtime).name
    venv = next(command for command, _ in runner.calls if command[1:2] == ["venv"])
    python = venv[venv.index("--python") + 1]
    assert Path(python) == layout.staging_path(release) / "python/py/python.exe"
    assert len(layout.environment_path(release).name) == 16
    assert any(command[0] == str(layout.environment_path(release) / "venv/Scripts/python.exe")
               for command, _ in runner.calls)
    assert sorted(p.name for p in (layout.environment_path(release) / "python").iterdir()) == ["py"]


def _field_attempt(user, host="openclaw"):
    from pathlib import PureWindowsPath
    return (PureWindowsPath(rf"C:\Users\{user}\AppData\Local\DaemonHarness\Companion\instance-p3RtVn\hosts")
            / host / "installations" / ("3" * 32))


def test_windows_field_path_that_failed_now_has_room():
    from daemons_companion.runtime.environment import check_windows_installation_room
    runtime = _field_attempt("Alice") / "daemon/data/runtime"
    uv_name = "python/cpython-3.12.999-windows-x86_64-none/Lib/encodings/__init__.py"
    # The c16 OpenClaw install refused this published probe at the end.
    assert len(str(runtime / "environments" / ("f" * 16) / uv_name)) >= 250
    check_windows_installation_room(_field_attempt("Alice"))
    for host in ("hermes", "openclaw"):
        check_windows_installation_room(_field_attempt("u" * 30, host))


def test_windows_installation_room_is_refused_before_anything_is_installed():
    from daemons_companion.runtime.environment import check_windows_installation_room
    with pytest.raises(ValueError, match="too long.*Nothing was installed"):
        check_windows_installation_room(_field_attempt("u" * 60))


def test_short_directory_collision_cannot_reuse_wrong_release(tmp_path):
    runtime = _runtime(tmp_path)
    layout, runner = _environment_layout(tmp_path, runtime, target_os="windows")
    _execute(tmp_path, CompanionRuntimeEnvironmentAdapter(layout, runner=runner))
    release = resolve_current_release(runtime).name
    manifest = layout.environment_path(release) / ".daemon-harness-environment.json"
    content = json.loads(manifest.read_text())
    content["release_id"] = release[:16] + ("0" * 48)
    manifest.write_text(json.dumps(content))
    next_runner = FakeRunner("windows")
    with pytest.raises(TransactionApplyError):
        _execute(tmp_path, CompanionRuntimeEnvironmentAdapter(layout, runner=next_runner), transaction_id="collision")
    assert not any(c[1:3] == ["python", "install"] for c, _ in next_runner.calls)


def test_windows_legacy_environment_is_reused_and_verified(tmp_path):
    from daemons_companion.runtime.environment import resolve_release_environment
    runtime = _runtime(tmp_path)
    layout, runner = _environment_layout(tmp_path, runtime, target_os="windows")
    _execute(tmp_path, CompanionRuntimeEnvironmentAdapter(layout, runner=runner))
    release = resolve_current_release(runtime).name
    legacy = layout.environments_dir / release
    layout.environment_path(release).rename(legacy)
    assert resolve_release_environment(layout) == legacy


def test_alias_validation_precedes_any_removal(tmp_path):
    from daemons_companion.runtime.environment import _direct_windows_python
    root = tmp_path / "python"
    chosen = root / "cpython-3.12.13-windows-x86_64-none"
    chosen.mkdir(parents=True)
    (chosen / "python.exe").write_text("fixture")
    alias = root / "cpython-3.12-windows-x86_64-none"
    alias.symlink_to(chosen, target_is_directory=True)
    external = tmp_path / "personal"
    external.mkdir()
    bad = root / "external"
    bad.symlink_to(external, target_is_directory=True)
    with pytest.raises(RuntimeError, match="outside"):
        _direct_windows_python(root)
    assert alias.is_symlink() and bad.is_symlink()


def test_junction_traversal_and_removal_do_not_enter_target(tmp_path, monkeypatch):
    import daemons_companion.runtime.environment as env
    root = tmp_path / "owned"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("preserve")
    alias = root / "alias"
    if os.name == "nt":
        subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(alias), str(outside)], check=True, capture_output=True)
    else:
        # Model a junction with a symlink; rmdir removes the link on Windows.
        alias.symlink_to(outside, target_is_directory=True)
        original = Path.rmdir
        monkeypatch.setattr(env, "_is_junction", lambda path: path == alias)
        monkeypatch.setattr(Path, "rmdir", lambda path: path.unlink() if path == alias else original(path))
    assert list(env._walk_tree(root)) == [alias]
    with pytest.raises(RuntimeError, match="external"):
        env._verify_internal_links(root)
    env._remove_tree(root)
    assert (outside / "keep").read_text() == "preserve"


def test_windows_alias_is_removed_before_venv_and_patch_target_survives(tmp_path):
    from daemons_companion.runtime.environment import _direct_windows_python
    root = tmp_path / "python"
    chosen = root / "cpython-3.12.13-windows-x86_64-none"
    chosen.mkdir(parents=True)
    (chosen / "python.exe").write_text("fixture")
    alias = root / "cpython-3.12-windows-x86_64-none"
    if os.name == "nt":
        subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(alias), str(chosen)], check=True, capture_output=True)
    else:
        alias.symlink_to(chosen, target_is_directory=True)
    assert _direct_windows_python(root) == root / "py/python.exe"
    assert not alias.exists() and not alias.is_symlink() and not chosen.exists()
    assert (root / "py/python.exe").read_text() == "fixture"


def test_failed_python_download_with_alias_rolls_back_without_traversing_twice(tmp_path, monkeypatch):
    import daemons_companion.runtime.environment as env
    from daemons_companion.runtime.bootstrap import HostCommandError
    runtime = _runtime(tmp_path)
    layout, base = _environment_layout(tmp_path, runtime, target_os="windows")
    before = _tree_state(runtime.companion_home)
    aliases = set()
    original_rmdir = Path.rmdir
    if os.name != "nt":
        monkeypatch.setattr(env, "_is_junction", lambda p: p in aliases)
        monkeypatch.setattr(Path, "rmdir", lambda p: p.unlink() if p in aliases else original_rmdir(p))

    def runner(command, environment):
        result = base(command, environment)
        if list(command)[1:3] == ["python", "install"]:
            root = Path(environment["UV_PYTHON_INSTALL_DIR"])
            alias = root / "cpython-3.12-windows-x86_64-none"
            target = root / "cpython-3.12.13-windows-x86_64-none"
            if os.name == "nt":
                subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(alias), str(target)], check=True, capture_output=True)
            else:
                alias.symlink_to(target, target_is_directory=True)
                aliases.add(alias)
            raise HostCommandError(2, "injected failure after Python alias creation")
        return result

    with pytest.raises(TransactionApplyError):
        _execute(tmp_path, CompanionRuntimeEnvironmentAdapter(layout, runner=runner))
    assert _tree_state(runtime.companion_home) == before


def test_windows_startup_path_guard_counts_utf16_units():
    from pathlib import PureWindowsPath
    from daemons_companion.runtime.environment import _validate_windows_startup_path
    _validate_windows_startup_path(PureWindowsPath("C:/private/env"))
    with pytest.raises(ValueError, match="too long"):
        _validate_windows_startup_path(PureWindowsPath("C:/") / ("x" * 220))
    with pytest.raises(ValueError, match="too long"):
        _validate_windows_startup_path(PureWindowsPath("C:/") / ("😀" * 110))


@pytest.mark.parametrize("relocate", [False, True])
def test_windows_launcher_home_resolves_after_publication(tmp_path, monkeypatch, relocate):
    import daemons_companion.runtime.environment as env
    runtime = _runtime(tmp_path)
    layout, fake = _environment_layout(tmp_path, runtime, target_os="windows")
    launches = []

    def launcher(command, environment):
        if command[0].endswith("python.exe"):
            config = Path(command[0]).parent.parent / "pyvenv.cfg"
            home = next(line.split("=", 1)[1].strip() for line in config.read_text().splitlines()
                        if line.startswith("home ="))
            found = (Path(home) / "python.exe").is_file()
            launches.append(found)
            if not found:
                return CommandResult(1, stderr="No Python at configured home")
        return fake(command, environment)

    if not relocate:
        monkeypatch.setattr(env, "_relocate_windows_python_home", lambda *args: None)
        with pytest.raises(TransactionApplyError):
            _execute(tmp_path, CompanionRuntimeEnvironmentAdapter(layout, runner=launcher))
        assert launches == [True, False]
    else:
        _execute(tmp_path, CompanionRuntimeEnvironmentAdapter(layout, runner=launcher))
        assert launches == [True, True]
        final = layout.environment_path(resolve_current_release(runtime).name)
        assert f"home = {final / 'python/py'}" in (final / "venv/pyvenv.cfg").read_text()
        assert "relocatable = true" in (final / "venv/pyvenv.cfg").read_text()


@pytest.mark.parametrize("invalid", ["missing", "duplicate", "external"])
def test_windows_publication_refuses_unexpected_python_home(tmp_path, invalid):
    runtime = _runtime(tmp_path)
    layout, fake = _environment_layout(tmp_path, runtime, target_os="windows")
    before = _tree_state(runtime.companion_home)
    external = tmp_path / "unrelated-python"
    external.mkdir()
    (external / "python.exe").write_text("preserve")

    def mutate(boundary):
        if boundary != "verified":
            return
        stage = layout.staging_path(resolve_current_release(runtime).name)
        config = stage / "venv/pyvenv.cfg"
        if invalid == "missing":
            config.write_text("relocatable = true\n")
        elif invalid == "duplicate":
            config.write_text(config.read_text() + f"home = {external}\n")
        else:
            config.write_text(f"home = {external}\n")

    with pytest.raises(TransactionApplyError):
        _execute(tmp_path, CompanionRuntimeEnvironmentAdapter(layout, runner=fake, mutation_hook=mutate))
    assert _tree_state(runtime.companion_home) == before
    assert (external / "python.exe").read_text() == "preserve"


def test_windows_hook_precedes_uv_inspection_and_survives_publication(tmp_path):
    runtime = _runtime(tmp_path)
    layout, fake = _environment_layout(tmp_path, runtime, target_os="windows")
    checked = []
    def runner(command, environment):
        if command[1:3] == ['pip', 'sync'] or command[0].endswith('python.exe'):
            executable = Path(command[command.index('--python')+1] if '--python' in command else command[0])
            directory = executable.parent.parent / 'Lib/site-packages'
            assert (directory / '000_daemons_paths.pth').read_text() == 'import _daemons_windows_paths; _daemons_windows_paths.enable()\n'
            assert (directory / '_daemons_windows_paths.py').is_file()
            checked.append(str(executable))
        return fake(command, environment)
    _execute(tmp_path, CompanionRuntimeEnvironmentAdapter(layout, runner=runner))
    assert len(checked) == 3
    assert '.env-' in checked[0] and '/environments/' in checked[-1]
