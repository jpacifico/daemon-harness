"""Run native qualification as a real disposable standard Windows account.

Only in the disposable Actions VM: create a normal local account, launch with
its credentials through the Windows Secondary Logon API, collect non-secret
proof, then remove that account. No UAC policy or existing user ACL changes.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import uuid

_LAUNCH = r"""
$ErrorActionPreference='Stop';
$c=[Console]::In.ReadToEnd()|ConvertFrom-Json;
$p=[Diagnostics.ProcessStartInfo]::new();
$p.FileName=$c.executable;$p.Arguments=$c.arguments;$p.WorkingDirectory=$c.cwd;
$p.UseShellExecute=$false;$p.CreateNoWindow=$true;$p.LoadUserProfile=$true;
$p.UserName=$c.user;$p.Domain=$env:COMPUTERNAME;
$s=[Security.SecureString]::new();foreach($ch in $c.password.ToCharArray()){$s.AppendChar($ch)};$s.MakeReadOnly();$p.Password=$s;
$p.RedirectStandardOutput=$true;$p.RedirectStandardError=$true;
foreach($item in $c.environment.psobject.Properties){$p.EnvironmentVariables[$item.Name]=[string]$item.Value};
$child=[Diagnostics.Process]::Start($p);
$o=$child.StandardOutput.ReadToEndAsync();$e=$child.StandardError.ReadToEndAsync();
if(-not $child.WaitForExit(1260000)){$child.Kill();throw 'Standard-user test timeout'};
[Console]::Out.Write($o.Result);[Console]::Error.Write($e.Result);exit $child.ExitCode
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--child', action='store_true')
    parser.add_argument('--mode', choices=['scheduler', 'registration', 'setup'], required=True)
    parser.add_argument('--uv')
    parser.add_argument('--baseline')
    args = parser.parse_args()
    if os.name != 'nt':
        raise RuntimeError('Native Windows required')
    import win32api
    import win32con
    import win32net
    import win32netcon
    import win32security as security

    repository = Path(__file__).resolve().parents[2]
    output = Path('qualification')
    output.mkdir(exist_ok=True)
    if not args.child:
        # Credentials are randomly generated, sent only over stdin, and never
        # included in a command line, report, artifact or repository file.
        username = 'DaemonQ' + uuid.uuid4().hex[:10]
        password = secrets.token_urlsafe(32) + '!aA7'
        win32net.NetUserAdd(None, 1, {'name': username, 'password': password,
            'priv': win32netcon.USER_PRIV_USER, 'home_dir': '', 'comment': 'Disposable installer qualification',
            'flags': win32netcon.UF_SCRIPT | win32netcon.UF_NORMAL_ACCOUNT, 'script_path': ''})
        try:
            users = security.LookupAccountSid(None, security.CreateWellKnownSid(security.WinBuiltinUsersSid))[0]
            win32net.NetLocalGroupAddMembers(None, users, 3, [{'domainandname': username}])
            sid = security.ConvertSidToStringSid(security.LookupAccountName(None, username)[0])
            token = security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
            owner = security.ConvertSidToStringSid(security.GetTokenInformation(token, security.TokenUser)[0])
            token.Close()
            root = Path(tempfile.mkdtemp(prefix='standard-user-', dir=os.environ['RUNNER_TEMP'])).resolve()
            descriptor = security.ConvertStringSecurityDescriptorToSecurityDescriptor(
                f'D:P(A;OICI;FA;;;{sid})(A;OICI;FA;;;{owner})(A;OICI;FA;;;SY)', 1)
            security.SetNamedSecurityInfo(str(root), security.SE_FILE_OBJECT,
                security.DACL_SECURITY_INFORMATION | security.PROTECTED_DACL_SECURITY_INFORMATION,
                None, None, descriptor.GetSecurityDescriptorDacl(), None)
            # Copy only the checked-out test sources. The account receives
            # permissions through the new root, never by changing existing ACLs.
            source = root / 'candidate'
            shutil.copytree(repository, source)
            baseline = root / 'baseline'
            if args.baseline:
                shutil.copytree(Path(args.baseline).resolve(), baseline)
            uv = root / 'uv.exe'
            if args.uv:
                shutil.copyfile(args.uv, uv)
            child_args = ['-B', str(source / 'scripts/qualification/windows_unprivileged_probe.py'),
                '--child', '--mode', args.mode]
            if args.uv:
                child_args += ['--uv', str(uv), '--baseline', str(baseline)]
            home = root / 'user-home'
            for folder in (home, home / 'AppData/Roaming', home / 'AppData/Local', root / 'temp'):
                folder.mkdir(parents=True, exist_ok=True)
            environment = {'RUNNER_TEMP': str(root / 'temp'), 'TEMP': str(root / 'temp'), 'TMP': str(root / 'temp'),
                'USERPROFILE': str(home), 'HOME': str(home), 'APPDATA': str(home / 'AppData/Roaming'),
                'LOCALAPPDATA': str(home / 'AppData/Local'), 'PYTHONUTF8': '1', 'PYTHONDONTWRITEBYTECODE': '1',
                'PYTHONPATH': os.pathsep.join((str(source), str(source / 'src'))),
                'GIT_CONFIG_COUNT': '2', 'GIT_CONFIG_KEY_0': 'safe.directory', 'GIT_CONFIG_VALUE_0': str(source),
                'GIT_CONFIG_KEY_1': 'safe.directory', 'GIT_CONFIG_VALUE_1': str(baseline)}
            launch = {'executable': sys.executable, 'arguments': subprocess.list2cmdline(child_args),
                'cwd': str(root), 'user': username, 'password': password, 'environment': environment}
            result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', _LAUNCH],
                input=json.dumps(launch), capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=22 * 60)
            (output / 'standard-user-launch.txt').write_text(result.stdout + result.stderr, encoding='utf-8')
            print(result.stdout, flush=True)
            print(result.stderr, file=sys.stderr, flush=True)
            if (root / 'qualification').exists():
                for path in (root / 'qualification').iterdir():
                    if path.is_file() and not path.is_symlink():
                        shutil.copyfile(path, output / path.name)
            return result.returncode
        finally:
            win32net.NetUserDel(None, username)
            (output / 'standard-user-cleanup.json').write_text(json.dumps({'account_removed': True}), encoding='utf-8')

    admin_sid = security.CreateWellKnownSid(security.WinBuiltinAdministratorsSid)
    token = security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    integrity, _ = security.GetTokenInformation(token, security.TokenIntegrityLevel)
    report = {'account_kind': 'disposable_standard_user',
        'admin_enabled': security.CheckTokenMembership(None, admin_sid),
        'elevated': bool(security.GetTokenInformation(token, security.TokenElevation)),
        'integrity_sid': security.ConvertSidToStringSid(integrity),
        'privileges': [security.LookupPrivilegeName(None, luid)
            for luid, _ in security.GetTokenInformation(token, security.TokenPrivileges)]}
    token.Close()
    (output / 'standard-user-token.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    assert report['admin_enabled'] is False, report
    assert report['elevated'] is False, report
    assert report['integrity_sid'] == 'S-1-16-8192', report
    assert not {'SeDebugPrivilege', 'SeTcbPrivilege', 'SeBackupPrivilege', 'SeRestorePrivilege'} & set(report['privileges'])
    commands = [('scheduler', [sys.executable, '-B', '-S', str(repository / 'scripts/qualification/windows_user_tasks_probe.py')])]
    if args.mode == 'registration':
        commands[0][1].append('--registration-only')
    if args.mode == 'setup':
        assert args.uv and args.baseline
        commands.append(('setup', [sys.executable, '-B', '-S', str(repository / 'scripts/qualification/companion_windows_setup_probe.py'),
            '--uv', args.uv, '--baseline', args.baseline, '--output', str((output / 'windows-user-setup.json').resolve())]))
    for label, command in commands:
        with (output / (label + '-unprivileged.log')).open('w', encoding='utf-8') as stream:
            result = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT, timeout=20 * 60)
        if result.returncode:
            return result.returncode
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
