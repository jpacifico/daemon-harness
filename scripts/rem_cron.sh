#!/usr/bin/env bash
#
# REM: manual invocation for all active users.
#
# Automatic execution is no longer a weekly cron: REM runs within
# nightly Dreaming (scripts/dreaming_cron.sh), writing only when
# the episode is mature by density or elapsed time. This helper requests
# a pass for every user with recent L0 activity. REM regulates itself:
# an immature episode produces no output.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$ROOT_DIR/.venv/bin/python}"
USERS_DIR="$ROOT_DIR/memory/users"
L0_DIR="$ROOT_DIR/memory/levels/L0_ground_zero/conversations"

# Resolve providers exactly as nightly Dreaming does.
set -a; [ -f "$ROOT_DIR/.env" ] && . "$ROOT_DIR/.env"; set +a
export MEMORY_LLM_PROVIDER="${REM_LLM_PROVIDER:-${DREAMING_LLM_PROVIDER:-${MEMORY_LLM_PROVIDER:-codex_cli}}}"

cd "$ROOT_DIR"
[ -d "$USERS_DIR" ] || { echo "$(date '+%F %T') | aucun dossier memory/users — rien à faire"; exit 0; }

for udir in "$USERS_DIR"/*/; do
  [ -d "$udir" ] || continue
  uid="$(basename "$udir")"
  case "$uid" in test-*) continue ;; esac

  recent="$(find "$L0_DIR" -name "${uid}_*.md" -mtime -7 2>/dev/null | head -1)"
  if [ -z "$recent" ]; then
    echo "$(date '+%F %T') | skip $uid (pas d'activité L0 < 7j)"
    continue
  fi

  echo "$(date '+%F %T') | REM ${uid}…"
  if PYTHONPATH=src "$PYTHON_BIN" -m daemon_core.rem --user-id "$uid"; then
    echo "$(date '+%F %T') | ok $uid"
  else
    rc=$?
    echo "$(date '+%F %T') | ECHEC $uid (rc=$rc)"
  fi
done
