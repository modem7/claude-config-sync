#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/memory-path.sh
source "${SCRIPT_DIR}/lib/memory-path.sh"
# shellcheck source=lib/machine-conf.sh
source "${SCRIPT_DIR}/lib/machine-conf.sh"
# shellcheck source=lib/safety.sh
source "${SCRIPT_DIR}/lib/safety.sh"
# shellcheck source=lib/sync-files.sh
source "${SCRIPT_DIR}/lib/sync-files.sh"
# shellcheck source=lib/skills-symlink.sh
source "${SCRIPT_DIR}/lib/skills-symlink.sh"

REPO_DIR="${SCRIPT_DIR}"
CLAUDE_HOME="${CLAUDE_SYNC_CLAUDE_HOME:-${HOME}/.claude}"
AGENTS_HOME="${CLAUDE_SYNC_AGENTS_HOME:-${HOME}/.agents}"
HOSTNAME_VAL="${CLAUDE_SYNC_HOSTNAME:-$(hostname)}"

# Empty when there's nothing to compare against yet (e.g. a repo that was
# git-init'd rather than cloned, with no shared history at all) - only a
# real `git clone` sets refs/remotes/origin/HEAD, which every machine this
# tool sets up goes through via install.sh.
default_branch() {
  local ref
  ref="$(git -C "${REPO_DIR}" symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null)" || true
  echo "${ref#origin/}"
}

on_default_branch() {
  local def current
  def="$(default_branch)"
  # Nothing to compare against - don't block a legitimate first-ever sync.
  [ -z "${def}" ] && return 0
  current="$(git -C "${REPO_DIR}" rev-parse --abbrev-ref HEAD 2>/dev/null)" || true
  [ "${current}" = "${def}" ]
}

require_project_path() {
  local conf_file
  conf_file="$(machine_conf_path "${REPO_DIR}" "${HOSTNAME_VAL}")"
  if ! read_primary_project_path "${conf_file}"; then
    echo "No machine config at ${conf_file}. Run install.sh first." >&2
    exit 1
  fi
}

do_capture_and_commit() {
  local project_path memory_dir
  project_path="$(require_project_path)"
  memory_dir="$(resolve_memory_dir "${CLAUDE_HOME}" "${project_path}")"

  sync_capture "${REPO_DIR}" "${CLAUDE_HOME}" "${AGENTS_HOME}" "${memory_dir}"

  if ! assert_no_denylisted_files "${REPO_DIR}"; then
    echo "Refusing to commit: denylisted file present in repo tree." >&2
    exit 1
  fi

  git -C "${REPO_DIR}" add -A
  if ! git -C "${REPO_DIR}" diff --cached --quiet; then
    git -C "${REPO_DIR}" commit -m "sync: ${HOSTNAME_VAL} $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  fi
}

do_integrate() {
  if ! git -C "${REPO_DIR}" rev-parse --abbrev-ref --symbolic-full-name '@{u}' >/dev/null 2>&1; then
    return 0
  fi
  if ! git -C "${REPO_DIR}" pull --rebase; then
    git -C "${REPO_DIR}" rebase --abort || true
    echo "git pull --rebase failed (conflict). Resolve manually in ${REPO_DIR} and re-run." >&2
    return 1
  fi
}

do_push() {
  if git -C "${REPO_DIR}" rev-parse --abbrev-ref --symbolic-full-name '@{u}' >/dev/null 2>&1; then
    git -C "${REPO_DIR}" push
  else
    git -C "${REPO_DIR}" push -u origin HEAD
  fi
}

do_apply() {
  local project_path memory_dir
  project_path="$(require_project_path)"
  memory_dir="$(resolve_memory_dir "${CLAUDE_HOME}" "${project_path}")"

  sync_apply "${REPO_DIR}" "${CLAUDE_HOME}" "${AGENTS_HOME}" "${memory_dir}"
  wire_skill_symlinks "${REPO_DIR}" "${CLAUDE_HOME}" "${AGENTS_HOME}"
}

# Non-destructive counterpart to do_apply, used only for a machine's
# first-ever sync: merges the repo's shared config into local paths without
# deleting anything already local-only, instead of the normal full mirror.
do_apply_merge() {
  local project_path memory_dir
  project_path="$(require_project_path)"
  memory_dir="$(resolve_memory_dir "${CLAUDE_HOME}" "${project_path}")"

  sync_apply "${REPO_DIR}" "${CLAUDE_HOME}" "${AGENTS_HOME}" "${memory_dir}" "false"
  wire_skill_symlinks "${REPO_DIR}" "${CLAUDE_HOME}" "${AGENTS_HOME}"
}

DOCTOR_ISSUES=0

doctor_report() {
  DOCTOR_ISSUES=1
  echo "ISSUE: $1"
}

# Diagnoses (and, if requested, repairs) the repo clone getting stuck out of
# sync with its own local ~/.claude/settings.json. This happens when
# do_capture_and_commit (which runs before the pull) commits a snapshot of
# local settings.json, and a subsequent `git pull --rebase` conflicts with
# remote changes to the same file - do_integrate aborts the rebase and exits
# before do_push/do_apply ever run, so nothing propagates. Because the next
# sync's capture step re-commits the same still-unapplied local state, this
# repeats on every future sync attempt until someone notices and fixes it
# manually.
do_doctor() {
  local remediate="${1:-false}"

  if [ -d "${REPO_DIR}/.git/rebase-merge" ] || [ -d "${REPO_DIR}/.git/rebase-apply" ]; then
    doctor_report "repo is mid-rebase (a previous sync's conflict was never resolved)."
    if [ "${remediate}" = "true" ]; then
      git -C "${REPO_DIR}" rebase --abort || true
      echo "  -> aborted the stuck rebase."
    fi
  fi

  if git -C "${REPO_DIR}" rev-parse --abbrev-ref --symbolic-full-name '@{u}' >/dev/null 2>&1; then
    git -C "${REPO_DIR}" fetch -q
    local ahead behind
    ahead="$(git -C "${REPO_DIR}" rev-list --count '@{u}..HEAD')"
    behind="$(git -C "${REPO_DIR}" rev-list --count 'HEAD..@{u}')"
    if [ "${ahead}" -gt 0 ] && [ "${behind}" -gt 0 ]; then
      doctor_report "local branch is ${ahead} ahead / ${behind} behind its upstream - a prior sync likely failed to push/apply."
      if [ "${remediate}" = "true" ]; then
        echo "  -> retrying a full sync now that a stuck rebase (if any) is cleared..."
        if do_capture_and_commit && do_integrate && do_push && do_apply; then
          echo "  -> sync succeeded."
        else
          echo "  -> still failing: a real conflict exists between local and remote settings, not just staleness." >&2
          echo "     Resolve manually in ${REPO_DIR} (git status), then re-run 'claude-sync.sh sync'." >&2
        fi
      fi
    fi
  fi

  local project_path memory_dir
  if project_path="$(require_project_path 2>/dev/null)"; then
    memory_dir="$(resolve_memory_dir "${CLAUDE_HOME}" "${project_path}")"
    if [ -f "${CLAUDE_HOME}/settings.json" ] && [ -f "${REPO_DIR}/dot-claude/settings.json" ] \
      && ! diff -q "${CLAUDE_HOME}/settings.json" "${REPO_DIR}/dot-claude/settings.json" >/dev/null 2>&1; then
      doctor_report "local ~/.claude/settings.json differs from the repo's dot-claude/settings.json."
      if [ "${remediate}" = "true" ]; then
        sync_apply "${REPO_DIR}" "${CLAUDE_HOME}" "${AGENTS_HOME}" "${memory_dir}"
        wire_skill_symlinks "${REPO_DIR}" "${CLAUDE_HOME}" "${AGENTS_HOME}"
        echo "  -> re-applied the repo's settings.json to local."
      fi
    fi
  fi

  if [ "${DOCTOR_ISSUES}" -eq 0 ]; then
    echo "No issues found - sync is healthy."
  fi
  return "${DOCTOR_ISSUES}"
}

usage() {
  echo "Usage: claude-sync.sh [push|pull|sync|bootstrap|doctor [remediate]]" >&2
  exit 1
}

main() {
  if [ ! -d "${REPO_DIR}/.git" ]; then
    echo "No git repo at ${REPO_DIR}. Run install.sh first." >&2
    exit 1
  fi

  local cmd="${1:-sync}"

  # push/sync/bootstrap capture-and-commit local config onto whatever
  # branch is currently checked out - almost always the default branch,
  # except when this same clone is being used to develop the sync tool
  # itself on a feature branch (e.g. via SessionStart/SessionEnd hooks
  # firing mid-session). Skip rather than pollute that branch.
  case "${cmd}" in
    push|sync|bootstrap)
      if ! on_default_branch; then
        echo "claude-config is on branch '$(git -C "${REPO_DIR}" rev-parse --abbrev-ref HEAD 2>/dev/null)', not its default branch '$(default_branch)' - skipping sync to avoid committing local config onto a feature branch. Switch back to the default branch to sync." >&2
        exit 0
      fi
      ;;
  esac

  case "${cmd}" in
    push)
      do_capture_and_commit
      do_integrate
      do_push
      ;;
    pull)
      do_integrate
      do_apply
      ;;
    sync)
      do_capture_and_commit
      do_integrate
      do_push
      do_apply
      ;;
    bootstrap)
      do_integrate
      do_apply_merge
      do_capture_and_commit
      do_push
      ;;
    doctor)
      if [ "${2:-}" = "remediate" ]; then
        do_doctor "true"
      else
        do_doctor "false"
      fi
      ;;
    *)
      usage
      ;;
  esac
}

main "$@"
