#!/usr/bin/env bash
set -euo pipefail
set +x

# Kept as the bash installer until phase 3 of the Python port; the install.sh
# shim at the clone root runs it when CLAUDE_SYNC_IMPL=bash. It finishes by
# running the clone's claude-sync.sh shim, which follows the same variable.
#
# install.sh [--repair|--reinstall] [--profile NAME]
# --profile (or CLAUDE_SYNC_PROFILE) makes this machine follow the machine
# profile machines/NAME/ instead of its own; it's written as PROFILE=NAME
# into machines/<hostname>.conf when that file is created.
REPAIR="false"
PROFILE_NAME="${CLAUDE_SYNC_PROFILE:-}"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --repair|--reinstall) REPAIR="true" ;;
    --profile)
      [ "$#" -ge 2 ] || { echo "--profile needs a profile name." >&2; exit 1; }
      PROFILE_NAME="$2"
      shift
      ;;
    --profile=*) PROFILE_NAME="${1#--profile=}" ;;
    *)
      echo "Unknown option: $1" >&2
      echo "Usage: install.sh [--repair|--reinstall] [--profile NAME]" >&2
      exit 1
      ;;
  esac
  shift
done
if [ -n "${PROFILE_NAME}" ] && { ! [[ "${PROFILE_NAME}" =~ ^[A-Za-z0-9._-]+$ ]] \
    || [ "${PROFILE_NAME}" = "." ] || [ "${PROFILE_NAME}" = ".." ]; }; then
  echo "Not a valid profile name: ${PROFILE_NAME}" >&2
  exit 1
fi

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

# Prerequisites: git 2.40+, and the GitHub CLI (gh) logged in with
# 'gh auth login'. claude-sync.sh never pushes to the default branch; it
# pushes sync/<hostname> and opens a PR for it with gh.
#
# --repair (alias --reinstall) resets this machine's OWN sync bookkeeping —
# the local repo clone, this machine's entry in machines/<hostname>.conf
# (rewritten from scratch; the change reaches the shared repo through this
# machine's sync PR like any other), and the skill-discovery
# symlinks this tool created — so the flow below runs exactly as it would
# on a machine that has never run this script before. It does NOT touch
# real Claude Code config content (~/.claude/CLAUDE.md, settings.json,
# ~/.claude/agents/, ~/.claude/hooks/, ~/.agents/skills/, memory) — repair
# resets the TOOL's state, not your actual config, which then gets adopted
# fresh via the normal bootstrap flow below. Machine profiles
# (machines/<profile>/) and settings overrides (machines/<name>.settings.json)
# are shared repo content, not bookkeeping, so they're kept; only the .conf
# goes, so a PROFILE= line in it must be given again with --profile.
if [ "${REPAIR}" = "true" ]; then
  echo "Repairing: resetting local sync state for ${HOSTNAME_VAL}..."

  # rm -rf "${REPO_DIR}" below would otherwise pull the rug out from under
  # a shell whose cwd is inside it (the natural place to be when invoking
  # ~/claude-config/install.sh), breaking getcwd() for every command after.
  cd "${HOME}" 2>/dev/null || cd /

  if [ -d "${REPO_DIR}/.git" ]; then
    if [ -n "$(git -C "${REPO_DIR}" status --porcelain 2>/dev/null)" ]; then
      echo "Refusing to repair: ${REPO_DIR} has changes that are not on its default branch." >&2
      echo "  The usual cause is this machine's pending sync PR (sync/${HOSTNAME_VAL}): the clone holds its changes." >&2
      echo "  Merge that PR (or close it and delete the branch), run 'claude-sync.sh sync', then re-run this." >&2
      echo "  Do not use git stash or git reset --hard in the clone: that hides or loses those changes." >&2
      exit 1
    fi
    if git -C "${REPO_DIR}" rev-parse --abbrev-ref --symbolic-full-name '@{u}' >/dev/null 2>&1 \
      && [ -n "$(git -C "${REPO_DIR}" log '@{u}..HEAD' --oneline 2>/dev/null)" ]; then
      echo "Refusing to repair: ${REPO_DIR} has commits on its default branch that are not on origin." >&2
      echo "  Run 'claude-sync.sh sync' to move them onto sync/${HOSTNAME_VAL} and open a PR (never push them" >&2
      echo "  to the default branch), then merge or close that PR and re-run this." >&2
      exit 1
    fi
  fi

  rm -rf "${REPO_DIR}"
  git clone "${REPO_URL}" "${REPO_DIR}"

  # Only from the working tree: the new conf written below goes out with
  # this machine's sync PR, so nothing is pushed to the default branch.
  rm -f "${REPO_DIR}/machines/${HOSTNAME_VAL}.conf"

  if [ -d "${CLAUDE_HOME}/skills" ]; then
    for link in "${CLAUDE_HOME}"/skills/*; do
      [ -L "${link}" ] || continue
      case "$(readlink "${link}")" in
        "${AGENTS_HOME}"/skills/*) rm -f "${link}" ;;
      esac
    done
  fi
fi

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

# claude-sync.sh opens each machine's sync PR with gh. Only a hint here:
# without gh, syncs still push the branch, then fail loudly.
GH_CMD="${CLAUDE_SYNC_GH:-gh}"
if ! command -v "${GH_CMD}" >/dev/null 2>&1; then
  echo "Note: the GitHub CLI (gh) is not installed. claude-sync.sh needs it to open its sync PRs;" >&2
  echo "  without it every sync pushes sync/${HOSTNAME_VAL} and then fails. Install gh, then run 'gh auth login'." >&2
elif ! GH_PROMPT_DISABLED=1 "${GH_CMD}" auth status >/dev/null 2>&1; then
  echo "Note: gh is not logged in, so claude-sync.sh can't open its sync PRs. Run 'gh auth login'." >&2
fi

# The agent-memory plugin (synced from claude.ai, not this repo) runs
# `python` and needs mcp 1.x plus a couple of pip packages, so provide them.
# No-ops if already in place.
"${REPO_DIR}/python-tools.sh" || true

CONF_FILE="${REPO_DIR}/machines/${HOSTNAME_VAL}.conf"
CONF_ALREADY_EXISTED="false"
[ -f "${CONF_FILE}" ] && CONF_ALREADY_EXISTED="true"

if [ -n "${PROFILE_NAME}" ] && [ "${CONF_ALREADY_EXISTED}" = "true" ]; then
  echo "machines/${HOSTNAME_VAL}.conf already exists, so --profile is ignored. Use: claude-sync.sh profile use ${PROFILE_NAME}" >&2
elif [ -n "${PROFILE_NAME}" ] && [ "${PROFILE_NAME}" != "${HOSTNAME_VAL}" ] \
    && [ ! -d "${REPO_DIR}/machines/${PROFILE_NAME}" ]; then
  echo "No profile called '${PROFILE_NAME}': machines/${PROFILE_NAME}/ does not exist in the repo." >&2
  exit 1
fi

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
  if [ -n "${PROFILE_NAME}" ] && [ "${PROFILE_NAME}" != "${HOSTNAME_VAL}" ]; then
    printf 'PROFILE=%s\n' "${PROFILE_NAME}" >> "${CONF_FILE}"
  fi
fi

if [ "${CONF_ALREADY_EXISTED}" = "true" ]; then
  exec "${REPO_DIR}/claude-sync.sh" sync
else
  exec "${REPO_DIR}/claude-sync.sh" bootstrap
fi
