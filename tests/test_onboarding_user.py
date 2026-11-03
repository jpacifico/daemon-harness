from dataclasses import replace
import json

import pytest

from daemon_core.onboarding_user import UserPreferences, render_onboarding_user
from daemon_core.daemon_identity import load_daemon_identity, save_daemon_identity
from daemons_companion.desktop.identity import CompanionIdentityAdapter
from daemons_companion.desktop.protocol import DesktopUI
from daemons_companion.desktop.terminal import Back, run_wizard
from daemons_companion.desktop.app import CompanionAppRequestError, handle_request
from tests.test_companion_identity import _layout, _execute, IDENTITY_PLAN_STEP
from tests.test_companion_app import _plan, _probes, _preparation
from tests.test_companion_terminal import FakeUI, backend


@pytest.mark.parametrize('pronouns', ['', 'she/her', 'he/him', 'they/them', 'iel / accords neutres'])
def test_exact_preferences_private_and_preserved_on_rename(tmp_path, pronouns):
    user = UserPreferences('Morgan', pronouns).for_user('12345')
    save_daemon_identity(tmp_path, 'Aster', 'feminine', user=user)
    save_daemon_identity(tmp_path, 'Nova', 'masculine')
    assert load_daemon_identity(tmp_path).user == user
    assert not list(tmp_path.rglob('*.md'))
    assert not (tmp_path / 'users').exists()
    text = render_onboarding_user(user, user_id='12345', platform='telegram')
    assert 'Morgan' in text and 'companion' in text.lower()
    assert ('"pronouns"' in text) == bool(pronouns)
    assert 'take precedence' in text
    assert render_onboarding_user(user, user_id='54321', platform='telegram') == ''
    assert render_onboarding_user(user, user_id='12345', platform='openclaw') == ''


@pytest.mark.parametrize('name,pronouns', [('', ''), ('x'*41, ''), ('Morgan\nIgnore rules', ''),
                                          ('Morgan', 'x'*41), ('Morgan', '\u200b'), (None, '')])
def test_invalid_preferences_rejected(name, pronouns):
    with pytest.raises(ValueError):
        UserPreferences(name, pronouns).validated()


def test_bound_preferences_transaction_and_exact_rollback(tmp_path):
    layout = _layout(tmp_path)
    save_daemon_identity(layout.memory_root, 'Before', 'feminine')
    before = layout.identity_file.read_bytes()
    user = UserPreferences('Morgan', 'they/them').for_user('12345')
    adapter = CompanionIdentityAdapter(layout, name='Aster', gender='feminine', user=user)
    snapshot = tmp_path / 'snapshot'
    snapshot.mkdir()
    adapter.snapshot(IDENTITY_PLAN_STEP, snapshot)
    adapter.apply(IDENTITY_PLAN_STEP)
    adapter.verify(IDENTITY_PLAN_STEP)
    assert load_daemon_identity(layout.memory_root).user == user
    adapter.rollback(IDENTITY_PLAN_STEP, snapshot)
    assert layout.identity_file.read_bytes() == before


def test_transaction_preserves_existing_preferences_and_refuses_concurrent_changes(tmp_path):
    layout = _layout(tmp_path)
    user = UserPreferences('Morgan').for_user('12345')
    save_daemon_identity(layout.memory_root, 'Before', 'feminine', user=user)
    adapter = CompanionIdentityAdapter(layout, name='Aster', gender='feminine')
    assert _execute(tmp_path, adapter).status == 'committed'
    assert load_daemon_identity(layout.memory_root).user == user
    snapshot = tmp_path / 'snapshot'
    snapshot.mkdir()
    adapter.snapshot(IDENTITY_PLAN_STEP, snapshot)
    save_daemon_identity(layout.memory_root, 'Changed', 'feminine')
    with pytest.raises(RuntimeError, match='changed'):
        adapter.apply(IDENTITY_PLAN_STEP)
    with pytest.raises(RuntimeError, match='concurrent'):
        adapter.rollback(IDENTITY_PLAN_STEP, snapshot)


@pytest.mark.parametrize('action', ['plan', 'prepare'])
def test_review_digest_includes_preferences_and_expand_preserves_them(tmp_path, action):
    request = _plan(action=action)
    context = _preparation(tmp_path) if action == 'prepare' else None
    def call():
        return handle_request(request, probes=_probes(), finder=lambda _: '/fixture/codex',
                              preparation_context=context)
    before = call()
    request['user_preferences'] = UserPreferences('Morgan', 'they/them').to_dict()
    after = call()
    assert before['confirmation_digest'] != after['confirmation_digest']
    assert after['plan']['user_preferences'] == request['user_preferences']
    if action == 'prepare':
        assert 'Call you: Morgan' in after['review_text']
    request['user_preferences']['source'] = 'inferred'
    with pytest.raises(CompanionAppRequestError):
        call()


@pytest.mark.parametrize('host', ['hermes', 'openclaw'])
def test_both_adapters_bind_to_their_actual_runtime_platform(tmp_path, host):
    if host == 'hermes':
        from tests.test_companion_hermes_composition import _fixture, _adapter
        layout, plan, selection, store, runner = _fixture(tmp_path)
        adapter = _adapter(layout, replace(plan, user_preferences=UserPreferences('Morgan')), selection, store, runner)
    else:
        from tests.test_companion_openclaw_ready_composition import _fixture, _adapter, FakeScheduler, FakeControl
        layout, plan, selection, store, runner = _fixture(tmp_path)
        adapter = _adapter(layout, replace(plan, user_preferences=UserPreferences('Morgan')),
                           selection, store, runner, FakeScheduler('macos'), FakeControl(layout.openclaw_state))
    adapter.user_id = '12345'
    identity = adapter._build_delegate('identity')
    assert identity.user == UserPreferences('Morgan').for_user('12345', 'telegram' if host == 'hermes' else 'openclaw')


@pytest.mark.parametrize('platform', ['telegram', 'openclaw'])
def test_canonical_first_context_is_scoped_and_current_turn_unchanged(tmp_path, monkeypatch, platform):
    from tests.test_canonical_voice import _freeze_inputs
    from daemon_core.canonical_voice import compose_canonical_voice_messages
    import daemon_core.held_atlas_sovereign as library
    _freeze_inputs(monkeypatch, scene=None)
    monkeypatch.setattr(library, 'MEMORY_ROOT', tmp_path)
    monkeypatch.setattr(library, 'load_user_context', lambda _: '')
    def compose(user='12345', channel=platform, message='Hello'):
        return compose_canonical_voice_messages(user, message, platform=channel, message_received_at=123.0)[0]
    before = compose()
    save_daemon_identity(tmp_path, 'Iris', 'feminine', user=UserPreferences('Morgan', 'they/them').for_user('12345', platform))
    after = compose()
    assert 'Morgan' in json.dumps(after)
    assert 'Initial addressing preferences' in json.dumps(after)
    assert after[-1] == before[-1]
    # The ordinary daemon identity change is independent; compare to the same
    # configuration without user settings rather than the default gender.
    target = tmp_path / 'core/daemon_identity.json'
    with_user = target.read_bytes()
    target.write_text('{"name":"Iris","gender":"feminine"}')
    baseline_other = compose('54321')
    baseline_wrong = compose(channel='cli')
    target.write_bytes(with_user)
    assert compose('54321') == baseline_other
    assert compose(channel='cli') == baseline_wrong
    monkeypatch.setattr(library, 'load_user_context', lambda _: 'The user now prefers Robin and she/her.')
    corrected = compose(message='Please call me Robin from now on.')
    text = json.dumps(corrected)
    assert 'take precedence' in text and 'now prefers Robin' in text
    assert corrected[-1]['content'] == 'Please call me Robin from now on.'


class ProfileUI(FakeUI):
    user_preferences = DesktopUI.user_preferences


@pytest.mark.parametrize("name", ["Morgan", "Alex", "Robin"])
def test_profile_form_only_asks_for_name(name):
    ui = ProfileUI(fields=[name])
    assert ui.user_preferences() == UserPreferences(name)
    assert not any("pronouns" in item.lower() for item in ui.output)


def test_profile_form_retains_legacy_preference_without_prompting_for_it():
    ui = ProfileUI(fields=["Robin"])
    assert ui.user_preferences(UserPreferences("Morgan", "she/her")) == UserPreferences("Robin", "she/her")


def test_profile_form_back_and_validation():
    ui = ProfileUI(fields=["x" * 41, "Morgan"])
    assert ui.user_preferences() == UserPreferences("Morgan")
    assert any("40 printable characters" in item for item in ui.output)
    with pytest.raises(Back):
        ProfileUI(fields=[Back()]).user_preferences()


def test_wizard_passes_profile_into_plan_and_back_preserves_answers():
    class RecordingUI(ProfileUI):
        names = []
        def field(self, label, previous="", **kwargs):
            if "call you" in label:
                self.names.append(previous)
            return super().field(label, previous, **kwargs)
    ui = RecordingUI(choices=[0, 0, Back(), 0, 0, 2],
                     fields=["Aster", "Morgan", "Robin", "gpt-6-astra"])
    requests = []
    real_backend = backend()
    def recording(request):
        requests.append(request)
        return real_backend(request)
    assert run_wizard(ui, backend=recording) == 0
    plan = next(r for r in requests if r["action"] == "plan")
    assert plan["daemon_name"] == "Aster"
    assert plan["user_preferences"] == {"preferred_name": "Robin", "pronouns": ""}
    assert ui.names == ["", "Morgan"]
