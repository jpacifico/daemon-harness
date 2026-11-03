"""Real private Hermes under long Windows paths; no accounts/model/channel."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import traceback
import uuid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--uv', type=Path, required=True)
    args = parser.parse_args()
    assert os.name == 'nt'
    import winreg
    repo = Path(__file__).resolve().parents[2]
    sys.path[:0] = [str(repo), str(repo / 'src')]
    from daemons_companion.runtime.host_install import HostInstallLayout, execute_host_install, host_install_plan
    from daemons_companion.runtime.bootstrap import clean_environment, private_home_environment, _windows_extended_path
    from daemons_companion.setup.transaction import public_plan_digest
    output = Path('qualification')
    output.mkdir(exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix='hp-', dir=os.environ['RUNNER_TEMP'])).resolve()
    home = root / ('p' * max(1, 66 - len(str(root)) - 1))
    layout = HostInstallLayout(home, args.uv.resolve(), hashlib.sha256(args.uv.read_bytes()).hexdigest(), uuid.uuid4().hex, 'windows')
    report = {'source':subprocess.check_output(['git','rev-parse','HEAD'],cwd=repo,text=True).strip(), 'observations':[], 'passed':False}
    key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'SYSTEM\CurrentControlSet\Control\FileSystem', 0, winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE)
    previous, kind = winreg.QueryValueEx(key, 'LongPathsEnabled')
    report['previous_long_paths'] = previous
    started = time.monotonic()
    try:
        # Test VM only. Restore in finally. Product never changes this policy.
        winreg.SetValueEx(key, 'LongPathsEnabled', 0, winreg.REG_DWORD, 0)
        report['tested_long_paths'] = winreg.QueryValueEx(key, 'LongPathsEnabled')[0]
        execute_host_install(layout, confirmed_digest=public_plan_digest(host_install_plan(layout)), progress=lambda s: print(s,flush=True))
        report['host_seconds'] = round(time.monotonic()-started,3)
        environment = clean_environment(layout.attempt)
        environment.update(private_home_environment(layout.attempt / 'os-home'))
        target = layout.host / '.venv/Lib/site-packages/openai/types/responses/response_function_shell_call_output_content_param.py'
        report['target_length'] = len(str(target))
        report['target_plain_exists'] = target.is_file()
        report['target_extended_exists'] = Path(_windows_extended_path(str(target))).is_file()
        code = "import json,sys; from openai import OpenAI; print(json.dumps({'prefix':sys.prefix,'executable':sys.executable,'sdk':OpenAI.__name__}))"
        for label, executable in [('ordinary',str(layout.python)),('extended-executable',_windows_extended_path(str(layout.python)))]:
            stamp=time.monotonic()
            result=subprocess.run([executable,'-B','-c',code],env=environment,cwd=layout.attempt,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=90)
            item={'case':label,'exit':result.returncode,'stdout':result.stdout,'stderr':result.stderr[-8000:],'seconds':round(time.monotonic()-stamp,3)}
            report['observations'].append(item)
            print(json.dumps(item),flush=True)
        environment["HERMES_HOME"] = str(layout.profile)
        managed_uv = layout.profile / "bin/uv.exe"
        managed_uv.unlink()
        lazy_code = "from tools.lazy_deps import ensure; ensure('provider.bedrock',prompt=False); import boto3, botocore; print('native lazy dependency ready')"
        for label, executable in [('legacy-lazy-install',str(layout.python)),('repaired-lazy-install',layout.launch_python)]:
            if label == 'repaired-lazy-install':
                from daemons_companion.runtime.uv import install_private_uv
                install_private_uv(layout)
            stamp=time.monotonic()
            result=subprocess.run([executable,'-B','-c',lazy_code],env=environment,cwd=layout.host,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=360)
            item={'case':label,'exit':result.returncode,'stdout':result.stdout[-2000:],'stderr':result.stderr[-8000:],'seconds':round(time.monotonic()-stamp,3)}
            report['observations'].append(item)
            print(json.dumps(item),flush=True)
        report['passed'] = report['observations'][0]['exit'] != 0 and report['observations'][1]['exit'] == 0 and report['observations'][-1]['exit'] == 0
    except Exception:
        report['error'] = traceback.format_exc()
    finally:
        winreg.SetValueEx(key, 'LongPathsEnabled', 0, kind, previous)
        report['policy_restored'] = winreg.QueryValueEx(key, 'LongPathsEnabled')[0] == previous
        winreg.CloseKey(key)
        report['seconds'] = round(time.monotonic()-started,3)
        (output/'windows-host-paths.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
        print(json.dumps(report,indent=2),flush=True)
    return 0 if report['passed'] and report['policy_restored'] else 1

if __name__ == '__main__':
    raise SystemExit(main())
