"""Counter-test the all-users logon trigger, then native user-bound tasks."""
from __future__ import annotations

import argparse
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
from xml.etree import ElementTree

repository = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(repository), str(repository / 'src')]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registration-only", action="store_true",
        help="Explicitly omit execution checks when no interactive desktop is available")
    args = parser.parse_args()
    from daemons_companion.runtime.services import (
        AFTER_TURN_JOB, NIGHTLY_JOB, HERMES_GATEWAY_JOB, BackgroundJob, WindowsTaskSchedulerBackend,
        _load_windows_task_capture, _powershell_command, _windows_definition_signature,
        _windows_task_name, _windows_task_identity, render_job_definition, scoped_job_code,
    )
    from daemon_core.platform_support import _user_sid
    assert os.name == 'nt'
    assert ctypes.windll.shell32.IsUserAnAdmin() == 0
    namespace = uuid.uuid4().hex
    root = Path(tempfile.mkdtemp(prefix='user-tasks-')).resolve()
    backend = WindowsTaskSchedulerBackend(job_namespace=namespace)
    jobs = [BackgroundJob(scoped_job_code(code, namespace), sys.executable,
        ('-B', '-c', 'import time;time.sleep(120)'), str(root), str(root / 'job.log'),
        persistent, hour) for code, persistent, hour in ((AFTER_TURN_JOB, True, None), (NIGHTLY_JOB, False, 4), (HERMES_GATEWAY_JOB, True, None))]
    started = time.monotonic()
    report = {'source': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repository, text=True).strip(),
        'admin_enabled': False, 'passed': False, 'cleanup': False, 'jobs': [],
        'scope': 'registration-restoration-removal-only' if args.registration_only else 'registration-and-execution',
        'execution_checked': not args.registration_only}
    try:
        for job in jobs:
            assert backend.capture(job.code) is None
        # Exact old task scope: no explicit execution/logon user. Register only,
        # never run it if Windows unexpectedly accepts it.
        old = ElementTree.fromstring(render_job_definition(jobs[0], 'windows'))
        for parent in old.iter():
            for node in list(parent):
                if node.tag.endswith('}UserId'):
                    parent.remove(node)
        try:
            backend._register(jobs[0].code, ElementTree.tostring(old, encoding='utf-16'))
        except RuntimeError as error:
            diagnostic = getattr(error.__cause__, 'diagnostic', str(error))
            report['baseline_error'] = diagnostic
            assert 'RegisterTask' in diagnostic
            assert '0x80070005' in diagnostic or 'E_ACCESSDENIED' in diagnostic or 'Access is denied' in diagnostic
        else:
            raise AssertionError('Old all-users trigger was not denied to the limited caller')
        assert backend.capture(jobs[0].code) is None
        for job in jobs:
            definition = render_job_definition(job, 'windows')
            if args.registration_only:
                backend._register(job.code, definition)
            else:
                backend.install(job, definition)
            observations = []
            record = {'persistent': job.persistent, 'observations': observations,
                'expected_xml': definition.decode('utf-16')}
            report['jobs'].append(record)
            for delay in ((0,) if args.registration_only else (0, 1, 3)):
                time.sleep(delay)
                capture = backend.capture(job.code)
                native = _load_windows_task_capture(capture)
                status = backend._run(_powershell_command(backend._CONNECT +
                    "$task=$scheduler.GetFolder($args[0]).GetTask($args[1]);" +
                    "[Console]::Out.WriteLine('state='+$task.State+';result='+$task.LastTaskResult+';instances='+$task.GetInstances(0).Count)",
                    backend._TASK_PATH, _windows_task_name(job.code, namespace)))
                observations.append({'running': native['running'], 'xml': native['definition'].decode('utf-16'),
                    'signature_matches': _windows_definition_signature(native['definition']) == _windows_definition_signature(definition),
                    'native_state': status.stdout, 'native_error': status.stderr})
            if args.registration_only:
                assert observations[-1]["signature_matches"]
                assert native["running"] is False
            else:
                assert backend.matches(job, definition)
            xml = ElementTree.fromstring(native['definition'])
            ns = {'t': 'http://schemas.microsoft.com/windows/2004/02/mit/task'}
            assert xml.findtext('t:Principals/t:Principal/t:UserId', namespaces=ns) == _user_sid()
            if job.persistent:
                assert _windows_task_identity(xml.findtext('t:Triggers/t:LogonTrigger/t:UserId', namespaces=ns)) == _user_sid()
            backend.restore(job.code, capture)
            if args.registration_only:
                restored = _load_windows_task_capture(backend.capture(job.code))
                assert _windows_definition_signature(restored['definition']) == _windows_definition_signature(definition)
                assert restored['running'] is False
            else:
                assert backend.matches(job, definition)
            record.update({'matched': True, 'restored': True, 'running': native['running']})
        report['passed'] = True
    except Exception:
        report['error'] = traceback.format_exc()
    finally:
        errors = []
        for job in reversed(jobs):
            try:
                backend.restore(job.code, None)
            except Exception as error:
                errors.append(str(error))
        report['cleanup'] = not errors and all(backend.capture(job.code) is None for job in jobs)
        report['cleanup_errors'] = errors
        report['seconds'] = round(time.monotonic() - started, 3)
        output = Path('qualification/windows-user-tasks.json')
        output.parent.mkdir(exist_ok=True)
        output.write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(json.dumps(report, indent=2), flush=True)
    return 0 if report['passed'] and report['cleanup'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
