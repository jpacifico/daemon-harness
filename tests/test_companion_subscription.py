from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from daemons_companion.setup.subscription import PrivateSubscriptionSetup, SubscriptionStatus
from tests.test_companion_host_install import fixture, execute
from daemons_companion.runtime.host_install import installed_host


class Native:
    def __init__(self, *, fail=None):
        self.codex = False
        self.hermes = False
        self.fail = fail
        self.calls = []

    def __call__(self, command, environment, *, interactive=False):
        self.calls.append((command, environment, interactive))
        if interactive:
            kind = "hermes" if "auth" in command else "codex"
            if self.fail == kind:
                return SimpleNamespace(returncode=1, stdout="", stderr="")
            setattr(self, kind, True)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command[-2:] == ["login", "status"]:
            return SimpleNamespace(returncode=0 if self.codex else 1, stdout="",
                                   stderr="Logged in using ChatGPT" if self.codex else "Not logged in")
        return SimpleNamespace(returncode=0, stderr="", stdout="DAEMONS_AUTH_READY" if self.hermes else "DAEMONS_AUTH_MISSING")


def operation(tmp_path, *, fail=None, target_os="macos"):
    layout, runner, adapter = fixture(tmp_path, target_os=target_os)
    execute(layout, adapter)
    codex = tmp_path / "codex"
    codex.write_text("#!/bin/sh\nexit 0\n")
    codex.chmod(0o700)
    native = Native(fail=fail)
    setup = PrivateSubscriptionSetup(layout, codex=codex, runner=native,
                                    host_reader=lambda layout: installed_host(layout, runner=runner))
    return setup, native


def test_native_signins_are_separate_private_and_resumable(tmp_path, monkeypatch):
    setup, native = operation(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "personal-secret")
    monkeypatch.setenv("CODEX_HOME", "/personal/codex")
    monkeypatch.setenv("HERMES_HOME", "/personal/hermes")
    assert setup.sign_in(lambda _: None) == SubscriptionStatus(True, True)
    signins = [call for call in native.calls if call[2]]
    assert len(signins) == 2
    assert signins[0][0][-1] == "login"
    assert signins[1][0][1:] == ["auth", "add", "openai-codex", "--browser"]
    assert signins[0][1]["CODEX_HOME"] != signins[1][1]["CODEX_HOME"]
    for command, env, _ in native.calls:
        assert "personal-secret" not in env.values()
        assert Path(env["HOME"]).is_relative_to(setup.layout.attempt)
        assert Path(env["HERMES_HOME"]).is_relative_to(setup.layout.attempt)
        assert Path(env["CODEX_HOME"]).is_relative_to(setup.layout.attempt)
        assert "gateway" not in command and "exec" not in command
    setup.sign_in(lambda _: None)
    assert len([c for c in native.calls if c[2]]) == 2
    assert not (setup.layout.profile / "auth.json").exists()  # the installer never writes native secrets


@pytest.mark.parametrize("failure", ["codex", "hermes"])
def test_cancelled_native_signin_is_not_success_and_retains_completed_step(tmp_path, failure):
    setup, native = operation(tmp_path, fail=failure)
    with pytest.raises(RuntimeError, match="cancelled or failed"):
        setup.sign_in(lambda _: None)
    assert native.codex is (failure == "hermes")
    assert not native.hermes
    native.fail = None
    assert setup.sign_in(lambda _: None).hermes
    assert len([c for c in native.calls if c[2] and "auth" not in c[0]]) == (2 if failure == "codex" else 1)


def test_wrong_managed_profile_never_invokes_auth(tmp_path):
    setup, native = operation(tmp_path)
    setup.layout = replace(setup.layout, installation_id="b" * 32)
    with pytest.raises(ValueError, match="exact active"):
        setup.sign_in(lambda _: None)
    assert native.calls == []


def test_changed_codex_executable_never_invokes_auth(tmp_path):
    setup, native = operation(tmp_path)
    setup.codex.write_text("changed")
    with pytest.raises(ValueError, match="Codex changed"):
        setup.sign_in(lambda _: None)
    assert native.calls == []


@pytest.mark.parametrize("name", [".codex", ".hermes-codex"])
def test_personal_auth_link_is_refused_before_native_command(tmp_path, name):
    setup, native = operation(tmp_path)
    personal = tmp_path / "personal"
    personal.mkdir()
    (setup.layout.attempt / "os-home" / name).symlink_to(personal, target_is_directory=True)
    with pytest.raises(ValueError, match="link"):
        setup.sign_in(lambda _: None)
    assert native.calls == []


def test_api_login_does_not_count_as_subscription(tmp_path):
    setup, native = operation(tmp_path)
    native.codex = True
    original = setup.runner
    def runner(command, env, **kwargs):
        result = original(command, env, **kwargs)
        if command[-2:] == ["login", "status"]:
            result.stderr = "Logged in using an API key"
        return result
    setup.runner = runner
    assert not setup.status().codex


def test_linked_hermes_configuration_is_refused_before_login(tmp_path):
    setup, native = operation(tmp_path)
    personal = tmp_path / "personal-config"
    personal.write_text("keep")
    (setup.layout.profile / "config.yaml").symlink_to(personal)
    with pytest.raises(ValueError, match="links"):
        setup.sign_in(lambda _: None)
    assert native.calls == []
    assert personal.read_text() == "keep"


def test_native_zero_exit_without_credentials_still_refuses_continuation(tmp_path):
    setup, native = operation(tmp_path)
    original = setup.runner
    calls = []
    def quit_without_auth(command, environment, *, interactive=False):
        if interactive:
            calls.append(command)
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        return original(command, environment)
    setup.runner = quit_without_auth
    with pytest.raises(RuntimeError, match='sign-in was not completed'):
        setup.sign_in(lambda _: None)
    assert not native.codex and not native.hermes
    assert len(calls) == 1 and calls[0][-1] == 'login'


def test_windows_browser_uses_user_profile_only_during_foreground_signin(tmp_path, monkeypatch):
    locations = {'USERPROFILE': '/synthetic/browser-user', 'APPDATA': '/synthetic/browser-roaming',
                 'LOCALAPPDATA': '/synthetic/browser-local'}
    for key, value in locations.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv('OPENAI_API_KEY', 'personal-key')
    monkeypatch.setenv('CODEX_HOME', '/personal/codex')
    monkeypatch.setenv('HERMES_HOME', '/personal/hermes')
    setup, native = operation(tmp_path, target_os='windows')
    setup.sign_in(lambda _: None)
    for _, environment, interactive in native.calls:
        for key, value in locations.items():
            if interactive:
                assert environment[key] == value
            else:
                assert Path(environment[key]).is_relative_to(setup.layout.attempt)
        assert environment['HOME'] == str(setup.layout.attempt / 'os-home')
        assert Path(environment['CODEX_HOME']).is_relative_to(setup.layout.attempt)
        assert Path(environment['HERMES_HOME']).is_relative_to(setup.layout.attempt)
        assert 'OPENAI_API_KEY' not in environment


def test_signin_checks_animate_but_foreground_oauth_keeps_terminal_ownership(tmp_path):
    from contextlib import contextmanager
    setup, native = operation(tmp_path)
    active = []
    phases = []
    @contextmanager
    def activity(label):
        assert not active
        active.append(label)
        phases.append(label)
        try:
            yield
        finally:
            active.clear()
    def run(command, environment, *, interactive=False):
        assert bool(active) is not interactive
        return native(command, environment, interactive=interactive)
    setup.runner = run
    assert setup.sign_in(lambda _: None, activity=activity) == SubscriptionStatus(True, True)
    assert len(phases) == 3 and not active
