# Read-only diagnostic for Companion 57a44512. No service/file mutation.
[CmdletBinding()]
param([string]$AppDirectory)
$ErrorActionPreference = 'Stop'
if ([Environment]::OSVersion.Platform -ne 'Win32NT') { throw 'Windows is required.' }
if (-not $AppDirectory) { $AppDirectory = Join-Path $env:LOCALAPPDATA 'Programs\Daemons Companion' }
$daemonBase = Join-Path $env:LOCALAPPDATA 'DaemonHarness'
$daemonBootstrap = Join-Path $daemonBase 'Companion\bootstrap'
$daemonSource = Join-Path $AppDirectory 'resources\installer\payload\src'
$daemonLibraries = Join-Path $daemonBootstrap 'libraries'
$daemonPython = @(Get-ChildItem -LiteralPath (Join-Path $daemonBootstrap 'python') -Directory |
    ForEach-Object { Join-Path $_.FullName 'python.exe' } |
    Where-Object { Test-Path -LiteralPath $_ -PathType Leaf })
if ($daemonPython.Count -ne 1 -or -not (Test-Path -LiteralPath (Join-Path $daemonSource 'daemons_companion\removal\removal.py'))) {
    throw 'The installed Companion runtime was not found unambiguously. No changes were made.'
}
$daemonProbe = @'
import ctypes
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from xml.etree import ElementTree as ET

sys.path[:0] = sys.argv[1:3]
base = Path(sys.argv[3])
if os.name != 'nt' or ctypes.windll.shell32.IsUserAnAdmin():
    print('Run this diagnostic from normal PowerShell, without administrator access.')
    sys.exit(1)
from daemons_companion.removal.removal import Removal, Refused
from daemons_companion.hermes.profile import _read_env_values
from daemons_companion.runtime.services import SchedulerCommandResult
from daemons_companion.removal import windows

# Only error class/known command names leave PowerShell. No raw stderr or XML.
catch = '''catch{
$e=$_.Exception.GetBaseException();
$c=$_.InvocationInfo.MyCommand.Name;
if($c -notin @('ConvertFrom-Json','New-Object','Get-CimInstance')){$c='native'};
[Console]::Error.WriteLine('DAEMON_PROBE|'+$e.GetType().FullName+'|'+$e.HResult+'|'+$c);
exit 91
}'''
report = {'schema':'daemon.windows-removal-probe.v1', 'read_only':True, 'installations':[]}
profiles = []
for pattern in ('hosts/hermes/installations/*/profile',
                'Companion/private/hosts/hermes/installations/*/profile',
                'Companion/instance-*/hosts/hermes/installations/*/profile'):
    profiles.extend(base.glob(pattern))
profiles = sorted(set(profiles))
if len(profiles) > 16:
    print('Too many installations for this bounded diagnostic. No changes were made.')
    sys.exit(1)
original_validate = windows.validate_task
for ordinal, profile in enumerate(profiles, 1):
    record = {'installation':ordinal, 'commands':[], 'task_checks':[]}
    report['installations'].append(record)
    started = time.monotonic()
    def runner(command, environment):
        command = list(command)
        position = command.index('-Command') + 1
        script = command[position]
        if any(token in script for token in ('.Stop(', '.Run(', 'RegisterTask', 'DeleteTask', 'DeleteFolder')):
            raise RuntimeError('Mutation refused by the read-only probe')
        if 'ConvertFrom-Json' in script:
            stage = 'dependency_scan'
        elif 'daemon.windows-task-capture.v1' in script:
            stage = 'task_capture'
        else:
            raise RuntimeError('Unexpected command refused by the read-only probe')
        if not script.startswith('& { ') or not script.endswith(' }'):
            raise RuntimeError('Unexpected command format')
        command[position] = '& { try { ' + script[4:-2] + ' } ' + catch + ' }'
        observation = {'stage':stage}
        record['commands'].append(observation)
        stamp = time.monotonic()
        try:
            result = subprocess.run(command, env=environment, capture_output=True,
                                    text=True, timeout=90, check=False)
        except subprocess.TimeoutExpired:
            observation.update(result='timeout', seconds=round(time.monotonic()-stamp, 2))
            return SchedulerCommandResult(92, '', '')
        observation.update(exit=result.returncode, seconds=round(time.monotonic()-stamp, 2))
        # Match only fixed error metadata; never forward raw command output.
        match = re.search(r'DAEMON_PROBE\|([A-Za-z.]+)\|(-?[0-9]+)\|(native|ConvertFrom-Json|New-Object|Get-CimInstance)', result.stderr)
        if match:
            observation.update(exception_type=match[1], hresult=int(match[2]), command=match[3])
        return SchedulerCommandResult(result.returncode, result.stdout, '')
    def validate(raw, attempt, sid):
        ns = {'t':'http://schemas.microsoft.com/windows/2004/02/mit/task'}
        root = ET.fromstring(raw)
        principals = root.findall('t:Principals/t:Principal', ns)
        actions = root.findall('t:Actions/*', ns)
        checks = {'one_principal':len(principals)==1, 'one_action':len(actions)==1}
        record['task_checks'].append(checks)
        if len(principals)==1 and len(actions)==1:
            principal, action = principals[0], actions[0]
            def value(node, key):
                return node.findtext('t:'+key, '', ns)
            checks.update(
                exec_action=action.tag=='{'+ns['t']+'}Exec',
                current_user=windows._windows_task_identity(value(principal,'UserId'))==sid,
                no_group=not bool(value(principal,'GroupId')),
                interactive=value(principal,'LogonType')=='InteractiveToken',
                least_privilege=value(principal,'RunLevel') in ('','LeastPrivilege','0'))
            for field in ('Command','WorkingDirectory'):
                path = windows.logical(value(action,field))
                checks[field] = {'absolute':path.is_absolute(),
                    'inside_installation':path.is_relative_to(attempt),
                    'resolved_inside':path.resolve().is_relative_to(attempt)}
        return original_validate(raw, attempt, sid)
    windows.validate_task = validate
    try:
        scheduler = windows.WindowsRemovalScheduler(runner=runner, job_namespace=profile.parent.name)
        removal = Removal(profile, base, Path.home()/'Library/LaunchAgents', scheduler=scheduler)
        # Read only the memory location. Never output environment values.
        memory_value = _read_env_values(profile/'.env').get('DAEMON_CORE_MEMORY_ROOT','')
        if not memory_value:
            record['result'] = 'no_memory_location'
            continue
        memory = windows.logical(memory_value)
        if memory.name == '.daemon':
            memory = memory.parent
        if not memory.is_absolute():
            record['result'] = 'invalid_memory_location'
            continue
        # The exact distributed gate, with no apply/inspect, writes or task stop.
        removal.service_snapshot(memory)
        record['result'] = 'services_verified'
    except Refused as error:
        record['result'] = 'refused'
        record['code'] = error.code if error.code in ('services','shared','unsafe','ownership') else 'other'
    except Exception as error:
        record['result'] = 'failed'
        record['exception_type'] = type(error).__name__
    finally:
        windows.validate_task = original_validate
        record['seconds'] = round(time.monotonic()-started, 2)
print(json.dumps(report, indent=2))
'@
Write-Host 'Read-only Windows service check. No uninstall, restart, download or memory change. This may take a few minutes.'
$daemonCode = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($daemonProbe))
& $daemonPython[0] -I -B -u -c 'import sys,base64;exec(base64.b64decode(sys.argv.pop(1)))' $daemonCode $daemonSource $daemonLibraries $daemonBase
if ($LASTEXITCODE -ne 0) { throw 'The diagnostic did not complete. No removal was requested.' }
