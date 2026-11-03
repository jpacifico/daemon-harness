"""Import-path support copied into the private Windows venv, before other .pth files.

This module has no daemon/package dependency: it also runs in uv's isolated
interpreter queries. It does not change environment variables or registry policy.
"""
import ntpath
import os
import sys


def extended_import_path(value):
    if not isinstance(value, str) or value.startswith("\\\\?\\"):
        return value
    drive, tail = ntpath.splitdrive(value)
    if not drive or not tail.startswith(("/", "\\")):
        return value
    normalized = ntpath.normpath(value)
    if normalized.startswith("\\\\"):
        return "\\\\?\\UNC\\" + normalized[2:]
    return "\\\\?\\" + normalized


def enable():
    if os.name != "nt":
        return
    sys.path[:] = [extended_import_path(p) for p in sys.path]
    # site or another startup hook may already have imported a package (notably
    # importlib). Its submodules use __path__, independently of sys.path.
    for module in tuple(sys.modules.values()):
        paths = getattr(module, "__path__", None)
        if isinstance(paths, list):
            paths[:] = [extended_import_path(p) for p in paths]
