"""Compare validated Windows roots without confusing an I/O prefix with identity."""
from __future__ import annotations

import re


def path_identity(value: str) -> str:
    """Remove only the extended drive/UNC prefix, never resolve or broaden a path.

    Callers still enforce direct paths, ownership and exact root membership.
    This is not an I/O path: keep the original extended spelling for file access.
    Device paths, relative paths, case, dot components and POSIX paths stay exact.
    """
    if re.match(r"^\\\\\?\\[A-Za-z]:\\", value):
        return value[4:]
    if value.startswith("\\\\?\\UNC\\"):
        rest = value[8:]
        parts = rest.split("\\")
        if len(parts) >= 2 and all(parts[:2]):
            return "\\\\" + rest
    return value
