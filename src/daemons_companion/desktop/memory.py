"""Fresh empty vault only. Never migrate or author a person's existing notes."""
from __future__ import annotations

import json
from pathlib import Path

from daemon_core.memory.library_layout import FOLDERS, MARKER, vault_root
from daemon_core.memory.integrity import atomic_write_text


def initialize_empty_vault(vault: Path) -> Path:
    if not vault.is_absolute() or vault.resolve() != vault or vault.is_symlink():
        raise ValueError('The new memory vault must be a direct private directory.')
    internal = vault / '.daemon'
    structure = internal / '.memory-structure-prototype.json'
    structure_value = {'schema': 1, 'memory_root': str(internal)}
    if (internal / MARKER).exists():
        if vault_root(internal) != vault:
            raise ValueError('The existing vault marker does not match.')
        return internal
    # This entry point cannot reorganize a previously installed legacy memory.
    # Empty category directories may remain after interrupted initialization.
    if vault.exists():
        for child in vault.iterdir():
            if (child == internal and not child.is_symlink() and child.is_dir()
                    and set(child.iterdir()) == {structure} and not structure.is_symlink()
                    and structure.stat().st_size < 8192
                    and json.loads(structure.read_text(encoding='utf-8')) == structure_value):
                continue  # interrupted between the two canonical markers
            if child.is_symlink() or not child.is_dir() or child.name not in {*FOLDERS, '.daemon'} or any(child.iterdir()):
                raise ValueError('Existing memory needs a separate migration; it will not be changed by setup.')
    internal.mkdir(parents=True, exist_ok=True, mode=0o700)
    for folder in FOLDERS:
        (vault / folder).mkdir(exist_ok=True, mode=0o700)
    # Publish the layout last, after the structure marker and the five folders.
    atomic_write_text(structure, json.dumps(structure_value) + '\n')
    atomic_write_text(internal / MARKER, json.dumps({'schema': 1, 'layout': 'root-folders-v1',
        'vault_root': str(vault), 'runtime_root': str(internal)}) + '\n')
    return internal
