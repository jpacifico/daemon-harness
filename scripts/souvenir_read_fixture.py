"""Synthetic tool-call fixture for opt-in native-host qualification only."""
from __future__ import annotations

import copy
import json
from pathlib import Path

PATH = 'theme/qualification_souvenir'
MARKER = 'SYNTHETIC_SOUVENIR_TOOL_PROOF_20261002'


def seed(memory_root: Path) -> None:
    target = memory_root / 'capsules' / (PATH + '.md')
    index = memory_root / 'capsules/index.md'
    if target.exists() or index.exists():
        raise RuntimeError('Synthetic seed requires an empty disposable memory.')
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('# Capsule: ' + PATH + '\n\n## Faits\n- ' + MARKER + '\n')
    index.write_text('- ' + PATH + ': Isolated qualification fact.\n')


def tool_round_trip_seen(requests: list[dict]) -> bool:
    return any(message.get('role') == 'tool' and MARKER in json.dumps(message)
               for request in requests for message in request.get('messages', []))


def completion(request: dict, terminal: dict, *, enabled: bool) -> dict:
    if not enabled:
        return terminal
    tool_names = {item.get('function', {}).get('name') for item in request.get('tools', [])}
    if 'daemon_read_souvenirs' not in tool_names:
        return terminal  # Internal maintenance requests are not voice tool requests.
    if tool_round_trip_seen([request]):
        return terminal
    result = copy.deepcopy(terminal)
    result['choices'][0]['message'] = {
        'role': 'assistant', 'content': None,
        'tool_calls': [{'id': 'souvenir-read-proof', 'type': 'function', 'function': {
            'name': 'daemon_read_souvenirs', 'arguments': json.dumps({
                'paths': [PATH], 'scene': {
                    'established': ['This is an isolated synthetic qualification.'],
                    'uncertain': [], 'need': 'Verify the synthetic fact.',
                    'handling': {'mode': 'direct', 'plan': 'Read the exact synthetic source.'},
                },
            }),
        }}],
    }
    result['choices'][0]['finish_reason'] = 'tool_calls'
    return result


def stream_chunks(terminal_chunks: tuple, completed: dict) -> tuple:
    if completed['choices'][0]['finish_reason'] != 'tool_calls':
        return terminal_chunks
    chunks = copy.deepcopy(terminal_chunks)
    calls = completed['choices'][0]['message']['tool_calls']
    chunks[0]['choices'][0]['delta'] = {'role': 'assistant', 'tool_calls': [
        {**call, 'index': index} for index, call in enumerate(calls)
    ]}
    chunks[-1]['choices'][0]['finish_reason'] = 'tool_calls'
    return chunks
