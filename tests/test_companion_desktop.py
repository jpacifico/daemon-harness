import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from daemons_companion.desktop.protocol import DesktopUI, SCHEMA, allowed_link
from daemons_companion.desktop.memory import initialize_empty_vault
from daemon_core.memory.library_layout import FOLDERS, vault_root


def frame(**kwargs):
    return json.dumps({'schema': SCHEMA, 'type': 'answer', **kwargs}) + '\n'


def transport(raw, prompt):
    output = io.StringIO()
    ui = DesktopUI(io.StringIO(raw), output)
    ui._cancel = lambda: setattr(ui, 'cancelled', True)
    ui.pending = prompt
    ui._read()
    return ui, [json.loads(line) for line in output.getvalue().splitlines()]


def test_answers_are_single_use_and_never_echoed():
    prompt = {'id': 7, 'kind': 'secret', 'maximum': 256}
    raw = frame(id=6, action='submit', value='stale-secret')
    raw += frame(id=7, action='submit', value='private-token') * 2
    ui, messages = transport(raw, prompt)
    assert ui.queue.get_nowait() == ('submit', 'private-token')
    assert ui.queue.empty()
    assert len(messages) == 2
    assert 'private-token' not in json.dumps(messages)
    assert 'stale-secret' not in json.dumps(messages)


@pytest.mark.parametrize('value', [True, -1, 2, '1', None, {'command': 'something'}])
def test_choice_bounds(value):
    ui, messages = transport(frame(id=1, action='submit', value=value),
                             {'id': 1, 'kind': 'choice', 'options': ['A', 'B']})
    assert ui.queue.empty()
    assert messages[0]['type'] == 'input-rejected'


@pytest.mark.parametrize('value', ['', ' ', 'A\nB', 'x' * 257])
def test_field_bounds(value):
    ui, _ = transport(frame(id=1, action='submit', value=value),
                      {'id': 1, 'kind': 'secret', 'maximum': 256})
    assert ui.queue.empty()


def test_invalid_protocol_and_disconnect_cancel_without_echo():
    ui, messages = transport('{"secret":"do-not-print"}\n' + 'x' * 65537, None)
    assert ui.cancelled
    assert 'do-not-print' not in json.dumps(messages)


@pytest.mark.parametrize('url', ['file:///tmp/x', 'javascript:alert(1)', 'https://auth.openai.com.evil.test/',
                                  'https://x@auth.openai.com/', 'https://auth.openai.com:444/', 'http://t.me/bot'])
def test_link_allowlist(url):
    assert not allowed_link(url)


def test_fresh_vault_and_reopen(tmp_path, monkeypatch):
    root = initialize_empty_vault(tmp_path / 'memory')
    assert vault_root(root) == root.parent
    assert sorted(p.name for p in root.parent.iterdir() if not p.name.startswith('.')) == sorted(FOLDERS)
    (root.parent / 'Projects' / 'My project.md').write_text('Human-owned note')
    assert initialize_empty_vault(root.parent) == root
    assert (root.parent / 'Projects' / 'My project.md').read_text() == 'Human-owned note'
    from daemon_core.memory.context_pages import structure_enabled
    monkeypatch.setenv('DAEMON_CORE_MEMORY_STRUCTURE', 'v1')
    assert structure_enabled(root)


def test_existing_memory_is_never_migrated(tmp_path):
    root = tmp_path / 'memory'
    (root / 'capsules').mkdir(parents=True)
    (root / 'capsules' / 'one.md').write_text('Existing facts')
    with pytest.raises(ValueError, match='separate migration'):
        initialize_empty_vault(root)
    assert not (root / '.daemon').exists()
    assert (root / 'capsules' / 'one.md').read_text() == 'Existing facts'


def test_interrupted_empty_vault_initialization_is_resumable(tmp_path):
    root = initialize_empty_vault(tmp_path / 'memory')
    (root / 'vault-layout.json').unlink()
    assert initialize_empty_vault(root.parent) == root
    assert vault_root(root) == root.parent


def test_cancel_after_commit_reports_retained_services(monkeypatch):
    from types import SimpleNamespace
    import daemons_companion.desktop.protocol as module
    from daemons_companion.desktop.terminal import Quit
    def cancel(*args, **kwargs):
        raise Quit()
    monkeypatch.setattr(module, 'run_wizard', cancel)
    output = io.StringIO()
    ui = DesktopUI(io.StringIO(), output)
    assert module.serve(ui, SimpleNamespace(backend=None, daemon_installed=True)) == 130
    event = json.loads(output.getvalue())
    assert event['result'] == 'installed'
    assert 'keeps running' in event['message']
    assert 'still needs confirmation' in event['message']


def test_desktop_never_falls_back_to_manual_model_or_telegram_without_auth():
    from types import SimpleNamespace
    from daemons_companion.desktop.terminal import Quit, run_wizard
    from tests.test_companion_terminal import FakeUI, backend
    owner = SimpleNamespace(context=SimpleNamespace(target_os='macos'),
                            offer=lambda *args: False, connect_subscription=lambda *args: None)
    ui = FakeUI(choices=[0, 0, 0, Quit()], fields=['Aster'])
    with pytest.raises(Quit):
        run_wizard(ui, backend=backend(), host_setup=owner, supported_only=True)
    assert not any('Model identifier' in item or 'Review details' in item for item in ui.output)
    assert 'Complete your private Hermes installation and model connection before continuing.' in ui.output


def test_cancel_reaches_idle_native_subprocess_and_cleanup(tmp_path):
    # Real pipes and a real child. The fixture runs no services or network.
    script = '''
import sys
from pathlib import Path
from daemons_companion.desktop.protocol import DesktopUI
from daemons_companion.runtime.bootstrap import run
ui=DesktopUI(sys.stdin,sys.stdout)
ui.start()
try:
    ui.emit('activity',active=True,message='waiting')
    run([sys.executable,'-c','import time; time.sleep(60)'],dict(__import__('os').environ),timeout=60)
except KeyboardInterrupt:
    Path(sys.argv[1]).write_text('cleanup completed')
    ui.emit('finished',result='cancelled')
finally:
    ui.finished=True
'''
    marker = tmp_path / 'cleanup'
    child = subprocess.Popen([sys.executable, '-u', '-c', script, str(marker)],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')}, text=True)
    try:
        assert json.loads(child.stdout.readline())['type'] == 'activity'
        time.sleep(.2)
        started = time.monotonic()
        child.stdin.write(json.dumps({'schema': SCHEMA, 'type': 'cancel'}) + '\n')
        child.stdin.flush()
        output, error = child.communicate(timeout=12)
        assert child.returncode == 0, error
        assert marker.read_text() == 'cleanup completed'
        assert json.loads(output)['result'] == 'cancelled'
        assert time.monotonic() - started < 10
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)


@pytest.mark.parametrize('code', [0, 1])
def test_a_browser_sign_in_ends_its_link_whatever_its_outcome(tmp_path, code):
    output = io.StringIO()
    ui = DesktopUI(io.StringIO(), output)
    url = 'https://auth.openai.com/oauth/authorize?state=synthetic'
    command = [sys.executable, '-c', f'import sys; print({url!r}); sys.exit({code})']
    result = ui.subscription_runner(command, {**os.environ, 'HOME': str(tmp_path)}, interactive=True)
    assert result.returncode == code
    events = [json.loads(line) for line in output.getvalue().splitlines()]
    types = [event['type'] for event in events]
    assert types[0] == 'connection-start' and types[-1] == 'sign-in-ended'
    assert {'type': 'link', 'url': url}.items() <= next(e for e in events if e['type'] == 'link').items()
    # The link was seen while the sign-in ran, never after it ended.
    assert types.index('link') < types.index('sign-in-ended')


def test_activity_and_secret_prompt_share_real_transport():
    output = io.StringIO()
    ui = DesktopUI(io.StringIO(), output)
    result = []
    thread = threading.Thread(target=lambda: result.append(ui.secret('Token')))
    thread.start()
    end = time.monotonic() + 2
    while ui.pending is None and time.monotonic() < end:
        time.sleep(.01)
    assert ui.pending['kind'] == 'secret'
    ui.queue.put(('submit', 'secret-value'))
    thread.join(timeout=2)
    assert result == ['secret-value']
    assert 'secret-value' not in output.getvalue()


def test_explicit_browser_action_is_distinct_from_displayed_links():
    output = io.StringIO()
    ui = DesktopUI(io.StringIO(), output)
    ui.say('Open https://t.me/SyntheticDaemon_bot')
    links = [json.loads(line) for line in output.getvalue().splitlines()
             if json.loads(line)['type'] == 'link']
    assert [item['url'] for item in links] == [
        'https://web.telegram.org/k/#@SyntheticDaemon_bot',
        'https://t.me/SyntheticDaemon_bot']
    assert all(not item.get('open') for item in links)
    assert ui.open_link('https://web.telegram.org/')
    assert json.loads(output.getvalue().splitlines()[-1])['open'] is True
    assert not ui.open_link('file:///tmp/private')


def test_prompt_guidance_contains_only_current_step():
    output = io.StringIO()
    ui = DesktopUI(io.StringIO(), output)
    ui.say('Old step')
    ui.queue.put(('submit', 0))
    ui.choose('First', ['Continue'])
    ui.say('Current step')
    ui.queue.put(('submit', 0))
    ui.choose('Second', ['Continue'])
    prompts = [json.loads(line) for line in output.getvalue().splitlines()
               if json.loads(line)['type'] == 'prompt']
    assert prompts[0]['guidance'] == ['Old step']
    assert prompts[1]['guidance'] == ['Current step']


@pytest.mark.parametrize("detected,version,status", [
    (False, "", "unavailable"), (True, "99.0.0", "verified"),
    (True, "", "unavailable"), (True, "0.21.5", "verified"),
])
@pytest.mark.parametrize("target_os", ["macos", "windows"])
def test_offer_installs_qualified_host_without_mutating_external_profile(tmp_path, detected, version, status, target_os):
    from types import SimpleNamespace
    from tests.test_companion_host_install import fixture, execute_host_install, installed_host
    from tests.test_companion_terminal import FakeUI
    from daemons_companion.desktop.host_terminal import PackagedHostSetup
    from daemons_companion.runtime.acquisition import review_host_setup
    layout, runner, adapter = fixture(tmp_path, target_os)
    external = tmp_path / "existing-profile"
    external.mkdir()
    original = b"model: external\n# preserve settings, credentials and services\n"
    (external / "config.yaml").write_bytes(original)
    context = SimpleNamespace(companion_home=layout.companion_home, uv_executable=layout.uv_executable,
                              uv_sha256=layout.uv_sha256, target_os=target_os, validate=lambda: None)
    review = review_host_setup(host="hermes", detected=detected, usable=detected,
                              version=version, version_status=status, platform=target_os)
    calls = []
    def install(actual, **kwargs):
        assert actual == layout
        calls.append(actual)
        return execute_host_install(actual, adapter=adapter, confirmed_digest=kwargs['confirmed_digest'])
    owner = PackagedHostSetup(context, execute=install)
    owner.layout = lambda: layout
    ui = FakeUI(choices=[1, 1] if detected else [1])
    ui.allow_preview = False
    assert owner.offer(ui, "hermes", review)
    assert len(calls) == 1
    assert installed_host(layout, runner=runner).version == "0.21.5"
    assert (external / "config.yaml").read_bytes() == original
    assert sorted(p.name for p in external.iterdir()) == ["config.yaml"]
    assert all(str(external) not in str(command) for command, _ in runner.calls)


def test_incompatible_host_back_does_not_download_or_continue(tmp_path):
    from types import SimpleNamespace
    from tests.test_companion_host_install import fixture
    from tests.test_companion_terminal import FakeUI
    from daemons_companion.desktop.host_terminal import PackagedHostSetup
    from daemons_companion.desktop.terminal import Back
    layout, _, _ = fixture(tmp_path)
    context = SimpleNamespace(companion_home=layout.companion_home, uv_executable=layout.uv_executable,
                              uv_sha256=layout.uv_sha256, target_os="windows", validate=lambda: None)
    calls = []
    owner = PackagedHostSetup(context, execute=lambda *a, **k: calls.append(a))
    ui = FakeUI(choices=[0])
    ui.allow_preview = False
    with pytest.raises(Back):
        owner.offer(ui, "hermes", {"action": "review_existing"})
    assert calls == [] and not layout.companion_home.exists()

@pytest.mark.parametrize('encoding', ['ascii', 'cp1252', 'utf-8'])
def test_daemon_settings_protocol_preserves_unicode_with_legacy_stdout(tmp_path, monkeypatch, encoding):
    # -I ignores PYTHONUTF8; Windows Python 3.12 may expose a legacy stdout.
    import io
    import sys
    from types import SimpleNamespace
    from daemons_companion.desktop.daemon_settings import main
    from daemon_core.daemon_identity import save_daemon_identity
    save_daemon_identity(tmp_path, 'Étoile', 'feminine')
    source = str(Path(__file__).resolve().parents[1] / 'src')
    request = {'action': 'inspect', 'root': str(tmp_path), 'source': source, 'binding': None}
    raw = io.BytesIO()
    outgoing = io.TextIOWrapper(raw, encoding=encoding, write_through=True)
    with monkeypatch.context() as scoped:
        scoped.setattr(sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(json.dumps(request).encode('utf-8'))))
        scoped.setattr(sys, 'stdout', outgoing)
        main()
    payload = raw.getvalue()
    assert payload.isascii()
    assert json.loads(payload.decode('utf-8'))['settings']['values']['name'] == 'Étoile'


def test_user_name_does_not_ask_for_pronouns(monkeypatch):
    ui = DesktopUI(io.StringIO(), io.StringIO())
    monkeypatch.setattr(ui, 'field', lambda *args, **kwargs: 'Morgan')
    monkeypatch.setattr(ui, 'choose', lambda *args, **kwargs: pytest.fail('Unexpected pronoun question'))
    preferences = ui.user_preferences()
    assert preferences.preferred_name == 'Morgan' and preferences.pronouns == ''


@pytest.mark.parametrize('previous_text', ['they/them', 'elle / iel'])
def test_removed_preset_and_custom_wording_survive_resumed_onboarding(monkeypatch, previous_text):
    from daemon_core.onboarding_user import UserPreferences
    ui = DesktopUI(io.StringIO(), io.StringIO())
    monkeypatch.setattr(ui, 'field', lambda label, previous='', **kwargs: previous)
    def choose(title, options, default=0):
        assert len(options) == 4 and default == 3
        return default
    monkeypatch.setattr(ui, 'choose', choose)
    saved = UserPreferences('Morgan', previous_text)
    assert ui.user_preferences(saved) == saved


@pytest.mark.parametrize('error', [KeyError('synthetic-secret'), TypeError('synthetic-secret'), StopIteration('synthetic-secret')])
def test_unexpected_desktop_failure_is_framed_without_secret(monkeypatch, error):
    from types import SimpleNamespace
    import daemons_companion.desktop.protocol as module
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(module, 'run_wizard', fail)
    output = io.StringIO()
    ui = DesktopUI(io.StringIO(), output)
    assert module.serve(ui, SimpleNamespace(backend=None, daemon_installed=False)) == 1
    assert ui.finished
    event = json.loads(output.getvalue())
    assert event['type'] == 'finished' and event['result'] == 'error'
    assert event['error_code'] == type(error).__name__
    assert 'synthetic-secret' not in output.getvalue()


@pytest.mark.parametrize("phase", ["host", "identity", "model", "telegram", "installation", "conversation"])
def test_activity_failures_report_bounded_codes_in_every_phase(phase):
    output = io.StringIO()
    ui = DesktopUI(io.StringIO(), output)
    ui.phase(phase)
    with pytest.raises(FileNotFoundError):
        with ui.activity("Synthetic check"):
            raise FileNotFoundError(2, "secret-token /private/account")
    events = [json.loads(line) for line in output.getvalue().splitlines()]
    diagnostic = [e for e in events if e["type"] == "diagnostic"]
    assert diagnostic[-1]["status"] == "failed"
    assert diagnostic[-1]["error_code"] == "FileNotFoundError"
    assert diagnostic[-1]["errno"] == 2
    assert "secret-token" not in output.getvalue()
    assert "/private/account" not in output.getvalue()


def test_diagnostic_unwraps_host_timeout_without_private_output():
    from daemons_companion.runtime.bootstrap import HostCommandTimeout
    from daemons_companion.setup.transaction import TransactionApplyError
    output = io.StringIO()
    ui = DesktopUI(io.StringIO(), output)
    try:
        try:
            raise HostCommandTimeout(1800, "private-native-output")
        except HostCommandTimeout as cause:
            raise TransactionApplyError("rollback wrapper") from cause
    except TransactionApplyError as error:
        ui.diagnostic("host-download", "failed", error)
    event = json.loads(output.getvalue())
    assert event["error_code"] == "HostCommandTimeout"
    assert event["timeout_seconds"] == 1800
    assert "private-native-output" not in output.getvalue()


def test_openclaw_acquisition_failure_keeps_stage_and_numeric_details():
    output = io.StringIO()
    ui = DesktopUI(io.StringIO(), output)
    error = OSError(2, 'secret path or message')
    error.host_stage, error.winerror = 'node-extract', 206
    ui.diagnostic('host-download', 'failed', error)
    frame = json.loads(output.getvalue())
    assert frame['host_stage'] == 'node-extract'
    assert frame['winerror'] == 206 and frame['errno'] == 2
    assert 'secret' not in output.getvalue()


def test_add_host_diagnostic_keeps_class_names_and_refusal_codes_only():
    from daemons_companion.hosts.attachment_command import Refused
    output = io.StringIO()
    ui = DesktopUI(io.StringIO(), output)
    ui.diagnostic("source-check", "failed", Refused("upgrade"))
    try:
        try:
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "private-text")
        except UnicodeDecodeError as cause:
            raise RuntimeError("private wrapper") from cause
    except RuntimeError as error:
        ui.diagnostic("host-prepare", "failed", error)

    class Leak(Exception):
        pass

    ui.diagnostic("add-host", "failed", Leak("private"))
    refused, decoded, unknown = [json.loads(line) for line in output.getvalue().splitlines()]
    assert (refused["error_code"], refused["refusal"]) == ("Refused", "upgrade")
    assert (decoded["exception_type"], decoded["error_code"]) == ("RuntimeError", "UnicodeDecodeError")
    assert unknown["error_code"] == unknown["exception_type"] == "UnexpectedError"
    assert "private" not in output.getvalue()


def test_back_on_the_first_add_host_screen_cancels(monkeypatch):
    from daemons_companion.desktop import protocol
    from daemons_companion.desktop.terminal import Back
    from daemons_companion.hosts import wizard

    def back(*_args):
        raise Back()

    monkeypatch.setattr(wizard, "run", back)
    output = io.StringIO()
    ui = DesktopUI(io.StringIO(), output)
    request = {"source_profile": "/owned/profile", "base": "/owned", "name": "Aster"}
    assert protocol.serve_host_addition(ui, request) == 130
    finished = json.loads(output.getvalue().splitlines()[-1])
    assert finished["type"] == "finished" and finished["result"] == "cancelled"


ENGINE = r"""
import subprocess, sys, threading, time
from daemons_companion.desktop.protocol import private_input
incoming = private_input(sys.stdin)
got = []
reader = threading.Thread(target=lambda: got.append(incoming.readline()))
reader.start()
time.sleep(0.5)
start = time.monotonic()
child = subprocess.run([sys.executable, '-c', 'import sys; print(repr(sys.stdin.read()))'],
                       capture_output=True, text=True, timeout=30)
print(child.stdout.strip(), round(time.monotonic() - start, 1), flush=True)
reader.join(30)
print(got[0].strip() if got else 'no frame', flush=True)
"""


@pytest.mark.skipif(os.name != 'nt', reason='Windows standard input inheritance')
def test_engine_children_never_inherit_the_answer_pipe(tmp_path):
    # Field, 2026-10-25: while the reader waited in readline() on the pipe from
    # Electron, every OpenClaw CLI probe that inherited it hung idle until its
    # timeout. Children now get NUL, and answers still reach the reader.
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, 'PYTHONPATH': os.pathsep.join((str(root), str(root / 'src')))}
    engine = subprocess.Popen([sys.executable, '-c', ENGINE], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              text=True, env=env, creationflags=subprocess.CREATE_NO_WINDOW)
    lines = []
    reading = threading.Thread(target=lambda: lines.extend(iter(engine.stdout.readline, '')), daemon=True)
    reading.start()
    try:
        deadline = time.monotonic() + 60
        while not lines and time.monotonic() < deadline:
            time.sleep(0.1)
        # Without the fix the grandchild blocks on the inherited pipe.
        assert lines, 'a child of the engine blocked on the answer pipe'
        first = lines[0].split()
        assert first[0] == "''" and float(first[1]) < 25
        engine.stdin.write('answer\n')
        engine.stdin.flush()
        reading.join(30)
        assert lines[1].strip() == 'answer'
    finally:
        engine.kill()
        engine.communicate()
