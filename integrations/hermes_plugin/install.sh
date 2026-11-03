#!/usr/bin/env bash
#
# Install daemon-core as a Hermes memory provider without forking Hermes.
#
# Four steps, reversible through uninstall.sh:
#   1. Link the plugin into $HERMES_HOME/plugins/daemon_core/.
#   2. Add DAEMON_CORE_SRC and provider notes to $HERMES_HOME/.env.
#   3. Set memory.{provider,memory_enabled,user_profile_enabled} in config.yaml.
#   4. Disable automatic Hermes skill rewriting.
#
# The plugin itself supplies the daemon persona with priority:
# no changes to SOUL.md are needed.
#
# Usage : ./install.sh
set -euo pipefail

for arg in "$@"; do
  case "$arg" in
    -h|--help)
      awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "$0"; exit 0 ;;
    *) echo "Option inconnue : $arg" >&2; exit 1 ;;
  esac
done

# --- paths -----------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
SRC_DIR="$REPO_ROOT/src"

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
PLUGINS_DIR="$HERMES_HOME/plugins"
PLUGIN_LINK="$PLUGINS_DIR/daemon_core"
CONFIG="$HERMES_HOME/config.yaml"
ENV_FILE="$HERMES_HOME/.env"

say()  { printf '  %s\n' "$*"; }
step() { printf '\n▸ %s\n' "$*"; }

for required in \
  "$SRC_DIR/daemon_core/__init__.py" \
  "$SRC_DIR/daemon_core/loop.py" \
  "$SCRIPT_DIR/__init__.py" \
  "$REPO_ROOT/integrations/hermes_voice_plugin/__init__.py" \
  "$REPO_ROOT/scripts/check_runtime_pointers.py"; do
  if [ ! -f "$required" ]; then
    echo "✗ Runtime daemon-core incomplet : $required" >&2
    exit 1
  fi
done

if [ ! -d "$HERMES_HOME" ]; then
  echo "✗ HERMES_HOME introuvable : $HERMES_HOME" >&2
  echo "  Installe Hermes d'abord, ou exporte HERMES_HOME." >&2
  exit 1
fi

echo "Installation daemon-core → Hermes"
say "HERMES_HOME : $HERMES_HOME"
say "repo        : $REPO_ROOT"

# --- 1. Plugin symlink -------------------------------------------------------
step "Plugin"
mkdir -p "$PLUGINS_DIR"
if [ -L "$PLUGIN_LINK" ]; then
  if [ "$(readlink "$PLUGIN_LINK")" = "$SCRIPT_DIR" ]; then
    say "symlink déjà en place ✓"
  else
    say "symlink existant pointe ailleurs → remplacement"
    rm "$PLUGIN_LINK"; ln -s "$SCRIPT_DIR" "$PLUGIN_LINK"
  fi
elif [ -e "$PLUGIN_LINK" ]; then
  echo "✗ $PLUGIN_LINK existe et n'est pas un symlink. Retire-le à la main." >&2
  exit 1
else
  ln -s "$SCRIPT_DIR" "$PLUGIN_LINK"
  say "symlink créé : $PLUGIN_LINK → $SCRIPT_DIR ✓"
fi

# --- 1b. Canonical voice plugin ------------------
step "Plugin voix (daemon_voice)"
VOICE_SRC="$REPO_ROOT/integrations/hermes_voice_plugin"
VOICE_LINK="$PLUGINS_DIR/daemon_voice"
if [ -L "$VOICE_LINK" ] || [ ! -e "$VOICE_LINK" ]; then
  ln -sfn "$VOICE_SRC" "$VOICE_LINK"
  say "symlink créé : $VOICE_LINK → $VOICE_SRC ✓"
else
  echo "✗ $VOICE_LINK existe et n'est pas un symlink. Retire-le à la main." >&2
  exit 1
fi
# The voice plugin is standalone and requires explicit activation.
if command -v hermes >/dev/null 2>&1; then
  if hermes plugins enable daemon_voice >/dev/null 2>&1; then
    say "plugin daemon_voice activé ✓"
  else
    say "⚠ active-le à la main : hermes plugins enable daemon_voice"
  fi
fi

# --- 2. .env -----------------------------------------------------------------
step ".env"
touch "$ENV_FILE"
python3 - "$ENV_FILE" "$SRC_DIR" <<'PY'
import os
from pathlib import Path
import stat
import sys
import tempfile

path = Path(sys.argv[1])
value = sys.argv[2]
key = "DAEMON_CORE_SRC"
lines = path.read_text(encoding="utf-8").splitlines()
matches = [index for index, line in enumerate(lines) if line.startswith(f"{key}=")]
if len(matches) > 1:
    raise SystemExit(f"duplicate {key} entries in {path}")
replacement = f"{key}={value}"
if matches:
    lines[matches[0]] = replacement
else:
    lines.append(replacement)
mode = stat.S_IMODE(path.stat().st_mode) or 0o600
descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
try:
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write("\n".join(lines) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temporary, mode)
    os.replace(temporary, path)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
PY
say "DAEMON_CORE_SRC vérifié et aligné sur ce dépôt ✓"
if grep -q '^DAEMON_CORE_FOLLOWUPS=' "$ENV_FILE"; then
  say "DAEMON_CORE_FOLLOWUPS déjà présent ✓"
else
  # Proactive follow-ups: without this flag, the follow-up faculty stays silent.
  printf 'DAEMON_CORE_FOLLOWUPS=on\n' >> "$ENV_FILE"
  say "DAEMON_CORE_FOLLOWUPS=on ajouté ✓"
fi
if grep -q '^FOLLOWUP_LLM_PROVIDER=' "$ENV_FILE"; then
  say "FOLLOWUP_LLM_PROVIDER déjà présent ✓"
else
  # Delivery is asynchronous; Codex CLI has been validated under launchd/cron
  # since 2026-07-13. Each installation can still override this explicitly.
  printf 'FOLLOWUP_LLM_PROVIDER=codex_cli\n' >> "$ENV_FILE"
  say "FOLLOWUP_LLM_PROVIDER=codex_cli ajouté ✓"
fi
if grep -q '^DAEMON_CORE_USER_ID=' "$ENV_FILE"; then
  say "DAEMON_CORE_USER_ID déjà présent ✓"
else
  # The daemon_voice middleware needs the user identifier in the gateway
  # environment; Hermes middleware does not provide it on each turn.
  # Autodetect it only when the memory contains exactly one user.
  USERS_DIR="$REPO_ROOT/memory/users"
  DETECTED_UID=""
  if [ -d "$USERS_DIR" ]; then
    # Telegram IDs are numeric; test directory names are not.
    DETECTED_UID="$(find "$USERS_DIR" -mindepth 1 -maxdepth 1 -type d -exec basename {} \; | grep -E '^[0-9]+$' | head -2)"
    [ "$(printf '%s\n' "$DETECTED_UID" | wc -l)" -gt 1 ] && DETECTED_UID=""
  fi
  if [ -n "$DETECTED_UID" ]; then
    printf 'DAEMON_CORE_USER_ID=%s\n' "$DETECTED_UID" >> "$ENV_FILE"
    say "DAEMON_CORE_USER_ID=$DETECTED_UID détecté et ajouté ✓"
  else
    printf '# DAEMON_CORE_USER_ID=<ton id Telegram> (requis pour la voix souveraine)\n' >> "$ENV_FILE"
    say "⚠ DAEMON_CORE_USER_ID à renseigner dans $ENV_FILE (sinon la voix souveraine reste inactive)"
  fi
fi
if grep -q '^DAEMON_CORE_SOVEREIGN_VOICE=' "$ENV_FILE"; then
  say "DAEMON_CORE_SOVEREIGN_VOICE déjà présent ✓"
else
  # Enable canonical voice composition in the Hermes middleware.
  # Hermes retains the response model and its native tool loop.
  printf 'DAEMON_CORE_SOVEREIGN_VOICE=on\n' >> "$ENV_FILE"
  say "DAEMON_CORE_SOVEREIGN_VOICE=on ajouté ✓"
fi
# Recognize either current heading form without duplicating reminders.
if ! grep -qE '^# --- daemon-core providers([[:space:]]|$)' "$ENV_FILE"; then
  cat >> "$ENV_FILE" <<'EOF'
# --- daemon-core providers (configure for your models) ---
# In delegate mode, the voice and initiative judge follow "hermes model".
# CONVERSATION_* serves selection, expansion and deferred follow-ups;
# use a model capable of judgment, without necessarily duplicating the voice.
# ROUTING_LLM_PROVIDER=openai
# CONVERSATION_LLM_PROVIDER=openai
# MEMORY_LLM_PROVIDER=openai
# Add the corresponding keys and models; see the repository's .env.example.
EOF
  say "rappels providers ajoutés (commentés) ✓"
fi

# --- 3. config.yaml ----------------------------------------------------------
step "config.yaml"
YAML_SNIPPET='memory:
  provider: daemon_core
  memory_enabled: false
  user_profile_enabled: false'

merge_config() {
  # Edit line by line to preserve ALL existing comments.
  # No PyYAML dependency; change only the three keys under memory.
  python3 - "$CONFIG" <<'PY'
import sys, re
path = sys.argv[1]
targets = {"provider": "daemon_core", "memory_enabled": "false", "user_profile_enabled": "false"}
try:
    with open(path, encoding="utf-8") as f:
        lines = f.read().split("\n")
except FileNotFoundError:
    lines = []

def find_block(lines):
    for i, ln in enumerate(lines):
        if re.match(r'^memory:\s*(#.*)?$', ln):
            j, end = i + 1, len(lines)
            while j < len(lines):
                l = lines[j]
                if l and not l[0].isspace() and not l.lstrip().startswith("#"):
                    end = j; break
                j += 1
            return i, end
    return None, None

start, end = find_block(lines)
changed = False
if start is None:
    if lines and lines[-1].strip():
        lines.append("")
    lines.append("memory:")
    for k, v in targets.items():
        lines.append(f"  {k}: {v}")
    changed = True
else:
    remaining = dict(targets)
    for idx in range(start + 1, end):
        m = re.match(r'^(\s+)([A-Za-z_]+):\s*(.*)$', lines[idx])
        if m and m.group(2) in remaining:
            indent, key = m.group(1), m.group(2)
            lines[idx] = f"{indent}{key}: {remaining.pop(key)}"
            changed = True
    if remaining:
        indent = "  "
        for idx in range(start + 1, end):
            mm = re.match(r'^(\s+)[A-Za-z_]+:', lines[idx])
            if mm:
                indent = mm.group(1); break
        ins = [f"{indent}{k}: {v}" for k, v in remaining.items()]
        lines[start + 1:start + 1] = ins
        changed = True

if changed:
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
print("OK" if changed else "NOCHANGE")
PY
}

if command -v python3 >/dev/null 2>&1 && [ -f "$CONFIG" ]; then
  [ ! -f "$CONFIG.bak" ] && cp "$CONFIG" "$CONFIG.bak" && say "sauvegarde : $CONFIG.bak"
  if out="$(merge_config)"; then
    say "config.yaml mis à jour ($out) ✓ — commentaires préservés"
  else
    say "⚠ édition auto échouée — ajoute ce bloc à la main dans $CONFIG :"; printf '\n%s\n\n' "$YAML_SNIPPET"
  fi
else
  say "⚠ python3 ou config.yaml absent — ajoute/édite ce bloc à la main dans $CONFIG :"; printf '\n%s\n\n' "$YAML_SNIPPET"
fi

if command -v hermes >/dev/null 2>&1; then
  # Otherwise Hermes periodically starts a second model to rewrite skills.
  # Skills remain available, but their evolution becomes explicit.
  if hermes config set skills.creation_nudge_interval 0 >/dev/null 2>&1; then
    say "auto-réécriture des skills Hermes → off ✓"
  else
    say "⚠ mets skills.creation_nudge_interval=0 dans $CONFIG"
  fi
  # Since Hermes 0.21, native memory/skill review also has a global switch.
  # The daemon already performs maintenance after the turn:
  # disable the native fork to avoid a second agent and redundant LLM calls.
  # The preceding setting is still required by older Hermes versions.
  if hermes config set auxiliary.background_review.enabled false >/dev/null 2>&1; then
    say "revue native mémoire/skills Hermes → off ✓"
  else
    say "⚠ mets auxiliary.background_review.enabled=false dans $CONFIG"
  fi
  # Also disable legacy notices about that review in the chat.
  if hermes config set display.memory_notifications off >/dev/null 2>&1; then
    say "notices self-improvement Hermes → off ✓"
  else
    say "⚠ mets display.memory_notifications=off si des '💾' polluent le chat"
  fi
  # Plain cron output: otherwise each reminder gets a "Cronjob Response"
  # header with a job ID and a management footer.
  # Setting this to false delivers the raw message.
  if hermes config set cron.wrap_response false >/dev/null 2>&1; then
    say "en-tête/footer des rappels cron → off ✓"
  else
    say "⚠ mets cron.wrap_response=false si l'en-tête 'Cronjob Response' pollue le chat"
  fi
  # Show tool activity on Telegram so a web search does not appear
  # as a long period of silence.
  # "all" updates one activity bubble while work is in progress;
  # cleanup_progress removes it after the response is delivered.
  # Activity remains visible during the wait without cluttering the conversation.
  if hermes config set display.platforms.telegram.tool_progress all >/dev/null 2>&1 \
     && hermes config set display.platforms.telegram.cleanup_progress true >/dev/null 2>&1; then
    say "activité d'outils Telegram → visible pendant le tour, effacée après ✓"
  else
    say "⚠ mets display.platforms.telegram.tool_progress=all et cleanup_progress=true à la main"
  fi
  # Incoming messages normally INTERRUPT the current Hermes turn,
  # potentially cancelling a search already in progress. "steer"
  # injects the new message into the turn while keeping the work running.
  # If injection is unavailable, it falls back to queuing,
  # never interruption.
  if hermes config set display.busy_input_mode steer >/dev/null 2>&1; then
    say "message en plein tour → injecté sans interrompre (steer) ✓"
  else
    say "⚠ mets display.busy_input_mode=steer pour éviter qu'un message casse le tour en cours"
  fi
fi

# Hermes replies with a quoted message by default (reply_to_mode: first),
# adding unnecessary weight to each direct-message bubble.
# Set the STRING "off": "hermes config set ... off" would coerce it
# to a boolean, although the code expects a string. Edit it directly
# while preserving comments, as merge_config does.
step "Telegram : citation systématique off"
set_reply_off() {
  python3 - "$CONFIG" <<'PY'
import sys, re
path = sys.argv[1]
try:
    with open(path, encoding="utf-8") as f:
        lines = f.read().split("\n")
except FileNotFoundError:
    sys.exit("NOCONFIG")

def find_block(lines, name):
    for i, ln in enumerate(lines):
        if re.match(rf'^{name}:\s*(#.*)?$', ln):
            j, end = i + 1, len(lines)
            while j < len(lines):
                l = lines[j]
                if l and not l[0].isspace() and not l.lstrip().startswith("#"):
                    end = j; break
                j += 1
            return i, end
    return None, None

start, end = find_block(lines, "telegram")
changed = False
if start is None:
    if lines and lines[-1].strip():
        lines.append("")
    lines += ["telegram:", '  reply_to_mode: "off"']
    changed = True
else:
    done = False
    for idx in range(start + 1, end):
        m = re.match(r'^(\s+)reply_to_mode:\s*(.*)$', lines[idx])
        if m:
            if m.group(2).strip() != '"off"':
                lines[idx] = f'{m.group(1)}reply_to_mode: "off"'
                changed = True
            done = True
            break
    if not done:
        indent = "  "
        for idx in range(start + 1, end):
            mm = re.match(r'^(\s+)[A-Za-z_]+:', lines[idx])
            if mm:
                indent = mm.group(1); break
        lines[start + 1:start + 1] = [f'{indent}reply_to_mode: "off"']
        changed = True

if changed:
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
print("OK" if changed else "NOCHANGE")
PY
}
if command -v python3 >/dev/null 2>&1 && [ -f "$CONFIG" ]; then
  if out="$(set_reply_off)"; then
    say "citation systématique Telegram → off ✓ ($out)"
  else
    say "⚠ mets telegram.reply_to_mode: \"off\" à la main dans $CONFIG"
  fi
fi

# --- 4. shared nightly maintenance -------------------------------------------
step "Dreaming/REM commun"
"$REPO_ROOT/integrations/dreaming_service/service.sh" install
say "planification commune installée sans dépendre de Hermes ✓"

# --- 5. runtime contract ------------------------------------------------------
step "Contrôle du runtime"
python3 "$REPO_ROOT/scripts/check_runtime_pointers.py" \
  --hermes-home "$HERMES_HOME" \
  --openclaw-home "$HOME/.openclaw"
say "source et plugins actifs vérifiés ✓"

# --- Done ---------------------------------------------------------------------
step "Terminé"
say "1. Renseigne les providers LLM dans $ENV_FILE (ROUTING_*/CONVERSATION_*/MEMORY_*)."
say "2. Vérifie : hermes memory status   puis   hermes gateway restart"
say "3. La voix du daemon (persona.md) et le dreaming nocturne sont actifs automatiquement."
say "Pour désinstaller : $SCRIPT_DIR/uninstall.sh"
