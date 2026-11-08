from __future__ import annotations

from dataclasses import replace
import hashlib
import io
from pathlib import Path

import pytest

from daemons_companion.runtime import acquisition


@pytest.mark.parametrize("host,raw,expected", [
    ("hermes", "Hermes 0.21.5", "0.21.5"),
    ("hermes", "Hermes Agent v0.21.5", "0.21.5"),
    ("hermes", "0.21.0", "0.21.0"),
    ("openclaw", "OpenClaw 2026.9.1-beta.1 (abcdef)", "2026.9.1-beta.1"),
    ("openclaw", "2026.9.1-beta.1", "2026.9.1-beta.1"),
    ("hermes", "Hermes 0.21.50", ""),
    ("hermes", "error: Hermes 0.21.5 not installed", ""),
    ("openclaw", "OpenClaw 2026.9.8", ""),
])
def test_known_versions_only(host, raw, expected):
    assert acquisition.recognized_version(host, raw, "verified") == expected
    assert acquisition.recognized_version(host, raw, "failed") == ""


@pytest.mark.parametrize("platform", ["macos", "windows", "unsupported"])
@pytest.mark.parametrize("host", ["hermes", "openclaw"])
def test_review_preserves_existing_and_distinguishes_qualification(host, platform):
    def review(detected, usable, version=""):
        return acquisition.review_host_setup(
            host=host, detected=detected, usable=usable, version=version,
            version_status="verified", platform=platform,
        )

    absent = review(False, False)
    assert absent["action"] == "install_qualified"
    assert absent["release"]["fresh_install_qualified_on"] == []
    assert absent["platform_supported"] is (platform != "unsupported")
    for result in (absent, review(True, False), review(True, True, "unknown")):
        assert result["preserves_existing_installation"] is True
        assert result["execution_enabled"] is False
        assert result["writes_performed"] is False
    assert review(True, False)["action"] == "review_existing"
    assert review(True, True, "unknown")["action"] == "review_existing"
    assert review(True, True, absent["release"]["version"])["action"] == "reuse_existing"


class Response(io.BytesIO):
    def __init__(self, body, release, **headers):
        super().__init__(body)
        self.status = 200
        self.url = release.url
        self.headers = headers

    def geturl(self):
        return self.url

    def read1(self, size):
        return self.read(size)


@pytest.fixture
def download(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    (root / acquisition.DISPOSABLE_MARKER).write_bytes(acquisition.DISPOSABLE_MARKER_CONTENT)
    content = b"synthetic pinned artifact"
    release = replace(
        acquisition._RELEASES["hermes"], size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )
    monkeypatch.setattr(acquisition, "_RELEASES", {"hermes": release})
    opened = []

    def respond(response):
        def opener(url):
            opened.append(url)
            return response
        monkeypatch.setattr(acquisition, "_open_artifact", opener)

    respond(Response(content, release, **{"Content-Length": str(len(content))}))
    return root, content, release, opened, respond


def assert_clean(root):
    assert not list(root.glob(".host-artifact-*"))
    assert not (root / ".host-download.lock").exists()


def test_download_verified_published_and_reused(download):
    root, content, release, opened, _ = download
    result = acquisition.stage_disposable_host_artifact(release.artifact_id, root)
    assert result.read_bytes() == content
    assert acquisition.stage_disposable_host_artifact(release.artifact_id, root) == result
    assert opened == [release.url]
    assert_clean(root)


@pytest.mark.parametrize("failure", ["short", "large", "hash", "length", "encoding", "status", "url", "interrupted", "timeout"])
def test_download_refuses_damage_and_cleans_up(download, monkeypatch, failure):
    root, content, release, _, respond = download
    body = {"short": content[:-1], "large": content + b"!", "hash": b"X" * len(content)}.get(failure, content)
    response = Response(body, release)
    if failure == "length":
        response.headers["Content-Length"] = "1"
    if failure == "encoding":
        response.headers["Content-Encoding"] = "gzip"
    if failure == "status":
        response.status = 206
    if failure == "url":
        response.url = "https://untrusted.example/archive"
    if failure == "interrupted":
        def interrupted(_size):
            raise OSError("connection interrupted")
        response.read = interrupted
    if failure == "timeout":
        ticks = iter([0, 181])
        monkeypatch.setattr(acquisition.time, "monotonic", lambda: next(ticks))
    respond(response)
    with pytest.raises((ValueError, OSError)):
        acquisition.stage_disposable_host_artifact(release.artifact_id, root)
    assert not list(root.glob("*.tar.gz"))
    assert_clean(root)


def test_redirect_handler_refuses_all_redirects():
    with pytest.raises(ValueError, match="redirects"):
        acquisition._NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example")


@pytest.mark.parametrize("failure", ["unknown", "unmarked", "bad_marker", "marker_link", "root_link", "relative", "home", "existing", "destination_link", "lock"])
def test_refusal_never_replaces_existing_data(download, monkeypatch, failure):
    root, _, release, opened, _ = download
    artifact_id = release.artifact_id
    destination = root / f"{artifact_id}.tar.gz"
    marker = root / acquisition.DISPOSABLE_MARKER
    target = root
    if failure == "unknown":
        artifact_id = "https://untrusted.example/archive"
    elif failure == "unmarked":
        marker.unlink()
    elif failure == "bad_marker":
        marker.write_bytes(b"invalid")
    elif failure == "marker_link":
        original = root / "original-marker"
        marker.rename(original)
        marker.symlink_to(original)
    elif failure == "root_link":
        target = root / "alias"
        target.symlink_to(root, target_is_directory=True)
    elif failure == "relative":
        target = Path("relative")
    elif failure == "home":
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: root))
    elif failure == "existing":
        destination.write_bytes(b"keep me")
    elif failure == "destination_link":
        destination.symlink_to(root / "not-created")
    elif failure == "lock":
        (root / ".host-download.lock").write_bytes(b"other process")
    with pytest.raises((ValueError, FileExistsError)):
        acquisition.stage_disposable_host_artifact(artifact_id, target)
    assert opened == []
    if failure == "existing":
        assert destination.read_bytes() == b"keep me"
    if failure == "destination_link":
        assert destination.is_symlink()
    if failure == "lock":
        assert (root / ".host-download.lock").read_bytes() == b"other process"
    else:
        assert_clean(root)


def test_concurrent_destination_is_never_overwritten(download, monkeypatch):
    root, _, release, _, _ = download
    destination = root / f"{release.artifact_id}.tar.gz"
    real_link = acquisition.os.link

    def racing_link(source, target):
        destination.write_bytes(b"concurrent file")
        return real_link(source, target)

    monkeypatch.setattr(acquisition.os, "link", racing_link)
    with pytest.raises(FileExistsError):
        acquisition.stage_disposable_host_artifact(release.artifact_id, root)
    assert destination.read_bytes() == b"concurrent file"
    assert_clean(root)


def _refusal(url, code, retry_after=None):
    from email.message import Message
    from urllib.error import HTTPError

    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return HTTPError(url, code, "refused", headers, io.BytesIO(b""))


def _sequence(monkeypatch, outcomes):
    opened, sleeps = [], []

    def opener(url):
        opened.append(url)
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(acquisition, "_open_artifact", opener)
    monkeypatch.setattr(acquisition, "_sleep", sleeps.append)
    return opened, sleeps


@pytest.mark.parametrize("code,retry_after,expected_wait", [
    (429, None, 2.0), (503, "7", 7.0), (502, None, 2.0), (504, "999", 120.0)])
def test_transient_refusal_is_retried_then_verified(download, monkeypatch, code, retry_after, expected_wait):
    root, content, release, _, _ = download
    ok = Response(content, release, **{"Content-Length": str(len(content))})
    opened, sleeps = _sequence(monkeypatch, [_refusal(release.url, code, retry_after), ok])
    result = acquisition.stage_disposable_host_artifact(release.artifact_id, root)
    assert result.read_bytes() == content
    assert opened == [release.url, release.url]
    assert sleeps == [expected_wait]
    assert_clean(root)


@pytest.mark.parametrize("failure", ["refusal", "transport"])
def test_exhausted_retries_explain_without_raw_error(download, monkeypatch, failure):
    from urllib.error import URLError

    root, _, release, _, _ = download
    make = (lambda: _refusal(release.url, 429)) if failure == "refusal" else (lambda: URLError(TimeoutError("timed out")))
    attempts = len(acquisition.RETRY_DELAYS) + 1
    opened, sleeps = _sequence(monkeypatch, [make() for _ in range(attempts)])
    with pytest.raises(acquisition.HostDownloadUnavailable) as caught:
        acquisition.stage_disposable_host_artifact(release.artifact_id, root)
    expected = (acquisition.DOWNLOAD_LIMITED if failure == "refusal" else acquisition.DOWNLOAD_UNREACHABLE).format(service="GitHub")
    assert str(caught.value) == expected
    assert caught.value.__cause__ is None and caught.value.__suppress_context__
    assert len(opened) == attempts and sum(sleeps) <= acquisition.MAX_TOTAL_RETRY_WAIT
    # A short outage of the download service is waited out, not reported.
    assert sum(sleeps) >= 240
    assert not list(root.glob("*.tar.gz"))
    assert_clean(root)


@pytest.mark.parametrize("error", ["not_found", "redirect"])
def test_permanent_failures_are_not_retried(download, monkeypatch, error):
    root, _, release, _, _ = download
    failure = _refusal(release.url, 404) if error == "not_found" else ValueError("Host artifact redirects are not allowed.")
    opened, sleeps = _sequence(monkeypatch, [failure])
    with pytest.raises(Exception) as caught:
        acquisition.stage_disposable_host_artifact(release.artifact_id, root)
    assert not isinstance(caught.value, acquisition.HostDownloadUnavailable)
    assert len(opened) == 1 and sleeps == []
    assert_clean(root)


@pytest.mark.parametrize("module_name", ["setup.codex_install", "setup.embeddings", "openclaw.host_install"])
def test_other_setup_downloads_share_the_retry(tmp_path, monkeypatch, module_name):
    import importlib

    module = importlib.import_module("daemons_companion." + module_name)
    content = b"synthetic setup download"
    digest = hashlib.sha256(content).hexdigest()
    url = "https://downloads.example/artifact"

    class Ok(io.BytesIO):
        def geturl(self):
            return url

        def read1(self, size):
            return self.read(size)

    outcomes = [_refusal(url, 429), Ok(content)]

    def opener(*args, **kwargs):
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    sleeps = []
    monkeypatch.setattr(acquisition, "_sleep", sleeps.append)
    if module_name == "openclaw.host_install":
        monkeypatch.setattr(module.urllib.request, "urlopen", opener)
        module.download(url, tmp_path / "artifact", digest)
    else:
        monkeypatch.setattr(module, "urlopen", opener)
        downloader = module.download_package if module_name == "setup.codex_install" else module.download
        downloader(url, tmp_path / "artifact", len(content), digest)
    assert (tmp_path / "artifact").read_bytes() == content
    assert sleeps == [2.0] and outcomes == []


@pytest.mark.parametrize("url,service", [
    ("https://github.com/ollama/ollama/releases/download/v1/x.tgz", "GitHub"),
    ("https://codeload.github.com/o/r/tar.gz/abc", "GitHub"),
    ("https://release-assets.githubusercontent.com/x", "GitHub"),
    ("https://registry.ollama.ai/v2/library/x/blobs/sha256:00", "Ollama"),
    ("https://nodejs.org/dist/v1/node.tgz", "Node.js"),
    ("https://example.test/x", "example.test"),
    (None, "The download service"),
])
def test_download_messages_name_the_service(url, service):
    assert acquisition.download_service(url) == service


def test_a_long_wait_is_explained_before_each_pause(monkeypatch):
    from urllib.error import URLError

    sleeps, notices = [], []
    monkeypatch.setattr(acquisition, "_sleep", sleeps.append)
    attempts = iter([URLError(TimeoutError("timed out")), URLError(TimeoutError("timed out")), "ok"])

    def open_once():
        value = next(attempts)
        if isinstance(value, Exception):
            raise value
        return value

    assert acquisition.open_with_retry(open_once, url="https://github.com/x", notice=notices.append) == "ok"
    total = len(acquisition.RETRY_DELAYS) + 1
    assert notices == [
        f"No response from GitHub. Retrying in 2 seconds (attempt 2 of {total})...",
        f"No response from GitHub. Retrying in 8 seconds (attempt 3 of {total})...",
    ]
    assert sleeps == [2.0, 8.0]
