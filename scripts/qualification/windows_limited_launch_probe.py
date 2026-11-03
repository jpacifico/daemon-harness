"""Bounded native token/desktop diagnostics; no installer or scheduled task."""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid


def main():
    import msvcrt
    import pywintypes
    import win32api
    import win32con
    import win32event
    import win32process
    import win32security as sec
    import win32service
    output = Path('qualification')
    output.mkdir(exist_ok=True)
    token = sec.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_ALL_ACCESS)
    admin = sec.CreateWellKnownSid(sec.WinBuiltinAdministratorsSid)
    sid = sec.ConvertSidToStringSid(sec.GetTokenInformation(token, sec.TokenUser)[0])
    def info(handle):
        il, _ = sec.GetTokenInformation(handle, sec.TokenIntegrityLevel)
        return {'elevation': sec.GetTokenInformation(handle, sec.TokenElevation),
            'elevation_type': sec.GetTokenInformation(handle, sec.TokenElevationType),
            'session': sec.GetTokenInformation(handle, sec.TokenSessionId),
            'integrity': sec.ConvertSidToStringSid(il),
            'admin_group': [flags for group, flags in sec.GetTokenInformation(handle, sec.TokenGroups) if group == admin]}
    tokens = [('original', token)]
    high = sec.CreateRestrictedToken(token, 1, [(admin, 0)], None, None)
    tokens.append(('limited-high', high))
    medium = sec.CreateRestrictedToken(token, 1, [(admin, 0)], None, None)
    sec.SetTokenInformation(medium, sec.TokenIntegrityLevel, (sec.ConvertStringSidToSid('S-1-16-8192'), sec.SE_GROUP_INTEGRITY))
    tokens.append(('limited-medium', medium))
    # LUA_TOKEN asks Windows for a consistent limited-user token, including
    # its default ownership/DACLs; manually denying one group was insufficient.
    lua = sec.CreateRestrictedToken(token, 4, None, None, None)
    tokens.append(('lua', lua))
    lua_medium = sec.CreateRestrictedToken(token, 4, None, None, None)
    sec.SetTokenInformation(lua_medium, sec.TokenIntegrityLevel, (sec.ConvertStringSidToSid('S-1-16-8192'), sec.SE_GROUP_INTEGRITY))
    tokens.append(('lua-medium', lua_medium))
    report = {'tokens': {name: info(handle) for name, handle in tokens}, 'cases': []}
    try:
        linked = sec.GetTokenInformation(token, sec.TokenLinkedToken)
        tokens.append(('linked', linked))
        report['tokens']['linked'] = info(linked)
    except pywintypes.error as error:
        report['linked_unavailable'] = error.winerror
    sa = pywintypes.SECURITY_ATTRIBUTES()
    sa.SECURITY_DESCRIPTOR = sec.ConvertStringSecurityDescriptorToSecurityDescriptor(
        f'D:P(A;;GA;;;{sid})(A;;GA;;;SY)S:(ML;;NW;;;ME)', 1)
    previous = win32service.GetProcessWindowStation()
    name = 'DaemonLaunch' + uuid.uuid4().hex
    station = win32service.CreateWindowStation(name, 0, win32con.GENERIC_ALL, sa)
    station.SetProcessWindowStation()
    try:
        desktop = win32service.CreateDesktop('default', 0, win32con.GENERIC_ALL, sa)
    finally:
        previous.SetProcessWindowStation()
    try:
        # Secondary Logon configures a complete limited-token process, unlike
        # CreateProcessAsUser in this elevated Actions service environment.
        import ctypes
        from ctypes import wintypes as w
        class Startup(ctypes.Structure):
            _fields_ = [('cb', w.DWORD), ('lpReserved', w.LPWSTR), ('lpDesktop', w.LPWSTR),
                ('lpTitle', w.LPWSTR), ('dwX', w.DWORD), ('dwY', w.DWORD),
                ('dwXSize', w.DWORD), ('dwYSize', w.DWORD), ('dwXCountChars', w.DWORD),
                ('dwYCountChars', w.DWORD), ('dwFillAttribute', w.DWORD), ('dwFlags', w.DWORD),
                ('wShowWindow', w.WORD), ('cbReserved2', w.WORD), ('lpReserved2', ctypes.c_void_p),
                ('hStdInput', w.HANDLE), ('hStdOutput', w.HANDLE), ('hStdError', w.HANDLE)]
        class ProcessInfo(ctypes.Structure):
            _fields_ = [('hProcess', w.HANDLE), ('hThread', w.HANDLE), ('pid', w.DWORD), ('tid', w.DWORD)]
        api = ctypes.WinDLL('advapi32', use_last_error=True)
        api.CreateProcessWithTokenW.argtypes = [w.HANDLE, w.DWORD, w.LPCWSTR, w.LPWSTR,
            w.DWORD, ctypes.c_void_p, w.LPCWSTR, ctypes.POINTER(Startup), ctypes.POINTER(ProcessInfo)]
        api.CreateProcessWithTokenW.restype = w.BOOL
        for label, handle in tokens:
            logfile = (output / ('with-token-' + label + '.json')).resolve()
            command = [sys.executable, '-B', '-c',
                "import ctypes,json,pathlib;pathlib.Path(" + repr(str(logfile)) + ").write_text(json.dumps({'ready':True,'admin':ctypes.windll.shell32.IsUserAnAdmin()}))"]
            startup = Startup(cb=ctypes.sizeof(Startup))
            startup.lpDesktop = None
            process = ProcessInfo()
            case = {'token': label, 'api': 'CreateProcessWithTokenW'}
            report['cases'].append(case)
            if not api.CreateProcessWithTokenW(int(handle), 0, sys.executable,
                    ctypes.create_unicode_buffer(subprocess.list2cmdline(command)),
                    win32con.CREATE_NO_WINDOW | win32con.CREATE_UNICODE_ENVIRONMENT,
                    ctypes.create_unicode_buffer('\0'.join(key + '=' + value for key, value in sorted(os.environ.items())) + '\0\0'),
                    str(Path.cwd()), ctypes.byref(startup), ctypes.byref(process)):
                case['winerror'] = ctypes.get_last_error()
            else:
                win32api.CloseHandle(process.hThread)
                if win32event.WaitForSingleObject(process.hProcess, 5000) != win32event.WAIT_OBJECT_0:
                    win32process.TerminateProcess(process.hProcess, 124)
                    case['timeout'] = True
                case['exit'] = win32process.GetExitCodeProcess(process.hProcess)
                win32api.CloseHandle(process.hProcess)
                if logfile.exists():
                    case['output'] = json.loads(logfile.read_text())
            print(json.dumps(case), flush=True)
        for label, handle in tokens:
            for isolated in (False, True):
                for explicit in (False, True):
                    stamp = time.monotonic()
                    case = {'token': label, 'isolated_desktop': isolated, 'explicit_process_security': explicit}
                    report['cases'].append(case)
                    startup = win32process.STARTUPINFO()
                    if isolated:
                        startup.lpDesktop = name + r'\default'
                    logfile = output / f'launch-{label}-{int(isolated)}-{int(explicit)}.txt'
                    with logfile.open('wb') as log, open(os.devnull, 'rb') as stdin:
                        startup.dwFlags = win32con.STARTF_USESTDHANDLES
                        startup.hStdOutput = startup.hStdError = msvcrt.get_osfhandle(log.fileno())
                        startup.hStdInput = msvcrt.get_osfhandle(stdin.fileno())
                        os.set_handle_inheritable(startup.hStdOutput, True)
                        os.set_handle_inheritable(startup.hStdInput, True)
                        command = [sys.executable, '-B', '-c', "import ctypes;print('ready',ctypes.windll.shell32.IsUserAnAdmin(),flush=True)"]
                        try:
                            process, thread, _, _ = win32process.CreateProcessAsUser(handle,
                                sys.executable, subprocess.list2cmdline(command), sa if explicit else None,
                                sa if explicit else None, True, win32con.CREATE_NO_WINDOW,
                                dict(os.environ), str(Path.cwd()), startup)
                            thread.Close()
                            if win32event.WaitForSingleObject(process, 5000) != win32event.WAIT_OBJECT_0:
                                win32process.TerminateProcess(process, 124)
                                case['timeout'] = True
                            case['exit'] = win32process.GetExitCodeProcess(process)
                            process.Close()
                        except pywintypes.error as error:
                            case['winerror'] = error.winerror
                    case['output'] = logfile.read_text(encoding='utf-8', errors='replace')[:2000]
                    case['seconds'] = round(time.monotonic() - stamp, 3)
                    print(json.dumps(case), flush=True)
    finally:
        desktop.CloseDesktop()
        desktop.Detach()
        station.CloseWindowStation()
        station.Detach()
        for _, handle in tokens:
            handle.Close()
        (output / 'limited-launch.json').write_text(json.dumps(report, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
