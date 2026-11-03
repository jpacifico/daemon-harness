"""Local management bridge for the pinned Hermes Agent installation.

Use the host's discovery, status and mutation functions. Inventory deliberately
omits catalog presentation metadata: listing two owned plugins must not fetch a
remote catalog or start the unrelated top-level CLI lifecycle. Native enable/
disable still own their security checks and configuration writes.
"""
from __future__ import annotations

import json
import sys

MANAGED_PLUGINS = frozenset({"daemon_core", "daemon_voice"})


def main(arguments=None):
    args = list(sys.argv[1:] if arguments is None else arguments)
    if args == ["plugins", "list", "--json"]:
        from hermes_cli import plugins_cmd as native
        entries = native._discover_all_plugins()
        enabled, disabled = native._get_enabled_set(), native._get_disabled_set()
        active = native._category_active_names()
        rows = [
            {"name": name, "status": native._plugin_status(
                name, enabled, disabled, key=key, source=source,
                dir_path=directory, active=active)}
            for name, _version, _description, source, directory, key in entries
            if name in MANAGED_PLUGINS
        ]
        print(json.dumps(rows))
    elif len(args) == 3 and args[0] == "plugins" and args[1] in {"enable", "disable"} and args[2] in MANAGED_PLUGINS:
        from hermes_cli import plugins_cmd as native
        if args[1] == "enable":
            native.cmd_enable(args[2])
        else:
            native.cmd_disable(args[2])
    elif args == ["gateway", "status"]:
        from gateway.status import get_running_pid
        raise SystemExit(0 if get_running_pid() else 1)
    else:
        raise ValueError("Unsupported private host management command.")


if __name__ == "__main__":
    main()
