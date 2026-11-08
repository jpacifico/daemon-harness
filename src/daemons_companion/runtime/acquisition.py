"""Bundled host catalogue and disposable, download-only qualification.

Nothing here installs a host, executes an archive or mutates an existing profile.
The native shells receive reviews, never arbitrary download URLs or commands.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from dataclasses import dataclass
from email.utils import parsedate_to_datetime
import hashlib
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


CATALOG_ID = "daemon-hosts-2026-10-03-v1"
DISPOSABLE_MARKER = ".daemon-host-download-disposable"
DISPOSABLE_MARKER_CONTENT = b"daemon.host-download.disposable.v1\n"


@dataclass(frozen=True)
class HostRelease:
    host: str
    version: str
    revision: str
    url: str
    sha256: str
    size: int
    recognized_versions: tuple[str, ...]
    requirements: tuple[str, ...]

    @property
    def artifact_id(self) -> str:
        return f"{self.host}-{self.version}"

    def public_dict(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "host": self.host,
            "version": self.version,
            "revision": self.revision,
            "url": self.url,
            "sha256": self.sha256,
            "size_bytes": self.size,
            "requirements": list(self.requirements),
            "runtime_qualified_on": ["macos"],
            "fresh_install_qualified_on": [],
        }


_RELEASES = {
    "hermes": HostRelease(
        "hermes", "0.21.5", "f97608f178d1ffeca59860195ab7da295f7c8e5f",
        "https://codeload.github.com/NousResearch/hermes-agent/tar.gz/f97608f178d1ffeca59860195ab7da295f7c8e5f",
        "babbdb9706ad1c96708d75914afb45d809cd4b73dec1ceae233a6814f5ed97e3", 75290311,
        ("0.21.0", "0.21.5"),
        (
            "Separate Python environment; upstream frozen lock and messaging extra.",
            "Verify configured native Web dependencies, including DDGS when selected.",
            "Verify both search and page extraction, not just the memory plugin.",
        ),
    ),
    "openclaw": HostRelease(
        "openclaw", "2026.10.7", "npm:openclaw@2026.10.7",
        "https://registry.npmjs.org/openclaw/-/openclaw-2026.10.7.tgz",
        "45dc6dadab3ca4e5286262226d6b61554309435a5e83b5d25c803a3a12ef2f81", 103494479,
        ("2026.10.7", "2026.9.1-beta.1"),
        (
            "Use the exact qualified package, not the latest npm release.",
            "A compatible private Node runtime and dependency installation still need qualification.",
            "Verify context-engine hooks, native tools, loopback API and after-turn worker.",
        ),
    ),
}


def target_platform() -> str:
    return {"darwin": "macos", "win32": "windows"}.get(sys.platform, "unsupported")


def recognized_version(host: str, raw: str, status: str) -> str:
    """Accept known CLI formats, not an arbitrary version substring or failed probe."""
    release = _RELEASES[host]
    if status != "verified":
        return ""
    prefix = r"(?:Hermes(?: Agent)? v?)?" if host == "hermes" else r"(?:🦞 )?(?:OpenClaw )?"
    for version in release.recognized_versions:
        if re.fullmatch(prefix + re.escape(version) + r"(?: \([A-Za-z0-9. -]+\))?", raw.strip()):
            return version
    return ""


def review_host_setup(
    *, host: str, detected: bool, usable: bool, version: str,
    version_status: str, platform: str,
) -> dict[str, object]:
    release = _RELEASES[host]
    matched = recognized_version(host, version, version_status) if usable else ""
    if matched:
        action = "reuse_existing"
        message = f"Keep your existing {host} {matched}. Its version is recognized; dependencies and settings still need verification."
    elif detected:
        action = "review_existing"
        message = "An existing installation or profile was found but could not be approved. It will not be overwritten, upgraded or downgraded automatically."
    else:
        action = "install_qualified"
        message = f"No {host} installation was found. The planned setup will install the qualified {release.version} release in a separate managed location."
    supported_platform = platform in {"macos", "windows"}
    lines = [message, f"Proposed release: {release.version}", *release.requirements]
    lines += [
        "This preview does not install or change an existing host.",
        "Complete fresh-install qualification is still pending on macOS and Windows.",
        "Channels, tool permissions and security choices remain visible host-owned setup steps.",
    ]
    if not supported_platform:
        lines.append("This operating system is not supported by the companion setup.")
    return {
        "schema": "daemon.companion-host-setup.v1",
        "catalog_id": CATALOG_ID,
        "host": host,
        "target_os": platform,
        "platform_supported": supported_platform,
        "action": action,
        "recognized_version": matched,
        "preserves_existing_installation": True,
        "release": release.public_dict(),
        "summary": "\n\n".join(lines),
        "execution_enabled": False,
        "writes_performed": False,
    }


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Host artifact redirects are not allowed.")


def _open_artifact(url: str):
    request = Request(url, headers={"Accept-Encoding": "identity", "User-Agent": "DaemonHarnessCompanion/0"})
    return build_opener(_NoRedirect()).open(request, timeout=20)


# A pinned download server may briefly refuse or drop an anonymous request.
# Opening is retried a few times, before any byte is written; every other
# response, redirect or integrity failure still fails immediately.
DOWNLOAD_LIMITED = ("Downloads from {service} are limited right now. The steps already done are kept: "
                    "wait a few minutes, then try again.")
DOWNLOAD_UNREACHABLE = ("No response from {service} for about five minutes, usually a temporary problem "
                        "on its side or no Internet connection. The steps already done are kept: "
                        "wait a few minutes, then try again.")
DOWNLOAD_WAITING = "No response from {service}. Retrying in {seconds} seconds (attempt {attempt} of {total})..."
TRANSIENT_HTTP = frozenset({429, 502, 503, 504})
# About five minutes in all: an outage of the download service usually lasts
# minutes, and giving up after thirty seconds sent users back a setup step.
RETRY_DELAYS = (2.0, 8.0, 20.0, 45.0, 90.0, 120.0)
MAX_RETRY_WAIT = 120.0
MAX_TOTAL_RETRY_WAIT = 300.0
_sleep = time.sleep


class HostDownloadUnavailable(RuntimeError):
    """A transient refusal or network failure outlasted the bounded retries."""


def _retry_after(error: HTTPError) -> float | None:
    value = (error.headers.get("Retry-After") or "").strip() if error.headers else ""
    if not value:
        return None
    if value.isdigit():
        return float(value)
    try:
        return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
    except (TypeError, ValueError):
        return None


def download_service(url: str | None) -> str:
    """The service a user can recognise in a download message."""
    host = (urlsplit(url).hostname or "").lower() if url else ""
    if host == "github.com" or host.endswith((".github.com", ".githubusercontent.com")):
        return "GitHub"
    if host == "ollama.com" or host.endswith(".ollama.ai") or host.endswith(".ollama.com"):
        return "Ollama"
    if host == "nodejs.org" or host.endswith(".nodejs.org"):
        return "Node.js"
    return host or "The download service"


def open_with_retry(open_once, *, url=None, notice=None):
    """Return open_once()'s response, retrying only transient refusals.

    notice, when given, is told before each pause, so a long wait is explained.
    """
    service = download_service(url)
    waited = 0.0
    for attempt, delay in enumerate((*RETRY_DELAYS, None), start=1):
        try:
            return open_once()
        except HTTPError as error:
            if error.code not in TRANSIENT_HTTP:
                raise
            message, wait = DOWNLOAD_LIMITED, _retry_after(error)
            error.close()
        except (URLError, TimeoutError, ConnectionError):
            message, wait = DOWNLOAD_UNREACHABLE, None
        if delay is None:
            # The raw HTTP or socket text is replaced, not chained, so the
            # installer shows one plain explanation that can be localized.
            raise HostDownloadUnavailable(message.format(service=service)) from None
        pause = min(MAX_RETRY_WAIT, delay if wait is None else max(wait, 0.0), MAX_TOTAL_RETRY_WAIT - waited)
        if notice is not None:
            notice(DOWNLOAD_WAITING.format(service=service, seconds=round(pause), attempt=attempt + 1,
                                           total=len(RETRY_DELAYS) + 1))
        _sleep(pause)
        waited += pause


def _verified_file(path: Path, release: HostRelease) -> bool:
    if path.is_symlink() or not path.is_file() or path.stat().st_size != release.size:
        return False
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest() == release.sha256


def stage_disposable_host_artifact(artifact_id: str, root: Path) -> Path:
    """Download and verify only; deliberately inaccessible from the GUI boundary.

    Requires an explicit disposable marker. Concurrent/stale locks and existing
    mismatched files fail closed. No archive extraction or package manager runs.
    """
    release = next((r for r in _RELEASES.values() if r.artifact_id == artifact_id), None)
    if release is None:
        raise ValueError("Unknown bundled host release.")
    if not root.is_absolute() or root.resolve() != root or not root.is_dir() or root == Path.home().resolve() or root == Path(root.anchor):
        raise ValueError("A real absolute disposable download directory is required.")
    marker = root / DISPOSABLE_MARKER
    if marker.is_symlink() or not marker.is_file() or marker.stat().st_size != len(DISPOSABLE_MARKER_CONTENT) or marker.read_bytes() != DISPOSABLE_MARKER_CONTENT:
        raise ValueError("Host downloads are restricted to marked disposable roots.")
    destination = root / f"{release.artifact_id}.tar.gz"
    lock = root / ".host-download.lock"
    descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    temporary = None
    try:
        if destination.exists() or destination.is_symlink():
            if _verified_file(destination, release):
                return destination
            raise ValueError("Existing host artifact is not the expected file; nothing was replaced.")
        response = open_with_retry(lambda: _open_artifact(release.url), url=release.url)
        deadline = time.monotonic() + 180
        with response:
            if response.status != 200 or response.geturl() != release.url:
                raise ValueError("Unexpected host artifact response.")
            length = response.headers.get("Content-Length")
            if length is not None and length != str(release.size):
                raise ValueError("Host artifact size differs from the bundled catalogue.")
            if response.headers.get("Content-Encoding", "identity") != "identity":
                raise ValueError("Encoded host artifacts are not accepted.")
            with tempfile.NamedTemporaryFile(dir=root, prefix=".host-artifact-", delete=False) as stream:
                temporary = Path(stream.name)
                digest = hashlib.sha256()
                size = 0
                # read1 returns after at most one socket read, so a trickling
                # server cannot defer the deadline check until a full MiB.
                while chunk := response.read1(1024 * 1024):
                    size += len(chunk)
                    if size > release.size or time.monotonic() > deadline:
                        raise ValueError("Host artifact exceeded its size or time limit.")
                    digest.update(chunk)
                    stream.write(chunk)
                if size != release.size or digest.hexdigest() != release.sha256 or time.monotonic() > deadline:
                    raise ValueError("Host artifact integrity verification failed.")
                stream.flush()
                os.fsync(stream.fileno())
        # Link publishes atomically without replacing a concurrently created file.
        os.link(temporary, destination)
        return destination
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        lock.unlink()
