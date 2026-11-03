#!/usr/bin/env bash
#
# Uninstall daemon-core from Hermes and restore the previous state.
#
# Annule ce que install.sh a fait :
#   1. Remove the plugin symlink.
#   2. Preserve shared nightly maintenance used by other hosts.
#   3. Restore config.yaml from config.yaml.bak when present.
#   4. Leave .env unchanged; remove DAEMON_CORE_SRC manually if desired.
#
# Usage : ./uninstall.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
PLUGIN_LINK="$HERMES_HOME/plugins/daemon_core"
CONFIG="$HERMES_HOME/config.yaml"
ENV_FILE="$HERMES_HOME/.env"

say()  { printf '  %s\n' "$*"; }
step() { printf '\n▸ %s\n' "$*"; }

echo "Désinstallation daemon-core ← Hermes"
say "HERMES_HOME : $HERMES_HOME"

# --- 1. plugin ---------------------------------------------------------------
step "Plugin"
launchctl bootout "gui/$(id -u)/com.daemonharness.rem" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/com.daemonharness.rem.plist" 2>/dev/null || true
if [ -L "$HERMES_HOME/plugins/daemon_voice" ]; then
  rm "$HERMES_HOME/plugins/daemon_voice"; say "symlink daemon_voice retiré ✓"
  command -v hermes >/dev/null 2>&1 && hermes plugins disable daemon_voice >/dev/null 2>&1 || true
fi
if [ -L "$PLUGIN_LINK" ]; then
  rm "$PLUGIN_LINK"; say "symlink retiré ✓"
else
  say "aucun symlink à retirer"
fi

# --- 2. shared nightly maintenance -------------------------------------------
step "Dreaming/REM commun"
say "conservé : il peut servir OpenClaw ou un autre hôte"
say "pour le retirer du daemon entier : $SCRIPT_DIR/../dreaming_service/service.sh uninstall"

# --- 3. config.yaml ----------------------------------------------------------
step "config.yaml"
if [ -e "$CONFIG.bak" ]; then
  mv -f "$CONFIG.bak" "$CONFIG"; say "config.yaml restauré depuis config.yaml.bak ✓"
else
  say "pas de config.yaml.bak — retire à la main le bloc 'memory:' si besoin"
fi

# --- 4. .env -----------------------------------------------------------------
step ".env"
say "laissé tel quel — retire DAEMON_CORE_SRC de $ENV_FILE si tu veux nettoyer"

step "Terminé"
say "Vérifie : hermes memory status"
