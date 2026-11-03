#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$ROOT_DIR/.venv/bin/python}"

read_dotenv_value() {
  local key="$1"
  "$PYTHON_BIN" - "$ROOT_DIR/.env" "$key" <<'PY'
import os
import sys
from pathlib import Path

try:
    from dotenv import dotenv_values
except ImportError:
    print("")
    raise SystemExit(0)

env_path = Path(sys.argv[1])
key = sys.argv[2]
values = dotenv_values(env_path) if env_path.exists() else {}
print(values.get(key) or "")
PY
}

USER_ID="${DAEMON_CORE_USER_ID:-$(read_dotenv_value DAEMON_CORE_USER_ID)}"
SINCE_HOURS="${DREAMING_SINCE_HOURS:-$(read_dotenv_value DREAMING_SINCE_HOURS)}"
SINCE_HOURS="${SINCE_HOURS:-24}"
DRY_RUN="${DREAMING_DRY_RUN:-$(read_dotenv_value DREAMING_DRY_RUN)}"
DRY_RUN="${DRY_RUN:-0}"

if [[ -z "$USER_ID" ]]; then
  echo "DAEMON_CORE_USER_ID is required. Set it in the environment or .env." >&2
  exit 2
fi

cd "$ROOT_DIR"
ARGS=(--user-id "$USER_ID" --since-hours "$SINCE_HOURS")
if [[ "$DRY_RUN" != "1" && "$DRY_RUN" != "true" && "$DRY_RUN" != "yes" ]]; then
  ARGS+=(--apply)
fi

PYTHONPATH=src "$PYTHON_BIN" -m daemon_core.dreaming "${ARGS[@]}"
