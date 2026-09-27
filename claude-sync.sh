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
# shellcheck source=lib/settings-overlay.sh
source "${SCRIPT_DIR}/lib/settings-overlay.sh"

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

  # With a per-machine override, local settings.json = shared * override, so
  # it can't be copied into the shared file verbatim. Keep the shared file
  # as it was before capture, so the override's leaves can be split back out.
  local override stamp shared_before="" override_before=""
  override="$(overlay_path "${REPO_DIR}" "${HOSTNAME_VAL}")"
  stamp="$(applied_override_stamp)"
  if [ -f "${CLAUDE_HOME}/settings.json" ] && { [ -f "${override}" ] || [ -f "${stamp}" ]; }; then
    shared_before="$(mktemp)"
    override_before="$(mktemp)"
    if [ -f "${REPO_DIR}/dot-claude/settings.json" ]; then
      cp "${REPO_DIR}/dot-claude/settings.json" "${shared_before}"
    else
      echo '{}' > "${shared_before}"
    fi
    # What the last apply actually layered on. No record (never applied, or
    # applied by a claude-sync.sh from before overrides existed) means
    # nothing was: local lacking an override key then means "not applied
    # yet", not "deleted locally".
    if [ -f "${stamp}" ]; then cp "${stamp}" "${override_before}"; else echo '{}' > "${override_before}"; fi
  fi

  sync_capture "${REPO_DIR}" "${CLAUDE_HOME}" "${AGENTS_HOME}" "${memory_dir}"

  if [ -n "${shared_before}" ]; then
    overlay_split "${CLAUDE_HOME}/settings.json" "${shared_before}" "${override_before}" \
      "${override}" "${REPO_DIR}/dot-claude/settings.json"
    rm -f "${shared_before}" "${override_before}"
  fi

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
  # Fetch separately from the rebase so an unreachable remote (network, SSH
  # port blocked, auth) isn't misreported as a merge conflict.
  if ! git -C "${REPO_DIR}" fetch; then
    echo "git fetch failed - could not reach the remote (network, SSH or auth problem). Nothing was changed locally; fix connectivity and re-run." >&2
    return 1
  fi
  if ! git -C "${REPO_DIR}" rebase; then
    if [ -d "${REPO_DIR}/.git/rebase-merge" ] || [ -d "${REPO_DIR}/.git/rebase-apply" ]; then
      git -C "${REPO_DIR}" rebase --abort || true
    fi
    echo "git rebase onto upstream failed (conflict). Resolve manually in ${REPO_DIR} and re-run." >&2
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
  apply_settings_overlay
  wire_skill_symlinks "${REPO_DIR}" "${CLAUDE_HOME}" "${AGENTS_HOME}"
}

# Local-only record of the override the last apply layered on, so capture
# can tell a local edit from "not applied yet". Lives in CLAUDE_HOME but
# outside everything sync_capture copies, so it's never synced.
applied_override_stamp() {
  echo "${CLAUDE_HOME}/.claude-sync-applied-override.json"
}

# Layers this machine's override (if any) over the shared settings.json that
# sync_apply just copied into place, and records what was layered.
apply_settings_overlay() {
  local override merged stamp
  override="$(overlay_path "${REPO_DIR}" "${HOSTNAME_VAL}")"
  stamp="$(applied_override_stamp)"
  if [ ! -f "${override}" ] || [ ! -f "${REPO_DIR}/dot-claude/settings.json" ]; then
    rm -f "${stamp}"
    return 0
  fi
  merged="$(mktemp)"
  overlay_merge "${REPO_DIR}/dot-claude/settings.json" "${override}" > "${merged}"
  mv "${merged}" "${CLAUDE_HOME}/settings.json"
  cp "${override}" "${stamp}"
}

# What this machine's settings.json should be after a clean apply.
expected_local_settings() {
  local override
  override="$(overlay_path "${REPO_DIR}" "${HOSTNAME_VAL}")"
  if [ -f "${override}" ]; then
    overlay_merge "${REPO_DIR}/dot-claude/settings.json" "${override}"
  else
    cat "${REPO_DIR}/dot-claude/settings.json"
  fi
}

# Non-destructive counterpart to do_apply, used only for a machine's
# first-ever sync: merges the repo's shared config into local paths without
# deleting anything already local-only, instead of the normal full mirror.
do_apply_merge() {
  local project_path memory_dir
  project_path="$(require_project_path)"
  memory_dir="$(resolve_memory_dir "${CLAUDE_HOME}" "${project_path}")"

  sync_apply "${REPO_DIR}" "${CLAUDE_HOME}" "${AGENTS_HOME}" "${memory_dir}" "false"
  apply_settings_overlay
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
      && ! diff -q "${CLAUDE_HOME}/settings.json" <(expected_local_settings) >/dev/null 2>&1; then
      doctor_report "local ~/.claude/settings.json differs from the repo's dot-claude/settings.json (plus this machine's overrides)."
      if [ "${remediate}" = "true" ]; then
        sync_apply "${REPO_DIR}" "${CLAUDE_HOME}" "${AGENTS_HOME}" "${memory_dir}"
        apply_settings_overlay
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

commit_if_changed() {
  git -C "${REPO_DIR}" add -A
  if ! git -C "${REPO_DIR}" diff --cached --quiet; then
    git -C "${REPO_DIR}" commit -q -m "$1"
  fi
}

# claude-sync.sh override add|remove|list [JQ_PATH]
do_override() {
  local action="${1:-list}"
  local expr="${2:-}"
  case "${action}" in
    list)
      overlay_list "${REPO_DIR}" "${HOSTNAME_VAL}"
      ;;
    add)
      [ -n "${expr}" ] || usage
      # Capture first so any other pending local edits are committed as
      # shared, then move this one key out of the shared file.
      do_capture_and_commit
      overlay_add "${REPO_DIR}" "${HOSTNAME_VAL}" "${CLAUDE_HOME}/settings.json" "${expr}"
      commit_if_changed "override: ${HOSTNAME_VAL} keeps ${expr} to itself"
      do_integrate
      do_push
      do_apply
      echo "${expr} is now specific to ${HOSTNAME_VAL}; other machines drop it on their next sync."
      ;;
    remove)
      [ -n "${expr}" ] || usage
      # Capture while the override still owns the key, so its local value
      # isn't mistaken for a new shared value, then drop it and re-apply.
      do_capture_and_commit
      overlay_remove "${REPO_DIR}" "${HOSTNAME_VAL}" "${expr}"
      commit_if_changed "override: ${HOSTNAME_VAL} stops overriding ${expr}"
      do_integrate
      do_push
      do_apply
      echo "${expr} on ${HOSTNAME_VAL} now follows the shared settings."
      ;;
    *)
      usage
      ;;
  esac
}

usage() {
  echo "Usage: claude-sync.sh [push|pull|sync|bootstrap|doctor [remediate]|override add|remove|list [JQ_PATH]]" >&2
  echo "  e.g. claude-sync.sh override add '.enabledPlugins[\"name@marketplace\"]'" >&2
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
    push|sync|bootstrap|override)
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
    override)
      do_override "${2:-list}" "${3:-}"
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
