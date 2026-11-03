"""Windows spelling fixtures backed by real temporary marker files, not Windows execution."""
import hashlib
import json
from pathlib import PureWindowsPath
from types import SimpleNamespace

import pytest

from daemon_core.memory import library_layout
from daemon_core import installation_profile as profile


@pytest.fixture
def mirror(tmp_path, monkeypatch):
    class Mirror(PureWindowsPath):
        def disk(self):
            return tmp_path.joinpath(*self.parts[1:])
        def absolute(self):
            return self
        def resolve(self):
            return self
        def is_symlink(self):
            return self.disk().is_symlink()
        def exists(self):
            return self.disk().exists()
        def is_file(self):
            return self.disk().is_file()
        def is_dir(self):
            return self.disk().is_dir()
        def read_text(self, **kwargs):
            return self.disk().read_text(**kwargs)
        def stat(self):
            return self.disk().stat()
    monkeypatch.setattr(library_layout, "Path", Mirror)
    monkeypatch.setattr(profile, "Path", Mirror)
    return Mirror


@pytest.mark.parametrize("extended", [False, True])
@pytest.mark.parametrize("fault", [None, "foreign-root", "foreign-owner"])
def test_worker_reads_exact_vault_and_profile_across_windows_prefix(mirror, extended, fault):
    normal = mirror(r"C:\Users\Fixture\DaemonHarness\hosts\hermes\installations\aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
    root = normal / "daemon/data/memory/.daemon"
    root.disk().mkdir(parents=True)
    vault = {"schema": 1, "layout": "root-folders-v1", "vault_root": str(root.parent), "runtime_root": str(root)}
    if fault == "foreign-root":
        vault["vault_root"] = str(root.parent.parent / "other")
    (root / library_layout.MARKER).disk().write_text(json.dumps(vault))
    def ref(s):
        return hashlib.sha256(s.encode()).hexdigest()
    marker = {"schema": "daemon.installation-profile.v1", "profile": "parga-v0", "user_ref": ref("777002" if fault == "foreign-owner" else "777001"), "memory_root_ref": ref(str(root))}
    (normal / profile.MARKER).disk().write_text(json.dumps(marker))
    attempt = mirror("\\\\?\\" + str(normal)) if extended else normal
    scope = SimpleNamespace(attempt=attempt, home=attempt / "daemon", validate_home=lambda p: p)
    before = (normal / profile.MARKER).disk().read_bytes()
    if fault:
        with pytest.raises(ValueError):
            profile.installed_profile_environment(scope, "777001")
    else:
        values = profile.installed_profile_environment(scope, "777001")
        assert library_layout.vault_root(attempt / "daemon/data/memory/.daemon") == attempt / "daemon/data/memory"
        assert profile.profile_status(values)["ok"]
        assert profile.profile_status(values)["memory_root_ref"] == ref(str(root))
    assert before == (normal / profile.MARKER).disk().read_bytes()


@pytest.mark.parametrize("source,expected", [
    (r"\\?\C:\Users\Fixture", r"C:\Users\Fixture"),
    (r"\\?\UNC\server\share\memory", r"\\server\share\memory"),
    (r"\\.\PhysicalDrive0", r"\\.\PhysicalDrive0"),
    (r"\\?\GLOBALROOT\Device\disk", r"\\?\GLOBALROOT\Device\disk"),
    ("/private/memory", "/private/memory"),
    (r"C:relative", r"C:relative"),
])
def test_only_io_prefix_changes_identity(source, expected):
    from daemon_core.memory.path_identity import path_identity
    assert path_identity(source) == expected
    assert path_identity(expected) == expected


def test_entry_validates_the_canonical_identity(mirror, monkeypatch):
    from daemons_companion.runtime import service_entry as background_entry
    root = mirror(r"C:\Users\Fixture\runtime")
    root.disk().mkdir(parents=True)
    monkeypatch.setattr(background_entry, "Path", mirror)
    assert background_entry._direct_absolute("\\\\?\\" + str(root), "Runtime", directory=True) == root
