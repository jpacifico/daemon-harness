from __future__ import annotations

import json
from urllib.request import Request, urlopen

from scripts.qualification.companion_telegram_fixture import (
    TelegramFixture, TOKEN, USER_ID, model_completion,
)


def test_loopback_bot_api_delivers_and_acknowledges_numeric_user():
    fixture = TelegramFixture()
    fixture.start()

    def call(method, values):
        request = Request(f'http://127.0.0.1:{fixture.port}/bot{TOKEN}/{method}',
                          data=json.dumps(values).encode(), headers={'Content-Type': 'application/json'})
        with urlopen(request, timeout=2) as response:
            return json.loads(response.read())

    try:
        assert call('getMe', {})['result']['is_bot'] is True
        assert call('deleteMyCommands', {})['result'] is True
        fixture.submit('Synthetic greeting.')
        updates = call('getUpdates', {})['result']
        assert updates[0]['message']['from']['id'] == USER_ID
        assert call('getUpdates', {'offset': 2})['result'] == []
        assert call('sendMessage', {'chat_id': USER_ID, 'text': 'Synthetic reply.'})['result']['text'] == 'Synthetic reply.'
        assert fixture.sent == ['Synthetic reply.']
        call('sendMessage', {'chat_id': USER_ID, 'text': r'HERMES\_E2E\_OK', 'parse_mode': 'MarkdownV2'})
        assert fixture.displayed[-1] == 'HERMES_E2E_OK'
        fixture.submit('Synthetic queued message.')
        call('deleteWebhook', {'drop_pending_updates': False})
        assert call('getUpdates', {})['result']
        call('deleteWebhook', {'drop_pending_updates': True})
        assert call('getUpdates', {})['result'] == []
        assert fixture.dropped_counts == [1]
        assert call('unexpectedMethod', {})['ok'] is False
    finally:
        fixture.stop()


def test_model_fixture_satisfies_real_scene_contract_with_source_accounting():
    from daemon_core.scene_model import (
        SceneSource, build_scene_model_messages, parse_scene_revision,
    )
    source = SceneSource('synthetic-turn', 'l0_turn', '2026-10-06 — user', 'abc', 'Synthetic greeting.')
    sources = {source.source_id: source}
    messages = build_scene_model_messages(previous=None, sources=sources,
        allowed_souvenirs=[], souvenir_contents={}, source_accounting_ids=[source.source_id])
    completion = {'choices': [{'message': {'content': 'visible reply'}}]}
    raw = model_completion({'messages': messages}, completion)['choices'][0]['message']['content']
    patch, dependency, reasons = parse_scene_revision(raw, previous=None,
        allowed_sources=set(sources), allowed_souvenirs=[], authoritative_sources=set(),
        temporal_source_ids=set(), current_topics_enabled=False,
        user_turn_sources=set(sources), required_source_accounting=set(sources))
    assert patch is not None and dependency is not None, reasons
    assert len(dependency.source_dispositions) == 1


def test_model_fixture_satisfies_native_bootstrap_selection():
    from daemon_core.scene_model import (
        SceneSource, build_scene_bootstrap_messages, parse_scene_bootstrap_selection,
    )
    source = SceneSource('synthetic-turn', 'l0_turn', '2026-10-06 — user', 'abc', 'Synthetic greeting.')
    messages = build_scene_bootstrap_messages({source.source_id: source})
    completion = {'choices': [{'message': {'content': 'visible reply'}}]}
    raw = model_completion({'messages': messages}, completion)['choices'][0]['message']['content']
    selected = parse_scene_bootstrap_selection(raw, allowed_sources={source.source_id})
    assert selected is not None and selected.source_ids == (source.source_id,)


def test_start_fixture_carries_native_telegram_command_entity():
    fixture = TelegramFixture()
    try:
        fixture.submit('/start')
        message = fixture.updates[0]['message']
        assert message['entities'] == [{'type': 'bot_command', 'offset': 0, 'length': 6}]
        fixture.submit('Hello')
        assert 'entities' not in fixture.updates[1]['message']
    finally:
        fixture.server.server_close()
