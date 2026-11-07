#!/usr/bin/env python3
"""Four bounded native root-folder observations, synthetic data, no real channel."""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / 'docs/evaluations/fixtures/memory_root_folders_en.json'
USER = 'synthetic-openclaw-memory-p1'
HERMES = Path.home() / '.local/share/daemon-harness/host-tools/hermes/0.21.5-f97608f'
CODEX = Path.home() / '.local/share/daemon-harness/host-tools/codex/0.159.2/bin/codex'


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + '\n')
    path.chmod(0o600)


def convert_fixture(root):
    from daemon_core.memory.root_migration import migrate_copy
    seed = root / 'legacy-memory-fixture'
    (root / 'memory').rename(seed)
    migrate_copy(seed, root / 'memory', {
        'theme/work/harbor-workshop': 'Projects/Work/Harbor Workshop.md'})
    memory = root / 'memory/.daemon'
    moved = root / 'memory/Areas/Work/Harbor Workshop renamed.md'
    moved.parent.mkdir(parents=True)
    (root / 'memory/Projects/Work/Harbor Workshop.md').rename(moved)
    return memory, moved


def assess(root, result, source):
    sys.path[:0] = [str(source), str(source / 'src')]
    from benchmarks.productivity_v1.runner.common import read_jsonl
    fixture = json.loads(FIXTURE.read_text())
    root_layout = (root / 'memory/.daemon/vault-layout.json').is_file()
    memory = root / ('memory/.daemon' if root_layout else 'memory')
    if root_layout:
        from daemon_core.memory.library_layout import unique_note_locations, resolve_note
        notes = [item.path for item in unique_note_locations(memory)]
        original = resolve_note(memory, 'theme/work/harbor-workshop')
    else:
        notes = list((memory / 'capsules/theme').rglob('*.md'))
        original = memory / 'capsules/theme/work/harbor-workshop.md'
    material = [p for p in notes if all(t in p.read_text().lower() for t in fixture['new_fact_terms'])]
    receipts = read_jsonl(root / 'continuity-receipts/continuity_receipts.jsonl')
    if (root / 'voice.json').exists():
        voice = json.loads((root / 'voice.json').read_text())
        response_ok = bool(voice.get('completed') and not voice.get('failed'))
        memory_read = any(call.get('function', {}).get('name') == 'daemon_read_souvenirs'
                          for message in voice.get('messages', []) for call in message.get('tool_calls', []))
        answer = str(voice.get('final_response', voice.get('response', '')))
    else:
        response_ok = result.get('returncode') == 0
        memory_read = bool(result.get('checks', {}).get('native_memory_tool_called'))
        answer = result.get('answer', '')
    checks = {
        'native_response_completed': response_ok,
        'native_memory_read': memory_read,
        'exact_source_recalled': 'reclaimed maple' in answer.lower(),
        'separate_new_project_saved': any(p != original for p in material),
        'old_history_preserved': all(fact in original.read_text() for fact in fixture['preserved_facts']),
        'new_existing_note_fact_saved': any('deliver' in line.lower() and 'maple' in line.lower() for line in original.read_text().splitlines()),
        'no_rejected_mutation': not any(e.get('stage') == 'memory_mutation' and e.get('status') == 'failed' for e in receipts),
        'complete_native_cycle': bool(result.get('lifecycle', {}).get('complete') and result.get('lifecycle', {}).get('cycle_status_counts', {}).get('completed') == 1 and not any(result.get('lifecycle', {}).get('spool_counts', {}).values())),
    }
    if root_layout:
        checks.update(new_project_in_real_projects=any(p.is_relative_to(root / 'memory/Projects') for p in material),
            renamed_note_updated_once=original == root / 'memory/Areas/Work/Harbor Workshop renamed.md' and not (root / 'memory/Projects/Work/Harbor Workshop.md').exists(),
            no_hidden_note_recreation=not list((memory / 'capsules/theme').rglob('*.md')),
            folder_navigation_only=not (root / 'memory/Home.md').exists())
    return checks


def hermes_child(args):
    root, source = args.output.resolve(), args.source.resolve()
    sys.path[:0] = [str(source), str(source / 'src'), str(HERMES)]
    from benchmarks.productivity_v1.runner.common import (isolated_daemon_env, durable_after_turn_env,
        safe_dotenv, prepare_codex_cli_home, wait_for_after_turn_service, wait_for_durable_after_turn, read_jsonl)
    from benchmarks.productivity_v1.runner import hermes
    fixture = json.loads(FIXTURE.read_text())
    revision = subprocess.check_output(['git','rev-parse','HEAD'],cwd=source,text=True).strip()
    if args.transition_case:
        from scripts.qualify_parga_note_transitions import case_fixture
        fixture = case_fixture(args.transition_case)
    values = durable_after_turn_env(isolated_daemon_env(safe_dotenv(args.source_env), run_root=root,
        memory_root=root / ('memory/.daemon' if args.arm == 'B' else 'memory'), user_id=USER, platform='telegram',
        telemetry_path=root / 'telemetry.jsonl', latency_path=root / 'latency.jsonl'), user_id=USER)
    values.update(DAEMON_CORE_SRC=str(source / 'src'), DAEMON_CORE_MEMORY_STRUCTURE='v1',
        DAEMON_CORE_OBSIDIAN_FORMAT='1', DAEMON_CORE_TEMPORAL_CURATOR='on',
        DAEMON_CORE_TEMPORAL_CURATOR_USER_IDS=USER, DAEMON_CORE_VOICE_CAPTURE_FILE='',
        DAEMON_CORE_RUNTIME_REVISION=revision, DAEMON_CORE_AFTER_TURN_SERVICE_REVISION=revision,
        DAEMON_CORE_SCENE_MODEL_SHADOW_PATH=str(root / 'scene-shadow.jsonl'),
        HERMES_HOME=str(root / 'hermes-home'), HERMES_CONFIG=str(root / 'hermes-home/config.yaml'),
        HERMES_ENV=str(root / 'hermes-home/.env'), CODEX_HOME=str(root / 'codex-home'),
        CODEX_CLI_BIN=str(CODEX), CODEX_CLI_MODEL='gpt-6-astra', LLM_PROVIDER='codex_cli',
        PRODUCTIVITY_BENCH_TRACE_FILE=str(root / 'host-trace.jsonl'),
        DAEMON_ROOT_QA_CAPTURE=str(root / 'memory-writes.jsonl'),
        PYTHONDONTWRITEBYTECODE='1', PYTHONPATH=os.pathsep.join([str(source),str(source / 'src'),str(HERMES)]))
    if args.transition_case:
        values.update(DAEMON_CORE_MEMORY_RELOCATION=args.transition_mode,
                      DAEMON_CORE_MEMORY_SITUATIONS='canary', DAEMON_CORE_MEMORY_SITUATIONS_USER_IDS=USER)
    for key in tuple(os.environ):
        if key.startswith(('DAEMON_CORE_', 'HERMES_', 'CODEX_CLI_', 'MEMORY_',
                           'TELEGRAM_', 'DISCORD_', 'SLACK_', 'WHATSAPP_', 'SIGNAL_')):
            os.environ.pop(key)
    os.environ.update(values)
    from daemon_core.paths import MEMORY_ROOT
    assert MEMORY_ROOT == root / ('memory/.daemon' if args.arm == 'B' else 'memory')
    root.mkdir(parents=True, exist_ok=False)
    (root / 'empty.env').touch()
    note = root / 'memory/capsules/theme/work/harbor-workshop.md'
    note.parent.mkdir(parents=True)
    note.write_text(fixture['structured_note'])
    (root / 'memory/capsules/index.md').write_text(fixture['index'])
    save(root / 'memory/.memory-structure-prototype.json', {'schema':1, 'memory_root':str(root / 'memory')})
    user = root / 'memory/users' / USER
    user.mkdir(parents=True)
    shutil.copy2(source / 'memory/templates/users/conversation.md',user / 'conversation.md')
    if args.transition_case:
        from scripts.qualify_parga_note_transitions import seed_case
        seed_case(root, args.transition_case)
    elif args.arm == 'B':
        convert_fixture(root)
    prepare_codex_cli_home(root / 'codex-home')
    hermes._prepare_profile(source_home=args.hermes_home,profile=root / 'hermes-home',
        arm='C',model='gpt-6-astra',provider='openai-codex',daemon_values=values)
    result, worker, log = {'source_revision':revision,'complete':False}, None, None
    try:
        from hermes_cli.runtime_provider import resolve_runtime_provider
        from run_agent import AIAgent
        runtime = resolve_runtime_provider(requested='openai-codex')
        began, deltas = time.perf_counter(), []
        agent = AIAgent(model='gpt-6-astra',provider=runtime['provider'],api_mode=runtime['api_mode'],
            base_url=runtime.get('base_url'),api_key=runtime.get('api_key'),user_id=USER,
            platform='telegram',session_id='isolated-root-folders-'+args.arm,
            reasoning_config={'enabled':True,'effort':'medium'},enabled_toolsets=['memory'],
            max_iterations=5,run_budget_seconds=120,quiet_mode=True,skip_background_review=True,
            skip_context_files=True,stream_delta_callback=lambda text:deltas.append((time.perf_counter()-began,text)) if text else None)
        assert {'daemon_read_souvenirs','daemon_recall_l0'} <= agent.valid_tool_names
        response = agent.run_conversation(user_message=fixture['prompt'],conversation_history=[])
        result['voice_seconds'] = time.perf_counter()-began
        result['first_delta_seconds'] = deltas[0][0] if deltas else None
        agent.shutdown_memory_provider(response.get('messages',[]))
        save(root / 'voice.json',response)
        log=(root / 'worker.log').open('w')
        worker=subprocess.Popen([str(HERMES / '.venv/bin/python'),str(REPO / 'scripts/qualify_openclaw_memory_structure.py'),'worker'],
            cwd=source,env=os.environ.copy(),stdout=log,stderr=subprocess.STDOUT)
        began=time.perf_counter()
        wait_for_after_turn_service(process=worker,spool_root=root / 'after-turn-spool',log_path=root / 'worker.log')
        lifecycle=wait_for_durable_after_turn(process=worker,spool_root=root / 'after-turn-spool',
            receipt_path=root / 'continuity-receipts/continuity_receipts.jsonl',telemetry_path=root / 'telemetry.jsonl',
            host='hermes',user_id=USER,expected_turns=1,timeout_seconds=300)
        result.update(lifecycle=lifecycle,after_turn_wait_seconds=time.perf_counter()-began,
            complete=bool(response.get('completed') and not response.get('failed') and lifecycle['complete']
                          and not any(lifecycle['spool_counts'].values())),
            daemon_events=read_jsonl(root / 'telemetry.jsonl'),host_events=read_jsonl(root / 'host-trace.jsonl'))
    except Exception as exc:
        result['error']=f'{type(exc).__name__}: {exc}'
    finally:
        if worker:
            worker.terminate()
            try:
                worker.wait(timeout=15)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.wait()
        if log:
            log.close()
        for auth in root.rglob('auth.json'):
            auth.unlink()
        save(root / 'result.json',result)



def _parga_cases():
    """Case identifiers from the versioned PARGA fixtures next to this script."""
    root = Path(__file__).resolve().parents[1] / 'docs/evaluations/fixtures'
    return tuple(case['id'] for name in ('parga_note_transitions_en.json', 'parga_garden_placement_en.json')
                 for case in json.loads((root / name).read_text())['cases'])


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--host',choices=('hermes','openclaw'),required=True)
    p.add_argument('--arm',choices=('A','B'),required=True)
    for name in ('source','source-env','output','archive'):
        p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--hermes-home',type=Path,default=Path.home() / '.hermes')
    p.add_argument('--child',action='store_true')
    p.add_argument('--transition-case', choices=_parga_cases())
    p.add_argument('--transition-mode', choices=('off','v1'), default='off')
    args=p.parse_args()
    os.umask(0o077)
    root=args.output.resolve()
    if not root.is_relative_to(Path('/private/tmp')):
        raise ValueError('Disposable output required')
    if args.child:
        if args.transition_case and args.arm != 'B':
            raise ValueError('Folder transition cases require the physical root-folder arm')
        hermes_child(args)
        return
    if root.exists():
        raise FileExistsError('No overwrites or quality rerolls')
    if not args.archive.resolve().is_relative_to(REPO / 'exports/private'):
        raise ValueError('Private evidence archive required')
    tracked=subprocess.check_output(['git','status','--porcelain','--untracked-files=no'],cwd=args.source,text=True)
    if tracked.strip():
        raise ValueError('Commit candidate before provider calls')
    if args.host=='openclaw':
        command=[sys.executable,str(REPO / 'scripts/qualify_openclaw_memory_structure.py'),
            '--arm','B','--source',str(args.source),'--source-env',str(args.source_env),
            '--output',str(root),'--archive',str(args.archive),'--fixture',str(FIXTURE)]
        if args.arm == 'B':
            command.append('--root-folders')
        if args.transition_case:
            command += ['--transition-case', args.transition_case, '--transition-mode', args.transition_mode]
    else:
        command=[str(HERMES / '.venv/bin/python'),str(Path(__file__).resolve()),*sys.argv[1:],'--child']
    process=None
    try:
        with root.with_suffix('.log').open('w') as log:
            process=subprocess.Popen(command,cwd=args.source,env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'},
                                     stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
            code=process.wait(timeout=500)
        result=json.loads((root / 'result.json').read_text())
        if args.transition_case:
            sys.path[:0] = [str(args.source.resolve()), str(args.source.resolve() / 'src')]
            from scripts.qualify_parga_note_transitions import assess_case
            checks=assess_case(root,result,args.transition_case,args.transition_mode)
        else:
            checks=assess(root,result,args.source.resolve())
        metrics = result.get('voice_telemetry', {}).get('task_metrics', [])
        first_delta = result.get('first_delta_seconds')
        if first_delta is None and metrics and metrics[0].get('time_to_first_token_ms') is not None:
            first_delta = metrics[0]['time_to_first_token_ms'] / 1000
        summary={'host':args.host,'arm':args.arm,'source_revision':result.get('source_revision'),
            'fixture_sha256':hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),'child_exit':code,
            'checks':checks,'passed':all(checks.values()),'voice_seconds':result.get('voice_seconds'),
            'first_delta_seconds':first_delta,
            'after_turn_wait_seconds':result.get('after_turn_wait_seconds')}
        if args.transition_case:
            from scripts.qualify_parga_note_transitions import fixture_for
            summary.update(transition_case=args.transition_case, transition_mode=args.transition_mode,
                           fixture_sha256=hashlib.sha256(fixture_for(args.transition_case).read_bytes()).hexdigest())
        save(root / 'root-folders-result.json',summary)
        print(json.dumps(summary),flush=True)
    finally:
        if process and process.poll() is None:
            import signal
            os.killpg(process.pid,signal.SIGTERM)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid,signal.SIGKILL)
                process.wait()
        if root.exists():
            for auth in root.rglob('auth.json'):
                auth.unlink(missing_ok=True)
            destination = args.archive / root.name
            if args.host == 'openclaw' and destination.is_dir():
                # The native runner already archived its evidence; do not try
                # to overwrite read-only Git packfiles in its plugin cache.
                shutil.copy2(root / 'root-folders-result.json', destination / 'root-folders-result.json')
            else:
                shutil.copytree(root,destination,dirs_exist_ok=True,symlinks=True,
                                ignore=shutil.ignore_patterns('node_modules','cache','.git','.tmp'))


if __name__=='__main__':
    main()
