#!/bin/bash
# ================================================================
# plugin-suggest.sh — Disabled-Plugin Suggester
# ================================================================
# PURPOSE:
#   Idle plugins are disabled by default to save tokens, and Claude
#   cannot see the descriptions of disabled plugins, so it can never
#   suggest one itself. This hook closes that gap: when the project
#   looks like it needs a disabled plugin, it says so in ONE line.
#   When nothing matches it prints nothing, so it costs no tokens.
#
# HOW IT WORKS:
#   1. Reads the hook JSON from stdin and takes "cwd".
#   2. Checks cheap file markers in that folder (the RULES table
#      below). No network, no model calls, no state files.
#   3. Skips a plugin when it is already listed in enabledPlugins of
#      <cwd>/.claude/settings.json or settings.local.json (true OR
#      false: an explicit false is the opt-out), when the user
#      settings enable it globally, or when it is named in
#      <cwd>/.claude/plugin-suggest-ignore (one id per line, or the
#      word "all").
#   4. Prints one plain-text line. SessionStart stdout is shown to
#      Claude.
#
# TRIGGER: SessionStart
#
# CONFIGURATION (environment variables, optional):
#   CC_PLUGIN_SUGGEST=0  — turn the hook off everywhere
#
# NEVER BLOCKS: always exits 0, and bad or missing input is a silent
#   no-op. Needs jq; without it the hook does nothing.
# ================================================================

# --- Rule table (edit here) -------------------------------------
# Format: plugin-id|what was found|kind|argument
#   kind glob : space-separated globs relative to the project root;
#               any match counts
#   kind grep : file::extended-regex; the file must exist and match
RULES=(
  "docker@claude-plugins-official|a Dockerfile or compose file|glob|Dockerfile Dockerfile.* *.dockerfile compose*.yml compose*.yaml docker-compose*.yml docker-compose*.yaml"
  "deployment-engineer@awesome-claude-code-plugins|CI/CD or deploy config|glob|.gitlab-ci.yml .woodpecker.yml .woodpecker.yaml .woodpecker .github/workflows/*.yml .github/workflows/*.yaml *.tf Chart.yaml helm k8s kubernetes kustomization.yaml"
  "crowdsec@crowdsecurity|CrowdSec config|glob|acquis.yaml acquis.d crowdsec crowdsec*.yaml *bouncer*"
  "frontend-design@claude-plugins-official|a frontend project|grep|package.json::\"(react|vue|svelte|next|@sveltejs/[a-z-]+)\""
  "frontend-design@claude-plugins-official|a frontend project|grep|index.html::(<style|stylesheet)"
)
# ----------------------------------------------------------------

[ "${CC_PLUGIN_SUGGEST:-1}" = "0" ] && exit 0
command -v jq >/dev/null 2>&1 || exit 0

# timeout guards against a stdin that never closes
INPUT=$(timeout 1 cat 2>/dev/null)
CWD=$(printf '%s' "$INPUT" | jq -r 'if type == "object" then (.cwd // empty | strings) else empty end' 2>/dev/null)
[ -n "$CWD" ] && [[ "$CWD" == /* ]] && [ -d "$CWD" ] || exit 0

# Plugin ids present in a settings file's enabledPlugins (any value).
listed_ids() {
  [ -f "$1" ] && [ ! -L "$1" ] || return 0
  jq -r '(.enabledPlugins | objects | keys[]) // empty' "$1" 2>/dev/null
}
# Plugin ids the user settings turn on globally.
global_on_ids() {
  local f="$HOME/.claude/settings.json"
  [ -f "$f" ] || return 0
  jq -r '(.enabledPlugins | objects | to_entries[] | select(.value == true) | .key) // empty' "$f" 2>/dev/null
}

SKIP=$'\n'"$(listed_ids "$CWD/.claude/settings.json")"$'\n'"$(listed_ids "$CWD/.claude/settings.local.json")"$'\n'"$(global_on_ids)"$'\n'
IGNORE_FILE="$CWD/.claude/plugin-suggest-ignore"
if [ -f "$IGNORE_FILE" ] && [ ! -L "$IGNORE_FILE" ]; then
  IGN=$(head -c 4096 "$IGNORE_FILE" 2>/dev/null | tr -d '\r')
  # Exact whole-line "all" opts the project out of every hint.
  printf '%s\n' "$IGN" | grep -qxF 'all' && exit 0
  SKIP+="$IGN"$'\n'
fi

matches() {
  local kind="$1" arg="$2" g f re
  case "$kind" in
    glob)
      for g in $arg; do
        compgen -G "$CWD/$g" >/dev/null 2>&1 && return 0
      done ;;
    grep)
      f="${arg%%::*}"; re="${arg#*::}"
      [ -f "$CWD/$f" ] && head -c 262144 "$CWD/$f" 2>/dev/null | grep -Eiq -- "$re" && return 0 ;;
  esac
  return 1
}

FOUND=""      # "found; found"
IDS=""        # newline-delimited ids already reported
PLUGINS=""    # "id, id"
for rule in "${RULES[@]}"; do
  IFS='|' read -r id label kind arg <<<"$rule"
  case "$SKIP" in *$'\n'"$id"$'\n'*) continue ;; esac
  case "$IDS" in *$'\n'"$id"$'\n'*) continue ;; esac
  matches "$kind" "$arg" || continue
  IDS+=$'\n'"$id"$'\n'
  FOUND+="${FOUND:+; }$label ($id)"
  PLUGINS+="${PLUGINS:+, }$id"
done

[ -n "$PLUGINS" ] || exit 0
echo "Plugin hint: this project has ${FOUND}; the plugin(s) are disabled. Offer to enable ${PLUGINS} under enabledPlugins in .claude/settings.json (takes effect next session). To stop this hint, the user can list the id in .claude/plugin-suggest-ignore."
exit 0
