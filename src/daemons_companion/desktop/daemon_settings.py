"""Bounded, explicit edits of existing daemon settings; never personal Markdown.

The desktop main process resolves profiles and supplies their configured paths.
This module owns validation, optimistic concurrency and the atomic metadata write.
It is not imported by the conversational runtime.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

from daemon_core.daemon_identity import identity_document, identity_path, validate_name, VALID_GENDERS
from daemon_core.memory.integrity import atomic_write_text, memory_file_try_lock
from daemon_core.onboarding_user import UserPreferences, validate_bound_user

LIMIT = 8192


class SettingsError(ValueError):
    pass


def _direct_directory(path: Path) -> None:
    if not path.is_absolute() or path.resolve() != path or not path.is_dir() or path.is_symlink():
        raise SettingsError("unsafe_path")


def _read(path: Path, maximum: int) -> bytes | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_size > maximum or info.st_nlink != 1:
        raise SettingsError("invalid_metadata")
    data = path.read_bytes()
    if len(data) > maximum:
        raise SettingsError("invalid_metadata")
    return data


def _stamp(path: Path) -> list | None:
    try:
        info = path.stat()
        return [info.st_dev, info.st_ino, stat.S_IMODE(info.st_mode)]
    except FileNotFoundError:
        return None


def _capabilities(source: str | None) -> dict:
    result = {"name": False, "user": False, "revision": "unknown"}
    if not source:
        return result
    try:
        root = Path(source)
        _direct_directory(root)
        core = root / "daemon_core"
        if not core.is_dir():
            core = root / "src" / "daemon_core"
        _direct_directory(core)
        identity = _read(core / "daemon_identity.py", 65536) or b""
        voice = _read(core / "canonical_voice.py", 1024 * 1024) or b""
        prefs = _read(core / "onboarding_user.py", 65536) or b""
        # Inspect syntax only: never import or execute an existing host's source.
        def names(raw):
            return {n.name for n in ast.walk(ast.parse(raw))
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        result["name"] = "load_daemon_identity" in names(identity) and b"load_daemon_identity(" in voice
        result["user"] = (result["name"] and "validate_bound_user" in names(prefs)
                          and b"validate_bound_user" in identity and b"render_onboarding_user(" in voice)
        result["revision"] = hashlib.sha256(identity + b"\0" + voice + b"\0" + prefs).hexdigest()
    except (OSError, ValueError, SyntaxError, UnicodeError):
        pass
    return result


def _binding(value: object) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"user_id", "platform"}:
        raise SettingsError("invalid_binding")
    try:
        checked = UserPreferences("Validation").for_user(value["user_id"], value["platform"])
    except (TypeError, ValueError):
        raise SettingsError("invalid_binding") from None
    return {key: checked[key] for key in ("user_id", "platform")}


def inspect_settings(root: Path, *, source: str | None, binding: dict | None) -> dict:
    _direct_directory(root)
    target = identity_path(root)
    if target.parent.exists() or target.parent.is_symlink():
        _direct_directory(target.parent)
    raw = _read(target, LIMIT)
    data = {}
    if raw is not None:
        try:
            data = json.loads(raw)
            if not isinstance(data, dict) or set(data) not in ({"name", "gender"}, {"name", "gender", "user"}):
                raise ValueError()
            if validate_name(data["name"]) != data["name"] or data["gender"] not in VALID_GENDERS:
                raise ValueError()
            if "user" in data:
                validate_bound_user(data["user"])
        except (TypeError, ValueError, KeyError):
            raise SettingsError("invalid_metadata") from None
    bound = _binding(binding)
    user = data.get("user")
    # Editing through a second host must never reassign the original account.
    selected = {k: user[k] for k in ("user_id", "platform")} if user else bound
    capabilities = _capabilities(source)
    name_editable = capabilities["name"] and (not user or capabilities["user"])
    user_editable = name_editable and capabilities["user"] and selected is not None
    descriptor = [str(root), _stamp(root), _stamp(target.parent), _stamp(target),
                  hashlib.sha256(raw).hexdigest() if raw is not None else None,
                  source, capabilities, bound]
    return {
        "revision": hashlib.sha256(json.dumps(descriptor, sort_keys=True).encode()).hexdigest(),
        "values": {"name": data.get("name", ""), "gender": data.get("gender", ""),
                   "preferred_name": user["preferred_name"] if user else "",
                   "pronouns": user["pronouns"] if user else ""},
        "canEditName": bool(name_editable), "canEditUser": bool(user_editable),
        "userReason": "" if user_editable else "unsupported" if not capabilities["user"] else "unbound",
        "userScope": selected, "configured": bool(data),
    }


def update_settings(root: Path, *, source: str | None, binding: dict | None,
                    revision: str, values: dict) -> dict:
    if not isinstance(values, dict) or set(values) != {"name", "gender", "preferred_name", "pronouns"}:
        raise SettingsError("invalid_values")
    _direct_directory(root)
    target = identity_path(root)
    # Same lock as the canonical writer; no wait on a background process.
    with memory_file_try_lock(target) as acquired:
        if not acquired:
            raise SettingsError("busy")
        before = inspect_settings(root, source=source, binding=binding)
        if before["revision"] != revision:
            raise SettingsError("changed")
        if not before["canEditName"]:
            raise SettingsError("unsupported")
        user = None
        try:
            if before["canEditUser"]:
                if values["preferred_name"] or values["pronouns"]:
                    prefs = UserPreferences(values["preferred_name"], values["pronouns"])
                    user = prefs.for_user(**before["userScope"])
                elif before["values"]["preferred_name"]:
                    raise ValueError("A configured addressing name cannot be erased implicitly.")
            elif any(values[k] != before["values"][k] for k in ("preferred_name", "pronouns")):
                raise SettingsError("unsupported")
            document = identity_document(root, values["name"], values["gender"], user=user)
        except (TypeError, ValueError):
            raise SettingsError("invalid_values") from None
        content = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
        if inspect_settings(root, source=source, binding=binding)["revision"] != revision:
            raise SettingsError("changed")
        current = _read(target, LIMIT)
        if current is None or json.loads(current) != document:
            created = not target.parent.exists()
            target.parent.mkdir(exist_ok=True, mode=0o700)
            _direct_directory(target.parent)
            try:
                atomic_write_text(target, content)
            except BaseException:
                if created:
                    try:
                        target.parent.rmdir()
                    except OSError:
                        pass
                raise
        # Atomic writes create a private file; preserve an unchanged file's mode.
        if os.name != "nt" and target.stat().st_mode & 0o077:
            target.chmod(0o600)
        after = inspect_settings(root, source=source, binding=binding)
        if after["values"] != {"name": document["name"], "gender": document["gender"],
                               "preferred_name": document.get("user", {}).get("preferred_name", ""),
                               "pronouns": document.get("user", {}).get("pronouns", "")}:
            raise SettingsError("unconfirmed")
        return after


def handle_request(request: object) -> dict:
    if not isinstance(request, dict) or request.get("action") not in {"inspect", "save"}:
        raise SettingsError("invalid_request")
    expected = {"action", "root", "source", "binding"}
    if request["action"] == "save":
        expected |= {"revision", "values"}
    if set(request) != expected or not isinstance(request["root"], str) or len(request["root"]) > 8192:
        raise SettingsError("invalid_request")
    root = Path(request["root"])
    args = {"source": request["source"], "binding": request["binding"]}
    if request["action"] == "inspect":
        return inspect_settings(root, **args)
    return update_settings(root, **args, revision=request["revision"], values=request["values"])


def main() -> None:
    try:
        raw = sys.stdin.buffer.read(32769)
        if len(raw) > 32768:
            raise SettingsError("invalid_request")
        result = {"ok": True, "settings": handle_request(json.loads(raw))}
    except SettingsError as error:
        result = {"ok": False, "code": str(error)}
    except (OSError, ValueError, TypeError, KeyError):
        result = {"ok": False, "code": "unavailable"}
    # No raw exception, credentials, Markdown or source content leaves this CLI.
    # ASCII framing also works with isolated Windows Python and legacy code pages.
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
