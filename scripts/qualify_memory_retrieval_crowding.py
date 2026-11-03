#!/usr/bin/env python3
"""Isolated French retrieval audit, not a production or public V1 gate."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import hashlib
import json
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / 'docs/evaluations/fixtures/memory_retrieval_crowding_fr.json'
USER = 'synthetic-retrieval-crowding'
CASES = ('old_open', 'resolved', 'other_person', 'ambiguous')
MAX_BACKGROUND_CALLS = 12


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + '\n')


def environment(args):
    run = args.output.resolve() / f'{args.case}-{args.arm}'
    sys.path[:0] = [str(REPO), str(REPO / 'src')]
    from benchmarks.productivity_v1.runner.common import isolated_daemon_env, durable_after_turn_env
    values = durable_after_turn_env(isolated_daemon_env(
        {}, run_root=run, memory_root=run / 'memory', user_id=USER, platform='telegram',
        telemetry_path=run / 'llm.jsonl', latency_path=run / 'latency.jsonl'), user_id=USER)
    values.update({
        'DAEMON_CORE_SRC': str(REPO / 'src'),
        'DAEMON_CORE_VOICE_CAPTURE_FILE': str(REPO / 'exports/private' / args.output.name / f'{args.case}-{args.arm}.json'),
        'DAEMON_CORE_SHARED_WORLD_STATE': 'on',
        'DAEMON_CORE_SCENE_MODEL_SHADOW_PATH': str(run / 'scene-shadow.jsonl'),
        'DAEMON_CORE_NARRATIVE_AUTHORITY_HIERARCHY': 'on',
        'DAEMON_CORE_NARRATIVE_AUTHORITY_HIERARCHY_USER_IDS': USER,
        'DAEMON_CORE_NARRATIVE_AUTHORITY_HIERARCHY_PLATFORMS': 'telegram,openclaw',
        'DAEMON_CORE_CURRENT_TOPICS': 'canary',
        'DAEMON_CORE_CURRENT_TOPICS_USER_IDS': USER,
        'DAEMON_CORE_TEMPORAL_CURATOR': 'on',
        'DAEMON_CORE_TEMPORAL_CURATOR_USER_IDS': USER,
        'DAEMON_CORE_ATLAS_EMBEDDING_TOP12': 'canary' if args.arm == 'top12' else 'off',
        'DAEMON_CORE_ATLAS_EMBEDDING_TOP12_USER_IDS': USER,
        'DAEMON_CORE_ATLAS_EMBEDDING_MODEL': 'qwen3-embedding:0.6b',
        'DAEMON_CORE_ATLAS_EMBEDDING_URL': 'http://127.0.0.1:11434',
        'CODEX_HOME': str(run / 'codex-home'),
        'CODEX_CLI_BIN': str(args.codex_bin.resolve()),
        'CODEX_CLI_MODEL': 'gpt-6-astra',
        'CODEX_CLI_TIMEOUT_SECONDS': '180',
        'LLM_PROVIDER': 'codex_cli',
        'CONVERSATION_REASONING_EFFORT': 'medium',
        'ROUTING_REASONING_EFFORT': 'medium',
        'MEMORY_REASONING_EFFORT': 'high',
    })
    for key in tuple(os.environ):
        if key.startswith(('DAEMON_CORE_', 'LIQUID_HARNESS_', 'CODEX_CLI_', 'HERMES_',
                           'MEMORY_', 'ROUTING_', 'CONVERSATION_', 'TELEGRAM_', 'DISCORD_',
                           'SLACK_', 'WHATSAPP_', 'SIGNAL_')):
            os.environ.pop(key)
    os.environ.update(values)
    profile = run / 'hermes-home'
    os.environ.update({'HERMES_HOME': str(profile), 'HERMES_CONFIG': str(profile / 'config.yaml'),
                       'HERMES_ENV': str(profile / '.env'), 'PRODUCTIVITY_BENCH_TRACE_FILE': str(run / 'host-trace.jsonl'),
                       'PYTHONDONTWRITEBYTECODE': '1'})
    from daemon_core.paths import MEMORY_ROOT
    assert MEMORY_ROOT == run / 'memory'
    return run, values


def fixture_case(case_id):
    fixture = json.loads(FIXTURE.read_text())
    assert fixture['user_id'] == USER
    return fixture, next(case for case in fixture['cases'] if case['id'] == case_id)


def seed(run, fixture, case):
    root = run / 'memory'
    entries = []
    for slug, tags, body in fixture['vision_notes']:
        body += '\n' + case['appends'].get(slug, '')
        entries.append((f'health/vision/{slug}', tags, body.strip()))
    for slug, tags, body in fixture['health_notes']:
        entries.append((f'health/{slug}', f'Camille, santé, {tags}', '2026-09-01 : ' + body))
    for group, bodies in fixture['background_groups'].items():
        for index, body in enumerate(bodies):
            entries.append((f'{group}/note_{index + 1:02d}', f'Camille, {group}', f'2026-0{index % 8 + 1}-15 : {body}'))
    assert len(entries) == 96
    for slug, tags, body in entries:
        path = root / 'capsules/theme' / (slug + '.md')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'---\nsubjects: [{tags}]\nfacets: [personal-history]\n---\n\n# {slug.rsplit("/", 1)[-1]}\n\n{body}\n')
    from daemon_core.memory.writer import append_ground_zero
    # Historical fixture evidence is retained, but outside all injected recent windows.
    for slug, _tags, body in entries[:16]:
        append_ground_zero(USER, 'user', body, occurred_at=datetime(2026, 9, 30, 10).timestamp())
    now = datetime.now()
    for index in range(16):
        moment = (now - timedelta(days=3) + timedelta(hours=index * 3)).timestamp()
        append_ground_zero(USER, 'user', f'J’ai dessiné {index + 1} petits croquis dans mon carnet.', occurred_at=moment)
        append_ground_zero(USER, 'assistant', 'Une page qui se remplit.', occurred_at=moment + 1)
    append_ground_zero(USER, 'user', 'J’ai rangé mon carnet de croquis.', occurred_at=time.time() - 600)
    append_ground_zero(USER, 'assistant', 'À la prochaine page.', occurred_at=time.time() - 599)
    from daemon_core.memory.reader import load_recent_dialogue
    from daemon_core.scene_model import SceneSnapshot, build_scene_sources, scene_model_path, _render_markdown
    sources = build_scene_sources(recent_dialogue=load_recent_dialogue(USER, max_turns=2))
    turns = [source for source in sources.values() if source.kind == 'l0_turn']
    watermark = turns[-1]
    scene = SceneSnapshot(scene_scope='world', source_trace_id='synthetic-drawing',
        source_turn_started_at=time.time() - 600, revised_at=time.time() - 590, revision=1,
        provider={'kind': 'controlled-fixture'}, dialogue_watermark_source_id=watermark.source_id,
        dialogue_watermark_locator=watermark.locator, conversation='Le carnet de croquis est rangé.',
        sources=tuple(turns))
    path = scene_model_path(USER, 'world')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_render_markdown(scene))


def snapshot(run, label):
    destination = run / 'snapshots' / label
    destination.mkdir(parents=True, exist_ok=False)
    manifest = {}
    for path in sorted((run / 'memory').rglob('*.md')):
        relative = path.relative_to(run / 'memory')
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        content = path.read_bytes()
        target.write_bytes(content)
        manifest[str(relative)] = {'sha256': hashlib.sha256(content).hexdigest(), 'bytes': len(content)}
    write_json(destination / 'manifest.json', manifest)
    return manifest


def seed_situation_note(run, fixture_path, case_id):
    """Optional preregistered synthetic note; never edits the 96 source notes."""
    if fixture_path is None:
        return None
    fixture = json.loads(fixture_path.read_text())
    assert fixture['user_id'] == USER
    note = fixture['cases'][case_id]
    relative = Path(note['path'])
    if relative.is_absolute() or '..' in relative.parts or relative.parts[0] != 'theme':
        raise ValueError('Situation note must be a relative thematic path')
    target = run / 'memory/capsules' / relative.with_suffix('.md')
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open('x') as handle:
        handle.write(note['content'])
    return hashlib.sha256(fixture_path.read_bytes()).hexdigest()


def worker(args):
    run, _ = environment(args)
    from daemon_core.after_turn_service import AfterTurnService
    from daemon_core.loop import DaemonCore
    from daemon_core.providers.factory import build_conversation_provider, build_memory_provider, build_routing_provider
    from integrations.after_turn_service.hermes_observer import HermesAfterTurnObserver
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(name)s %(levelname)s %(message)s')
    lock = threading.Lock()
    counter = [0]

    class Captured:
        def __init__(self, wrapped, faculty):
            self.wrapped, self.faculty = wrapped, faculty

        def __getattr__(self, name):
            return getattr(self.wrapped, name)

        def complete(self, messages, temperature=0.4):
            with lock:
                if counter[0] >= MAX_BACKGROUND_CALLS:
                    raise RuntimeError('Registered background call bound reached')
                counter[0] += 1
                number = counter[0]
            path = run / 'calls' / f'{number:03d}.json'
            payload = {'faculty': self.faculty, 'messages': messages}
            write_json(path, payload)
            started = time.perf_counter()
            try:
                payload['response'] = self.wrapped.complete(messages, temperature=temperature)
                return payload['response']
            except Exception as error:
                payload['error_type'] = type(error).__name__
                raise
            finally:
                payload['seconds'] = time.perf_counter() - started
                write_json(path, payload)

    def build():
        return DaemonCore(provider=Captured(build_conversation_provider(), 'conversation'),
            routing_provider=Captured(build_routing_provider(), 'routing'),
            memory_provider=Captured(build_memory_provider(), 'memory'))

    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    AfterTurnService(user_ids=(USER,), harness_factory=build,
        batch_observers={'hermes': HermesAfterTurnObserver(USER, hermes_home=str(run / 'hermes-home'))}).run(stop)


def stop_process(process, group=False):
    if process is None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM) if group else process.terminate()
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL) if group else process.kill()
        process.wait()


def child(args):
    run, values = environment(args)
    from benchmarks.productivity_v1.runner.common import (source_revision, prepare_codex_cli_home,
        wait_for_after_turn_service, wait_for_durable_after_turn)
    from benchmarks.productivity_v1.runner import hermes
    revision = source_revision(REPO)
    if revision['dirty'] and not args.preflight_only:
        raise RuntimeError('Commit the runner and protocol before live voice calls')
    run.mkdir(parents=True, exist_ok=False)
    (run / 'empty.env').touch()
    logging.basicConfig(filename=run / 'runtime.log', level=logging.INFO,
                        format='%(asctime)s %(name)s %(levelname)s %(message)s', force=True)
    fixture, case = fixture_case(args.case)
    seed(run, fixture, case)
    situation_hash = seed_situation_note(run, args.situation_fixture, args.case)
    before = snapshot(run, 'before')
    outcome = {'case': args.case, 'arm': args.arm, 'source': revision,
               'fixture_sha256': hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
               'situation_fixture_sha256': situation_hash}
    process = None
    log = None
    try:
        if args.arm == 'top12':
            from daemon_core.semantic_atlas import prewarm_semantic_atlas_cache
            outcome['embedding_prewarm'] = prewarm_semantic_atlas_cache(run / 'memory')
        from daemon_core.canonical_voice import compose_canonical_voice_messages
        from daemon_core.voice_brief_renderer import authority_hierarchy_canary_enabled
        preflight = {}
        for platform in ('telegram', 'openclaw'):
            started = time.perf_counter()
            messages, context = compose_canonical_voice_messages(USER, case['prompt'], platform=platform)
            serialized = json.dumps(messages, ensure_ascii=False)
            checks = {'scene_usable': context.situation_source == 'scene',
                'no_active_context': not context.active_context.strip(),
                'no_target_fact_leak': all(value not in serialized for value in
                    ('deux tentatives infructueuses', 'les irritations ont disparu', 'une nouvelle paire qui me convient')),
                'catalog_size': len(context.atlas.souvenir_names) == (12 if args.arm == 'top12' else 96 + bool(situation_hash)),
                'authority_renderer': authority_hierarchy_canary_enabled(USER, platform)}
            preflight[platform] = {'checks': checks, 'seconds': time.perf_counter() - started,
                                  'catalog': context.atlas.souvenir_names}
            write_json(run / f'composition-{platform}.json', {'messages': messages, **preflight[platform]})
        outcome['preflight'] = preflight
        if not all(value for entry in preflight.values() for value in entry['checks'].values()):
            raise RuntimeError('Fixture failed before any live voice call')
        if args.preflight_only:
            outcome['preflight_complete'] = True
            return
        prepare_codex_cli_home(run / 'codex-home')
        hermes._prepare_profile(source_home=args.hermes_home, profile=run / 'hermes-home',
            arm='C', model='gpt-6-astra', provider='openai-codex', daemon_values=values)
        from hermes_cli.runtime_provider import resolve_runtime_provider
        from run_agent import AIAgent
        runtime = resolve_runtime_provider(requested='openai-codex')
        deltas = []
        started = time.perf_counter()
        agent = AIAgent(model='gpt-6-astra', provider=runtime['provider'], api_mode=runtime['api_mode'],
            base_url=runtime.get('base_url'), api_key=runtime.get('api_key'), user_id=USER,
            platform='telegram', session_id=f'isolated-crowding-{args.case}-{args.arm}',
            reasoning_config={'enabled': True, 'effort': 'high'}, enabled_toolsets=['memory'],
            max_iterations=5, run_budget_seconds=180, quiet_mode=True,
            skip_background_review=True, skip_context_files=True,
            stream_delta_callback=lambda text: deltas.append((time.perf_counter() - started, text)) if text else None)
        assert {'daemon_read_souvenirs', 'daemon_recall_l0'} <= agent.valid_tool_names
        initialized = time.perf_counter() - started
        result = agent.run_conversation(user_message=case['prompt'], conversation_history=[])
        elapsed = time.perf_counter() - started
        agent.shutdown_memory_provider(result.get('messages', []))
        voice = {'result': result, 'response_seconds': elapsed, 'initialization_seconds': initialized,
                 'first_delta_seconds': deltas[0][0] if deltas else None, 'prompt': case['prompt']}
        write_json(run / 'voice.json', voice)
        print(json.dumps({'case': args.case, 'arm': args.arm, 'voice_seconds': round(elapsed, 3),
                          'answer': result.get('final_response')}, ensure_ascii=False), flush=True)
        captured = json.loads(Path(values['DAEMON_CORE_VOICE_CAPTURE_FILE']).read_text())
        outcome['capture_metadata'] = captured['metadata']
        assert captured['metadata']['prompt_variant'] == 'voice_brief_authority_hierarchy_v3'
        assert captured['metadata']['situation_source'] == 'scene'
        effective = json.dumps(captured['request']['body'], ensure_ascii=False)
        assert all(value not in effective for value in ('deux tentatives infructueuses', 'les irritations ont disparu'))
        # Keep a copy of the verified first request with the disposable artifacts.
        write_json(run / 'first-request.json', captured)
        log = (run / 'worker.log').open('w')
        process = subprocess.Popen(child_command(args) + ['--worker'], cwd=run,
            env=os.environ.copy(), stdout=log, stderr=subprocess.STDOUT)
        started = time.perf_counter()
        wait_for_after_turn_service(process=process, spool_root=run / 'after-turn-spool', log_path=run / 'worker.log')
        lifecycle = wait_for_durable_after_turn(process=process, spool_root=run / 'after-turn-spool',
            receipt_path=run / 'continuity-receipts/continuity_receipts.jsonl', telemetry_path=run / 'llm.jsonl',
            host='hermes', user_id=USER, expected_turns=1, timeout_seconds=360)
        outcome.update(lifecycle=lifecycle, after_turn_seconds=time.perf_counter() - started)
        outcome['complete'] = bool(result.get('completed') and not result.get('failed')
            and lifecycle['complete'] and not any(lifecycle['spool_counts'].values()))
    except Exception as error:
        outcome.update(complete=False, error_type=type(error).__name__, error=str(error))
        raise
    finally:
        stop_process(process)
        if log:
            log.close()
        for auth in (run / 'hermes-home/auth.json', run / 'codex-home/auth.json'):
            auth.unlink(missing_ok=True)
        after = snapshot(run, 'after')
        outcome['changed_paths'] = sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))
        write_json(run / 'result.json', outcome)
        print(json.dumps({'case': args.case, 'arm': args.arm, 'complete': outcome.get('complete'),
                          'error_type': outcome.get('error_type')}, ensure_ascii=False), flush=True)


def child_command(args):
    return [str(args.hermes_source / '.venv/bin/python'), str(Path(__file__).resolve()),
        '--hermes-source', str(args.hermes_source), '--hermes-home', str(args.hermes_home),
        '--codex-bin', str(args.codex_bin), '--output', str(args.output), '--case', args.case, '--arm', args.arm
    ] + (['--situation-fixture', str(args.situation_fixture.resolve())] if args.situation_fixture else [])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('hermes-source', 'hermes-home', 'codex-bin', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--case', choices=CASES)
    parser.add_argument('--arm', choices=('top12', 'full'))
    parser.add_argument('--child', action='store_true')
    parser.add_argument('--worker', action='store_true')
    parser.add_argument('--preflight-only', action='store_true')
    parser.add_argument('--situation-fixture', type=Path,
        help='Optional preregistered synthetic situation-note fixture (source notes remain identical)')
    args = parser.parse_args()
    if Path('/private/tmp') not in args.output.resolve().parents:
        raise ValueError('Run artifacts must be isolated under /private/tmp')
    os.umask(0o077)
    if args.worker:
        worker(args)
        return
    if args.child:
        child(args)
        return
    selected_cases, selected_arm = ([args.case] if args.case else CASES), args.arm
    args.output.mkdir(parents=True, exist_ok=True)
    for case in selected_cases:
        arms = [selected_arm] if selected_arm else (('top12', 'full') if CASES.index(case) % 2 == 0 else ('full', 'top12'))
        for arm in arms:
            args.case, args.arm = case, arm
            if (args.output / f'{case}-{arm}').exists():
                raise FileExistsError('Refusing to overwrite or rerun an existing observation')
            command = child_command(args) + ['--child'] + (['--preflight-only'] if args.preflight_only else [])
            process = None
            try:
                with (args.output / f'{case}-{arm}.log').open('w') as log:
                    process = subprocess.Popen(command, cwd=args.output,
                        env={**os.environ, 'PYTHONPATH': str(args.hermes_source), 'PYTHONDONTWRITEBYTECODE': '1'},
                        stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                    code = process.wait(timeout=650)
                print(json.dumps({'case': case, 'arm': arm, 'exit_code': code}), flush=True)
                if code:
                    raise RuntimeError('Observation failed; artifacts preserved, no automatic reroll')
            finally:
                stop_process(process, group=True)
                for auth in ('hermes-home/auth.json', 'codex-home/auth.json'):
                    (args.output / f'{case}-{arm}' / auth).unlink(missing_ok=True)


if __name__ == '__main__':
    main()
