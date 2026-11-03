#!/usr/bin/env python3
"""Publish or verify the approved alpha download scripts, without installing apps.

The maintainer selects an immutable release. Both package assets must already
exist and match the SHA-256 values in its scripts. Default mode checks local
entry points; --apply stages their exact published bytes for one Git commit;
--remote verifies the permanent README URLs after that commit is pushed.
Each mode also covers channel.json, the document the app reads to update itself.
--ref names the branch those URLs read, and the app channel channel.json
serves: main, or the companion-candidate channel for releases that are not
distributed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile

REPO = "jpacifico/daemon-harness"
DIRECTORY = "apps/companion/distribution"
SCRIPTS = ("install-macos.sh", "install-windows.ps1")
PACKAGES = ("Daemons-Companion-macOS-arm64.dmg", "Daemons-Companion-Windows-x64.exe")
PLATFORMS = ("macos-arm64", "windows-x64")
MANIFEST = "release-manifest.json"
CHANNEL = "channel.json"
# The app's update channel for each published branch.
CHANNELS = {"main": "main", "companion-candidate": "candidate"}
VERSION_FILE = "apps/companion/desktop/package.json"
# Every release up to companion-alpha-2026-10-26-r10 shipped this version
# under a dated tag; those tags stay valid for rollback.
UNNUMBERED = "0.1.0"
VERSION_PATTERNS = {
    "main": r"\d+\.\d+\.\d+",
    "candidate": r"\d+\.\d+\.\d+-rc\.\d+",
}


def gh(*args):
    return subprocess.check_output(["gh", *args], timeout=120)


def validate(release, scripts, tag):
    if release.get("tag_name") != tag or release.get("draft"):
        raise ValueError("The selected release is missing or still a draft.")
    assets = {item["name"]: item for item in release["assets"]}
    for script, package in zip(SCRIPTS, PACKAGES):
        for name in (script, package):
            if name not in assets or assets[name].get("state") != "uploaded":
                raise ValueError(f"Incomplete release: {name}")
        raw = scripts[script]
        if (
            len(raw) > 65536
            or assets[script].get("digest")
            != "sha256:" + hashlib.sha256(raw).hexdigest()
        ):
            raise ValueError(f"Published script checksum mismatch: {script}")
        text = raw.decode("utf-8-sig")
        patterns = (
            (
                r"^\s*daemon_tag=([^\s]+)$",
                r"^\s*daemon_sha=([a-f0-9]{64})$",
                r"^\s*daemon_asset=([^\s]+)$",
                r"^\s*daemon_repo=([^\s]+)$",
            )
            if script.endswith(".sh")
            else (
                r"^\s*\$daemonTag = '([^']+)'$",
                r"^\s*\$daemonDigest = '([a-f0-9]{64})'$",
                r"^\s*\$daemonAsset = '([^']+)'$",
                r"^\s*\$daemonRepo = '([^']+)'$",
            )
        )
        values = [re.findall(pattern, text, re.M) for pattern in patterns]
        if any(len(value) != 1 for value in values):
            raise ValueError(f"Ambiguous release metadata: {script}")
        actual_tag, digest, actual_package, actual_repo = [value[0] for value in values]
        if (actual_tag, actual_package, actual_repo) != (tag, package, REPO):
            raise ValueError(f"Release metadata mismatch: {script}")
        if assets[package].get("digest") != "sha256:" + digest:
            raise ValueError(f"Published package checksum mismatch: {package}")


def check_version(tag, version, ref):
    """A numbered release's tag is the app version its source declares."""
    if version == UNNUMBERED:
        return
    if not re.fullmatch(VERSION_PATTERNS[CHANNELS[ref]], version or ""):
        raise ValueError(f"App version {version!r} is not valid on {ref}.")
    if tag != f"companion-v{version}":
        raise ValueError(f"Release tag {tag} does not name app version {version}.")


def channel_document(release, manifest, ref):
    """The exact channel bytes for a validated release, from its own assets."""
    tag = release["tag_name"]
    assets = {item["name"]: item for item in release["assets"]}
    revision = manifest.get("source_revision")
    if manifest.get("release") != tag or not re.fullmatch(r"[a-f0-9]{40}", revision or ""):
        raise ValueError("Release manifest does not describe this release.")
    # A candidate is never offered on main, and only candidates on the candidate channel.
    if (manifest.get("channel") == "candidate") != (CHANNELS[ref] == "candidate"):
        raise ValueError(f"Release channel does not match {ref}.")
    platforms = {}
    for key, script, package in zip(PLATFORMS, SCRIPTS, PACKAGES):
        entry = {}
        for role, name in (("package", package), ("installer", script)):
            digest = assets[name]["digest"].removeprefix("sha256:")
            if manifest.get("files", {}).get(name, {}).get("sha256") != digest:
                raise ValueError(f"Release manifest checksum mismatch: {name}")
            entry[role] = {"name": name, "sha256": digest, "size": assets[name]["size"]}
        platforms[key] = entry
    document = {
        "schema": "daemon.companion.channel.v1",
        "channel": CHANNELS[ref],
        "release": tag,
        "source_revision": revision,
        "platforms": platforms,
    }
    return (json.dumps(document, indent=2) + "\n").encode()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--remote", action="store_true")
    parser.add_argument("--ref", default="main", choices=tuple(CHANNELS))
    args = parser.parse_args()
    if not re.fullmatch(r"companion-[A-Za-z0-9._-]+", args.release):
        parser.error("Expected an immutable Companion release tag.")
    release = json.loads(gh("api", f"repos/{REPO}/releases/tags/{args.release}"))
    with tempfile.TemporaryDirectory(prefix="daemons-distribution-") as temporary:
        gh(
            "release",
            "download",
            args.release,
            "--repo",
            REPO,
            "--dir",
            temporary,
            "--pattern",
            SCRIPTS[0],
            "--pattern",
            SCRIPTS[1],
            "--pattern",
            MANIFEST,
        )
        scripts = {name: (Path(temporary) / name).read_bytes() for name in SCRIPTS}
        manifest = json.loads((Path(temporary) / MANIFEST).read_bytes())
    validate(release, scripts, args.release)
    scripts[CHANNEL] = channel_document(release, manifest, args.ref)
    package = gh(
        "api",
        f"repos/{REPO}/contents/{VERSION_FILE}?ref={manifest['source_revision']}",
        "-H",
        "Accept: application/vnd.github.raw+json",
    )
    check_version(args.release, json.loads(package).get("version"), args.ref)
    root = Path(__file__).resolve().parents[1] / DIRECTORY
    if args.apply:
        # Validation precedes both writes. Git publishes the pair atomically.
        for name, raw in scripts.items():
            (root / name).write_bytes(raw)
        print(
            f"Staged both exact download scripts and {CHANNEL} for {args.release}. Commit them together."
        )
    else:
        for name, raw in scripts.items():
            current = (
                gh(
                    "api",
                    f"repos/{REPO}/contents/{DIRECTORY}/{name}?ref={args.ref}",
                    "-H",
                    "Accept: application/vnd.github.raw+json",
                )
                if args.remote
                else (root / name).read_bytes()
            )
            if current != raw:
                raise ValueError(
                    f"Download entry point still differs from {args.release}: {name}"
                )
        print(
            f"{'Permanent GitHub URLs' if args.remote else 'Local entry points'} verified: {args.release}"
        )


if __name__ == "__main__":
    main()
