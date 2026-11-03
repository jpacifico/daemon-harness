import argparse
import hashlib
import json
from pathlib import Path


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


def summarize(root):
    observations = []
    for run in sorted(p for p in root.iterdir() if p.is_dir()):
        if not (run / 'voice.json').exists():
            continue
        voice = json.loads((run / 'voice.json').read_text())
        native = voice['result']
        final = json.loads((run / 'result.json').read_text()) if (run / 'result.json').exists() else {}
        capture_path = run / 'first-request.json'
        capture = json.loads(capture_path.read_text()) if capture_path.exists() else {}
        catalog = [line[2:].split(' | ')[0] for value in strings(capture.get('request', {}).get('body', {}))
                   for line in value.splitlines() if line.startswith('- theme/')]
        calls = []
        reads = []
        for message in native.get('messages', []):
            for call in message.get('tool_calls', []):
                function = call.get('function', {})
                arguments = function.get('arguments', '{}')
                calls.append({'name': function.get('name'),
                              'arguments': json.loads(arguments) if isinstance(arguments, str) else arguments})
            if message.get('role') == 'tool':
                try:
                    result = json.loads(message.get('content', '{}'))
                except (ValueError, TypeError):
                    reads.append({'name': message.get('name'), 'unparsed': True})
                    continue
                reads.append({'name': message.get('name'), 'status': result.get('status'),
                    'paths': [{'path': item['path'], 'content_sha256': hashlib.sha256(item.get('content', '').encode()).hexdigest(),
                               'chars': len(item.get('content', ''))} for item in result.get('capsules', [])],
                    'rejected_paths': result.get('rejected_paths', []), 'total_chars': result.get('total_chars')})
        telemetry = rows(run / 'llm.jsonl')
        usage = [row.get('provider_usage') for row in telemetry]
        latency = rows(run / 'latency.jsonl')
        background_tokens = sum(item.get('total_tokens', 0) for item in usage if item is not None)
        before = json.loads((run / 'snapshots/before/manifest.json').read_text())
        souvenir_fingerprints = {key: value['sha256'] for key, value in before.items() if key.startswith('capsules/')}
        observation = {'id': run.name, 'case': run.name.rsplit('-', 1)[0], 'arm': run.name.rsplit('-', 1)[1],
            'source': final.get('source'), 'fixture_sha256': final.get('fixture_sha256'),
            'souvenir_corpus_sha256': hashlib.sha256(json.dumps(souvenir_fingerprints, sort_keys=True).encode()).hexdigest(),
            'souvenir_count': len(souvenir_fingerprints), 'prompt': voice['prompt'], 'answer': native.get('final_response'),
            'catalog': catalog, 'tools': calls, 'tool_results': reads,
            'source_paths_read': sorted({item['path'] for read in reads for item in read.get('paths', [])}),
            'api_calls': native.get('api_calls'), 'native_completed': native.get('completed'),
            'response_seconds': voice['response_seconds'], 'initialization_seconds': voice['initialization_seconds'],
            'conversation_seconds': voice['response_seconds'] - voice['initialization_seconds'],
            'first_delta_seconds': voice['first_delta_seconds'],
            'first_compose_seconds': next((row.get('compose') for row in latency if row.get('event') == 'voice_start'), None),
            'voice_usage': {key: native.get(key) for key in ('input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_write_tokens', 'reasoning_tokens', 'total_tokens')},
            'voice_usage_source': 'Hermes native result, 0.21.5-f97608f',
            'background_calls': len(telemetry), 'background_tokens_reported': background_tokens,
            'background_usage_missing': sum(item is None for item in usage),
            'background_provider_errors': [row.get('error_type') for row in telemetry if row.get('status') != 'ok'],
            'background_usage_source': 'Codex CLI provider_usage, 0.159.2',
            'embedding_usage': None, 'telegram_transport': None,
            'complete': final.get('complete'), 'after_turn_seconds': final.get('after_turn_seconds'),
            'lifecycle': final.get('lifecycle'), 'changed_paths': final.get('changed_paths'),
            'capture_metadata': capture.get('metadata'), 'preflight': final.get('preflight'),
            'benchmark_observer_load_timeout': any('productivity_benchmark_observer' in line and 'load timed out' in line
                for line in (run / 'runtime.log').read_text().splitlines()),
            'error_type': final.get('error_type')}
        observations.append(observation)
    return {'schema': 'daemon.retrieval-crowding.v1', 'language': 'fr-FR', 'observations': observations}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('root', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = summarize(args.root)
    if args.output:
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    for row in result['observations']:
        print(json.dumps({key: row[key] for key in ('id', 'answer', 'source_paths_read', 'api_calls',
             'response_seconds', 'first_delta_seconds', 'background_calls', 'complete', 'error_type')}, ensure_ascii=False))
