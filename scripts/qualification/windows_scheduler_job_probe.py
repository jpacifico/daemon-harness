"""Inspect native task normalization using only one disposable synthetic job."""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

repository = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(repository), str(repository / 'src')]



def main():
    from daemons_companion.runtime.services import (
        AFTER_TURN_JOB, BackgroundJob, WindowsTaskSchedulerBackend,
        _load_windows_task_capture, _windows_definition_signature,
        render_job_definition, scoped_job_code,
    )
    if os.name != 'nt':
        raise RuntimeError('Windows required')
    from daemons_companion.runtime.bootstrap import _windows_extended_path
    executable = _windows_extended_path(sys.executable) if '--extended' in sys.argv else sys.executable
    namespace = uuid.uuid4().hex
    root = Path(tempfile.mkdtemp(prefix='scheduler-probe-')).resolve()
    backend = WindowsTaskSchedulerBackend(job_namespace=namespace)
    code = scoped_job_code(AFTER_TURN_JOB, namespace)
    job = BackgroundJob(code, executable, ('-B', '-c', 'import time;time.sleep(120)'), str(root), str(root / 'job.log'), True)
    definition = render_job_definition(job, 'windows')
    report = {'source': subprocess.check_output(['git','rev-parse','HEAD'], cwd=repository, text=True).strip(), 'expected_xml': definition.decode('utf-16'), 'expected_signature': _windows_definition_signature(definition), 'captures': [], 'cleanup': False}
    assert backend.capture(code) is None
    try:
        backend.install(job, definition)
        for delay in (0, 2, 5):
            time.sleep(delay)
            content = backend.capture(code)
            capture = _load_windows_task_capture(content)
            report['captures'].append({'running': capture['running'], 'xml': capture['definition'].decode('utf-16'), 'signature': _windows_definition_signature(capture['definition']), 'matches': backend.matches(job, definition)})
    finally:
        backend.restore(code, None)
        report['cleanup'] = backend.capture(code) is None
        output = Path('qualification/scheduler-job.json')
        output.parent.mkdir(exist_ok=True)
        output.write_text(json.dumps(report, indent=2), encoding='utf-8')
    assert report['cleanup']
    assert all(item['running'] and item['matches'] for item in report['captures'])


if __name__ == '__main__':
    main()
