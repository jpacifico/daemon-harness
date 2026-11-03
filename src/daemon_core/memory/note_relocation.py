"""Publish a curator's sourced folder transition; never infer its meaning.

Identity addresses stay stable. Only local link destinations change as part of
the move. The private write-ahead journal permits recovery without a new model
decision, and compare-before-write checks preserve intervening human edits.
"""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
import base64
import hashlib
import json
import os
from pathlib import Path
import posixpath
import re
import tempfile
import unicodedata
from urllib.parse import quote, unquote, urlsplit

from daemon_core.memory.integrity import atomic_write_text, memory_file_lock
from daemon_core.memory.library_layout import (
    FOLDERS, LibraryLayoutError, checked_note_path, note_locations,
    resolve_note, vault_root,
)

ENV = "DAEMON_CORE_MEMORY_RELOCATION"
MAX_NOTE_BYTES = 512_000
MAX_TRANSACTION_BYTES = 8_000_000
MAX_CHANGED_NOTES = 256
SCHEMA = "daemon.note-relocation.v1"

RULES = """PARGA folder transitions for exact loaded notes (after this answer only):
- An existing capsule_updates item may additionally contain
  "relocation": {"from": "exact supplied physical path", "to": "new physical path",
  "evidence_quote": "verbatim excerpt of the latest user message", "reason": "why the same note changes use"}.
  Include the complete content_md as usual; protected merging still applies.
- Judge whether the latest direct user evidence changes this note's primary
  purpose or explicitly ends it. A finished/abandoned project belongs in Archives;
  the same idea becoming a concrete committed project may move Garden -> Projects;
  explicit renewed activity can reactivate an archive. Preserve identity, dated
  history and sources. Use an English destination under the five PARGA folders,
  retaining the existing filename. No historical batch reclassification.
- Mere age, silence, a reference to the past, a hypothetical plan or an assistant
  suggestion never authorizes a move. A still reusable idea that inspires a
  separate project stays in Garden: create/link the distinct project instead.
- Only propose relocation of a supplied exact note with a supplied location.
  Current user edits/moves take priority. A folder mismatch or memory_views alone
  is not a reason to move. When uncertain, preserve the present location.
- The publisher keeps links pointing to the same identities. Do not rewrite
  unrelated notes, REM episodes, L0 or the index to simulate a move.
"""


@dataclass(frozen=True)
class NoteSnapshot:
    name: str
    relative: str
    content: bytes


@dataclass(frozen=True)
class RelocationContext:
    source_ref: str
    snapshots: dict[str, NoteSnapshot]


@dataclass(frozen=True)
class Relocation:
    snapshot: NoteSnapshot
    destination: str
    source_ref: str
    evidence_quote: str
    reason: str


def relocation_enabled(root: Path) -> bool:
    from daemon_core.memory.context_pages import structure_enabled
    return os.getenv(ENV) == "v1" and structure_enabled(root) and vault_root(root) is not None


def capture_context(root: Path, exact_notes: dict[str, str], source_ref: str) -> RelocationContext:
    """Bind the decision to the bytes and locations actually read, before LLM."""
    vault = vault_root(root)
    snapshots = {}
    if vault is not None and source_ref:
        for name, content in exact_notes.items():
            path = resolve_note(root, name)
            raw = path.read_bytes()
            if len(raw) <= MAX_NOTE_BYTES and raw.decode("utf-8").replace("\r\n", "\n").strip() == content.strip():
                snapshots[name] = NoteSnapshot(name, path.relative_to(vault).as_posix(), raw)
    return RelocationContext(source_ref, snapshots)


def location_prompt(context: RelocationContext) -> str:
    return "\n\nExact physical locations of the loaded notes:\n" + json.dumps(
        {name: s.relative for name, s in context.snapshots.items()}, ensure_ascii=False)


def validate_proposal(root: Path, name: str, raw: object,
                      context: RelocationContext | None, user_message: str) -> Relocation:
    from daemon_core.memory.reader import is_system_managed_souvenir
    if not relocation_enabled(root) or context is None or not context.source_ref:
        raise LibraryLayoutError("Folder transitions are not enabled with an exact user source")
    if not isinstance(raw, dict) or is_system_managed_souvenir(name):
        raise LibraryLayoutError("Invalid or protected relocation target")
    snapshot = context.snapshots.get(name)
    if snapshot is None or raw.get("from") != snapshot.relative:
        raise LibraryLayoutError("Relocation must use the exact loaded location")
    evidence, reason, destination = (raw.get(k) for k in ("evidence_quote", "reason", "to"))
    if (not isinstance(evidence, str) or not 1 <= len(evidence.strip()) <= 1200
            or " ".join(evidence.split()) not in " ".join(user_message.split())
            or not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 1200
            or not isinstance(destination, str)):
        raise LibraryLayoutError("Relocation requires exact current user evidence and a bounded reason")
    target = checked_note_path(vault_root(root), destination)
    if target.name != Path(snapshot.relative).name or destination == snapshot.relative:
        raise LibraryLayoutError("A folder transition preserves the filename and changes its location")
    if len(Path(destination).parts) > 5:
        raise LibraryLayoutError("Overly nested relocation destination")
    ensure_snapshot(root, snapshot)
    _check_destination(root, destination)
    return Relocation(snapshot, destination, context.source_ref, evidence.strip(), reason.strip())


def ensure_snapshot(root: Path, snapshot: NoteSnapshot) -> None:
    path = resolve_note(root, snapshot.name)
    if (path.relative_to(vault_root(root)).as_posix() != snapshot.relative
            or path.read_bytes() != snapshot.content):
        raise LibraryLayoutError("Note changed or moved since the curator read it")


def _check_destination(root: Path, destination: str) -> Path:
    vault = vault_root(root)
    target = checked_note_path(vault, destination)
    key = unicodedata.normalize("NFC", destination).casefold()
    if target.exists() or any(unicodedata.normalize("NFC", p.path.relative_to(vault).as_posix()).casefold() == key
                              for p in note_locations(root)):
        raise LibraryLayoutError("Relocation destination already exists")
    return target


def _relative_target(raw: str, source: str, paths: set[str], *, wiki: bool) -> str | None:
    """Resolve local addresses, not prose, aliases or a guessed relationship."""
    raw = unquote(raw)
    if not raw or raw.startswith(("/", "\\")) or urlsplit(raw).scheme:
        return None
    if wiki and "/" not in raw:
        matches = [p for p in paths if Path(p).stem == raw.removesuffix(".md")]
        return matches[0] if len(matches) == 1 else None
    if raw.split("/", 1)[0] in FOLDERS:
        path = posixpath.normpath(raw)
    else:
        path = posixpath.normpath(posixpath.join(posixpath.dirname(source), raw))
    if path.startswith("../") or path.startswith("/"):
        return None
    if wiki and not path.endswith(".md"):
        path += ".md"
    return path


def retarget_links(content: str, *, source: str, old: str, new: str, paths: set[str]) -> str:
    """Change only link destinations. Anchors, labels, examples and prose survive."""
    next_source = new if source == old else source

    def destination(raw, wiki=False):
        address, anchor, fragment = raw.partition("#")
        if not address or "?" in address:
            return raw
        target = _relative_target(address, source, paths, wiki=wiki)
        if target is None:
            return raw
        final = new if target == old else target
        if target != old and (source != old or (wiki and not address.startswith(("./", "../")))):
            return raw
        if wiki:
            final = final if address.endswith(".md") else final.removesuffix(".md")
        else:
            final = quote(posixpath.relpath(final, posixpath.dirname(next_source)), safe="/.-_~")
        return final + (anchor + fragment if anchor else "")

    def wiki(match):
        address, separator, label = match[1].partition("|")
        return "[[" + destination(address, True) + (separator + label if separator else "") + "]]"

    def markdown(match):
        raw = match[2] or match[3]
        changed = destination(raw)
        return match[1] + ("<" + changed + ">" if match[2] is not None else changed) + match[4]

    output, fence = [], ""
    for line in content.splitlines(keepends=True):
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            if not fence:
                fence = marker[1]
            elif marker[1][0] == fence[0] and len(marker[1]) >= len(fence):
                fence = ""
            output.append(line)
            continue
        if fence or line.startswith(("    ", "\t")):
            output.append(line)
            continue
        # Inline code is inert; scanning links must not edit examples.
        def plain(text):
            text = re.sub(r"\[\[([^\]\r\n]+)\]\]", wiki, text)
            text = re.sub(r'(\[[^\]\r\n]*\]\()(?:<([^>\r\n]+)>|([^\s()\r\n]+))(\s+(?:"[^"\r\n]*"|\x27[^\x27\r\n]*\x27)\)|\))', markdown, text)
            # Native Markdown reference definitions: [ref]: destination "title".
            text = re.sub(r'(^ {0,3}\[[^\]\r\n]+\]:\s*)(?:<([^>\r\n]+)>|([^\s\r\n]+))(.*)', markdown, text)
            return text
        cursor = 0
        for code in re.finditer(r"(?<!`)(`+)(?!`)(.*?)\1(?!`)", line):
            output.extend((plain(line[cursor:code.start()]), code[0]))
            cursor = code.end()
        output.append(plain(line[cursor:]))
    return "".join(output)


def _state_dir(root: Path) -> Path:
    directory = root / "levels" / "note_relocations"
    if directory.resolve() != directory or not directory.is_relative_to(root):
        raise LibraryLayoutError("Unsafe relocation state directory")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory


def _json(path: Path, value: dict) -> None:
    if path.is_symlink():
        raise LibraryLayoutError("Symlink relocation state")
    atomic_write_text(path, json.dumps(value, ensure_ascii=False) + "\n")
    path.chmod(0o600)


def _bytes(path: Path, content: bytes) -> None:
    """Atomic binary writes retain original newline bytes on Windows too."""
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _decode(entry: dict, key: str) -> bytes:
    raw = base64.b64decode(entry[key], validate=True)
    if len(raw) > MAX_NOTE_BYTES:
        raise LibraryLayoutError("Relocation note exceeds byte bound")
    return raw


def _locks(vault: Path, journal: dict, stack: ExitStack) -> dict[str, Path]:
    paths = {entry["path"] for entry in journal["writes"]} | {journal["to"]}
    checked = {p: checked_note_path(vault, p) for p in paths}
    for p in sorted(checked):
        stack.enter_context(memory_file_lock(checked[p]))
    return checked


def _validate_journal(vault: Path, journal: dict) -> None:
    """Recovery revalidates that stored byte changes are only link retargeting."""
    from daemon_core.memory.metadata import parse_souvenir_frontmatter
    if (journal.get("schema") != SCHEMA or not re.fullmatch(r"[a-f0-9]{64}", journal.get("event_id", ""))
            or not 1 <= len(journal.get("writes", [])) <= MAX_CHANGED_NOTES):
        raise LibraryLayoutError("Invalid relocation journal schema")
    event = hashlib.sha256((journal["source_ref"] + "\0" + journal["name"]).encode()).hexdigest()
    old, new = journal["from"], journal["to"]
    if event != journal["event_id"] or checked_note_path(vault, old).name != checked_note_path(vault, new).name or old == new:
        raise LibraryLayoutError("Invalid relocation journal identity")
    paths = set(journal["paths"])
    if len(paths) > 50_000 or old not in paths or new in paths:
        raise LibraryLayoutError("Invalid relocation link snapshot")
    for path in paths:
        checked_note_path(vault, path)
    seen, total = set(), 0
    for entry in journal["writes"]:
        if entry["path"] in seen or entry["path"] not in paths:
            raise LibraryLayoutError("Ambiguous relocation journal write")
        seen.add(entry["path"])
        before, after = _decode(entry, "before"), _decode(entry, "after")
        total += len(before) + len(after)
        expected = retarget_links(before.decode("utf-8"), source=entry["path"], old=old, new=new, paths=paths)
        if expected.encode("utf-8") != after or total > MAX_TRANSACTION_BYTES:
            raise LibraryLayoutError("Journal contains a non-mechanical or oversized write")
        if entry["path"] == old and parse_souvenir_frontmatter(before.decode("utf-8"))[0].get("memory_id") != journal["name"]:
            raise LibraryLayoutError("Journal source identity mismatch")
    if old not in seen:
        raise LibraryLayoutError("Journal lacks its source note")


def _finish(root: Path, journal: dict, checked: dict[str, Path]) -> None:
    source, target = checked[journal["from"]], checked[journal["to"]]
    for entry in journal["writes"]:
        path = checked[entry["path"]]
        before, after = _decode(entry, "before"), _decode(entry, "after")
        if path == source and not source.exists() and target.exists():
            path = target  # Crash after unlink: identity is already at destination.
        current = path.read_bytes()
        if current == after:
            continue
        if current != before:
            raise LibraryLayoutError("Human edit during relocation; preserve it")
        _bytes(path, after)
    if source.exists():
        if not target.exists():
            _check_destination(root, journal["to"])
            target.parent.mkdir(parents=True, exist_ok=True)
            # Unlike POSIX rename, link refuses to overwrite a concurrent file.
            os.link(source, target)
        if not os.path.samefile(source, target):
            raise LibraryLayoutError("Relocation destination belongs to another file")
        expected = _decode(next(e for e in journal["writes"] if e["path"] == journal["from"]), "after")
        if source.read_bytes() != expected:
            raise LibraryLayoutError("Source changed during relocation")
        source.unlink()
    _receipt(root, journal, "applied")


def _receipt(root: Path, journal: dict, status: str) -> None:
    directory = _state_dir(root)
    _json(directory / (journal["event_id"] + ".json"), {
        key: value for key, value in {**journal, "status": status}.items() if key not in {"writes", "paths"}})
    (directory / "pending.json").unlink(missing_ok=True)


def _rollback(root: Path, journal: dict, checked: dict[str, Path]) -> list[str]:
    """Undo only our exact writes. Never restore over changed or missing files."""
    conflicts = []
    source, target = checked[journal["from"]], checked[journal["to"]]
    if source.exists() and target.exists() and os.path.samefile(source, target):
        target.unlink()  # Interrupted no-clobber link, source remains authoritative.
    for entry in reversed(journal["writes"]):
        path = checked[entry["path"]]
        before, after = _decode(entry, "before"), _decode(entry, "after")
        if not path.exists():
            conflicts.append(entry["path"])
        elif path.read_bytes() == after:
            if before != after:
                _bytes(path, before)
        elif path.read_bytes() != before:
            conflicts.append(entry["path"])
    return conflicts


def recover_relocation(root: Path) -> str:
    """Complete a persisted technical intent before loading after-turn sources."""
    if vault_root(root) is None:
        return "none"
    directory = root / "levels/note_relocations"
    pending = directory / "pending.json"
    if not pending.exists():
        return "none"
    directory = _state_dir(root)
    with memory_file_lock(directory / "publication.lock"), ExitStack() as stack:
        if not pending.exists():
            return "none"
        if pending.is_symlink() or pending.stat().st_size > 3 * MAX_TRANSACTION_BYTES:
            raise LibraryLayoutError("Invalid relocation journal")
        journal = json.loads(pending.read_text(encoding="utf-8"))
        _validate_journal(vault_root(root), journal)
        checked = _locks(vault_root(root), journal, stack)
        source, target = checked[journal["from"]], checked[journal["to"]]
        # All link writes precede the rename commit point. Subsequent user edits
        # at the new address must not be overwritten during receipt recovery.
        if not source.exists() and target.exists():
            if resolve_note(root, journal["name"]) == target:
                _receipt(root, journal, "applied")
                return "applied"
        try:
            _finish(root, journal, checked)
        except (LibraryLayoutError, FileNotFoundError):
            conflicts = _rollback(root, journal, checked)
            _receipt(root, {**journal, "conflicts": conflicts}, "cancelled")
            return "cancelled"
        return "applied"


def publish_relocation(root: Path, proposal: Relocation, expected_content: bytes) -> str:
    """Use the protected merge's exact result, not an unguarded LLM draft."""
    recover_relocation(root)
    vault = vault_root(root)
    directory = _state_dir(root)
    event_id = hashlib.sha256((proposal.source_ref + "\0" + proposal.snapshot.name).encode()).hexdigest()
    with memory_file_lock(directory / "publication.lock"), ExitStack() as stack:
        receipt = directory / (event_id + ".json")
        if receipt.exists():
            if receipt.is_symlink():
                raise LibraryLayoutError("Symlink relocation receipt")
            return json.loads(receipt.read_text(encoding="utf-8"))["status"]
        source = resolve_note(root, proposal.snapshot.name)
        if (source.relative_to(vault).as_posix() != proposal.snapshot.relative
                or source.read_bytes() != expected_content):
            raise LibraryLayoutError("Note changed after protected merge; relocation refused")
        _check_destination(root, proposal.destination)
        note_locations(root)  # Reject symlink directories and ambiguous portable names.
        # Unregistered human notes (even invalid YAML) can contain incoming links.
        paths = {p.relative_to(vault).as_posix() for folder in FOLDERS
                 for p in (vault / folder).rglob("*.md")
                 if not any(part.startswith('.') for part in p.relative_to(vault).parts)}
        writes, total = [], 0
        for relative in sorted(paths):
            path = checked_note_path(vault, relative)
            before = path.read_bytes()
            if len(before) > MAX_NOTE_BYTES:
                raise LibraryLayoutError("Note too large for a bounded link-preserving move")
            after = retarget_links(before.decode("utf-8"), source=relative,
                old=proposal.snapshot.relative, new=proposal.destination, paths=paths).encode("utf-8")
            if before == after and path != source:
                continue
            writes.append({"path": relative, "before": base64.b64encode(before).decode(),
                           "after": base64.b64encode(after).decode()})
            total += len(before) + len(after)
        if len(writes) > MAX_CHANGED_NOTES or total > MAX_TRANSACTION_BYTES:
            raise LibraryLayoutError("Relocation exceeds transaction bounds")
        journal = {"schema": SCHEMA, "event_id": event_id, "name": proposal.snapshot.name,
            "from": proposal.snapshot.relative, "to": proposal.destination,
            "source_ref": proposal.source_ref, "evidence_quote": proposal.evidence_quote,
            "reason": proposal.reason, "writes": writes, "paths": sorted(paths)}
        _validate_journal(vault, journal)
        checked = _locks(vault, journal, stack)
        _check_destination(root, proposal.destination)
        for entry in writes:
            if checked[entry["path"]].read_bytes() != _decode(entry, "before"):
                raise LibraryLayoutError("Note changed while relocation was prepared")
        _json(directory / "pending.json", journal)
        try:
            _finish(root, journal, checked)
        except Exception as exc:
            # An I/O failure can leave the intent for the durable retry if
            # rollback also fails. A changed human file is always preserved.
            if not source.exists():
                raise  # Rename committed: recover the receipt, not the links.
            conflicts = _rollback(root, journal, checked)
            if conflicts or isinstance(exc, LibraryLayoutError):
                _receipt(root, {**journal, "conflicts": conflicts}, "cancelled")
            raise
    return "applied"
