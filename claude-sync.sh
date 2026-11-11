#!/usr/bin/env bash
set -euo pipefail
set +x

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
GH_BIN="${CLAUDE_SYNC_GH:-gh}"
FLOCK_BIN="${CLAUDE_SYNC_FLOCK:-flock}"

# How changes leave this machine
# ------------------------------
# Nothing is ever committed or pushed to the default branch. Each machine
# has one branch, sync/<hostname>, holding exactly one commit on top of the
# default branch: everything this machine has captured that is not merged
# yet. Each sync rebuilds that commit, force-pushes the branch (with a
# lease) and makes sure a PR is open for it. Merging the PR shares it.
#
# The clone stays on the default branch, but its index and working tree
# hold the effective state: the sync branch's tree when one exists, else
# the default branch's. So apply, doctor, profiles, overrides and hand
# edits in the clone all see this machine's pending changes. A command
# that rewrites the working tree first commits whatever is in it to the
# sync branch, so nothing in the clone can be lost.

# fd 9 holds the lock; closing it here stops a detached child (git's
# background gc) from keeping the lock after this run exits.
g() { git -C "${REPO_DIR}" "$@" 9>&-; }

FAIL_REPORTED=0
LOCK_LINK=""
LOCK_FD_OPEN=0
SYNC_START=""
DEF=""

# Prints each line to stderr (the first prefixed) and exits 1.
fail() {
  FAIL_REPORTED=1
  echo "claude-sync: $1" >&2
  shift
  local line
  for line in "$@"; do echo "${line}" >&2; done
  exit 1
}

on_exit() {
  local rc=$?
  release_lock
  if [ "${rc}" -ne 0 ] && [ "${FAIL_REPORTED}" -eq 0 ]; then
    echo "claude-sync: stopped with an error (exit ${rc}). See the messages above." >&2
  fi
}

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
  ref="$(g symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null)" || true
  echo "${ref#origin/}"
}

on_default_branch() {
  local def current
  def="$(default_branch)"
  # Nothing to compare against - don't block a legitimate first-ever sync.
  [ -z "${def}" ] && return 0
  current="$(g rev-parse --abbrev-ref HEAD 2>/dev/null)" || true
  [ "${current}" = "${def}" ]
}

sync_branch() {
  echo "sync/${HOSTNAME_VAL}"
}

has_sync_branch() {
  g show-ref -q --verify "refs/heads/$(sync_branch)"
}

# The commit the clone's working tree stands for: the sync branch tip when
# this machine has pending changes, else the default branch.
effective_commit() {
  if has_sync_branch; then
    g rev-parse "refs/heads/$(sync_branch)"
  else
    g rev-parse HEAD
  fi
}

# Absolute path inside .git (the script's cwd is not REPO_DIR).
git_path() {
  echo "$(g rev-parse --absolute-git-dir)/$1"
}

tree_of() {
  g rev-parse "$1^{tree}"
}

has_origin() {
  g remote get-url origin >/dev/null 2>&1
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

# The ref keeps the stamped commit from being garbage-collected once the
# sync branch is rebuilt or deleted.
record_applied_commit() {
  local eff
  eff="$(effective_commit)"
  g update-ref -m "claude-sync: applied" refs/claude-sync/applied "${eff}" \
    || fail "could not record the applied commit in ${REPO_DIR} (git update-ref failed)."
  echo "${eff}" > "$(applied_commit_stamp)"
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
  g archive "${sha}" | tar -x -C "${tmp}/applied"
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

# Capture copies the live files over the clone, so it must never run when
# the live files are older than the clone's effective state: it would
# commit the stale files over newer work (this reverted two merged PRs on
# 2026-10-18). The effective state is the sync branch while this machine
# has a PR pending, so its own unmerged PR never counts as "moved"; only a
# default branch that moved without an apply (e.g. a manual git pull) does.
# Trees are compared, not commit ids, because each sync rebuilds the sync
# branch's commit. Returns 0 to capture, 10 to skip capture (live is stale
# but unmodified, so integrate and apply will bring it up to date), 1 when
# live has local edits AND the clone moved, which needs a human.
check_capture_is_safe() {
  local memory_dir="$1" stamp sha eff
  stamp="$(applied_commit_stamp)"
  [ -f "${stamp}" ] || return 0
  sha="$(cat "${stamp}")"
  eff="$(effective_commit)"
  [ "${sha}" != "${eff}" ] || return 0
  FAIL_REPORTED=1
  if ! g cat-file -e "${sha}^{commit}" 2>/dev/null; then
    echo "claude-sync: refusing to capture. The last applied commit ${sha} (recorded in ${stamp}) is no longer in ${REPO_DIR}." >&2
    echo "  Usually the clone was re-cloned or garbage-collected; claude-sync now pins it, so this should not recur." >&2
    echo "  Without it, local edits can't be told apart from stale files, so nothing was captured or changed." >&2
    echo "  Either run 'claude-sync.sh pull' (discards local edits, applies the repo)," >&2
    echo "  or copy your edits somewhere safe, delete ${stamp}, and re-run 'claude-sync.sh sync' (captures the live config as it is)." >&2
    return 1
  fi
  FAIL_REPORTED=0
  [ "$(tree_of "${sha}")" != "$(tree_of "${eff}")" ] || return 0
  if live_matches_commit "${sha}" "${memory_dir}"; then
    echo "claude-sync: the clone moved on since the last apply and the live config is unmodified; skipping capture so stale files are not committed." >&2
    return 10
  fi
  FAIL_REPORTED=1
  echo "claude-sync: refusing to capture. The clone's HEAD moved since the last apply and the live config also has local edits, so capturing could overwrite newer repo changes." >&2
  echo "  Review the differences, then run 'claude-sync.sh pull' (discards local edits, applies the repo) or copy your edits somewhere safe first." >&2
  return 1
}

# Takes the lock that stops two runs (e.g. one session's SessionEnd and
# another's SessionStart) from rewriting the clone at the same time.
# flock is used when installed: the kernel drops the lock when the process
# dies, so a crash or kill can never leave a stale lock behind.
acquire_lock() {
  local gitdir wait="${CLAUDE_SYNC_LOCK_WAIT:-10}"
  [ "${LOCK_FD_OPEN}" -eq 0 ] && [ -z "${LOCK_LINK}" ] || return 0
  gitdir="$(g rev-parse --absolute-git-dir)"
  if command -v "${FLOCK_BIN}" >/dev/null 2>&1; then
    exec 9> "${gitdir}/claude-sync.flock" \
      || fail "could not open the lock file ${gitdir}/claude-sync.flock."
    if ! "${FLOCK_BIN}" -w "${wait}" 9; then
      exec 9>&-
      fail "another claude-sync run is still working on ${REPO_DIR} (waited ${wait}s for its lock)." \
        "  Wait for it to finish and re-run. The lock frees itself when that run exits."
    fi
    LOCK_FD_OPEN=1
    return 0
  fi
  echo "claude-sync: '${FLOCK_BIN}' not found (it is part of util-linux), so a weaker lock is used." \
    "Install flock so that two runs at once are always kept apart." >&2
  acquire_link_lock "${gitdir}/claude-sync.lockpid" "${wait}"
}

# Fallback lock: a symlink whose target is the owner's pid. ln -s creates
# it with the pid in one atomic step, so a crash can't leave it ownerless.
acquire_link_lock() {
  local link="$1" wait="$2" pid moved waited=0
  until ln -s "$$" "${link}" 2>/dev/null; do
    pid="$(readlink "${link}" 2>/dev/null || true)"
    if [ -n "${pid}" ] && ! kill -0 "${pid}" 2>/dev/null; then
      # mv is atomic, so only one of several waiting runs clears it.
      if mv "${link}" "${link}.stale.$$" 2>/dev/null; then
        moved="$(readlink "${link}.stale.$$" 2>/dev/null || true)"
        rm -f "${link}.stale.$$"
        if [ "${moved}" = "${pid}" ]; then
          echo "claude-sync: removed a stale lock left by process ${pid}, which is not running." >&2
        else
          # Lost a race and moved a new, live lock: put it back.
          ln -s "${moved}" "${link}" 2>/dev/null || true
        fi
      fi
      continue
    fi
    if [ "${waited}" -ge "${wait}" ]; then
      fail "another claude-sync run (pid ${pid:-unknown}) is still working on ${REPO_DIR}." \
        "  Wait for it to finish and re-run. If no claude-sync is running, remove ${link}."
    fi
    sleep 1
    waited=$((waited + 1))
  done
  LOCK_LINK="${link}"
}

release_lock() {
  if [ "${LOCK_FD_OPEN}" -eq 1 ]; then
    exec 9>&-
    LOCK_FD_OPEN=0
  fi
  if [ -n "${LOCK_LINK}" ]; then
    if [ "$(readlink "${LOCK_LINK}" 2>/dev/null || true)" = "$$" ]; then
      rm -f "${LOCK_LINK}" || echo "claude-sync: could not remove the lock ${LOCK_LINK}; remove it by hand." >&2
    fi
    LOCK_LINK=""
  fi
}

# Makes the working tree match the effective commit, and the index match
# HEAD, so nothing pending is staged (a commit made by hand in this clone
# can't sweep it in). Only call it once everything in the working tree is
# in a commit (commit_worktree), as it overwrites the working tree. Signals
# are held off so a timeout can't leave the working tree half old, half new.
materialise() {
  local eff rc=0
  eff="$(effective_commit)"
  trap '' INT TERM HUP
  g read-tree -u --reset "${eff}" || rc=$?
  [ "${rc}" -ne 0 ] || g reset -q || rc=$?
  trap 'exit 130' INT
  trap 'exit 143' TERM
  trap 'exit 129' HUP
  if [ "${rc}" -ne 0 ]; then
    fail "could not update the working tree in ${REPO_DIR} to ${eff}." \
      "  Nothing was lost: every change is committed on $(sync_branch) or ${DEF}. Check 'git -C ${REPO_DIR} status' and re-run."
  fi
}

# Points the sync branch at $1, expecting it to be at $2 (empty: absent).
set_sync_branch() {
  local new="$1" old="$2" ref
  ref="refs/heads/$(sync_branch)"
  if [ -z "${old}" ]; then
    printf 'create %s %s\n' "${ref}" "${new}" | g update-ref --stdin -m "claude-sync" \
      || fail "could not create ${ref}. Another process may have changed it; re-run."
  else
    printf 'update %s %s %s\n' "${ref}" "${new}" "${old}" | g update-ref --stdin -m "claude-sync" \
      || fail "could not update ${ref}. Another process may have changed it; re-run."
  fi
}

delete_sync_branch() {
  local sb
  sb="$(sync_branch)"
  has_sync_branch || return 0
  g branch -q -D "${sb}" || fail "could not delete the local branch ${sb}."
}

# Commits everything in the working tree onto the sync branch (creating it
# off the default branch if needed), when it differs from the effective
# state. Never commits on the default branch.
commit_worktree() {
  local msg="$1" tree eff new old=""
  if ! assert_no_denylisted_files "${REPO_DIR}"; then
    fail "refusing to commit: denylisted file present in repo tree." \
      "  Remove it from ${REPO_DIR} (it must never be synced), then re-run."
  fi
  g add -A || fail "git add failed in ${REPO_DIR}."
  tree="$(g write-tree)" || fail "git write-tree failed in ${REPO_DIR}."
  eff="$(effective_commit)"
  [ "${tree}" != "$(tree_of "${eff}")" ] || return 0
  new="$(g commit-tree "${tree}" -p "${eff}" -m "${msg}")" \
    || fail "git commit-tree failed in ${REPO_DIR} (is git's user.name/user.email set?)."
  has_sync_branch && old="${eff}"
  set_sync_branch "${new}" "${old}"
}

# True when merging $2 into $1 would change nothing, i.e. everything $2
# changed is already in $1.
changes_contained() {
  local mb
  mb="$(g merge-base "$1" "$2")" || return 1
  merge_trees --merge-base="${mb}" "$1" "$2" || return 1
  [ "${MT_OUT%%$'\n'*}" = "$(tree_of "$1")" ]
}

# Runs git merge-tree --write-tree --name-only with the given arguments,
# leaving its output (the tree, then any conflicts) in MT_OUT. Returns 0
# when clean and 1 on a conflict. Any other exit is git failing, which is
# reported as that rather than as a conflict. Not for use inside $(...):
# fail has to exit the script itself.
MT_OUT=""
merge_trees() {
  local rc=0
  MT_OUT="$(g merge-tree --write-tree --name-only "$@")" || rc=$?
  if [ "${rc}" -gt 1 ]; then
    fail "git merge-tree failed (exit ${rc}, see the git error above). This is a git error, not a merge conflict." \
      "  Nothing was pushed. Check 'git -C ${REPO_DIR} status' and re-run."
  fi
  return "${rc}"
}

# merge-tree --write-tree arrived in git 2.38 and --merge-base in 2.40.
require_git_version() {
  local v major="" minor=""
  v="$(git --version 2>/dev/null || true)"
  if [[ "${v}" =~ ([0-9]+)\.([0-9]+) ]]; then
    major="${BASH_REMATCH[1]}"
    minor="${BASH_REMATCH[2]}"
  fi
  if [ -z "${major}" ] || [ "${major}" -lt 2 ] || { [ "${major}" -eq 2 ] && [ "${minor}" -lt 40 ]; }; then
    fail "git 2.40 or newer is needed (found: ${v:-no git}). claude-sync relies on 'git merge-tree --write-tree --merge-base'." \
      "  Upgrade git and re-run. Nothing was changed."
  fi
}

rebuild_conflict() {
  local out="$1" sb
  sb="$(sync_branch)"
  fail "this machine's pending changes on ${sb} conflict with changes merged on ${DEF} (a real conflict, not staleness)." \
    "  Nothing was applied and ${DEF} was not moved; the live config is untouched." \
    "  Conflicts:" \
    "$(printf '%s\n' "${out}" | tail -n +2 | sed '/^$/d; s/^/    /')" \
    "  Resolve manually: in ${REPO_DIR}, merge origin/${DEF} into ${sb}, fix the conflict and commit, then re-run 'claude-sync.sh sync'." \
    "  Or drop this machine's pending changes: git -C ${REPO_DIR} branch -D ${sb} && git -C ${REPO_DIR} reset -q --hard && claude-sync.sh pull" \
    "  If ${sb}'s PR was already merged or closed, a stale copy was picked up from origin: drop it as above."
}

# Rebuilds the sync branch as one commit on top of $1. Two steps, so a
# pending change that was already merged (even squash-merged) drops out
# cleanly before this run's new commits go on top: first the pending state
# at the start of the run (SYNC_START), then everything committed since.
# Deletes the branch when nothing is left that $1 doesn't already have.
rebuild_sync_branch() {
  local target="$1" sb tip mb out tree1 tree2 s1 new msg body
  sb="$(sync_branch)"
  has_sync_branch || return 0
  tip="$(g rev-parse "refs/heads/${sb}")"
  target="$(g rev-parse "${target}^{commit}")"
  mb="$(g merge-base "${SYNC_START}" "${target}")" \
    || fail "${sb} and ${DEF} share no history, so the sync branch can't be rebuilt." \
      "  Inspect ${REPO_DIR} by hand; to start over: git -C ${REPO_DIR} branch -D ${sb}"
  merge_trees --merge-base="${mb}" "${target}" "${SYNC_START}" || rebuild_conflict "${MT_OUT}"
  tree1="${MT_OUT%%$'\n'*}"
  tree2="${tree1}"
  if [ "${tip}" != "${SYNC_START}" ]; then
    s1="$(g commit-tree "${tree1}" -p "${target}" -m "claude-sync: intermediate")"
    merge_trees --merge-base="${SYNC_START}" "${s1}" "${tip}" || rebuild_conflict "${MT_OUT}"
    tree2="${MT_OUT%%$'\n'*}"
  fi

  if [ "${tree2}" = "$(tree_of "${target}")" ]; then
    # Everything pending is merged: back to plain default branch.
    delete_sync_branch
  elif [ "${tree2}" = "$(tree_of "${tip}")" ] \
       && [ "$(g rev-list --count "${target}..${tip}")" = "1" ] \
       && [ "$(g rev-parse "${tip}^")" = "${target}" ]; then
    : # already one commit with this content on this target
  else
    body="$(g log --format=%B "${target}..${tip}" | grep -v '^sync: ' | grep -v '^claude-sync: ' \
      | sed '/^$/d; /^- /!s/^/- /' | sort -u || true)"
    msg="sync: ${HOSTNAME_VAL} $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    [ -z "${body}" ] || msg="${msg}"$'\n\n'"${body}"
    new="$(g commit-tree "${tree2}" -p "${target}" -m "${msg}")" \
      || fail "git commit-tree failed while rebuilding ${sb}."
    set_sync_branch "${new}" "${tip}"
  fi
  SYNC_START="$(effective_commit)"
}

# A clone whose default branch carries commits that are not on origin was
# left by a claude-sync.sh from before sync branches existed. Those commits
# move onto the sync branch (and out as a PR); the default branch goes back
# to where it left origin. The working tree is untouched: it already shows
# those commits, which is now the sync branch's state.
migrate_legacy_default() {
  local ahead head mb sb
  sb="$(sync_branch)"
  ahead="$(g rev-list --count "refs/remotes/origin/${DEF}..HEAD")"
  [ "${ahead}" -gt 0 ] || return 0
  if has_sync_branch; then
    fail "local ${DEF} has ${ahead} commit(s) that are not on origin, and ${sb} also exists." \
      "  claude-sync never commits on ${DEF}, so these came from elsewhere. See them with: git -C ${REPO_DIR} log origin/${DEF}..${DEF}" \
      "  Move them onto ${sb} or drop them (git -C ${REPO_DIR} branch -f ${DEF} origin/${DEF} while on another branch), then re-run."
  fi
  head="$(g rev-parse HEAD)"
  mb="$(g merge-base HEAD "refs/remotes/origin/${DEF}")" \
    || fail "local ${DEF} shares no history with origin/${DEF}. Fix ${REPO_DIR} by hand."
  set_sync_branch "${head}" ""
  g update-ref -m "claude-sync: move local commits to ${sb}" "refs/heads/${DEF}" "${mb}" "${head}" \
    || fail "could not move ${DEF} back to origin/${DEF} after copying its commits to ${sb}."
  echo "claude-sync: moved ${ahead} unpushed commit(s) from ${DEF} onto ${sb}; they will go out as a PR."
}

# A fresh clone (e.g. after install.sh --repair) has no local sync branch,
# but origin may still hold this machine's open one. Pick it up so its
# pending changes are neither dropped from the live config nor overwritten
# by the next push. Edits already in the working tree are merged on top.
# Not when it is the tip this machine last pushed: the local branch was
# then deleted because the PR was merged (or dropped by hand), and GitHub
# just kept the branch. Adopting it would bring merged changes back and
# conflict with anything the default branch changed since.
adopt_remote_sync_branch() {
  local sb remote last tree head wt merged new
  sb="$(sync_branch)"
  has_sync_branch && return 0
  remote="$(g rev-parse -q --verify "refs/remotes/origin/${sb}")" || return 0
  last="$(g config --get claude-sync.lastPushed || true)"
  [ "${remote}" != "${last}" ] || return 0
  changes_contained "refs/remotes/origin/${DEF}" "${remote}" && return 0
  # No record (a fresh clone): skip it only on proof that its PR at this
  # very tip was merged or closed. A branch pushed without a PR (gh was
  # missing) still holds pending changes, so it is picked up.
  case "$(gh_sync_pr_state "${sb}" "${remote}")" in
    done)
      g config claude-sync.lastPushed "${remote}"
      echo "claude-sync: origin's ${sb} belongs to a PR that was merged or closed, so it was not picked up; the next push replaces it." >&2
      return 0
      ;;
    unknown)
      echo "claude-sync: gh could not say whether ${sb} still has an open PR, so origin's ${sb} is picked up as if it had." >&2
      ;;
  esac

  g add -A || fail "git add failed in ${REPO_DIR}."
  tree="$(g write-tree)" || fail "git write-tree failed in ${REPO_DIR}."
  head="$(g rev-parse HEAD)"
  merged="$(tree_of "${remote}")"
  if [ "${tree}" != "$(tree_of "${head}")" ]; then
    wt="$(g commit-tree "${tree}" -p "${head}" -m "claude-sync: clone edits")"
    merge_trees --merge-base="${head}" "${remote}" "${wt}" \
      || fail "origin has this machine's open ${sb}, but it conflicts with uncommitted changes in ${REPO_DIR}." \
        "$(printf '%s\n' "${MT_OUT}" | tail -n +2 | sed '/^$/d; s/^/    /')" \
        "  Move those changes aside (git -C ${REPO_DIR} stash -u), re-run, then reapply them."
    merged="${MT_OUT%%$'\n'*}"
  fi
  set_sync_branch "${remote}" ""
  SYNC_START="${remote}"
  g config claude-sync.lastPushed "${remote}"
  if [ "${merged}" != "$(tree_of "${remote}")" ]; then
    new="$(g commit-tree "${merged}" -p "${remote}" -m "sync: ${HOSTNAME_VAL} clone edits")"
    set_sync_branch "${new}" "${remote}"
  fi
  materialise
  echo "claude-sync: picked up this machine's open ${sb} from origin."
}

fetch_failed() {
  fail "git fetch failed - could not reach the remote (network, SSH or auth problem). Nothing was changed locally; fix connectivity and re-run."
}

# Common start of every command that changes the repo: lock, fetch, and
# get the sync branch into a known state.
begin_sync() {
  local sb
  sb="$(sync_branch)"
  has_origin || fail "the clone at ${REPO_DIR} has no 'origin' remote, so there is nowhere to push." \
    "  Add it (git -C ${REPO_DIR} remote add origin <url>) and re-run."
  require_git_version
  acquire_lock
  if [ -d "$(git_path rebase-merge)" ] || [ -d "$(git_path rebase-apply)" ] \
     || [ -f "$(git_path MERGE_HEAD)" ]; then
    fail "${REPO_DIR} is in the middle of a rebase or merge." \
      "  Run 'claude-sync.sh doctor remediate', or finish/abort it by hand (git -C ${REPO_DIR} status), then re-run."
  fi
  # Fetch separately so an unreachable remote (network, SSH port blocked,
  # auth) isn't misreported as a merge conflict.
  g fetch -q --prune origin || fetch_failed
  DEF="$(default_branch)"
  if [ -z "${DEF}" ]; then
    g remote set-head origin --auto >/dev/null || true
    DEF="$(default_branch)"
  fi
  [ -n "${DEF}" ] || fail "can't tell origin's default branch (is the remote repository empty?)." \
    "  claude-sync only pushes sync/<hostname> branches and opens PRs against the default branch," \
    "  so the default branch must exist first: push an initial commit to it by hand, then re-run."
  g show-ref -q --verify "refs/remotes/origin/${DEF}" \
    || fail "origin/${DEF} does not exist after fetching. Check the remote, then re-run."
  if ! g check-ref-format --branch "${sb}" >/dev/null || [ "${sb}" = "${DEF}" ]; then
    fail "'${sb}' can't be used as this machine's sync branch name." \
      "  Set CLAUDE_SYNC_HOSTNAME to a simple name (letters, digits, '-', '.') and re-run."
  fi
  migrate_legacy_default
  adopt_remote_sync_branch
  [ -n "${SYNC_START}" ] || SYNC_START="$(effective_commit)"
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

  commit_worktree "sync: ${HOSTNAME_VAL} $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  # Live now equals the effective state by construction, so a failed push
  # after this point must not later look like a stale clone.
  record_applied_commit
}

# Brings this machine onto origin's default branch: rebuilds the sync
# branch on top of it, fast-forwards the local default branch, and updates
# the working tree. Callers apply straight after, so the live config moves
# with the clone.
do_integrate() {
  local target head
  commit_worktree "sync: ${HOSTNAME_VAL} clone edits $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  target="$(g rev-parse "refs/remotes/origin/${DEF}")"
  rebuild_sync_branch "${target}"
  head="$(g rev-parse HEAD)"
  if [ "${head}" != "${target}" ]; then
    g merge-base --is-ancestor "${head}" "${target}" \
      || fail "local ${DEF} has commits that are not on origin/${DEF}; it can only fast-forward." \
        "  See them with: git -C ${REPO_DIR} log origin/${DEF}..${DEF}"
    g update-ref -m "claude-sync: fast-forward" "refs/heads/${DEF}" "${target}" "${head}" \
      || fail "could not fast-forward ${DEF} to origin/${DEF}."
  fi
  materialise
}

# Squashes the sync branch onto the local default branch without moving
# anything else ('push' doesn't integrate).
do_squash() {
  commit_worktree "sync: ${HOSTNAME_VAL} clone edits $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  rebuild_sync_branch "$(g rev-parse HEAD)"
  materialise
}

# owner/repo for gh, from CLAUDE_SYNC_GH_REPO or the origin URL
# (git@host:owner/repo.git, ssh://git@host/owner/repo.git, https://host/owner/repo).
gh_repo_slug() {
  if [ -n "${CLAUDE_SYNC_GH_REPO:-}" ]; then
    echo "${CLAUDE_SYNC_GH_REPO}"
    return 0
  fi
  local url host path
  url="$(g remote get-url origin)" || return 1
  case "${url}" in
    *://*)
      path="${url#*://}"
      host="${path%%/*}"
      path="${path#*/}"
      host="${host#*@}"
      host="${host%%:*}"
      ;;
    *@*:*)
      host="${url#*@}"
      host="${host%%:*}"
      path="${url#*:}"
      ;;
    *) return 1 ;;
  esac
  path="${path%/}"
  path="${path%.git}"
  [[ "${path}" =~ ^[^/]+/[^/]+$ ]] || return 1
  if [ "${host}" = "github.com" ]; then echo "${path}"; else echo "${host}/${path}"; fi
}

# What GitHub knows about head branch $1 at tip $2: "open" (a PR is open),
# "done" (no open PR, and a merged or closed one has exactly that tip),
# "nopr" (neither: e.g. pushed while gh was missing), or "unknown" (gh
# missing, not logged in, or failing).
gh_sync_pr_state() {
  local repo state
  if ! repo="$(gh_repo_slug)" || ! command -v "${GH_BIN}" >/dev/null 2>&1 \
     || ! GH_PROMPT_DISABLED=1 "${GH_BIN}" auth status >/dev/null 2>&1 9>&- \
     || ! state="$(GH_PROMPT_DISABLED=1 "${GH_BIN}" pr list --repo "${repo}" --head "$1" --state all \
          --json state,headRefOid \
          --jq "if any(.[]; .state == \"OPEN\") then \"open\" elif any(.[]; .headRefOid == \"$2\") then \"done\" else \"nopr\" end" \
          2>/dev/null 9>&-)"; then
    state=unknown
  fi
  case "${state}" in
    open|done|nopr) echo "${state}" ;;
    *) echo unknown ;;
  esac
}

# Fails loudly about the PR step. The branch is already pushed by now.
pr_fail() {
  local sb tip
  sb="$(sync_branch)"
  tip="$(g rev-parse "refs/heads/${sb}")"
  fail "$@" \
    "  This repeats on every sync until a PR is open. If you open it by hand, silence it with:" \
    "  git -C ${REPO_DIR} config claude-sync.prVerified ${tip}"
}

# Opens a PR for the sync branch unless one is open. Never enables
# auto-merge: a human merges.
ensure_pr() {
  local sb tip repo manual out url body
  sb="$(sync_branch)"
  tip="$(g rev-parse "refs/heads/${sb}")"
  [ "${tip}" != "$(g config --get claude-sync.prVerified || true)" ] || return 0

  manual="gh pr create --repo <owner/repo> --base ${DEF} --head ${sb} --title 'sync: ${HOSTNAME_VAL}' --body 'Config sync from ${HOSTNAME_VAL}.'"
  if ! repo="$(gh_repo_slug)"; then
    pr_fail "pushed ${sb}, but could not work out owner/repo from origin's URL, so no PR was opened." \
      "  Set CLAUDE_SYNC_GH_REPO=owner/repo, or open it by hand:" "  ${manual}"
  fi
  manual="${manual/<owner\/repo>/${repo}}"
  if ! command -v "${GH_BIN}" >/dev/null 2>&1; then
    pr_fail "pushed ${sb}, but the GitHub CLI (gh) is not installed, so no PR was opened." \
      "  Install gh and run 'gh auth login', then re-run 'claude-sync.sh sync', or open it by hand:" "  ${manual}"
  fi
  if ! out="$(GH_PROMPT_DISABLED=1 "${GH_BIN}" auth status 2>&1 9>&-)"; then
    pr_fail "pushed ${sb}, but gh is not logged in, so no PR was opened." \
      "$(printf '%s\n' "${out}" | sed 's/^/    /')" \
      "  Run 'gh auth login', then re-run 'claude-sync.sh sync', or open it by hand:" "  ${manual}"
  fi
  if ! url="$(GH_PROMPT_DISABLED=1 "${GH_BIN}" pr list --repo "${repo}" --head "${sb}" --state open --json url --jq '.[0].url // ""' 9>&-)"; then
    pr_fail "pushed ${sb}, but 'gh pr list' failed, so it is unknown whether a PR is open." \
      "  Check the gh error above and re-run, or open it by hand:" "  ${manual}"
  fi
  if [ -n "${url}" ]; then
    echo "claude-sync: updated the open PR ${url}"
  else
    body="$(mktemp)"
    cat > "${body}" << EOF
Config changes captured on machine \`${HOSTNAME_VAL}\` by claude-sync.sh.

Every later sync from that machine updates this same PR. Merge it to share the changes; each machine picks them up on its next sync or pull.

To drop the changes instead, close this PR and delete the \`${sb}\` branch, then on that machine run \`git -C <clone> branch -D ${sb} && git -C <clone> reset --hard && claude-sync.sh pull\`.
EOF
    if ! url="$(GH_PROMPT_DISABLED=1 "${GH_BIN}" pr create --repo "${repo}" --base "${DEF}" --head "${sb}" \
        --title "sync: ${HOSTNAME_VAL}" --body-file "${body}" 9>&-)"; then
      rm -f "${body}"
      pr_fail "pushed ${sb}, but 'gh pr create' failed (see the gh error above)." \
        "  Fix it and re-run 'claude-sync.sh sync', or open it by hand:" "  ${manual}"
    fi
    rm -f "${body}"
    echo "claude-sync: opened PR ${url}"
  fi
  g config claude-sync.prVerified "${tip}"
}

# Pushes the sync branch (never the default branch) and makes sure its PR
# is open. Does nothing when there's nothing pending or nothing new.
do_push() {
  local sb tip remote_tip last
  sb="$(sync_branch)"
  has_sync_branch || return 0
  tip="$(g rev-parse "refs/heads/${sb}")"
  remote_tip="$(g rev-parse -q --verify "refs/remotes/origin/${sb}" || true)"
  if [ "${tip}" != "${remote_tip}" ]; then
    last="$(g config --get claude-sync.lastPushed || true)"
    # Overwriting is fine when origin holds what this machine last pushed,
    # or nothing it lacks; anything else (an edit made on GitHub) is kept.
    if [ -n "${remote_tip}" ] && [ "${remote_tip}" != "${last}" ] \
       && ! changes_contained "${tip}" "${remote_tip}" \
       && ! changes_contained "refs/remotes/origin/${DEF}" "${remote_tip}"; then
      fail "origin's ${sb} has commits this machine did not push (edited on GitHub?), so it was not overwritten." \
        "  Merge or close that PR first. To keep its changes, merge it, then re-run 'claude-sync.sh sync'." \
        "  To discard them: git -C ${REPO_DIR} push --force-with-lease=refs/heads/${sb}:${remote_tip} origin ${sb}"
    fi
    if ! g push -q --force-with-lease="refs/heads/${sb}:${remote_tip}" origin "${tip}:refs/heads/${sb}"; then
      fail "git push of ${sb} failed (see the git error above). Nothing on ${DEF} was touched." \
        "  Fix the cause (network, auth, or ${sb} changed on origin since the fetch) and re-run 'claude-sync.sh sync'."
    fi
    g config claude-sync.lastPushed "${tip}"
    echo "claude-sync: pushed ${sb}."
  fi
  ensure_pr
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

# 'pull' on a clone that isn't on its default branch (developing the tool
# itself) or has no origin: the old behaviour, rebase onto the upstream if
# there is one. Never pushes.
integrate_current_branch() {
  if ! g rev-parse --abbrev-ref --symbolic-full-name '@{u}' >/dev/null 2>&1; then
    return 0
  fi
  g fetch || fetch_failed
  if ! g rebase; then
    if [ -d "$(git_path rebase-merge)" ] || [ -d "$(git_path rebase-apply)" ]; then
      g rebase --abort || true
    fi
    fail "git rebase onto upstream failed (conflict). Resolve manually in ${REPO_DIR} and re-run."
  fi
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

# Diagnoses (and, if requested, repairs) a clone stuck out of sync. A
# claude-sync.sh from before sync branches committed captures on the
# default branch; if a rebase onto upstream then conflicted, those commits
# stayed local and every later sync repeated the failure. Today's sync
# moves such commits onto sync/<hostname> instead, so remediation is a
# normal sync, which fails loudly if they really conflict.
do_doctor() {
  local remediate="${1:-false}"

  # Remediation changes the clone and live config, so it holds the lock,
  # except around the nested sync, which takes the lock itself.
  [ "${remediate}" != "true" ] || acquire_lock

  if [ -d "$(git_path rebase-merge)" ] || [ -d "$(git_path rebase-apply)" ]; then
    doctor_report "repo is mid-rebase (a previous sync's conflict was never resolved)."
    if [ "${remediate}" = "true" ]; then
      g rebase --abort || true
      echo "  -> aborted the stuck rebase."
    fi
  fi

  if g rev-parse --abbrev-ref --symbolic-full-name '@{u}' >/dev/null 2>&1; then
    g fetch -q || fetch_failed
    local ahead behind
    ahead="$(g rev-list --count '@{u}..HEAD')"
    behind="$(g rev-list --count 'HEAD..@{u}')"
    if [ "${ahead}" -gt 0 ]; then
      if [ "${behind}" -gt 0 ]; then
        doctor_report "local branch is ${ahead} ahead / ${behind} behind its upstream - a prior sync likely failed to push/apply."
      else
        doctor_report "local branch is ${ahead} commit(s) ahead of its upstream - claude-sync never commits there, so the next sync moves them onto $(sync_branch) as a PR."
      fi
      if [ "${remediate}" = "true" ]; then
        echo "  -> retrying a full sync now that a stuck rebase (if any) is cleared..."
        release_lock
        if "${SCRIPT_DIR}/claude-sync.sh" sync; then
          echo "  -> sync succeeded."
        else
          echo "  -> still failing: a real conflict exists between local and remote settings, not just staleness." >&2
          echo "     Resolve manually in ${REPO_DIR} (git status), then re-run 'claude-sync.sh sync'." >&2
        fi
        acquire_lock
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
  if has_sync_branch; then
    echo "Note: $(sync_branch) holds this machine's changes that are not merged yet; its PR shares them once merged."
  fi
  FAIL_REPORTED=1
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

# The tail every repo-changing command shares once its own commits are on
# the sync branch: integrate, apply, then push and open the PR. Applying
# before pushing means a failed push or PR never leaves the live config
# behind the clone.
finish_sync() {
  do_integrate
  do_apply
  do_push
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
      begin_sync
      # Capture first so any other pending local edits are committed as
      # shared, then move this one key out of the shared file.
      do_capture_and_commit
      overlay_add "${REPO_DIR}" "$(current_profile)" "${CLAUDE_HOME}/settings.json" "${expr}"
      commit_worktree "override: ${HOSTNAME_VAL} keeps ${expr} to itself"
      finish_sync
      echo "${expr} is now specific to ${HOSTNAME_VAL}; other machines drop it once $(sync_branch)'s PR is merged."
      ;;
    remove)
      [ -n "${expr}" ] || usage
      begin_sync
      # Capture while the override still owns the key, so its local value
      # isn't mistaken for a new shared value, then drop it and re-apply.
      do_capture_and_commit
      overlay_remove "${REPO_DIR}" "$(current_profile)" "${expr}"
      commit_worktree "override: ${HOSTNAME_VAL} stops overriding ${expr}"
      finish_sync
      echo "${expr} on ${HOSTNAME_VAL} now follows the shared settings."
      ;;
    *)
      usage
      ;;
  esac
}

die() {
  FAIL_REPORTED=1
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
      begin_sync
      # Capture under the old profile first, so pending edits land in it.
      do_capture_and_commit
      if [ "$1" = "${HOSTNAME_VAL}" ]; then
        write_machine_profile "${conf}" ""
      else
        write_machine_profile "${conf}" "$1"
      fi
      commit_worktree "profile: ${HOSTNAME_VAL} uses profile $1"
      finish_sync
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

  begin_sync
  do_capture_and_commit
  rm -rf "${dest}"
  [ ! -d "${src}" ] || cp -a "${src}" "${dest}"
  rm -f "$(overlay_path "${REPO_DIR}" "${own}")"
  if [ -f "$(overlay_path "${REPO_DIR}" "${from}")" ]; then
    cp "$(overlay_path "${REPO_DIR}" "${from}")" "$(overlay_path "${REPO_DIR}" "${own}")"
  fi
  # The copy only takes effect if this machine follows its own profile.
  write_machine_profile "${conf}" ""
  commit_worktree "profile: ${own} copies profile ${from}"
  finish_sync
  echo "${own} now has its own copy of profile '${from}' in machines/${own}/; edit it freely."
}

usage() {
  FAIL_REPORTED=1
  echo "Usage: claude-sync.sh [push|pull|sync|bootstrap|doctor [remediate]|override add|remove|list [JQ_PATH]" >&2
  echo "                       |profile list|show NAME|copy FROM [--force] [--yes]|use NAME]" >&2
  echo "  sync       capture local changes onto sync/<hostname>, merge the default branch in, apply, push, open/update its PR" >&2
  echo "  push       capture onto sync/<hostname>, push it and open/update its PR (no apply)" >&2
  echo "  pull       bring in the default branch (keeping this machine's unmerged changes) and apply" >&2
  echo "  Nothing is ever pushed to the default branch; changes reach other machines when the PR is merged." >&2
  echo "  The PR step needs gh, logged in. CLAUDE_SYNC_GH=<path> picks the gh binary;" >&2
  echo "  CLAUDE_SYNC_GH_REPO=owner/repo sets the repo when origin's URL doesn't show it." >&2
  echo "  Needs git 2.40+. Runs are kept apart by flock (CLAUDE_SYNC_FLOCK=<path>, wait" >&2
  echo "  CLAUDE_SYNC_LOCK_WAIT seconds, default 10); without flock a weaker lock is used." >&2
  echo "  e.g. claude-sync.sh override add '.enabledPlugins[\"name@marketplace\"]'" >&2
  echo "       claude-sync.sh profile use other-host   # follow machines/other-host/ (agents, hooks, skills, CLAUDE.md)" >&2
  exit 1
}

main() {
  if [ ! -d "${REPO_DIR}/.git" ]; then
    echo "No git repo at ${REPO_DIR}. Run install.sh first." >&2
    exit 1
  fi
  trap on_exit EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  trap 'exit 129' HUP

  local cmd="${1:-sync}"

  # The capture and PR flow only runs with the clone on its default branch.
  # Any other branch means this clone is being used to develop the sync
  # tool itself (e.g. SessionStart/SessionEnd hooks firing mid-session), so
  # skip rather than mix local config into that work.
  case "${cmd}" in
    push|sync|bootstrap|override|profile)
      # profile list/show only read, so they work on any branch.
      if [ "${cmd}" = "profile" ] && { [ "${2:-list}" = "list" ] || [ "${2:-}" = "show" ]; }; then
        :
      elif ! on_default_branch; then
        echo "claude-sync: sync SKIPPED, nothing was captured, pushed or applied." >&2
        echo "  Why: claude-config is on branch '$(g rev-parse --abbrev-ref HEAD 2>/dev/null)', not its default branch '$(default_branch)'," >&2
        echo "  and syncing there would commit local config onto that branch. Switch back to the default branch to sync." >&2
        exit 0
      fi
      ;;
  esac

  case "${cmd}" in
    push)
      begin_sync
      do_capture_and_commit
      do_squash
      do_push
      ;;
    pull)
      if on_default_branch && has_origin; then
        begin_sync
        do_integrate
      else
        integrate_current_branch
      fi
      do_apply
      ;;
    sync)
      begin_sync
      do_capture_and_commit
      finish_sync
      ;;
    bootstrap)
      begin_sync
      do_integrate
      do_apply_merge
      do_capture_and_commit
      do_squash
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
