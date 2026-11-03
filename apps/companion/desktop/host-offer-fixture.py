"""Isolated packaged host-choice regression: no network, accounts or services."""
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from daemons_companion.desktop.protocol import DesktopUI
from daemons_companion.runtime.acquisition import review_host_setup
from daemons_companion.desktop.host_terminal import PackagedHostSetup
from daemons_companion.desktop.terminal import Back, Quit

ui = DesktopUI(sys.stdin, sys.stdout)
ui.start()
root = Path(os.environ['DAEMON_HARNESS_COMPANION_INSTALL_HOME'])
owner = PackagedHostSetup.from_environment(os.environ)
owner.context = replace(owner.context, root_folder_memory=True)
def execute(layout, **kwargs):
    # External download/process boundary only; production plan and user approval run.
    assert layout.companion_home == root
    from daemons_companion.runtime.bootstrap import run
    run([sys.executable, '-c', "import time;print('synthetic download',flush=True);time.sleep(2.5)"], dict(os.environ), progress=kwargs.get('progress'))
    (root / 'synthetic-host-confirmed.json').write_text(json.dumps({'root': str(root), 'target_os': layout.target_os}))
    return SimpleNamespace(status='committed')
owner.execute = execute
try:
    if sys.argv[1] == 'first-choice':
        from functools import partial
        from daemons_companion.desktop.app import handle_request
        from daemons_companion.setup.plan import HostKind, HostProbe
        from daemons_companion.desktop.terminal import run_wizard
        probes = tuple(HostProbe(kind, False, False) for kind in HostKind)
        owner.resume = lambda ui: False
        run_wizard(ui, backend=partial(handle_request, probes=probes, finder=lambda _:None,
                   target_os=owner.context.target_os), host_setup=owner, packaged=True, supported_only=True)
        raise SystemExit(0)
    ui.phase('host')
    setup = review_host_setup(host='hermes', detected=sys.argv[1] != 'absent', usable=True,
                             version='99.0.0', version_status='verified', platform=owner.context.target_os)
    try:
        owner.offer(ui, 'hermes', setup)
    except Back:
        ui.emit('finished', result='unfinished', message='Synthetic host choice returned without installing.')
    else:
        ui.emit('finished', result='unfinished', message='Synthetic host confirmation complete. No real host was installed.')
except (Quit, KeyboardInterrupt):
    ui.emit('finished', result='cancelled', message='Synthetic setup cancelled.')
finally:
    ui.finished = True
