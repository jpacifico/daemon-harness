# Companion app source and distribution

Companion is the desktop app for installing and managing local Daemon Harness
installations. It uses Electron and supports English and French.
[User setup](../../docs/getting-started.md) ·
[Download commands](../../README.md#install-the-app)

## Where the source lives

Since r10, `main` holds the complete app source:

| Path | Content |
| --- | --- |
| `apps/companion/desktop` | Electron app: interface, main process, update client and tests |
| `src/daemons_companion` | Python setup engine the app drives: host installation, profiles, removal, Add a host |
| `apps/companion/distribution` | Permanent install scripts, rollback scripts and `channel.json` |
| `apps/companion/openclaw-runtime` | Pinned OpenClaw runtime manifest |

A release is built from one committed revision. Its `release-manifest.json`
records that revision, the package checksums and the channel, and the immutable
`companion-v<version>` tag points at it. The About screen shows the running
app's version and revision. [Version numbers](distribution/README.md#version-numbers). To inspect a shipped source without disturbing an existing checkout:

~~~sh
git worktree add --detach ../daemons-app-source <release-tag>
~~~

Building and publishing are described in [distribution maintenance](distribution/README.md)
and [the desktop app's README](desktop/README.md).

## Release channels

Each build names the channel it follows for its own updates
(`--channel main|candidate` in `scripts/build_companion_desktop_payload.py`).
An installed app reads `apps/companion/distribution/channel.json` on its
channel's branch: `main` for distributed releases, `companion-candidate` for
candidates. A candidate app is never moved to `main` silently.

## Legacy shells in main

The `macos` and `windows` directories contain earlier native-shell previews,
not the current Electron app. They are retained because historical package
tests and references still use them; they do not describe the current download.

No build or source inspection should start, replace or stop a personal daemon.

## Preparing the host interface

The desktop package includes the pinned Hermes browser frontend outside its
runtime source tree. Build it from the exact SHA-256-verified host archive with
`scripts/build_hermes_dashboard.py --archive <archive> --output <new-directory>`.
Copy the resulting directory to `host-interfaces/hermes` beside the staged
Electron package before packaging Mac and Windows. The frontend is common to
both platforms; native Python remains the selected private host's interpreter.
The manifest records the host revision and each frontend file digest. Opening
checks those digests and never installs npm dependencies on the user's machine.

This action supports profiles installed by Companion. Gmail and Outlook
authorization remains a separate native-host operation, not something that
opening the interface claims to complete.
