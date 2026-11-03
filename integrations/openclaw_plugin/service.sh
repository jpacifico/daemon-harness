#!/usr/bin/env bash
set -euo pipefail

LABEL="com.daemonharness.openclaw-api"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_ROOT="${DAEMON_CORE_LOG_DIR:-$HOME/Library/Logs/daemon-harness}"
LOG="$LOG_ROOT/openclaw_api.log"
PYTHON="${DAEMON_CORE_PYTHON:-}"
ENV_FILE="${DAEMON_CORE_ENV_FILE:-}"
MEMORY_ROOT="${DAEMON_CORE_MEMORY_ROOT:-}"
CONTINUITY_MODE="${DAEMON_CORE_CONTINUITY_RECEIPTS:-}"
CONTINUITY_DIR="${DAEMON_CORE_CONTINUITY_RECEIPT_DIR:-}"
RELATION_MODE="${DAEMON_CORE_RELATION_GARDENING:-}"
RELATION_USERS="${DAEMON_CORE_RELATION_GARDENING_USER_IDS:-}"
RUNTIME_REVISION="${DAEMON_CORE_RUNTIME_REVISION:-}"
AFTER_TURN_OWNER="${DAEMON_CORE_AFTER_TURN_OWNER:-}"
AFTER_TURN_SERVICE_REVISION="${DAEMON_CORE_AFTER_TURN_SERVICE_REVISION:-}"
OPENCLAW_AGENT_DB="${DAEMON_CORE_OPENCLAW_AGENT_DB:-}"
OPENCLAW_HEALTH_FILE="${DAEMON_CORE_OPENCLAW_HEALTH_FILE:-}"
AFTER_TURN_SERVICE_PLIST="$HOME/Library/LaunchAgents/com.daemonharness.after-turn-worker.plist"
DREAMING_SERVICE="$REPO_ROOT/integrations/dreaming_service/service.sh"
ACTION="${1:-install}"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "Ce gestionnaire cible launchd (macOS). Sous Linux, installer le serveur comme service systemd."
  exit 1
fi

status() {
  local healthy=0
  if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
    echo "launchd: actif"
  else
    echo "launchd: inactif"
    healthy=1
  fi
  if curl -fsS --max-time 2 http://127.0.0.1:8000/health >/dev/null 2>&1; then
    echo "API: vivante"
  else
    echo "API: indisponible"
    healthy=1
  fi
  if curl -fsS --max-time 2 http://127.0.0.1:8000/ready >/dev/null 2>&1; then
    echo "API: prête et attestée"
  else
    echo "API: non attestée"
    healthy=1
  fi
  "$DREAMING_SERVICE" status || healthy=1
  return "$healthy"
}

case "$ACTION" in
  status)
    status
    exit 0
    ;;
  uninstall)
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "Service $LABEL supprimé. Le Dreaming/REM commun est conservé pour les autres hôtes."
    exit 0
    ;;
  install)
    ;;
  *)
    echo "Usage: $0 [install|status|uninstall]" >&2
    exit 2
    ;;
esac

existing_plist_value() {
  local key_path="$1"
  [[ -f "$PLIST" ]] || return 0
  /usr/bin/plutil -extract "$key_path" raw -o - "$PLIST" 2>/dev/null || true
}

# A promotion normally replaces an existing LaunchAgent. Preserve its explicit
# state/venv overrides unless the caller supplies a new non-empty value; this
# avoids silently falling back to a worktree-local venv or memory root.
PYTHON="${PYTHON:-$(existing_plist_value "ProgramArguments.0")}"
PYTHON="${PYTHON:-$HOME/.local/share/daemon-harness/runtime/venv/bin/python}"
ENV_FILE="${ENV_FILE:-$(existing_plist_value "EnvironmentVariables.DAEMON_CORE_ENV_FILE")}"
ENV_FILE="${ENV_FILE:-$REPO_ROOT/.env}"
MEMORY_ROOT="${MEMORY_ROOT:-$(existing_plist_value "EnvironmentVariables.DAEMON_CORE_MEMORY_ROOT")}"
MEMORY_ROOT="${MEMORY_ROOT:-$HOME/.local/share/daemon-harness/memory}"
CONTINUITY_MODE="${CONTINUITY_MODE:-$(existing_plist_value "EnvironmentVariables.DAEMON_CORE_CONTINUITY_RECEIPTS")}"
CONTINUITY_DIR="${CONTINUITY_DIR:-$(existing_plist_value "EnvironmentVariables.DAEMON_CORE_CONTINUITY_RECEIPT_DIR")}"
RELATION_MODE="${RELATION_MODE:-$(existing_plist_value "EnvironmentVariables.DAEMON_CORE_RELATION_GARDENING")}"
RELATION_USERS="${RELATION_USERS:-$(existing_plist_value "EnvironmentVariables.DAEMON_CORE_RELATION_GARDENING_USER_IDS")}"
AFTER_TURN_OWNER="${AFTER_TURN_OWNER:-$(existing_plist_value "EnvironmentVariables.DAEMON_CORE_AFTER_TURN_OWNER")}"
AFTER_TURN_OWNER="${AFTER_TURN_OWNER:-external}"
# The installed worker is the deployment authority. Retaining the API's
# previous value here would hide a worker promotion and mark /ready
# degraded even when both services are deployed correctly.
if [[ -z "$AFTER_TURN_SERVICE_REVISION" && -f "$AFTER_TURN_SERVICE_PLIST" ]]; then
  AFTER_TURN_SERVICE_REVISION="$(/usr/bin/plutil -extract "EnvironmentVariables.DAEMON_CORE_RUNTIME_REVISION" raw -o - "$AFTER_TURN_SERVICE_PLIST" 2>/dev/null || true)"
fi
AFTER_TURN_SERVICE_REVISION="${AFTER_TURN_SERVICE_REVISION:-$(existing_plist_value "EnvironmentVariables.DAEMON_CORE_AFTER_TURN_SERVICE_REVISION")}"
OPENCLAW_AGENT_DB="${OPENCLAW_AGENT_DB:-$(existing_plist_value "EnvironmentVariables.DAEMON_CORE_OPENCLAW_AGENT_DB")}"
OPENCLAW_AGENT_DB="${OPENCLAW_AGENT_DB:-$HOME/.openclaw/agents/main/agent/openclaw-agent.sqlite}"
OPENCLAW_HEALTH_FILE="${OPENCLAW_HEALTH_FILE:-$(existing_plist_value "EnvironmentVariables.DAEMON_CORE_OPENCLAW_HEALTH_FILE")}"
OPENCLAW_HEALTH_FILE="${OPENCLAW_HEALTH_FILE:-$HOME/.local/state/daemon-harness/openclaw-runtime-health.json}"

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

ready_for_promotion() {
  curl -fsS --max-time 1 http://127.0.0.1:8000/ready \
    | "$PYTHON" -c 'import json, sys; report = json.load(sys.stdin); raise SystemExit(0 if report.get("status") == "ready" and report.get("promotable") is True else 1)'
}

mkdir -p "$HOME/Library/LaunchAgents" "$LOG_ROOT" "$MEMORY_ROOT"
chmod 700 "$LOG_ROOT" "$MEMORY_ROOT"
mkdir -p "$(dirname "$OPENCLAW_HEALTH_FILE")"
chmod 700 "$(dirname "$OPENCLAW_HEALTH_FILE")"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>-m</string><string>uvicorn</string>
    <string>server.main:app</string>
    <string>--host</string><string>127.0.0.1</string>
    <string>--port</string><string>8000</string>
    <string>--log-level</string><string>warning</string>
  </array>
  <key>WorkingDirectory</key><string>$REPO_ROOT</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key><string>$HOME</string>
    <key>PYTHONPATH</key><string>$REPO_ROOT:$REPO_ROOT/src</string>
    <key>PATH</key><string>/Applications/ChatGPT.app/Contents/Resources:$HOME/.local/bin:$HOME/.hermes/node/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
EOF

insert_plist_env() {
  local key="$1" value="$2"
  [[ -n "$value" ]] || return 0
  /usr/bin/plutil -insert "EnvironmentVariables.$key" -string "$value" "$PLIST"
}

insert_plist_env "DAEMON_CORE_ENV_FILE" "$ENV_FILE"
insert_plist_env "DAEMON_CORE_MEMORY_ROOT" "$MEMORY_ROOT"
insert_plist_env "DAEMON_CORE_CONTINUITY_RECEIPTS" "$CONTINUITY_MODE"
insert_plist_env "DAEMON_CORE_CONTINUITY_RECEIPT_DIR" "$CONTINUITY_DIR"
insert_plist_env "DAEMON_CORE_RELATION_GARDENING" "$RELATION_MODE"
insert_plist_env "DAEMON_CORE_RELATION_GARDENING_USER_IDS" "$RELATION_USERS"
insert_plist_env "DAEMON_CORE_RUNTIME_REVISION" "$RUNTIME_REVISION"
insert_plist_env "DAEMON_CORE_AFTER_TURN_OWNER" "$AFTER_TURN_OWNER"
insert_plist_env "DAEMON_CORE_AFTER_TURN_SERVICE_REVISION" "$AFTER_TURN_SERVICE_REVISION"
insert_plist_env "DAEMON_CORE_OPENCLAW_AGENT_DB" "$OPENCLAW_AGENT_DB"
insert_plist_env "DAEMON_CORE_OPENCLAW_HEALTH_FILE" "$OPENCLAW_HEALTH_FILE"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
for attempt in {1..5}; do
  if launchctl bootstrap "gui/$(id -u)" "$PLIST"; then
    break
  fi
  if [[ "$attempt" -eq 5 ]]; then
    echo "Impossible de recharger $LABEL après $attempt tentatives." >&2
    exit 1
  fi
  sleep 1
done
if ! launchctl kickstart -k "gui/$(id -u)/$LABEL"; then
  echo "Le service $LABEL est chargé, mais son démarrage explicite a échoué." >&2
  exit 1
fi

API_READY=0
for _ in {1..20}; do
  if ready_for_promotion >/dev/null 2>&1; then
    API_READY=1
    break
  fi
  sleep 0.5
done

if [[ "$API_READY" -ne 1 ]]; then
  echo "Le service est chargé, mais l'API n'est pas prête. Voir $LOG et /ready." >&2
  exit 1
fi

DAEMON_CORE_PYTHON="$PYTHON" \
DAEMON_CORE_ENV_FILE="$ENV_FILE" \
DAEMON_CORE_MEMORY_ROOT="$MEMORY_ROOT" \
  "$DREAMING_SERVICE" install
echo "Service $LABEL installé ; API prête et Dreaming/REM commun planifié."
