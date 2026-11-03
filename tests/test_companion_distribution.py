"""Release-channel checks use scripts as data; never install or contact GitHub."""

import hashlib

import pytest

import json

from scripts.update_companion_distribution import PACKAGES, SCRIPTS, channel_document, check_version, validate

TAG = "companion-test-alpha"


def fixture():
    scripts = {}
    assets = []
    for name, package in zip(SCRIPTS, PACKAGES):
        if name.endswith(".sh"):
            text = f"daemon_tag={TAG}\ndaemon_sha={'a' * 64}\ndaemon_asset={package}\ndaemon_repo=jpacifico/daemon-harness\n"
        else:
            text = f"$daemonTag = '{TAG}'\n$daemonDigest = '{'a' * 64}'\n$daemonAsset = '{package}'\n$daemonRepo = 'jpacifico/daemon-harness'\n"
        scripts[name] = text.encode()
        assets.extend(
            [
                {
                    "name": name,
                    "state": "uploaded",
                    "digest": "sha256:" + hashlib.sha256(scripts[name]).hexdigest(),
                    "size": len(scripts[name]),
                },
                {"name": package, "state": "uploaded", "digest": "sha256:" + "a" * 64, "size": 1000},
            ]
        )
    return {
        "tag_name": TAG,
        "draft": False,
        "prerelease": True,
        "assets": assets,
    }, scripts


def test_complete_alpha_is_eligible_even_if_github_marks_prerelease():
    release, scripts = fixture()
    validate(release, scripts, TAG)


@pytest.mark.parametrize(
    "failure",
    [
        "draft",
        "missing-windows",
        "uploading",
        "wrong-package-hash",
        "changed-script",
        "wrong-tag",
    ],
)
def test_incomplete_or_mismatched_release_is_refused(failure):
    release, scripts = fixture()
    if failure == "draft":
        release["draft"] = True
    elif failure == "missing-windows":
        release["assets"].pop()
    elif failure == "uploading":
        release["assets"][-1]["state"] = "new"
    elif failure == "wrong-package-hash":
        release["assets"][-1]["digest"] = "sha256:" + "b" * 64
    elif failure == "changed-script":
        scripts[SCRIPTS[0]] += b"# changed\n"
    elif failure == "wrong-tag":
        release["tag_name"] = "companion-other"
    with pytest.raises(ValueError):
        validate(release, scripts, TAG)


def test_ambiguous_script_metadata_is_refused_even_with_valid_asset_digest():
    release, scripts = fixture()
    scripts[SCRIPTS[0]] += b"daemon_tag=companion-other\n"
    release["assets"][0]["digest"] = (
        "sha256:" + hashlib.sha256(scripts[SCRIPTS[0]]).hexdigest()
    )
    with pytest.raises(ValueError, match="Ambiguous"):
        validate(release, scripts, TAG)


def manifest(release, channel="candidate"):
    files = {
        item["name"]: {"sha256": item["digest"].removeprefix("sha256:"), "size": item["size"]}
        for item in release["assets"]
    }
    value = {"release": TAG, "source_revision": "c" * 40, "files": files}
    if channel:
        value["channel"] = channel
    return value


def test_channel_document_names_the_release_and_both_platforms_from_its_assets():
    release, scripts = fixture()
    validate(release, scripts, TAG)
    document = json.loads(channel_document(release, manifest(release), "companion-candidate"))
    assert document["schema"] == "daemon.companion.channel.v1"
    assert (document["channel"], document["release"], document["source_revision"]) == ("candidate", TAG, "c" * 40)
    assert document["platforms"]["macos-arm64"]["package"] == {"name": PACKAGES[0], "sha256": "a" * 64, "size": 1000}
    assert document["platforms"]["windows-x64"]["installer"]["name"] == SCRIPTS[1]
    stable = json.loads(channel_document(release, manifest(release, None), "main"))
    assert stable["channel"] == "main"


@pytest.mark.parametrize("failure", ["candidate-on-main", "alpha-on-candidate", "other-release", "revision", "changed-file"])
def test_channel_document_is_refused_when_the_manifest_disagrees(failure):
    release, _ = fixture()
    value, ref = manifest(release), "companion-candidate"
    if failure == "candidate-on-main":
        ref = "main"
    elif failure == "alpha-on-candidate":
        value.pop("channel")
    elif failure == "other-release":
        value["release"] = "companion-other"
    elif failure == "revision":
        value["source_revision"] = "short"
    elif failure == "changed-file":
        value["files"][PACKAGES[1]]["sha256"] = "b" * 64
    with pytest.raises(ValueError):
        channel_document(release, value, ref)


@pytest.mark.parametrize(
    "tag, version, ref",
    [
        ("companion-v0.11.0", "0.11.0", "main"),
        ("companion-v1.2.3", "1.2.3", "main"),
        ("companion-v0.11.0-rc.2", "0.11.0-rc.2", "companion-candidate"),
        # Unnumbered releases keep their dated tags, so rollback stays possible.
        ("companion-alpha-2026-10-26-r10", "0.1.0", "main"),
        ("companion-candidate-2026-10-25-c26", "0.1.0", "companion-candidate"),
    ],
)
def test_numbered_release_tag_names_the_app_version(tag, version, ref):
    check_version(tag, version, ref)


@pytest.mark.parametrize(
    "tag, version, ref",
    [
        ("companion-alpha-2026-11-01-r11", "0.11.0", "main"),
        ("companion-v0.11.1", "0.11.0", "main"),
        ("companion-v0.11.0-rc.1", "0.11.0-rc.1", "main"),
        ("companion-v0.11.0", "0.11.0", "companion-candidate"),
        ("companion-v0.11", "0.11", "main"),
        ("companion-vNone", None, "main"),
    ],
)
def test_release_tag_and_app_version_must_agree(tag, version, ref):
    with pytest.raises(ValueError):
        check_version(tag, version, ref)
