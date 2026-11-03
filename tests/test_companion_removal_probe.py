"""Read-only diagnostic transport contracts; these do not simulate native Windows."""
import ast
import json
from pathlib import Path
import re
import subprocess
import time
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import pytest
from daemons_companion.removal import windows
from daemons_companion.runtime.services import SchedulerCommandResult, _powershell_command

FILE = Path(__file__).resolve().parents[1] / 'scripts/qualification/diagnose-windows-removal.ps1'


def load_function(name, **extra):
    script = FILE.read_text().split("$daemonProbe = @'\n")[1].split("\n'@", 1)[0]
    tree = ast.parse(script)
    compile(tree, '<probe>', 'exec')
    function = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)
    catch = next(n.value.value for n in tree.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'catch' for t in n.targets))
    scope = dict(re=re, time=time, subprocess=subprocess, SchedulerCommandResult=SchedulerCommandResult,
                 record={'commands': [], 'task_checks': []}, catch=catch, ET=ET,
                 windows=windows, original_validate=windows.validate_task, **extra)
    exec(compile(ast.Module(body=[function], type_ignores=[]), '<probe>', 'exec'), scope)
    return scope[name], scope


def test_probe_redacts_private_output_and_preserves_data_arguments(monkeypatch):
    calls = []
    def run(command, **options):
        calls.append((command, options))
        return SimpleNamespace(returncode=91, stdout='private-task-xml', stderr='SECRET\nDAEMON_PROBE|System.UnauthorizedAccessException|-2147024891|native\nSECRET')
    monkeypatch.setattr(subprocess, 'run', run)
    runner, scope = load_function('runner')
    argument = 'encoded-private-argument'
    result = runner(_powershell_command("$inputData=$args[0] | ConvertFrom-Json;", argument), {'test': 'value'})
    assert result.stdout == 'private-task-xml'  # Kept only for the installed parser.
    assert result.stderr == ''
    assert calls[0][0][-1] == argument
    assert 'try {' in calls[0][0][-2]
    assert calls[0][1]['timeout'] == 90
    output = json.dumps(scope['record'])
    assert 'SECRET' not in output and 'private' not in output
    assert scope['record']['commands'][0]['hresult'] == -2147024891


@pytest.mark.parametrize('script', ['DeleteTask(0)', '$task.Stop(0)', '$task.Run(0)', 'RegisterTask(0)', 'unexpected'])
def test_probe_never_executes_mutation_or_unknown_commands(script, monkeypatch):
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: pytest.fail('Native command must not run'))
    runner, _ = load_function('runner')
    with pytest.raises(RuntimeError):
        runner(_powershell_command(script), {})


def test_probe_timeout_is_explicit_and_contains_no_native_output(monkeypatch):
    def timeout(*a, **kw):
        raise subprocess.TimeoutExpired('private command', 90, output='SECRET')
    monkeypatch.setattr(subprocess, 'run', timeout)
    runner, scope = load_function('runner')
    result = runner(_powershell_command("$null='daemon.windows-task-capture.v1';"), {})
    assert result.returncode == 92 and result.stdout == ''
    assert scope['record']['commands'][0]['result'] == 'timeout'
    assert 'SECRET' not in json.dumps(scope['record'])


def test_probe_reports_task_conditions_without_paths(tmp_path):
    validate, scope = load_function('validate')
    xml = f'''<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"><Principals><Principal>
<UserId>S-1-5-21-123</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel>
</Principal></Principals><Actions><Exec><Command>{tmp_path / 'python.exe'}</Command>
<WorkingDirectory>{tmp_path}</WorkingDirectory></Exec></Actions></Task>'''
    validate(xml, tmp_path, 'S-1-5-21-123')
    assert scope['record']['task_checks'][0]['current_user'] is True
    assert str(tmp_path) not in json.dumps(scope['record'])
    with pytest.raises(ValueError, match='services'):
        validate(xml, tmp_path, 'S-1-5-21-999')
    assert scope['record']['task_checks'][1]['current_user'] is False
