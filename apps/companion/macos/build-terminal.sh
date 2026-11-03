#!/usr/bin/env bash
set -euo pipefail
DAEMONS_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DAEMONS_REPO="$(cd "$DAEMONS_SCRIPT_DIR/../../.." && pwd)"
DAEMONS_PYTHON="${DAEMON_HARNESS_BUILD_PYTHON:-$DAEMONS_REPO/.venv/bin/python}"
exec "$DAEMONS_PYTHON" "$DAEMONS_REPO/scripts/build_companion_macos_package.py" "$@"
