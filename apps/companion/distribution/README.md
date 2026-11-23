# Install Daemons Companion from a permanent command

Both permanent commands install the release named inside `install-macos.sh`
and `install-windows.ps1` on `main`, on Mac ARM64 and Windows x64. Each script
pins that release's tag and package digest; `channel.json` names the same
release for apps that update themselves.
Replacing or updating the app does not upgrade an existing daemon. Previous
release assets and their exact rollback scripts are retained.

No account or credential is needed, pasted into the command or collected by
Daemon Harness. The installation guide keeps the command-based route while the app is
unsigned. Each script downloads its package directly from the release. If that
fails and a signed-in [GitHub CLI](https://cli.github.com/) is available, the
script retries through it, which also reaches a release the public cannot read.

## macOS, Apple Silicon

Paste this single command into Terminal, without `sudo`:

```sh
(set -e; daemons_script=$(mktemp); trap 'rm -f "$daemons_script"' EXIT; curl --proto '=https' --tlsv1.2 -fsSL 'https://raw.githubusercontent.com/jpacifico/daemon-harness/main/apps/companion/distribution/install-macos.sh' -o "$daemons_script"; sh "$daemons_script")
```

It downloads and verifies the DMG, checks the application signature and bundle
identity, installs into `~/Applications/Daemons Companion.app`, then opens it.
An existing app is replaced only after staging and verification; failure during
replacement restores the previous app. Close the app before running the command
again. No personal daemon, vault, credential or service is moved or restarted.

## Windows x64

Paste into **PowerShell**, in your normal session:

```powershell
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor 3072; & ([scriptblock]::Create((Invoke-RestMethod -UseBasicParsing -ErrorAction Stop 'https://raw.githubusercontent.com/jpacifico/daemon-harness/main/apps/companion/distribution/install-windows.ps1')))
```

The script verifies the EXE, runs the existing per-user NSIS installer, then
opens `%LOCALAPPDATA%\Programs\Daemons Companion\Daemons Companion.exe`.
It does not change PowerShell execution policy or request administrator access.
The same command installs the current published app after closing the window;
it does not reinstall an already configured daemon. An installation in another
folder is refused instead of silently relocated. NSIS owns the app replacement.

The app is a research preview: Mac signing is ad hoc, without notarization; Windows
has no Authenticode certificate. No security setting or quarantine attribute
is disabled by these scripts. Initial onboarding offers Hermes Agent or OpenClaw;
Add a host adds OpenClaw to an existing Hermes Agent daemon.

## Permanent commands and release publication

Both commands fetch the complete installer from `main` on
`raw.githubusercontent.com`, then execute it only if the download succeeded:
the macOS command writes it to a temporary file first, and the Windows command
fails before running anything when the download fails. Each script names one immutable release and
pins its package SHA-256. A new download cannot mix a tag and digest from two
releases, because both values come from the same downloaded script.

`main` is the public installation entry point. It must contain both installation
scripts and `channel.json`. No version appears in the copyable commands. Apps
also read `channel.json` on `main` to offer their own update; candidate
builds read it on `companion-candidate` instead.
GitHub's generic `latest` endpoint is deliberately unused: Companion is currently
a prerelease, and unrelated releases also exist in this repository.

A platform-specific correction may publish only its qualified package and script.
Keep the other platform’s script byte-identical and state its retained release;
never point it to the platform-specific tag. This exception avoids implying a
Windows qualification from a Mac-only build.

## Version numbers

Each release has an app version `MAJOR.MINOR.PATCH`, declared in
`apps/companion/desktop/package.json`. The release tag repeats it:
`companion-v0.11.0`. The About screen and both packages show the same number.

- A release with new or changed behaviour raises MINOR (`0.11.0`, `0.12.0`).
- A release that only corrects the previous one raises PATCH (`0.11.1`).
- MAJOR stays `0` during the research preview.
- A candidate for the next version adds `-rc.N`: `companion-v0.11.0-rc.1`,
  then `-rc.2`. Once qualified, the same code is rebuilt as `0.11.0` for `main`.

Set the version in the release commit, before building, with
`npm version <version> --no-git-tag-version` in `apps/companion/desktop`, which
updates `package.json` and `package-lock.json` together.
`update_companion_distribution.py` refuses a tag that differs from the version
declared at the release's source revision, an `-rc` version on `main` and a
final version on the candidate channel.

Releases up to `companion-alpha-2026-10-26-r10` and
`companion-candidate-2026-10-25-c26` were unnumbered: their packages all
declare `0.1.0`, and their tags carry a date and a sequence letter instead. They
remain valid for rollback. The first numbered release is `0.11.0`, which
follows r10.

## Publishing a release

When publishing a release for both platforms:

1. Finish package checks, document OS limitations, and obtain the required scope
   authorization. Publish both packages and their exact scripts to an immutable,
   non-draft release. A prerelease is eligible for the main channel.
2. In an isolated checkout based on main, run:

   ```sh
   python3 scripts/update_companion_distribution.py --release <tag> --apply
   ```

   This verifies both uploaded scripts and package digests before updating either
   entry point, and writes `channel.json`. It installs nothing and does not
   execute the downloaded scripts. The packages must be built with `--channel main`.
3. Commit both scripts and `channel.json` with current documentation and evidence. Push the qualified
   distribution-only change to main. Keep the user commands unchanged. Preserve
   previous packages; do not merge experimental runtime changes as a side effect.
4. Verify the actual public entry points:

   ```sh
   python3 scripts/update_companion_distribution.py --release <tag> --remote
   ```

   The command fails if either permanent URL still serves another release. Only
   after this check may the release be announced as available through the README.
   Without `--apply` or `--remote`, it checks the local checkout instead.

The three files are published in one Git commit. A release upload alone is not a
completed delivery. This intentionally retains exact tag/digest pins instead of
installing an unrelated, incomplete or unapproved release automatically.
A channel rollback uses the same verified procedure with the prior immutable tag.
No CI or paid workflow is required for publication checks.

## Historical command-install checks

`scripts/qualify_companion_command_install.py --package <DMG-or-EXE> --output
<JSON>` uses a fresh temporary destination, refuses altered/unrelated inputs,
installs twice, checks an external sentinel, launches only the synthetic app
scenario, and removes the test app. No daemon service or real account is used.
The native `companion-distribution` workflow scenario runs on both systems.

The scripts accept `--package`, `--destination`, `--no-launch`,
`--verify-only` on Mac and `-Package`, `-Destination`, `-NoLaunch`,
`-VerifyOnly` on Windows. A supplied package still must match the pinned hash.
These are diagnostic options; the normal installation command needs none.
