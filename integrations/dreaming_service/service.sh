#!/usr/bin/env bash
set -euo pipefail

LABEL="com.daemonharness.dreaming"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
DREAM_SCRIPT="$REPO_ROOT/scripts/dreaming_cron.sh"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
ACTION="${1:-install}"

say() { printf '%s\n' "$*"; }

existing_plist_value() {
  local key_path="$1"
  [[ -f "$PLIST" ]] || return 0
  python3 - "$PLIST" "$key_path" <<'PY'
import plistlib
import sys

path, key_path = sys.argv[1:]
try:
    with open(path, "rb") as stream:
        value = plistlib.load(stream)
    for key in key_path.split("."):
        if isinstance(value, list):
            value = value[int(key)]
        else:
            value = value[key]
except (FileNotFoundError, KeyError, IndexError, TypeError, ValueError):
    raise SystemExit(0)
if isinstance(value, bool):
    print("true" if value else "false")
elif isinstance(value, (str, int, float)):
    print(value)
PY
}

resolve_settings() {
  local existing_python=""
  local existing_env_file=""
  local existing_memory_root=""
  local existing_state_root=""
  local existing_log=""
  local existing_hour=""

  if [[ "$(uname -s)" == "Darwin" ]]; then
    existing_python="$(existing_plist_value "EnvironmentVariables.DAEMON_CORE_PYTHON")"
    existing_env_file="$(existing_plist_value "EnvironmentVariables.DAEMON_CORE_ENV_FILE")"
    existing_memory_root="$(existing_plist_value "EnvironmentVariables.DAEMON_CORE_MEMORY_ROOT")"
    existing_state_root="$(existing_plist_value "EnvironmentVariables.DAEMON_CORE_STATE_ROOT")"
    existing_log="$(existing_plist_value "StandardOutPath")"
    existing_hour="$(existing_plist_value "StartCalendarInterval.Hour")"
  fi

  DREAM_PYTHON="${DREAMING_PYTHON:-${existing_python:-${DAEMON_CORE_PYTHON:-$HOME/.local/share/daemon-harness/runtime/venv/bin/python}}}"
  DREAM_ENV_FILE="${DREAMING_ENV_FILE:-${existing_env_file:-${DAEMON_CORE_ENV_FILE:-$HOME/.config/daemon-harness/dreaming.env}}}"
  DREAM_MEMORY_ROOT="${DREAMING_MEMORY_ROOT:-${existing_memory_root:-${DAEMON_CORE_MEMORY_ROOT:-$HOME/.local/share/daemon-harness/memory}}}"
  DREAM_STATE_ROOT="${DREAMING_STATE_ROOT:-${existing_state_root:-${DAEMON_CORE_STATE_ROOT:-$HOME/.local/state/daemon-harness}}}"
  DREAM_LOG="${DREAMING_LOG_FILE:-${existing_log:-${DAEMON_CORE_LOG_DIR:-$HOME/Library/Logs/daemon-harness}/dreaming_cron.log}}"
  DREAM_HOUR="${DREAMING_CRON_HOUR:-${existing_hour:-4}}"

  if [[ ! "$DREAM_HOUR" =~ ^([0-9]|0[0-9]|1[0-9]|2[0-3])$ ]]; then
    say "Heure nocturne invalide : $DREAM_HOUR (attendu : 0 à 23)." >&2
    return 1
  fi
  DREAM_HOUR="$((10#$DREAM_HOUR))"
}

status_macos() {
  local healthy=0
  local configured_script=""
  local configured_root=""
  local configured_hour=""

  if [[ -f "$PLIST" ]]; then
    say "Dreaming/REM commun : configuré"
  else
    say "Dreaming/REM commun : non configuré"
    return 1
  fi
  if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
    say "Planification nocturne : active"
  else
    say "Planification nocturne : inactive"
    healthy=1
  fi
  configured_script="$(existing_plist_value "ProgramArguments.1")"
  configured_root="$(existing_plist_value "WorkingDirectory")"
  configured_hour="$(existing_plist_value "StartCalendarInterval.Hour")"
  say "Code exécuté : ${configured_script:-non défini}"
  say "Dépôt exécuté : ${configured_root:-non défini}"
  say "Horaire : ${configured_hour:-non défini}:00"
  if [[ "$configured_script" != "$DREAM_SCRIPT" || "$configured_root" != "$REPO_ROOT" ]]; then
    say "Alignement du code nocturne : incorrect"
    healthy=1
  else
    say "Alignement du code nocturne : correct"
  fi
  return "$healthy"
}

status_linux() {
  local line
  line="$(crontab -l 2>/dev/null | grep 'daemonharness-dreaming' || true)"
  if [[ -z "$line" ]]; then
    say "Dreaming/REM commun : non configuré"
    return 1
  fi
  say "Dreaming/REM commun : configuré"
  say "Planification : $line"
  if [[ "$line" == *"$DREAM_SCRIPT"* ]]; then
    say "Alignement du code nocturne : correct"
    return 0
  fi
  say "Alignement du code nocturne : incorrect"
  return 1
}

status() {
  case "$(uname -s)" in
    Darwin) status_macos ;;
    Linux) status_linux ;;
    *) say "Planification automatique non prise en charge sur cet OS."; return 1 ;;
  esac
}

uninstall() {
  case "$(uname -s)" in
    Darwin)
      launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
      rm -f "$PLIST"
      say "Planification commune Dreaming/REM supprimée. La mémoire elle-même est conservée."
      ;;
    Linux)
      if command -v crontab >/dev/null 2>&1; then
        crontab -l 2>/dev/null | grep -v 'daemonharness-dreaming' | crontab - 2>/dev/null || true
      fi
      say "Planification commune Dreaming/REM supprimée. La mémoire elle-même est conservée."
      ;;
    *)
      say "Planification automatique non prise en charge sur cet OS." >&2
      return 1
      ;;
  esac
}

install_macos() {
  mkdir -p "$HOME/Library/LaunchAgents" "$(dirname "$DREAM_LOG")" \
    "$DREAM_MEMORY_ROOT" "$DREAM_STATE_ROOT" "$(dirname "$DREAM_ENV_FILE")"
  chmod 700 "$(dirname "$DREAM_LOG")" "$DREAM_MEMORY_ROOT" \
    "$DREAM_STATE_ROOT" "$(dirname "$DREAM_ENV_FILE")"

  # Preserve existing daemon flags in the shared job (canaries,
  # owners and target users), replacing only its structural contract
  # and the four stable paths managed here.
  python3 - "$PLIST" "$LABEL" "$DREAM_SCRIPT" "$REPO_ROOT" "$DREAM_LOG" \
    "$DREAM_HOUR" "$HOME" "$DREAM_ENV_FILE" "$DREAM_MEMORY_ROOT" \
    "$DREAM_PYTHON" "$DREAM_STATE_ROOT" <<'PY'
import os
from pathlib import Path
import plistlib
import sys

(
    plist_path,
    label,
    script,
    repo_root,
    log_path,
    hour,
    home,
    env_file,
    memory_root,
    python_bin,
    state_root,
) = sys.argv[1:]
path = Path(plist_path)
document = {}
if path.exists():
    with path.open("rb") as stream:
        document = plistlib.load(stream)
environment = dict(document.get("EnvironmentVariables", {}))
environment.setdefault("PATH", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")
environment.update(
    {
        "HOME": home,
        "DAEMON_CORE_ENV_FILE": env_file,
        "DAEMON_CORE_MEMORY_ROOT": memory_root,
        "DAEMON_CORE_PYTHON": python_bin,
        "DAEMON_CORE_STATE_ROOT": state_root,
    }
)
document.update(
    {
        "Label": label,
        "ProgramArguments": ["/bin/bash", script],
        "WorkingDirectory": repo_root,
        "EnvironmentVariables": environment,
        "StartCalendarInterval": {"Hour": int(hour), "Minute": 0},
        "RunAtLoad": False,
        "StandardOutPath": log_path,
        "StandardErrorPath": log_path,
    }
)
temporary = path.with_name(f"{path.name}.daemon-harness-{os.getpid()}")
with temporary.open("wb") as stream:
    plistlib.dump(document, stream, sort_keys=False)
os.chmod(temporary, 0o644)
os.replace(temporary, path)
PY

  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  if ! launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null; then
    if ! launchctl load -w "$PLIST" 2>/dev/null; then
      say "Impossible d'activer la planification nocturne $LABEL." >&2
      return 1
    fi
  fi

  launchctl bootout "gui/$(id -u)/com.daemonharness.rem" 2>/dev/null || true
  rm -f "$HOME/Library/LaunchAgents/com.daemonharness.rem.plist"
  launchctl bootout "gui/$(id -u)/com.daemonharness.reveparadoxal" 2>/dev/null || true
  rm -f "$HOME/Library/LaunchAgents/com.daemonharness.reveparadoxal.plist"
  say "Dreaming/REM commun planifié chaque nuit à $(printf '%02d' "$DREAM_HOUR"):00."
}

install_linux() {
  local current
  local line
  if ! command -v crontab >/dev/null 2>&1; then
    say "crontab absent : programme /bin/bash $DREAM_SCRIPT quotidiennement." >&2
    return 1
  fi
  mkdir -p "$(dirname "$DREAM_LOG")" "$DREAM_MEMORY_ROOT" \
    "$DREAM_STATE_ROOT" "$(dirname "$DREAM_ENV_FILE")"
  chmod 700 "$(dirname "$DREAM_LOG")" "$DREAM_MEMORY_ROOT" \
    "$DREAM_STATE_ROOT" "$(dirname "$DREAM_ENV_FILE")"
  line="0 $DREAM_HOUR * * * DAEMON_CORE_ENV_FILE='$DREAM_ENV_FILE' DAEMON_CORE_MEMORY_ROOT='$DREAM_MEMORY_ROOT' DAEMON_CORE_PYTHON='$DREAM_PYTHON' DAEMON_CORE_STATE_ROOT='$DREAM_STATE_ROOT' /bin/bash '$DREAM_SCRIPT' >> '$DREAM_LOG' 2>&1  # daemonharness-dreaming"
  current="$(crontab -l 2>/dev/null || true)"
  ({ printf '%s\n' "$current" | grep -v 'daemonharness-dreaming' || true; } ; printf '%s\n' "$line") | crontab -
  say "Dreaming/REM commun planifié chaque nuit à $(printf '%02d' "$DREAM_HOUR"):00."
}

install() {
  resolve_settings
  if [[ ! -f "$DREAM_SCRIPT" ]]; then
    say "Script nocturne introuvable : $DREAM_SCRIPT" >&2
    return 1
  fi
  if [[ ! -x "$DREAM_PYTHON" ]]; then
    say "Python du daemon introuvable ou non exécutable : $DREAM_PYTHON" >&2
    return 1
  fi
  case "$(uname -s)" in
    Darwin) install_macos ;;
    Linux) install_linux ;;
    *) say "Planification automatique non prise en charge sur cet OS." >&2; return 1 ;;
  esac
}

case "$ACTION" in
  install) install ;;
  status) status ;;
  uninstall) uninstall ;;
  *) say "Usage: $0 [install|status|uninstall]" >&2; exit 2 ;;
esac
