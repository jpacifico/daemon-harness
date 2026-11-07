"""Physical, user-owned note folders with identities independent of filenames.

Only an explicit prototype marker enables this layout. Classification remains
with the existing curator; this module bounds paths, identities and writes.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import json
import logging
import os
from functools import lru_cache
from pathlib import Path, PurePosixPath
import re
import unicodedata

from daemon_core.memory.path_identity import path_identity
from daemon_core.memory.metadata import parse_souvenir_frontmatter, _render_frontmatter

FOLDERS = ('Projects', 'Areas', 'Resources', 'Garden', 'Archives')
MARKER = 'vault-layout.json'
IDENTITY = 'memory_id'
logger = logging.getLogger(__name__)
# One definition of Garden for every prompt that files or classifies notes.
GARDEN_RULE = (
    "Garden holds the user's own ideas, principles, methods or lessons, stated or "
    "explicitly adopted by the user, that could serve in another part of their life. "
    "A fact, relationship, feeling or situation of the user's life stays with its "
    "subject (usually Areas or Projects), however important. An assistant suggestion "
    "the user has not taken up is not kept."
)
# Only prompts that write notes after a conversation receive the growth rule.
GARDEN_GROWTH_RULE = (
    "When the user returns to, applies, refines or contradicts an idea, update its "
    "existing Garden note and keep the earlier steps in History instead of creating "
    "another note."
)


def garden_rule_block(*, growth: bool = False) -> str:
    """The Garden definition as wrapped prompt bullets."""
    import textwrap
    rules = (GARDEN_RULE, GARDEN_GROWTH_RULE) if growth else (GARDEN_RULE,)
    return ''.join(textwrap.fill(rule, 78, initial_indent='- ', subsequent_indent='  ') + '\n'
                   for rule in rules)


_NOTE_SCAN: ContextVar[dict | None] = ContextVar('note_scan', default=None)


class LibraryLayoutError(ValueError):
    """A physical location or identity is unsafe or ambiguous."""


@dataclass(frozen=True)
class NoteLocation:
    name: str
    path: Path
    registered: bool


def vault_root(memory_root: Path) -> Path | None:
    """Resolve an explicit sibling vault, never arbitrary external directories."""
    root = Path(memory_root).absolute()
    marker = root / MARKER
    if not marker.exists():
        return None
    if root.name != '.daemon' or root.is_symlink() or root.resolve() != root or marker.is_symlink():
        raise LibraryLayoutError('Invalid internal memory root')
    expected = {'schema': 1, 'layout': 'root-folders-v1',
                'vault_root': str(root.parent), 'runtime_root': str(root)}
    recorded = json.loads(marker.read_text(encoding='utf-8'))
    if isinstance(recorded, dict):
        recorded = {key: path_identity(value) if key in {"vault_root", "runtime_root"} and isinstance(value, str) else value
                    for key, value in recorded.items()}
    expected["vault_root"] = path_identity(expected["vault_root"])
    expected["runtime_root"] = path_identity(expected["runtime_root"])
    if recorded != expected:
        raise LibraryLayoutError('Vault marker does not match the exact roots')
    return root.parent


def valid_name(name: str) -> str:
    if (not isinstance(name, str) or not name or len(name) > 512
            or name.startswith('/') or '\\' in name or any(c in name for c in '\x00\r\n:#|[]')
            or any(part in ('', '.', '..') for part in name.split('/'))):
        raise LibraryLayoutError('Invalid note identity')
    return name.removesuffix('.md')


def checked_note_path(vault: Path, relative: str) -> Path:
    raw = PurePosixPath(relative)
    if (not relative or raw.is_absolute() or '\\' in relative
            or any(p in ('', '.', '..') for p in relative.split('/'))
            or len(raw.parts) < 2 or raw.parts[0] not in FOLDERS or raw.suffix != '.md'
            or any(c in relative for c in '\x00\r\n<>:"|?*[]#')
            or any(p.endswith((' ', '.')) or re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', p) for p in raw.parts)):
        raise LibraryLayoutError('A note must be a Markdown file inside a root category')
    path = vault.joinpath(*raw.parts)
    if path.resolve() != path or not path.resolve().is_relative_to(vault):
        raise LibraryLayoutError('Symlink or escaped note location')
    return path


def _unregistered_name(relative: str) -> str:
    # A new human file is discoverable immediately. Its identity is persisted
    # only on a legitimate write; a stale update still needs an existing file.
    return 'local/' + hashlib.sha256(relative.encode('utf-8')).hexdigest()[:24]


@contextmanager
def single_note_scan():
    """Resolve every note link of one read-only pass against a single scan.

    Without it each link rescans the whole vault: about 400 links on a large
    memory cost seconds on a cold first turn. Writes never run inside it.
    """
    token = _NOTE_SCAN.set({}) if _NOTE_SCAN.get() is None else None
    try:
        yield
    finally:
        if token is not None:
            _NOTE_SCAN.reset(token)


def note_locations(memory_root: Path) -> list[NoteLocation]:
    scan = _NOTE_SCAN.get()
    key = str(Path(memory_root).absolute())
    if scan is not None and key in scan:
        return list(scan[key])
    found = _scan_note_locations(memory_root)
    if scan is not None:
        scan[key] = tuple(found)
    return found


def _scan_note_locations(memory_root: Path) -> list[NoteLocation]:
    vault = vault_root(memory_root)
    if vault is None:
        root = Path(memory_root) / 'capsules'
        return [NoteLocation(p.relative_to(root).with_suffix('').as_posix(), p, True)
                for p in sorted(root.rglob('*.md')) if not p.is_symlink()]
    signatures = []
    for folder in FOLDERS:
        directory = vault / folder
        if directory.is_symlink():
            raise LibraryLayoutError('Symlink category directory')
        for parent, directories, filenames in os.walk(directory, followlinks=False):
            for child in directories:
                if (Path(parent) / child).is_symlink():
                    raise LibraryLayoutError('Symlink note directory')
            directories[:] = sorted(d for d in directories if not d.startswith('.'))
            for filename in sorted(filenames):
                if not filename.endswith('.md') or filename.startswith('.'):
                    continue
                path = Path(parent) / filename
                if path.is_symlink():
                    raise LibraryLayoutError('Symlink note file')
                try:
                    st = path.stat()
                except FileNotFoundError:
                    continue  # An ordinary concurrent move: the next scan sees it.
                signatures.append((str(path), st.st_mtime_ns, st.st_ctime_ns, st.st_size, st.st_ino))
    return list(_locations_from_signatures(str(vault), tuple(sorted(signatures))))


@lru_cache(maxsize=16)
def _locations_from_signatures(vault_string, signatures):
    vault = Path(vault_string)
    found, case_paths = [], set()
    for signature in signatures:
        path = Path(signature[0])
        relative = path.relative_to(vault).as_posix()
        checked_note_path(vault, relative)
        portable_key = unicodedata.normalize('NFC', relative).casefold()
        if portable_key in case_paths:
            raise LibraryLayoutError('Case-insensitive filename collision')
        case_paths.add(portable_key)
        try:
            name, registered = _read_identity(signature, relative)
        except (OSError, ValueError, UnicodeError):
            logger.warning('Unreadable note metadata excluded from discovery; file preserved')
            continue
        found.append(NoteLocation(name, path, registered))
    return tuple(found)


@lru_cache(maxsize=8192)
def _read_identity(signature, relative):
    import yaml
    try:
        metadata, _ = parse_souvenir_frontmatter(Path(signature[0]).read_text(encoding='utf-8'))
    except yaml.YAMLError as exc:
        raise LibraryLayoutError('Invalid note metadata') from exc
    identity = metadata.get(IDENTITY)
    return (valid_name(identity) if identity is not None else _unregistered_name(relative), identity is not None)


def unique_note_locations(memory_root: Path) -> list[NoteLocation]:
    notes = note_locations(memory_root)
    counts = {}
    for item in notes:
        counts[item.name] = counts.get(item.name, 0) + 1
    if any(count > 1 for count in counts.values()):
        logger.error('Duplicate memory identities excluded from discovery; files preserved')
    return [item for item in notes if counts[item.name] == 1]


def resolve_note(memory_root: Path, name: str, *, storage_path: str | None = None,
                 create: bool = False) -> Path:
    name = valid_name(name)
    root = Path(memory_root)
    vault = vault_root(root)
    if vault is None:
        return root / 'capsules' / (name + '.md')
    found = [item for item in note_locations(root) if item.name == name]
    if len(found) > 1:
        raise LibraryLayoutError('Duplicate note identity; refuse an ambiguous operation')
    if found:
        return found[0].path
    if not create:
        raise FileNotFoundError('Note identity no longer exists: ' + name)
    # A known identity cannot be resurrected as a fresh file after a user delete.
    index = root / 'capsules/index.md'
    if index.exists() and any(line.strip().startswith('- ' + name + ':')
                              for line in index.read_text(encoding='utf-8').splitlines()):
        raise LibraryLayoutError('Known note was removed; a stale update cannot recreate it')
    migration = root / 'root-migration.json'
    if migration.is_file() and any(item.get('name') == name for item in
            json.loads(migration.read_text(encoding='utf-8')).get('notes', [])):
        raise LibraryLayoutError('Migrated note was removed; a stale update cannot recreate it')
    if name.startswith('local/'):
        raise LibraryLayoutError('A human-created note moved or disappeared; reread it')
    if not isinstance(storage_path, str):
        raise LibraryLayoutError('A new note requires an explicit storage_path from its curator')
    target = checked_note_path(vault, storage_path)
    if target.exists():
        raise LibraryLayoutError('The chosen path already belongs to another note')
    key = unicodedata.normalize('NFC', storage_path).casefold()
    if any(unicodedata.normalize('NFC', p.path.relative_to(vault).as_posix()).casefold() == key
           for p in note_locations(root)):
        raise LibraryLayoutError('Portable filename collision')
    return target


def name_for_path(memory_root: Path, path: Path) -> str | None:
    vault = vault_root(memory_root)
    if vault is None:
        try:
            return path.relative_to(Path(memory_root) / 'capsules').with_suffix('').as_posix()
        except ValueError:
            return None
    matches = [item.name for item in note_locations(memory_root) if item.path == path]
    return matches[0] if len(matches) == 1 else None


def with_identity(content: str, name: str, *, current: str = '') -> str:
    valid_name(name)
    metadata, body = parse_souvenir_frontmatter(content)
    previous, _ = parse_souvenir_frontmatter(current)
    if any(value is not None and value != name for value in (metadata.get(IDENTITY), previous.get(IDENTITY))):
        raise LibraryLayoutError('A note identity is immutable during content maintenance')
    if metadata.get(IDENTITY) == name:
        return content
    return _render_frontmatter({IDENTITY: name, **metadata}, body)


def resolve_reference(memory_root: Path, relative: str) -> Path:
    """Historical technical addresses stay stable, while note paths may move."""
    relative = relative.removeprefix('memory/')
    if relative.startswith('capsules/') and relative.endswith('.md') and relative != 'capsules/index.md':
        return resolve_note(memory_root, relative[len('capsules/'):].removesuffix('.md'))
    target = Path(memory_root) / relative
    if target.resolve() != target or not target.resolve().is_relative_to(Path(memory_root).resolve()):
        raise LibraryLayoutError('Unsafe memory reference')
    return target


def memory_reference(memory_root: Path, path: Path) -> str:
    name = name_for_path(memory_root, path)
    if name is not None:
        return 'capsules/' + name + '.md'
    return path.relative_to(memory_root).as_posix()


def link_target(memory_root: Path, name: str) -> str:
    vault = vault_root(memory_root)
    if vault is None:
        return 'capsules/' + valid_name(name)
    return resolve_note(memory_root, name).relative_to(vault).with_suffix('').as_posix()


def name_from_vault_link(memory_root: Path, target: str, *, source: str) -> str | None:
    vault = vault_root(memory_root)
    if vault is None:
        return None
    import posixpath
    if target.startswith(('./', '../')):
        parent = resolve_note(memory_root, source).relative_to(vault).parent.as_posix()
        target = posixpath.normpath(posixpath.join(parent, target))
    if target.split('/', 1)[0] not in FOLDERS:
        if '/' not in target and not any(c in target for c in ':#|[]'):
            stem = target.removesuffix('.md')
            matches = [item.name for item in unique_note_locations(memory_root) if item.path.stem == stem]
            return matches[0] if len(matches) == 1 else None
        return None
    path = checked_note_path(vault, target if target.endswith('.md') else target + '.md')
    return name_for_path(memory_root, path)


def render_vault_links(memory_root: Path, content: str, *, mapping: dict[str, str] | None = None) -> str:
    """Retarget explicit historical note links, never link plain factual prose."""
    if vault_root(memory_root) is None:
        return content
    def replace(match):
        raw = match[1]
        reference, separator, label = raw.partition('|')
        target, anchor_separator, anchor = reference.partition('#')
        normalized = target.removeprefix('memory/')
        if normalized.startswith('levels/L0_ground_zero/') and anchor_separator:
            from urllib.parse import quote
            relative = normalized if normalized.endswith('.md') else normalized + '.md'
            path = resolve_reference(memory_root, relative)
            if path.is_file():
                return '[' + (label if separator else 'Original source') + '](' + path.as_uri() + '#' + quote(anchor, safe='') + ')'
        if not normalized.startswith('capsules/'):
            return match[0]
        name = normalized[len('capsules/'):].removesuffix('.md')
        try:
            physical = mapping[name] if mapping is not None else link_target(memory_root, name)
        except (OSError, ValueError, KeyError):
            return match[0]  # Unresolved links remain explicit, never fabricated.
        return '[[' + physical.removesuffix('.md') + (anchor_separator + anchor if anchor_separator else '') + (separator + label if separator else '') + ']]'
    lines, fence = [], ''
    for line in content.splitlines(keepends=True):
        match = re.match(r'^\s*(`{3,}|~{3,})', line)
        if match:
            fence = '' if fence == match[1][0] else match[1][0]
            lines.append(line)
        elif fence:
            lines.append(line)
        else:
            lines.append(re.sub(r'\[\[([^\]\r\n]+)\]\]', replace, line))
    return ''.join(lines)


def internal_source_reference(memory_root: Path, raw: str) -> str | None:
    """Read an explicit local-source link only inside this vault's L0 root."""
    from urllib.parse import unquote, urlsplit
    match = re.fullmatch(r'\[[^\]\r\n]*\]\((file://[^\s]+)\)', raw.strip())
    if not match or vault_root(memory_root) is None:
        return None
    parsed = urlsplit(match[1])
    if parsed.netloc or parsed.query or not parsed.fragment:
        return None
    path = Path(unquote(parsed.path))
    try:
        relative = path.relative_to(memory_root).as_posix()
        if not relative.startswith('levels/L0_ground_zero/') or resolve_reference(memory_root, relative) != path:
            return None
    except ValueError:
        return None
    return relative.removesuffix('.md') + '#' + unquote(parsed.fragment)

