#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE="${OPENCLAW_DAEMON_WORKSPACE:-$HOME/.openclaw/workspace-daemon-harness}"
ACTION="${1:-install}"

status() {
  local healthy=0
  local configured
  local native_memory_hook
  local native_memory_plugin
  local autonomous_skill_learning
  local denied_tools
  local plugin_allow
  local browser_enabled
  local browser_headless
  local also_allowed_tools
  local daemon_conversation_hooks
  local skip_bootstrap
  configured="$(openclaw config get agents.defaults.workspace 2>/dev/null || true)"
  skip_bootstrap="$(openclaw config get agents.defaults.skipBootstrap 2>/dev/null || true)"
  native_memory_hook="$(openclaw config get hooks.internal.entries.session-memory.enabled 2>/dev/null || true)"
  native_memory_plugin="$(openclaw config get plugins.entries.memory-core.enabled 2>/dev/null || true)"
  autonomous_skill_learning="$(openclaw config get skills.workshop.autonomous.mode 2>/dev/null || true)"
  denied_tools="$(openclaw config get tools.deny 2>/dev/null || true)"
  plugin_allow="$(openclaw config get plugins.allow 2>/dev/null || true)"
  browser_enabled="$(openclaw config get plugins.entries.browser.enabled 2>/dev/null || true)"
  browser_headless="$(openclaw config get browser.headless 2>/dev/null || true)"
  also_allowed_tools="$(openclaw config get tools.alsoAllow 2>/dev/null || true)"
  daemon_conversation_hooks="$(openclaw config get plugins.entries.daemon-harness.hooks.allowConversationAccess 2>/dev/null || true)"
  echo "Workspace daemon : $WORKSPACE"
  echo "Workspace configuré : ${configured:-non défini}"
  [[ -f "$WORKSPACE/AGENTS.md" ]] && echo "AGENTS.md : présent" || echo "AGENTS.md : absent"
  [[ -f "$WORKSPACE/TOOLS.md" ]] && echo "Ancien TOOLS.md : présent (migration requise)" || echo "Ancien TOOLS.md : absent"
  [[ -f "$WORKSPACE/openclaw-workspace-state.json" ]] && echo "Ancien état workspace : présent (migration requise)" || echo "Ancien état workspace : absent"
  echo "Bootstrap OpenClaw : ${skip_bootstrap:-non défini}"
  echo "Hook mémoire natif : ${native_memory_hook:-non défini}"
  echo "Plugin mémoire/Dreaming natif : ${native_memory_plugin:-non défini}"
  echo "Apprentissage automatique des skills : ${autonomous_skill_learning:-non défini}"
  echo "Outils refusés : ${denied_tools:-aucun}"
  echo "Plugins autorisés : ${plugin_allow:-liste ouverte}"
  echo "Navigateur OpenClaw activé : ${browser_enabled:-non défini}"
  echo "Navigateur sans fenêtre : ${browser_headless:-non défini}"
  echo "Outils ajoutés au profil : ${also_allowed_tools:-aucun}"
  echo "Hooks conversationnels du daemon : ${daemon_conversation_hooks:-non défini}"

  [[ "$configured" == "$WORKSPACE" ]] || healthy=1
  [[ -f "$WORKSPACE/AGENTS.md" ]] || healthy=1
  [[ -f "$WORKSPACE/AGENTS.md" ]] \
    && cmp -s "$SCRIPT_DIR/workspace/AGENTS.md" "$WORKSPACE/AGENTS.md" \
    || healthy=1
  [[ ! -f "$WORKSPACE/TOOLS.md" ]] || healthy=1
  [[ ! -f "$WORKSPACE/openclaw-workspace-state.json" ]] || healthy=1
  [[ "$skip_bootstrap" == "true" ]] || healthy=1
  [[ "$native_memory_hook" == "false" ]] || healthy=1
  [[ "$native_memory_plugin" == "false" ]] || healthy=1
  [[ "$autonomous_skill_learning" == "off" ]] || healthy=1
  json_array_has_all "$denied_tools" memory_search memory_get || healthy=1
  json_array_has_all "$also_allowed_tools" \
    daemon_read_souvenirs daemon_recall_l0 browser || healthy=1
  [[ "$daemon_conversation_hooks" == "true" ]] || healthy=1
  [[ "$browser_enabled" == "true" ]] || healthy=1
  if [[ -n "$plugin_allow" ]]; then
    json_array_has_all "$plugin_allow" browser || healthy=1
  fi
  if [[ "$healthy" -eq 0 ]]; then
    echo "Contrat du workspace daemon : correct"
  else
    echo "Contrat du workspace daemon : incorrect"
  fi
  return "$healthy"
}

json_array_has_all() {
  local current="$1"
  shift
  [[ -n "$current" ]] || return 1
  node -e '
    const current = JSON.parse(process.argv[1]);
    if (!Array.isArray(current)) process.exit(1);
    const required = process.argv.slice(2);
    process.exit(required.every((item) => current.includes(item)) ? 0 : 1);
  ' "$current" "$@"
}

normalize_legacy_workspace_state() {
  local state_path="$WORKSPACE/openclaw-workspace-state.json"
  [[ -f "$state_path" ]] || return 0
  node -e '
    const fs = require("fs");
    const statePath = process.argv[1];
    const raw = fs.readFileSync(statePath, "utf8");
    const state = JSON.parse(raw);
    let changed = false;
    const keys = Object.keys(state).sort();
    const oldInstallerMarker =
      state.version === 1 &&
      keys.length === 2 &&
      keys[0] === "setupCompletedAt" &&
      keys[1] === "version" &&
      typeof state.setupCompletedAt === "string" &&
      Number.isFinite(new Date(state.setupCompletedAt).getTime());
    if (oldInstallerMarker) {
      // This date was created by the old installer, not by OpenClaw.
      // Omitting it lets Doctor check and preserve any existing
      // canonical SQLite record.
      delete state.setupCompletedAt;
      changed = true;
    } else {
      for (const field of ["bootstrapSeededAt", "setupCompletedAt", "onboardingCompletedAt"]) {
        const value = state[field];
        if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(value)) continue;
        const normalized = value.replace(/Z$/, ".000Z");
        if (new Date(normalized).toISOString() !== normalized) continue;
        state[field] = normalized;
        changed = true;
      }
    }
    if (!changed) process.exit(0);
    const mode = fs.statSync(statePath).mode & 0o777;
    const temporary = `${statePath}.daemon-harness-normalizing-${process.pid}`;
    fs.writeFileSync(temporary, `${JSON.stringify(state, null, 2)}\n`, { mode });
    fs.renameSync(temporary, statePath);
  ' "$state_path"
}

migrate_retired_workspace_files() {
  if [[ -f "$WORKSPACE/openclaw-workspace-state.json" || \
        -f "$WORKSPACE/.openclaw/workspace-state.json" || \
        -f "$WORKSPACE/TOOLS.md" ]]; then
    # Older installers wrote ISO dates without milliseconds, rejected by recent
    # beta versions. Normalize only that exact legacy format.
    normalize_legacy_workspace_state
    # Recent betas keep workspace state in SQLite and moved tool conventions
    # into AGENTS.md's "Tools" section. Doctor imports legacy files,
    # verifies the write, and only then removes the originals.
    openclaw doctor --fix --non-interactive
  fi
  if [[ -f "$WORKSPACE/openclaw-workspace-state.json" || \
        -f "$WORKSPACE/.openclaw/workspace-state.json" || \
        -f "$WORKSPACE/TOOLS.md" ]]; then
    echo "Migration du workspace OpenClaw incomplète. Arrêter la gateway puis relancer l'installation." >&2
    return 1
  fi
}

install_file() {
  local source="$1"
  local target="$2"
  if [[ -f "$target" ]] && ! cmp -s "$source" "$target"; then
    cp "$target" "$target.bak-$(date +%Y%m%d-%H%M%S)"
  fi
  cp "$source" "$target"
}

deny_native_memory_tools() {
  local current
  local merged
  current="$(openclaw config get tools.deny 2>/dev/null || printf '[]')"
  merged="$(
    node -e '
      const current = JSON.parse(process.argv[1]);
      const denied = [...new Set([...current, "memory_search", "memory_get"])];
      process.stdout.write(JSON.stringify(denied));
    ' "$current"
  )"
  openclaw config set tools.deny "$merged" --strict-json
}

disable_parallel_personal_memory() {
  # The daemon owns personal memory and Dreaming.
  # Skills remain available, but automatic creation must not silently
  # reintroduce a second persona or memory corpus.
  openclaw config set --batch-json \
    '[{"path":"plugins.entries.memory-core.enabled","value":false},{"path":"skills.workshop.autonomous.mode","value":"off"}]'
}

enable_native_browser_tool() {
  local plugin_allow
  local merged_plugin_allow
  local also_allowed_tools
  local merged_also_allowed_tools
  local browser_headless

  # An absent plugin allowlist permits bundled plugins. Do not create one
  # containing only the browser: that would exclude other active plugins.
  # If an allowlist exists, extend it without removing entries.
  plugin_allow="$(openclaw config get plugins.allow 2>/dev/null || true)"
  if [[ -n "$plugin_allow" ]]; then
    merged_plugin_allow="$(
      node -e '
        const current = JSON.parse(process.argv[1]);
        const allowed = [...new Set([...current, "browser"])];
        process.stdout.write(JSON.stringify(allowed));
      ' "$plugin_allow"
    )"
    openclaw config set plugins.allow "$merged_plugin_allow" --strict-json
  fi

  # The browser is an OpenClaw native plugin. Daemon integration does not
  # reimplement it; it only makes activation explicit and persistent.
  openclaw config set plugins.entries.browser.enabled true --strict-json

  # A Telegram search should not open a desktop window. Still respect
  # any explicit preference already present in the host configuration.
  browser_headless="$(openclaw config get browser.headless 2>/dev/null || true)"
  if [[ -z "$browser_headless" ]]; then
    openclaw config set browser.headless true --strict-json
  fi

  # The coding profile does not expose browser by default. Add it
  # to the effective profile while preserving existing tool permissions.
  also_allowed_tools="$(openclaw config get tools.alsoAllow 2>/dev/null || printf '[]')"
  merged_also_allowed_tools="$(
    node -e '
      const current = JSON.parse(process.argv[1]);
      const allowed = [...new Set([
        ...current,
        "daemon_read_souvenirs",
        "daemon_recall_l0",
        "browser",
      ])];
      process.stdout.write(JSON.stringify(allowed));
    ' "$also_allowed_tools"
  )"
  openclaw config set tools.alsoAllow "$merged_also_allowed_tools" --strict-json
}

enable_daemon_conversation_hooks() {
  # OpenClaw 2026.9 requires explicit permission for non-bundled plugins
  # to project the system prompt and filter turn output.
  # Without it, the context engine appears loaded, but before_prompt_build
  # and llm_output are silently blocked.
  openclaw config set \
    plugins.entries.daemon-harness.hooks.allowConversationAccess \
    true --strict-json
}

case "$ACTION" in
  status)
    status
    exit 0
    ;;
  install)
    ;;
  *)
    echo "Usage: $0 [install|status]" >&2
    exit 2
    ;;
esac

mkdir -p "$WORKSPACE"
openclaw config set agents.defaults.workspace "$WORKSPACE"
openclaw config set agents.defaults.skipBootstrap true --strict-json
migrate_retired_workspace_files
install_file "$SCRIPT_DIR/workspace/AGENTS.md" "$WORKSPACE/AGENTS.md"
openclaw hooks disable session-memory
disable_parallel_personal_memory
deny_native_memory_tools
enable_native_browser_tool
enable_daemon_conversation_hooks
echo "Workspace souverain OpenClaw installé. Redémarrer la gateway pour l'activer."
