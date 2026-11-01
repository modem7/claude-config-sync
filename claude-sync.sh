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
# shellcheck source=lib/profile.sh
source "${SCRIPT_DIR}/lib/profile.sh"

REPO_DIR="${SCRIPT_DIR}"
CLAUDE_HOME="${CLAUDE_SYNC_CLAUDE_HOME:-${HOME}/.claude}"
AGENTS_HOME="${CLAUDE_SYNC_AGENTS_HOME:-${HOME}/.agents}"
HOSTNAME_VAL="${CLAUDE_SYNC_HOSTNAME:-$(hostname)}"

# This machine's profile name: PROFILE= in its conf, else the hostname.
# Read afresh each time, since a pull or 'profile use' can change it.
current_profile() {
  profile_resolve_name "${REPO_DIR}" "${HOSTNAME_VAL}"
}

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

# Local-only record of the repo commit the live config was last made to
# match (by an apply, or by a capture that committed it). Capture uses it to
# tell "live files are stale because the clone moved on" (e.g. a PR was
# merged and the clone fast-forwarded without an apply) from "live files
# hold genuine local edits". Like the override stamp it lives in CLAUDE_HOME
# but outside everything sync_capture copies, so it's never synced.
applied_commit_stamp() {
  echo "${CLAUDE_HOME}/.claude-sync-applied-commit"
}

record_applied_commit() {
  git -C "${REPO_DIR}" rev-parse HEAD > "$(applied_commit_stamp)"
}

# True when the live config is exactly what a clean apply of commit $1 would
# have left behind, i.e. nothing has been edited locally since that commit.
# The capture into the scratch tree is the same one a real capture does, so
# owned profile entries are judged against the stamp (an edit shows up as a
# write-back into machines/<profile>/) and the append block is stripped.
live_matches_commit() {
  local sha="$1" memory_dir="$2" tmp override_stamp rc=0
  tmp="$(mktemp -d)"
  mkdir -p "${tmp}/applied"
  git -C "${REPO_DIR}" archive "${sha}" | tar -x -C "${tmp}/applied"
  # git archive drops empty directories, which sync_capture always creates.
  mkdir -p "${tmp}/applied/dot-claude/agents" "${tmp}/applied/dot-claude/hooks" \
    "${tmp}/applied/dot-agents/skills" "${tmp}/applied/memory"
  cp -a "${tmp}/applied" "${tmp}/live"
  profile_capture "${tmp}/live" "${CLAUDE_HOME}" "${AGENTS_HOME}" "${memory_dir}" "$(current_profile)"
  override_stamp="$(applied_override_stamp)"
  if [ -f "${override_stamp}" ] && [ -f "${tmp}/applied/dot-claude/settings.json" ]; then
    overlay_merge "${tmp}/applied/dot-claude/settings.json" "${override_stamp}" > "${tmp}/merged" \
      && mv "${tmp}/merged" "${tmp}/applied/dot-claude/settings.json"
  fi
  diff -rq "${tmp}/applied" "${tmp}/live" >/dev/null 2>&1 || rc=1
  rm -rf "${tmp}"
  return "${rc}"
}

# Capture copies the live files over the repo, so it must never run when the
# live files are older than the clone's HEAD: it would commit the stale
# files over newer work (this reverted two merged PRs on 2026-10-18).
# Returns 0 to capture, 10 to skip capture (live is stale but unmodified, so
# integrate and apply will bring it up to date), 1 when live has local edits
# AND the clone moved, which needs a human.
check_capture_is_safe() {
  local memory_dir="$1" stamp sha
  stamp="$(applied_commit_stamp)"
  [ -f "${stamp}" ] || return 0
  sha="$(cat "${stamp}")"
  [ "${sha}" != "$(git -C "${REPO_DIR}" rev-parse HEAD)" ] || return 0
  if git -C "${REPO_DIR}" cat-file -e "${sha}^{commit}" 2>/dev/null \
     && live_matches_commit "${sha}" "${memory_dir}"; then
    echo "claude-sync: the clone moved on since the last apply and the live config is unmodified; skipping capture so stale files are not committed." >&2
    return 10
  fi
  echo "claude-sync: refusing to capture. The clone's HEAD moved since the last apply and the live config also has local edits, so capturing could overwrite newer repo changes." >&2
  echo "  Review the differences, then run 'claude-sync.sh pull' (discards local edits, applies the repo) or copy your edits somewhere safe first." >&2
  return 1
}

do_capture_and_commit() {
  local project_path memory_dir
  project_path="$(require_project_path)"
  memory_dir="$(resolve_memory_dir "${CLAUDE_HOME}" "${project_path}")"

  local safe=0
  check_capture_is_safe "${memory_dir}" || safe=$?
  if [ "${safe}" -eq 10 ]; then return 0; fi
  if [ "${safe}" -ne 0 ]; then return 1; fi

  # With a per-machine override, local settings.json = shared * override, so
  # it can't be copied into the shared file verbatim. Keep the shared file
  # as it was before capture, so the override's leaves can be split back out.
  local profile override stamp shared_before="" override_before=""
  profile="$(current_profile)"
  override="$(overlay_path "${REPO_DIR}" "${profile}")"
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

  # Profile-owned agents, hooks and skills stay out of the shared copy; a
  # local edit to one goes back into machines/<profile>/ instead.
  profile_capture "${REPO_DIR}" "${CLAUDE_HOME}" "${AGENTS_HOME}" "${memory_dir}" "${profile}" true

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
  # Live now equals HEAD by construction, so a failed push after this point
  # must not later look like a stale clone.
  record_applied_commit
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

# Every apply goes through here, so the profile is never left out: live
# reverting to the shared version while the stamp still holds the profile
# version would make the next capture write the shared version into the
# profile.
apply_local_config() {
  local memory_dir="$1"
  local mirror="${2:-true}"
  local profile
  profile="$(current_profile)"
  profile_unapply "${CLAUDE_HOME}" "${AGENTS_HOME}"
  sync_apply "${REPO_DIR}" "${CLAUDE_HOME}" "${AGENTS_HOME}" "${memory_dir}" "${mirror}"
  apply_settings_overlay
  profile_apply "${REPO_DIR}" "${CLAUDE_HOME}" "${AGENTS_HOME}" "${profile}"
  wire_skill_symlinks "${REPO_DIR}" "${CLAUDE_HOME}" "${AGENTS_HOME}" "$(profile_dir "${REPO_DIR}" "${profile}")"
}

do_apply() {
  local project_path memory_dir
  project_path="$(require_project_path)"
  memory_dir="$(resolve_memory_dir "${CLAUDE_HOME}" "${project_path}")"

  apply_local_config "${memory_dir}"
  record_applied_commit
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
  override="$(overlay_path "${REPO_DIR}" "$(current_profile)")"
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
  override="$(overlay_path "${REPO_DIR}" "$(current_profile)")"
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

  apply_local_config "${memory_dir}" "false"
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
        apply_local_config "${memory_dir}"
        record_applied_commit
        echo "  -> re-applied the repo's settings.json to local."
      fi
    fi
    doctor_profile "${remediate}" "${memory_dir}"
  fi

  # A hook command pointing at a script that isn't there (e.g. a hook a
  # profile disabled but settings.json still wires up) fails on every use.
  local missing
  while IFS= read -r missing; do
    doctor_report "settings.json runs ${missing}, which does not exist. Remove that hook from settings.json (or 'override add' it away on this machine), or restore the script."
  done < <(profile_dangling_hook_refs "${CLAUDE_HOME}" "${CLAUDE_HOME}/settings.json")

  if [ "${DOCTOR_ISSUES}" -eq 0 ]; then
    echo "No issues found - sync is healthy."
  fi
  return "${DOCTOR_ISSUES}"
}

# Profile checks, only when a profile or its stamp exists. Stale markers are
# reported, not fixed: that would mean editing the shared repo. A live
# mismatch is re-applied, unless that would discard a local edit to an
# owned entry that a sync would write back into the profile first.
doctor_profile() {
  local remediate="$1"
  local memory_dir="$2"
  local profile pdir kind name mismatches
  profile="$(current_profile)"
  pdir="$(profile_dir "${REPO_DIR}" "${profile}")"
  profile_in_use "${REPO_DIR}" "${CLAUDE_HOME}" "${profile}" || return 0

  for kind in "${PROFILE_KINDS[@]}"; do
    while IFS= read -r name; do
      [ -e "$(profile_shared_dir "${REPO_DIR}" "${kind}")/${name}" ] && continue
      doctor_report "machines/${profile}/${kind}/${name}.disabled hides nothing: there is no shared ${kind} entry called ${name}. Delete the marker."
    done < <(profile_disabled "${pdir}" "${kind}")
  done

  mismatches="$(profile_live_mismatches "${REPO_DIR}" "${CLAUDE_HOME}" "${AGENTS_HOME}" "${profile}")"
  [ -n "${mismatches}" ] || return 0
  doctor_report "live config differs from the shared config plus profile '${profile}' for: $(echo "${mismatches}" | paste -sd, - | sed 's/,/, /g')."
  [ "${remediate}" = "true" ] || return 0
  if profile_has_local_edits "${REPO_DIR}" "${CLAUDE_HOME}" "${AGENTS_HOME}" "${profile}"; then
    echo "  -> not re-applied: an entry of profile '${profile}' was edited locally. Run 'claude-sync.sh sync' to keep the edit in the profile."
    return 0
  fi
  apply_local_config "${memory_dir}"
  record_applied_commit
  echo "  -> re-applied the shared config and profile '${profile}' to local."
}

commit_if_changed() {
  git -C "${REPO_DIR}" add -A
  if ! git -C "${REPO_DIR}" diff --cached --quiet; then
    git -C "${REPO_DIR}" commit -q -m "$1"
  fi
}

# claude-sync.sh override add|remove|list [JQ_PATH]
# The override file follows the profile name, so a machine following
# another profile shares that profile's settings override.
do_override() {
  local action="${1:-list}"
  local expr="${2:-}"
  case "${action}" in
    list)
      overlay_list "${REPO_DIR}" "$(current_profile)"
      ;;
    add)
      [ -n "${expr}" ] || usage
      # Capture first so any other pending local edits are committed as
      # shared, then move this one key out of the shared file.
      do_capture_and_commit
      overlay_add "${REPO_DIR}" "$(current_profile)" "${CLAUDE_HOME}/settings.json" "${expr}"
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
      overlay_remove "${REPO_DIR}" "$(current_profile)" "${expr}"
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

die() {
  echo "$1" >&2
  exit 1
}

# claude-sync.sh profile list|show NAME|copy FROM [--force] [--yes]|use NAME
do_profile() {
  local action="${1:-list}"
  shift || true
  local profile conf
  profile="$(current_profile)"
  conf="$(machine_conf_path "${REPO_DIR}" "${HOSTNAME_VAL}")"
  case "${action}" in
    list)
      if read_machine_profile "${conf}" >/dev/null; then
        echo "${HOSTNAME_VAL} follows profile '${profile}' (PROFILE= in machines/${HOSTNAME_VAL}.conf):"
      else
        echo "${HOSTNAME_VAL} uses its own profile '${profile}':"
      fi
      profile_describe "${REPO_DIR}" "${profile}"
      ;;
    show)
      [ -n "${1:-}" ] || usage
      profile_valid_name "$1" || die "Not a valid profile name: $1"
      echo "Profile '$1':"
      profile_describe "${REPO_DIR}" "$1"
      ;;
    copy)
      do_profile_copy "$@"
      ;;
    use)
      [ -n "${1:-}" ] || usage
      profile_valid_name "$1" || die "Not a valid profile name: $1"
      # Going back to the machine's own profile needs no directory.
      if [ "$1" != "${HOSTNAME_VAL}" ] && [ ! -d "$(profile_dir "${REPO_DIR}" "$1")" ]; then
        die "No profile called '$1': machines/$1/ does not exist."
      fi
      # Capture under the old profile first, so pending edits land in it.
      do_capture_and_commit
      if [ "$1" = "${HOSTNAME_VAL}" ]; then
        write_machine_profile "${conf}" ""
      else
        write_machine_profile "${conf}" "$1"
      fi
      commit_if_changed "profile: ${HOSTNAME_VAL} uses profile $1"
      do_integrate
      do_push
      do_apply
      echo "${HOSTNAME_VAL} now follows profile '$1'; edits to its entries go to machines/$1/."
      ;;
    *)
      usage
      ;;
  esac
}

# Snapshots another profile (machines/<from>/ and its settings override)
# into this machine's own, machines/<hostname>/, and makes this machine
# follow it. Hook scripts run automatically, so they're shown first.
do_profile_copy() {
  local from="" force=false yes=false arg
  for arg in "$@"; do
    case "${arg}" in
      --force) force=true ;;
      --yes) yes=true ;;
      -*) usage ;;
      *) [ -z "${from}" ] || usage; from="${arg}" ;;
    esac
  done
  [ -n "${from}" ] || usage
  profile_valid_name "${from}" || die "Not a valid profile name: ${from}"
  local own="${HOSTNAME_VAL}" src dest conf hooks answer=""
  [ "${from}" != "${own}" ] || die "'${from}' is already this machine's own profile."
  src="$(profile_dir "${REPO_DIR}" "${from}")"
  dest="$(profile_dir "${REPO_DIR}" "${own}")"
  conf="$(machine_conf_path "${REPO_DIR}" "${own}")"
  if [ ! -d "${src}" ] && [ ! -f "$(overlay_path "${REPO_DIR}" "${from}")" ]; then
    die "No profile called '${from}': neither machines/${from}/ nor machines/${from}.settings.json exists."
  fi
  if { [ -e "${dest}" ] || [ -f "$(overlay_path "${REPO_DIR}" "${own}")" ]; } && [ "${force}" != true ]; then
    die "${own} already has its own profile (machines/${own}/ or machines/${own}.settings.json). Re-run with --force to replace it."
  fi

  hooks=""
  [ -d "${src}/hooks" ] && hooks="$(find "${src}/hooks" -type f -perm -u+x | LC_ALL=C sort)"
  if [ -n "${hooks}" ]; then
    echo "These executable hook scripts will be copied, and run by Claude Code on this machine:"
    echo "${hooks//${REPO_DIR}\//  }"
    if [ "${yes}" != true ]; then
      read -r -p "Copy them? [y/N] " answer || answer=""
      case "${answer}" in
        y|Y|yes|YES) ;;
        *) die "Aborted: nothing was copied." ;;
      esac
    fi
  fi

  do_capture_and_commit
  rm -rf "${dest}"
  [ ! -d "${src}" ] || cp -a "${src}" "${dest}"
  rm -f "$(overlay_path "${REPO_DIR}" "${own}")"
  if [ -f "$(overlay_path "${REPO_DIR}" "${from}")" ]; then
    cp "$(overlay_path "${REPO_DIR}" "${from}")" "$(overlay_path "${REPO_DIR}" "${own}")"
  fi
  # The copy only takes effect if this machine follows its own profile.
  write_machine_profile "${conf}" ""
  commit_if_changed "profile: ${own} copies profile ${from}"
  do_integrate
  do_push
  do_apply
  echo "${own} now has its own copy of profile '${from}' in machines/${own}/; edit it freely."
}

usage() {
  echo "Usage: claude-sync.sh [push|pull|sync|bootstrap|doctor [remediate]|override add|remove|list [JQ_PATH]" >&2
  echo "                       |profile list|show NAME|copy FROM [--force] [--yes]|use NAME]" >&2
  echo "  e.g. claude-sync.sh override add '.enabledPlugins[\"name@marketplace\"]'" >&2
  echo "       claude-sync.sh profile use other-host   # follow machines/other-host/ (agents, hooks, skills, CLAUDE.md)" >&2
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
    push|sync|bootstrap|override|profile)
      # profile list/show only read, so they work on any branch.
      if [ "${cmd}" = "profile" ] && { [ "${2:-list}" = "list" ] || [ "${2:-}" = "show" ]; }; then
        :
      elif ! on_default_branch; then
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
    profile)
      do_profile "${@:2}"
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
