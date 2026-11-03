#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
UV=""
UV_SHA256=""
OUTPUT="$SCRIPT_DIR/build/Daemon Harness Companion.app"
PYTHON="${DAEMON_HARNESS_BUILD_PYTHON:-$REPO_ROOT/.venv/bin/python}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --uv) UV="$2"; shift 2 ;;
    --uv-sha256) UV_SHA256="$2"; shift 2 ;;
    --output) OUTPUT="$2"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

[[ -f "$UV" && ! -L "$UV" ]] || { echo "A direct uv executable is required." >&2; exit 2; }
[[ "$UV_SHA256" =~ ^[0-9a-f]{64}$ ]] || { echo "A lowercase uv SHA-256 is required." >&2; exit 2; }
[[ -x "$PYTHON" ]] || { echo "The build Python is unavailable." >&2; exit 2; }
[[ ! -e "$OUTPUT" && ! -L "$OUTPUT" ]] || { echo "Output already exists: $OUTPUT" >&2; exit 2; }

ACTUAL_SHA="$(/usr/bin/shasum -a 256 "$UV" | /usr/bin/awk '{print $1}')"
[[ "$ACTUAL_SHA" == "$UV_SHA256" ]] || { echo "uv SHA-256 mismatch." >&2; exit 2; }
[[ "$("$UV" --version)" == "uv 0.12.16 "* ]] || { echo "Expected uv 0.12.16." >&2; exit 2; }
[[ -z "$(git -C "$REPO_ROOT" status --porcelain)" ]] || { echo "A clean committed candidate is required." >&2; exit 2; }

STAGE="$(mktemp -d "${TMPDIR:-/tmp}/daemon-companion-macos.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT
APP="$STAGE/Daemon Harness Companion.app"
RESOURCES="$APP/Contents/Resources"
mkdir -p "$APP/Contents/MacOS" "$RESOURCES/payload" "$RESOURCES/bin"
"$SCRIPT_DIR/build.sh" "$APP/Contents/MacOS/DaemonHarnessCompanion" >/dev/null
cp "$SCRIPT_DIR/Info.plist" "$APP/Contents/Info.plist"
git -C "$REPO_ROOT" archive --format=tar HEAD | tar -xf - -C "$RESOURCES/payload"
cp "$UV" "$RESOURCES/bin/uv"
chmod 755 "$RESOURCES/bin/uv" "$APP/Contents/MacOS/DaemonHarnessCompanion"
env PYTHONPATH="$REPO_ROOT/src" "$PYTHON" -m daemons_companion.desktop.package create "$RESOURCES" >/dev/null
# Seal the completed bundle, not just the linker-generated Mach-O signature.
# Ad-hoc signing provides structural integrity, not Developer ID / notarization.
# Preserve the verified vendor signature and bytes of the bundled uv executable.
/usr/bin/codesign --force --sign - --timestamp=none "$APP"
/usr/bin/codesign --verify --deep --strict --verbose=2 "$APP"
mkdir -p "$(dirname "$OUTPUT")"
mv "$APP" "$OUTPUT"
echo "$OUTPUT"
