#!/bin/bash
# Single terminal entry point. No companion app or custom native executable.
set -euo pipefail
DAEMONS_ROOT="$(cd "$(dirname "$0")" && pwd -P)"
DAEMONS_UV="$DAEMONS_ROOT/bin/uv"
DAEMONS_UV_SHA="@DAEMONS_UV_SHA256@"
DAEMONS_STATE="${DAEMON_HARNESS_COMPANION_STATE_ROOT:-$HOME/Library/Application Support/DaemonHarnessCompanion/bootstrap}"
DAEMONS_INSTALL="${DAEMON_HARNESS_COMPANION_INSTALL_HOME:-$HOME/Library/Application Support/DaemonHarness}"
if [[ ! -t 0 || ! -t 1 ]]; then
  echo "Open Start-Daemons.command in an interactive terminal." >&2
  exit 2
fi
DAEMONS_PROGRESS_PID=""
daemons_stop_progress() {
  if [[ -n "$DAEMONS_PROGRESS_PID" ]]; then
    kill "$DAEMONS_PROGRESS_PID" 2>/dev/null || true
    wait "$DAEMONS_PROGRESS_PID" 2>/dev/null || true
    DAEMONS_PROGRESS_PID=""
    printf '\r\033[2K\033[?25h'
  fi
}
trap daemons_stop_progress EXIT
trap 'exit 130' INT TERM
case " $* " in
  *" --plain "*) ;;
  *)
    (
      DAEMONS_FRAME=0
      DAEMONS_FRAMES='|/-\'
      printf '\033[?25l'
      while :; do
        printf '\r  %s Preparing DAEMONS...' "${DAEMONS_FRAMES:$DAEMONS_FRAME:1}"
        DAEMONS_FRAME=$(( (DAEMONS_FRAME + 1) % 4 ))
        sleep 0.12
      done
    ) &
    DAEMONS_PROGRESS_PID=$!
    ;;
esac
if [[ ! -f "$DAEMONS_UV" || -L "$DAEMONS_UV" || ! -x "$DAEMONS_UV" ]]; then
  echo "The DAEMONS package is incomplete. Keep the entire folder together." >&2
  exit 1
fi
DAEMONS_ACTUAL_SHA="$(/usr/bin/shasum -a 256 "$DAEMONS_UV" | /usr/bin/awk '{print $1}')"
[[ "$DAEMONS_ACTUAL_SHA" == "$DAEMONS_UV_SHA" ]] || { echo "uv integrity verification failed." >&2; exit 1; }
# Check every shipped file before creating the private bootstrap.
[[ -z "$(/usr/bin/find "$DAEMONS_ROOT" -type l -print -quit)" ]] || { echo "Package links are not supported." >&2; exit 1; }
[[ -f "$DAEMONS_ROOT/package-sha256.txt" && ! -L "$DAEMONS_ROOT/package-sha256.txt" ]] || exit 1
while IFS= read -r DAEMONS_LINE; do
  DAEMONS_HASH="${DAEMONS_LINE:0:64}"
  DAEMONS_FILE="${DAEMONS_LINE:66}"
  [[ "$DAEMONS_HASH" =~ ^[0-9a-f]{64}$ && "${DAEMONS_LINE:64:2}" == "  " \
     && "$DAEMONS_FILE" == ./* && "$DAEMONS_FILE" != *../* \
     && "$DAEMONS_FILE" != *\\* && -f "$DAEMONS_ROOT/$DAEMONS_FILE" \
     && ! -L "$DAEMONS_ROOT/$DAEMONS_FILE" ]] || { echo "Invalid package checksum list." >&2; exit 1; }
done < "$DAEMONS_ROOT/package-sha256.txt"
(cd "$DAEMONS_ROOT" && /usr/bin/shasum -a 256 -c package-sha256.txt >/dev/null) || {
  echo "Companion package integrity verification failed." >&2; exit 1;
}
# The signed vendor executable is unchanged. No quarantine/security settings are edited.
daemons_uv() {
  /usr/bin/env -i HOME="$HOME" PATH=/usr/bin:/bin:/usr/sbin:/sbin \
    TMPDIR="${TMPDIR:-/tmp}" UV_CACHE_DIR="$DAEMONS_STATE/cache" \
    UV_PYTHON_INSTALL_DIR="$DAEMONS_STATE/python" UV_NO_CONFIG=1 \
    "$DAEMONS_UV" "$@"
}

[[ "$(daemons_uv --version)" == "uv 0.12.16 "* ]] || { echo "Unsupported uv version." >&2; exit 1; }
if ! DAEMONS_DOWNLOAD_OUTPUT="$(daemons_uv python install 3.12 --managed-python --no-bin --no-registry --no-config --no-progress 2>&1)"; then
  daemons_stop_progress
  printf '%s\n' "$DAEMONS_DOWNLOAD_OUTPUT" >&2
  exit 1
fi
DAEMONS_PYTHON="$(daemons_uv python find 3.12 --managed-python --no-python-downloads --no-config)"
[[ "$DAEMONS_PYTHON" == "$DAEMONS_STATE/python/"* && -x "$DAEMONS_PYTHON" ]] || { echo "Private Python is unavailable." >&2; exit 1; }
# Isolated Python validates all package bytes before importing any payload code.
daemons_stop_progress
exec /usr/bin/env -i HOME="$HOME" PATH="$PATH" TERM="${TERM:-xterm-256color}" \
  LANG="${LANG:-en_US.UTF-8}" TMPDIR="${TMPDIR:-/tmp}" \
  PYTHONDONTWRITEBYTECODE=1 PYTHONUTF8=1 \
  "$DAEMONS_PYTHON" -I -B -c '
import hashlib, json, os, pathlib, runpy, sys
root = pathlib.Path(sys.argv[1])
expected = json.loads((root / "companion-manifest.json").read_text())
if expected.get("schema") != "daemon.companion-package.v1":
    raise SystemExit("Invalid package manifest")
actual = {}
for path in root.rglob("*"):
    if path.is_symlink():
        raise SystemExit("Package links are not supported")
    if path.is_dir():
        continue
    key = path.relative_to(root).as_posix()
    if key == "companion-manifest.json":
        continue
    if not path.is_file():
        raise SystemExit("Unsupported package entry")
    actual[key] = {"sha256": hashlib.file_digest(path.open("rb"), "sha256").hexdigest(),
                   "size": path.stat().st_size, "executable": bool(path.stat().st_mode & 0o111)}
if expected.get("files") != actual:
    raise SystemExit("Companion package integrity verification failed")
values = {"PACKAGE_ROOT": root, "INSTALL_HOME": sys.argv[2], "BUNDLE_ROOT": root / "payload",
          "UV_EXECUTABLE": root / "bin/uv", "UV_SHA256": sys.argv[3], "TARGET_OS": "macos",
          "OPENCLAW_API_PORT": "8000", "OPENCLAW_GATEWAY_PORT": "18789"}
os.environ.update({"DAEMON_HARNESS_COMPANION_" + k: str(v) for k, v in values.items()})
sys.path.insert(0, str(root / "payload/src"))
sys.argv = ["daemons", *sys.argv[4:]]
runpy.run_module("daemons_companion.desktop.terminal", run_name="__main__")
' "$DAEMONS_ROOT" "$DAEMONS_INSTALL" "$DAEMONS_UV_SHA" "$@"
