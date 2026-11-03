"""Real local file operations with an explicit simulated Win32 MAX_PATH guard."""
import io
import os
from pathlib import Path


class WindowsPathGuard:
    # A POSIX-only stand-in for the extended Win32 namespace. Production uses
    # the separately tested \\?\ drive/UNC conversion, never this test prefix.
    prefix = '//?'

    def __init__(self, monkeypatch, root):
        self.root = str(root)
        self.checked = []
        self.denied = []
        self.extended = []
        for module, names in ((os, ('open', 'stat', 'lstat', 'mkdir', 'scandir', 'listdir', 'chmod', 'unlink', 'rmdir', 'readlink')),
                              (io, ('open',))):
            for name in names:
                original = getattr(module, name)
                def call(path, *args, _original=original, **kwargs):
                    return _original(self.translate(path), *args, **kwargs)
                monkeypatch.setattr(module, name, call)
        for name in ('replace', 'rename'):
            original = getattr(os, name)
            def move(source, destination, *args, _original=original, **kwargs):
                return _original(self.translate(source), self.translate(destination), *args, **kwargs)
            monkeypatch.setattr(os, name, move)

    def translate(self, path):
        if isinstance(path, int):
            return path
        value = os.fspath(path)
        if not isinstance(value, str):
            return value
        extended = value.startswith(self.prefix + '/')
        plain = value[len(self.prefix):] if extended else value
        if plain.startswith(self.root):
            self.checked.append(plain)
            if extended:
                self.extended.append(plain)
            elif len(plain) >= 260:
                self.denied.append(plain)
                raise FileNotFoundError(2, 'Win32 MAX_PATH guard (simulated)', plain)
        return plain

    def io_path(self, path):
        value = str(path)
        if value.startswith(self.prefix + '/'):
            return path
        return Path(self.prefix + str(path.absolute()))
