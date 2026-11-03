"""Regression checks for isolated first-meeting qualification and host handoff."""
import importlib.util
from types import SimpleNamespace

from scripts.qualify_first_meeting_native import exact_user_counts


def test_counter_matches_exact_user_turns_not_assistant_quotes(tmp_path):
    conversations = tmp_path / 'levels/L0_ground_zero/conversations'
    conversations.mkdir(parents=True)
    (conversations / 'test.md').write_text(
        '## 2026-10-20T12:00:00 — user\nSalut !\n\n'
        '## 2026-10-20T12:00:01 — assistant\nSalut ! Contente de te retrouver.\n\n'
        '## 2026-10-20T12:00:02 — user\nSalut ! Autre chose.\n'
    )
    assert exact_user_counts(tmp_path, ['Salut !', 'Missing']) == [1, 0]


def test_gateway_onboarding_sidecar_does_not_replace_exact_user_handoff(monkeypatch, tmp_path):
    import daemon_core.turn_handoff as handoff
    import daemon_core.turn_runtime as runtime
    from scripts.qualify_first_meeting_native import SOURCE

    spec = importlib.util.spec_from_file_location(
        'first_meeting_handoff_voice_test',
        SOURCE / 'integrations/hermes_voice_plugin/__init__.py',
    )
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)
    monkeypatch.setenv('DAEMON_CORE_SOVEREIGN_VOICE', 'on')
    monkeypatch.setenv('DAEMON_CORE_SOVEREIGN_PLATFORMS', 'telegram')
    monkeypatch.setenv('DAEMON_CORE_USER_ID', '999000112233')
    monkeypatch.setenv('DAEMON_CORE_SRC', str(SOURCE / 'src'))
    monkeypatch.setattr(handoff, 'MEMORY_ROOT', tmp_path)
    handoff.write_turn_handoff('999000112233', 'Salut !', 'meeting-trace', 1.0)
    requests = []

    def compose(request):
        requests.append(request)
        return SimpleNamespace(
            voice_context=SimpleNamespace(
                atlas=SimpleNamespace(souvenir_names=(), rendered_chars=0),
                hot_memory=SimpleNamespace(souvenir_names=(), rendered_chars=0),
                scene_model=None, scene_delta='',
            ),
            hot_souvenirs=(), voice_path='canonical', prompt_variant='thin',
            situation_source='current', memory_source='atlas',
            leading_system_prompt='Canonical first meeting',
            turns=[{'role': 'user', 'content': request.message}],
        )

    monkeypatch.setattr(runtime, 'prepare_turn', compose)
    tools = [{'type': 'function', 'name': 'daemon_read_souvenirs'}]
    payload = {
        'instructions': 'Native host context',
        'input': [{'role': 'user', 'content': [{'type': 'input_text', 'text':
            'Salut !\n\n[System note: First contact. Offer profile building and mention /help.]'}]}],
        'tools': tools, 'tool_choice': 'auto',
    }
    rewritten = plugin._delegated_request_or_none(
        payload, platform='telegram', api_mode='codex_responses',
        api_call_count=1, turn_id='isolated-first-meeting',
    )
    assert requests[0].message == 'Salut !'
    assert rewritten['input'] == [{'role': 'user', 'content': [{'type': 'input_text', 'text': 'Salut !'}]}]
    assert rewritten['instructions'] == 'Canonical first meeting'
    assert rewritten['tools'] == tools
    assert handoff.read_turn_handoff('999000112233', 'Salut !')[0] == 'Salut !'
