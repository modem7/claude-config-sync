#!/usr/bin/env bash
set -euo pipefail

if [ -n "${CLAUDE_SYNC_REPO_URL:-}" ]; then
  REPO_URL="${CLAUDE_SYNC_REPO_URL}"
elif [ -t 0 ]; then
  read -r -p "Git URL of your claude-config sync repo (create an empty one on GitHub/GitLab/etc first): " REPO_URL
  if [ -z "${REPO_URL}" ]; then
    echo "A repo URL is required. Re-run with CLAUDE_SYNC_REPO_URL=<url> $0" >&2
    exit 1
  fi
else
  echo "CLAUDE_SYNC_REPO_URL is not set and no terminal is attached to prompt for it." >&2
  echo "Re-run with: CLAUDE_SYNC_REPO_URL=<your sync repo url> $0" >&2
  exit 1
fi
REPO_DIR="${CLAUDE_SYNC_REPO_DIR:-${HOME}/claude-config}"
HOSTNAME_VAL="${CLAUDE_SYNC_HOSTNAME:-$(hostname)}"
CLAUDE_HOME="${CLAUDE_SYNC_CLAUDE_HOME:-${HOME}/.claude}"
AGENTS_HOME="${CLAUDE_SYNC_AGENTS_HOME:-${HOME}/.agents}"

# --repair (alias --reinstall) resets this machine's OWN sync bookkeeping —
# the local repo clone, this machine's entry in machines/<hostname>.conf
# (removed from the shared repo too, not just locally, so a stale entry
# doesn't linger for other machines to see), and the skill-discovery
# symlinks this tool created — so the flow below runs exactly as it would
# on a machine that has never run this script before. It does NOT touch
# real Claude Code config content (~/.claude/CLAUDE.md, settings.json,
# ~/.claude/agents/, ~/.claude/hooks/, ~/.agents/skills/, memory) — repair
# resets the TOOL's state, not your actual config, which then gets adopted
# fresh via the normal bootstrap flow below.
case "${1:-}" in
  --repair|--reinstall)
    echo "Repairing: resetting local sync state for ${HOSTNAME_VAL}..."

    # rm -rf "${REPO_DIR}" below would otherwise pull the rug out from under
    # a shell whose cwd is inside it (the natural place to be when invoking
    # ~/claude-config/install.sh), breaking getcwd() for every command after.
    cd "${HOME}" 2>/dev/null || cd /

    if [ -d "${REPO_DIR}/.git" ]; then
      if [ -n "$(git -C "${REPO_DIR}" status --porcelain 2>/dev/null)" ]; then
        echo "Refusing to repair: ${REPO_DIR} has uncommitted or untracked changes. Commit, stash, or discard them first." >&2
        exit 1
      fi
      if git -C "${REPO_DIR}" rev-parse --abbrev-ref --symbolic-full-name '@{u}' >/dev/null 2>&1 \
        && [ -n "$(git -C "${REPO_DIR}" log '@{u}..HEAD' --oneline 2>/dev/null)" ]; then
        echo "Refusing to repair: ${REPO_DIR} has unpushed commits. Push or discard them first." >&2
        exit 1
      fi
    fi

    rm -rf "${REPO_DIR}"
    git clone "${REPO_URL}" "${REPO_DIR}"

    HOST_CONF_PATH="machines/${HOSTNAME_VAL}.conf"
    if [ -f "${REPO_DIR}/${HOST_CONF_PATH}" ]; then
      git -C "${REPO_DIR}" rm -q "${HOST_CONF_PATH}"
      git -C "${REPO_DIR}" commit -q -m "repair: reset ${HOSTNAME_VAL}'s machine config"
      git -C "${REPO_DIR}" push
    fi

    if [ -d "${CLAUDE_HOME}/skills" ]; then
      for link in "${CLAUDE_HOME}"/skills/*; do
        [ -L "${link}" ] || continue
        case "$(readlink "${link}")" in
          "${AGENTS_HOME}"/skills/*) rm -f "${link}" ;;
        esac
      done
    fi
    ;;
esac

if [ ! -d "${REPO_DIR}/.git" ]; then
  git clone "${REPO_URL}" "${REPO_DIR}"
fi

# npm-installed companion tools (e.g. ccusage) live outside the plugin
# marketplace system, so claude-sync.sh has no way to carry them via git.
# Runs on every install.sh invocation, not just first-time setup: npm
# install -g pkg@latest installs if missing, upgrades if outdated, and
# no-ops if already current.
"${REPO_DIR}/npm-tools.sh" || true

# gh CLI is what Claude Code is configured to use for GitHub work (the
# github MCP plugin is disabled - see dot-claude/settings.json), so make
# sure it's actually present. No-ops if already installed.
"${REPO_DIR}/gh-tools.sh" || true

CONF_FILE="${REPO_DIR}/machines/${HOSTNAME_VAL}.conf"
CONF_ALREADY_EXISTED="false"
[ -f "${CONF_FILE}" ] && CONF_ALREADY_EXISTED="true"

if [ ! -f "${CONF_FILE}" ]; then
  DEFAULT_PROJECT_PATH="${HOME}/project"
  if [ -n "${CLAUDE_SYNC_PROJECT_PATH:-}" ]; then
    project_path="${CLAUDE_SYNC_PROJECT_PATH}"
  else
    read -r -p "Primary Claude Code working directory on this machine [${DEFAULT_PROJECT_PATH}]: " project_path
    project_path="${project_path:-${DEFAULT_PROJECT_PATH}}"
  fi
  mkdir -p "${REPO_DIR}/machines"
  printf 'PRIMARY_PROJECT_PATH=%s\n' "${project_path}" > "${CONF_FILE}"
fi

if [ "${CONF_ALREADY_EXISTED}" = "true" ]; then
  exec "${REPO_DIR}/claude-sync.sh" sync
else
  exec "${REPO_DIR}/claude-sync.sh" bootstrap
fi
