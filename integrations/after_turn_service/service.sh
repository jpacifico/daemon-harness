#!/usr/bin/env bash
set -euo pipefail

LABEL="com.daemonharness.after-turn-worker"
OPENCLAW_LABEL="com.daemonharness.openclaw-api"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
OPENCLAW_PLIST="$HOME/Library/LaunchAgents/$OPENCLAW_LABEL.plist"
LOG_ROOT="${DAEMON_CORE_LOG_DIR:-$HOME/Library/Logs/daemon-harness}"
LOG="$LOG_ROOT/after_turn_worker.log"
PYTHON="${DAEMON_CORE_PYTHON:-}"
ENV_FILE="${DAEMON_CORE_ENV_FILE:-}"
MEMORY_ROOT="${DAEMON_CORE_MEMORY_ROOT:-}"
SPOOL_MODE="${DAEMON_CORE_AFTER_TURN_SPOOL:-}"
SPOOL_DIR="${DAEMON_CORE_AFTER_TURN_SPOOL_DIR:-}"
CONTINUITY_MODE="${DAEMON_CORE_CONTINUITY_RECEIPTS:-}"
CONTINUITY_DIR="${DAEMON_CORE_CONTINUITY_RECEIPT_DIR:-}"
RUNTIME_REVISION="${DAEMON_CORE_RUNTIME_REVISION:-}"
HERMES_HOME_VALUE="${HERMES_HOME:-$HOME/.hermes}"
ACTION="${1:-install}"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "Ce gestionnaire cible launchd (macOS)." >&2
  exit 1
fi

plist_value() {
  local plist="$1" key_path="$2"
  [[ -f "$plist" ]] || return 0
  /usr/bin/plutil -extract "$key_path" raw -o - "$plist" 2>/dev/null || true
}

shared_value() {
  local key_path="$1"
  local value
  value="$(plist_value "$PLIST" "$key_path")"
  if [[ -z "$value" ]]; then
    value="$(plist_value "$OPENCLAW_PLIST" "$key_path")"
  fi
  printf '%s' "$value"
}

PYTHON="${PYTHON:-$(shared_value "ProgramArguments.0")}"
PYTHON="${PYTHON:-$HOME/.local/share/daemon-harness/runtime/venv/bin/python}"
ENV_FILE="${ENV_FILE:-$(shared_value "EnvironmentVariables.DAEMON_CORE_ENV_FILE")}"
ENV_FILE="${ENV_FILE:-$REPO_ROOT/.env}"
MEMORY_ROOT="${MEMORY_ROOT:-$(shared_value "EnvironmentVariables.DAEMON_CORE_MEMORY_ROOT")}"
MEMORY_ROOT="${MEMORY_ROOT:-$HOME/.local/share/daemon-harness/memory}"
SPOOL_MODE="${SPOOL_MODE:-$(shared_value "EnvironmentVariables.DAEMON_CORE_AFTER_TURN_SPOOL")}"
SPOOL_MODE="${SPOOL_MODE:-canary}"
SPOOL_DIR="${SPOOL_DIR:-$(shared_value "EnvironmentVariables.DAEMON_CORE_AFTER_TURN_SPOOL_DIR")}"
SPOOL_DIR="${SPOOL_DIR:-$HOME/.local/state/daemon-harness/after-turn-spool}"
CONTINUITY_MODE="${CONTINUITY_MODE:-$(shared_value "EnvironmentVariables.DAEMON_CORE_CONTINUITY_RECEIPTS")}"
CONTINUITY_DIR="${CONTINUITY_DIR:-$(plist_value "$PLIST" "EnvironmentVariables.DAEMON_CORE_CONTINUITY_RECEIPT_DIR")}"
CONTINUITY_DIR="${CONTINUITY_DIR:-$HOME/.local/state/daemon-harness/continuity/shared}"

check_service() {
  env \
    DAEMON_CORE_ENV_FILE="$ENV_FILE" \
    DAEMON_CORE_MEMORY_ROOT="$MEMORY_ROOT" \
    DAEMON_CORE_AFTER_TURN_SPOOL="$SPOOL_MODE" \
    DAEMON_CORE_AFTER_TURN_SPOOL_DIR="$SPOOL_DIR" \
    DAEMON_CORE_RUNTIME_REVISION="$RUNTIME_REVISION" \
    PYTHONPATH="$REPO_ROOT:$REPO_ROOT/src:$HERMES_HOME_VALUE/hermes-agent" \
    "$PYTHON" -m daemon_core.after_turn_daemon --check
}

status() {
  if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
    echo "launchd: actif"
  else
    echo "launchd: inactif"
    return 1
  fi
  if check_service >/dev/null 2>&1; then
    echo "service après-tour: prêt"
  else
    echo "service après-tour: non prêt"
    return 1
  fi
}

case "$ACTION" in
  status)
    RUNTIME_REVISION="${RUNTIME_REVISION:-$(shared_value "EnvironmentVariables.DAEMON_CORE_RUNTIME_REVISION")}"
    status
    exit $?
    ;;
  uninstall)
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "Service $LABEL supprimé."
    exit 0
    ;;
  install)
    ;;
  *)
    echo "Usage: $0 [install|status|uninstall]" >&2
    exit 2
    ;;
esac

if [[ ! -x "$PYTHON" ]]; then
  echo "Python du virtualenv introuvable : $PYTHON" >&2
  exit 1
fi

if [[ -z "$RUNTIME_REVISION" ]]; then
  RUNTIME_REVISION="$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || true)"
  if [[ -n "$(git -C "$REPO_ROOT" status --porcelain --untracked-files=normal 2>/dev/null || true)" ]]; then
    RUNTIME_REVISION="${RUNTIME_REVISION:-unknown}-dirty"
  fi
fi

mkdir -p "$HOME/Library/LaunchAgents" "$LOG_ROOT" "$MEMORY_ROOT" "$SPOOL_DIR" "$CONTINUITY_DIR"
chmod 700 "$LOG_ROOT" "$MEMORY_ROOT" "$SPOOL_DIR" "$CONTINUITY_DIR"

BACKUP=""
if [[ -f "$PLIST" ]]; then
  BACKUP="$PLIST.before-$(date -u +%Y%m%dT%H%M%SZ)"
  cp -p "$PLIST" "$BACKUP"
fi
TEMP_PLIST="$(mktemp "$PLIST.XXXXXX")"
trap 'rm -f "$TEMP_PLIST"' EXIT

cat > "$TEMP_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>-m</string><string>daemon_core.after_turn_daemon</string>
  </array>
  <key>WorkingDirectory</key><string>$REPO_ROOT</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key><string>$HOME</string>
    <key>HERMES_HOME</key><string>$HERMES_HOME_VALUE</string>
    <key>PYTHONPATH</key><string>$REPO_ROOT:$REPO_ROOT/src:$HERMES_HOME_VALUE/hermes-agent</string>
    <key>PATH</key><string>/Applications/ChatGPT.app/Contents/Resources:$HOME/.local/bin:$HERMES_HOME_VALUE/node/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    <key>DAEMON_CORE_AFTER_TURN_OWNER</key><string>external</string>
    <key>DAEMON_CORE_AFTER_TURN_SPOOL</key><string>$SPOOL_MODE</string>
    <key>DAEMON_CORE_AFTER_TURN_SPOOL_DIR</key><string>$SPOOL_DIR</string>
    <key>DAEMON_CORE_ENV_FILE</key><string>$ENV_FILE</string>
    <key>DAEMON_CORE_MEMORY_ROOT</key><string>$MEMORY_ROOT</string>
    <key>DAEMON_CORE_RUNTIME_REVISION</key><string>$RUNTIME_REVISION</string>
    <key>DAEMON_CORE_AFTER_TURN_SERVICE_REVISION</key><string>$RUNTIME_REVISION</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>60</integer>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
EOF

insert_plist_env() {
  local key="$1" value="$2"
  [[ -n "$value" ]] || return 0
  /usr/bin/plutil -insert "EnvironmentVariables.$key" -string "$value" "$TEMP_PLIST"
}

insert_plist_env "DAEMON_CORE_CONTINUITY_RECEIPTS" "$CONTINUITY_MODE"
insert_plist_env "DAEMON_CORE_CONTINUITY_RECEIPT_DIR" "$CONTINUITY_DIR"
/usr/bin/plutil -lint "$TEMP_PLIST" >/dev/null
chmod 600 "$TEMP_PLIST"
mv "$TEMP_PLIST" "$PLIST"

bootstrap_plist() {
  local attempt
  for attempt in {1..5}; do
    if launchctl bootstrap "gui/$(id -u)" "$PLIST"; then
      return 0
    fi
    sleep 1
  done
  return 1
}

rollback() {
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  if [[ -n "$BACKUP" && -f "$BACKUP" ]]; then
    cp -p "$BACKUP" "$PLIST"
    if bootstrap_plist >/dev/null 2>&1; then
      launchctl kickstart -k "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
    fi
    echo "Échec du canary ; ancien LaunchAgent restauré depuis $BACKUP." >&2
  else
    rm -f "$PLIST"
    echo "Échec du canary ; nouveau LaunchAgent retiré." >&2
  fi
}

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
if ! bootstrap_plist; then
  rollback
  exit 1
fi
if ! launchctl kickstart -k "gui/$(id -u)/$LABEL"; then
  rollback
  exit 1
fi

for _ in {1..30}; do
  if check_service >/dev/null 2>&1; then
    echo "Service $LABEL installé et prêt."
    [[ -z "$BACKUP" ]] || echo "Sauvegarde : $BACKUP"
    exit 0
  fi
  sleep 0.5
done

rollback
echo "Le service n'a pas produit de battement de cœur valide. Voir $LOG." >&2
exit 1
