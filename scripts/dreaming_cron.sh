#!/usr/bin/env bash
# Legacy Unix entry point. The orchestration itself is portable Python so the
# companion can run exactly the same cycle on macOS and Windows.
set -euo pipefail

# launchd can start this job during a macOS DarkWake. Without a job-scoped
# assertion, the Mac may sleep again and suspend the active Codex call.
# On AC power, caffeinate keeps the machine awake only for this cycle,
# then immediately returns control to the system.
if [ "$(uname -s)" = "Darwin" ] \
    && [ "${DREAMING_CAFFEINATED:-0}" != "1" ] \
    && [ -x /usr/bin/caffeinate ]; then
  export DREAMING_CAFFEINATED=1
  exec /usr/bin/caffeinate -s /bin/bash "$0" "$@"
fi

CODE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_ROOT="${DAEMON_CORE_STATE_ROOT:-$CODE_ROOT}"
ORCHESTRATOR="${NIGHTLY_ORCHESTRATOR_PYTHON:-${DAEMON_CORE_PYTHON:-${PYTHON_BIN:-$STATE_ROOT/.venv/bin/python}}}"
cd "$CODE_ROOT"
export PYTHONPATH="$CODE_ROOT/src"
exec "$ORCHESTRATOR" -m daemon_core.nightly_maintenance
