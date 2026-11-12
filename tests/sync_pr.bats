#!/usr/bin/env bats
# Sync goes out through one PR per machine (sync/<hostname>), never a push
# to the default branch. Placeholder hostnames only: this file is
# published to the public mirror.

setup() {
  load 'test_helper'
  install_fake_gh

  ORIGIN="${BATS_TEST_TMPDIR}/origin.git"
  git init --bare -q "${ORIGIN}"
  git -C "${ORIGIN}" symbolic-ref HEAD refs/heads/master

  SEED="${BATS_TEST_TMPDIR}/seed"
  git clone -q "${ORIGIN}" "${SEED}" 2>/dev/null
  mkdir -p "${SEED}/lib" "${SEED}/machines" "${SEED}/dot-claude/agents" "${SEED}/dot-agents/skills" "${SEED}/memory"
  cp "${REPO_ROOT}/claude-sync.sh" "${SEED}/claude-sync.sh"
  mkdir -p "${SEED}/legacy"
  cp "${REPO_ROOT}"/legacy/*.sh "${SEED}/legacy/"
  cp "${REPO_ROOT}"/lib/*.sh "${SEED}/lib/"
  chmod +x "${SEED}/claude-sync.sh"
  touch "${SEED}/dot-claude/agents/.gitkeep" "${SEED}/dot-agents/skills/.gitkeep" "${SEED}/memory/.gitkeep"
  printf '# shared\n' > "${SEED}/dot-claude/CLAUDE.md"
  echo '{"theme":"dark"}' > "${SEED}/dot-claude/settings.json"
  source "${LIB_DIR}/machine-conf.sh"
  write_machine_conf "${SEED}/machines/host-a.conf" "/home/user/project"
  write_machine_conf "${SEED}/machines/host-b.conf" "/home/user/work"
  git -C "${SEED}" add -A
  git -C "${SEED}" -c user.email=test@test -c user.name=test commit -q -m seed
  git -C "${SEED}" push -q origin master 2>/dev/null

  REPO_A="${BATS_TEST_TMPDIR}/repo_a"
  CLAUDE_HOME_A="${BATS_TEST_TMPDIR}/claude_home_a"
  AGENTS_HOME_A="${BATS_TEST_TMPDIR}/agents_home_a"
  REPO_B="${BATS_TEST_TMPDIR}/repo_b"
  CLAUDE_HOME_B="${BATS_TEST_TMPDIR}/claude_home_b"
  AGENTS_HOME_B="${BATS_TEST_TMPDIR}/agents_home_b"
}

raw_as() {  # raw_as a|b CMD [ARGS...]
  local m="$1"; shift
  local home agents repo
  if [ "$m" = a ]; then home="${CLAUDE_HOME_A}" agents="${AGENTS_HOME_A}" repo="${REPO_A}"
  else home="${CLAUDE_HOME_B}" agents="${AGENTS_HOME_B}" repo="${REPO_B}"; fi
  CLAUDE_SYNC_CLAUDE_HOME="${home}" CLAUDE_SYNC_AGENTS_HOME="${agents}" CLAUDE_SYNC_HOSTNAME="host-${m}" \
  GIT_AUTHOR_NAME=test GIT_AUTHOR_EMAIL=test@test GIT_COMMITTER_NAME=test GIT_COMMITTER_EMAIL=test@test \
    "${repo}/claude-sync.sh" "$@"
}

# A machine with the shared config applied and nothing pending.
start_machine() {  # start_machine a|b
  local m="$1" repo home agents
  if [ "$m" = a ]; then repo="${REPO_A}" home="${CLAUDE_HOME_A}" agents="${AGENTS_HOME_A}"
  else repo="${REPO_B}" home="${CLAUDE_HOME_B}" agents="${AGENTS_HOME_B}"; fi
  git clone -q "${ORIGIN}" "${repo}" 2>/dev/null
  mkdir -p "${home}" "${agents}/skills"
  raw_as "$m" pull >/dev/null
}

origin_refs() { git -C "${ORIGIN}" for-each-ref --format='%(refname) %(objectname)'; }
pr_creates() { grep -c "^gh pr create" "${FAKE_GH_LOG}" || true; }

@test "sync commits onto sync/<host> and opens a PR; the default branch is never touched" {
  start_machine a
  local master_before; master_before="$(git -C "${ORIGIN}" rev-parse master)"
  echo "edit one" > "${CLAUDE_HOME_A}/CLAUDE.md"

  run raw_as a sync
  [ "$status" -eq 0 ]
  [[ "$output" == *"pushed sync/host-a"* ]]
  [[ "$output" == *"opened PR"* ]]

  # Origin: master untouched, the edit is on sync/host-a, one commit on master.
  [ "$(git -C "${ORIGIN}" rev-parse master)" = "${master_before}" ]
  [ "$(git -C "${ORIGIN}" show sync/host-a:dot-claude/CLAUDE.md)" = "edit one" ]
  [ "$(git -C "${ORIGIN}" rev-list --count master..sync/host-a)" = "1" ]
  # Local: still on master, master not moved, working tree shows the pending state.
  [ "$(git -C "${REPO_A}" rev-parse --abbrev-ref HEAD)" = "master" ]
  [ "$(git -C "${REPO_A}" rev-parse master)" = "${master_before}" ]
  [ "$(cat "${REPO_A}/dot-claude/CLAUDE.md")" = "edit one" ]
  [ "$(cat "${CLAUDE_HOME_A}/CLAUDE.md")" = "edit one" ]

  # The PR names repo, base and head explicitly, uses a body file, never auto-merges.
  grep -q "^gh pr list --repo test/repo --head sync/host-a --state open " "${FAKE_GH_LOG}"
  grep -q "^gh pr create --repo test/repo --base master --head sync/host-a --title sync: host-a --body-file " "${FAKE_GH_LOG}"
  refute grep -qE "merge|--auto" "${FAKE_GH_LOG}"
  # No AI attribution anywhere.
  refute grep -qiE "co-authored-by|generated with" "${FAKE_GH_STATE}/body-sync_host-a"
  git -C "${ORIGIN}" log --format=%B master..sync/host-a > "${BATS_TEST_TMPDIR}/msg"
  refute grep -qiE "co-authored-by|generated with" "${BATS_TEST_TMPDIR}/msg"
}

@test "a second sync updates the same branch and PR as a single commit" {
  start_machine a
  echo "edit one" > "${CLAUDE_HOME_A}/CLAUDE.md"
  raw_as a sync >/dev/null
  local first; first="$(git -C "${ORIGIN}" rev-parse sync/host-a)"

  echo "edit two" > "${CLAUDE_HOME_A}/CLAUDE.md"
  run raw_as a sync
  [ "$status" -eq 0 ]
  [[ "$output" == *"updated the open PR https://github.example/test/repo/pull/1"* ]]
  [[ "$output" != *"refusing"* ]]

  [ "$(git -C "${ORIGIN}" rev-parse sync/host-a)" != "${first}" ]
  [ "$(git -C "${ORIGIN}" show sync/host-a:dot-claude/CLAUDE.md)" = "edit two" ]
  [ "$(git -C "${ORIGIN}" rev-list --count master..sync/host-a)" = "1" ]
  [ "$(pr_creates)" = "1" ]
}

@test "a sync with nothing new is a quiet no-op" {
  start_machine a
  echo "edit one" > "${CLAUDE_HOME_A}/CLAUDE.md"
  raw_as a sync >/dev/null
  local refs calls; refs="$(origin_refs)"; calls="$(wc -l < "${FAKE_GH_LOG}")"

  run raw_as a sync
  [ "$status" -eq 0 ]
  [ -z "$output" ]
  [ "$(origin_refs)" = "${refs}" ]
  [ "$(wc -l < "${FAKE_GH_LOG}")" = "${calls}" ]

  # Same with nothing pending at all.
  merge_sync_pr "${ORIGIN}" host-a
  raw_as a sync >/dev/null
  refs="$(origin_refs)"
  run raw_as a sync
  [ "$status" -eq 0 ]
  [ -z "$output" ]
  [ "$(origin_refs)" = "${refs}" ]
}

@test "without gh the branch is still pushed and applied, and sync fails loudly with the manual command" {
  start_machine a
  echo "edit one" > "${CLAUDE_HOME_A}/CLAUDE.md"

  CLAUDE_SYNC_GH="${BATS_TEST_TMPDIR}/no-such-gh" run raw_as a sync
  [ "$status" -ne 0 ]
  [[ "$output" == *"the GitHub CLI (gh) is not installed, so no PR was opened"* ]]
  [[ "$output" == *"gh pr create --repo test/repo --base master --head sync/host-a"* ]]
  [ "$(git -C "${ORIGIN}" show sync/host-a:dot-claude/CLAUDE.md)" = "edit one" ]
  [ "$(cat "${CLAUDE_HOME_A}/CLAUDE.md")" = "edit one" ]

  # Not silent next time either, until a PR is open; then it is.
  CLAUDE_SYNC_GH="${BATS_TEST_TMPDIR}/no-such-gh" run raw_as a sync
  [ "$status" -ne 0 ]
  [[ "$output" == *"git -C ${REPO_A} config claude-sync.prVerified "* ]]
  run raw_as a sync
  [ "$status" -eq 0 ]
  [[ "$output" == *"opened PR"* ]]
}

@test "an unauthenticated gh or a failed pr create fails loudly" {
  start_machine a
  echo "edit one" > "${CLAUDE_HOME_A}/CLAUDE.md"
  FAKE_GH_AUTH_RC=1 run raw_as a sync
  [ "$status" -ne 0 ]
  [[ "$output" == *"gh is not logged in"* ]]
  [[ "$output" == *"You are not logged into any GitHub hosts"* ]]
  [[ "$output" == *"gh pr create --repo test/repo"* ]]

  FAKE_GH_CREATE_RC=1 run raw_as a sync
  [ "$status" -ne 0 ]
  [[ "$output" == *"'gh pr create' failed"* ]]
}

@test "a rejected push fails loudly, after the live config was applied" {
  start_machine a
  cat > "${ORIGIN}/hooks/pre-receive" << 'EOF'
#!/usr/bin/env bash
echo "push rejected by test" >&2
exit 1
EOF
  chmod +x "${ORIGIN}/hooks/pre-receive"
  echo "edit one" > "${CLAUDE_HOME_A}/CLAUDE.md"

  run raw_as a sync
  [ "$status" -ne 0 ]
  [[ "$output" == *"git push of sync/host-a failed"* ]]
  refute git -C "${ORIGIN}" show-ref -q --verify refs/heads/sync/host-a
  [ "$(cat "${CLAUDE_HOME_A}/CLAUDE.md")" = "edit one" ]
  refute grep -q "^gh pr create" "${FAKE_GH_LOG}"

  # Fixed remote: the next sync pushes without refusing.
  rm "${ORIGIN}/hooks/pre-receive"
  run raw_as a sync
  [ "$status" -eq 0 ]
  [ "$(git -C "${ORIGIN}" show sync/host-a:dot-claude/CLAUDE.md)" = "edit one" ]
}

@test "an unmerged own PR plus another machine's merged change does not cause a refusal" {
  start_machine a
  start_machine b
  echo "edit one" > "${CLAUDE_HOME_A}/CLAUDE.md"
  raw_as a sync >/dev/null

  # Machine B's PR (a new agent) is merged while A's is still open.
  echo "from b" > "${CLAUDE_HOME_B}/agents/b-agent.md"
  raw_as b sync >/dev/null
  merge_sync_pr "${ORIGIN}" host-b

  echo "edit two" > "${CLAUDE_HOME_A}/CLAUDE.md"
  run raw_as a sync
  [ "$status" -eq 0 ]
  [[ "$output" != *"refusing"* ]]
  [[ "$output" != *"skipping capture"* ]]
  # A now has both; its branch sits on the new master with one commit.
  [ "$(cat "${CLAUDE_HOME_A}/CLAUDE.md")" = "edit two" ]
  [ "$(cat "${CLAUDE_HOME_A}/agents/b-agent.md")" = "from b" ]
  [ "$(git -C "${ORIGIN}" rev-parse sync/host-a^)" = "$(git -C "${ORIGIN}" rev-parse master)" ]
  [ "$(git -C "${ORIGIN}" show sync/host-a:dot-claude/CLAUDE.md)" = "edit two" ]
}

@test "with a PR pending, a manual git pull plus a live edit keeps both changes" {
  start_machine a
  start_machine b
  echo "edit one" > "${CLAUDE_HOME_A}/CLAUDE.md"
  raw_as a sync >/dev/null
  echo "from b" > "${CLAUDE_HOME_B}/agents/b-agent.md"
  raw_as b sync >/dev/null
  merge_sync_pr "${ORIGIN}" host-b
  git -C "${REPO_A}" pull -q --ff-only 2>/dev/null
  echo "edit two" > "${CLAUDE_HOME_A}/CLAUDE.md"

  run raw_as a sync
  [ "$status" -eq 0 ]
  [[ "$output" != *"refusing"* ]]
  [ "$(cat "${CLAUDE_HOME_A}/CLAUDE.md")" = "edit two" ]
  [ "$(cat "${CLAUDE_HOME_A}/agents/b-agent.md")" = "from b" ]
}

@test "with nothing pending, a clone that moved without an apply plus a live edit is still refused" {
  start_machine a
  start_machine b
  echo "from b" > "${CLAUDE_HOME_B}/agents/b-agent.md"
  raw_as b sync >/dev/null
  merge_sync_pr "${ORIGIN}" host-b
  git -C "${REPO_A}" pull -q --ff-only 2>/dev/null
  echo "local edit" > "${CLAUDE_HOME_A}/CLAUDE.md"
  local refs; refs="$(origin_refs)"

  run raw_as a sync
  [ "$status" -ne 0 ]
  [[ "$output" == *"refusing to capture"* ]]
  [ "$(cat "${CLAUDE_HOME_A}/CLAUDE.md")" = "local edit" ]
  [ "$(origin_refs)" = "${refs}" ]
  refute git -C "${REPO_A}" show-ref -q --verify refs/heads/sync/host-a
}

@test "after a squash merge, a same-line edit and every machine converge without conflicts" {
  start_machine a
  start_machine b
  echo "edit one" > "${CLAUDE_HOME_A}/CLAUDE.md"
  raw_as a sync >/dev/null
  merge_sync_pr "${ORIGIN}" host-a squash

  # The same line again, before A ever synced after the merge.
  echo "edit two" > "${CLAUDE_HOME_A}/CLAUDE.md"
  run raw_as a sync
  [ "$status" -eq 0 ]
  [ "$(git -C "${ORIGIN}" rev-list --count master..sync/host-a)" = "1" ]
  [ "$(git -C "${ORIGIN}" diff --name-only master sync/host-a)" = "dot-claude/CLAUDE.md" ]
  [ "$(pr_creates)" = "2" ]
  merge_sync_pr "${ORIGIN}" host-a squash

  # A converges: branch gone locally, clean clone on the merged master.
  run raw_as a sync
  [ "$status" -eq 0 ]
  refute git -C "${REPO_A}" show-ref -q --verify refs/heads/sync/host-a
  [ "$(git -C "${REPO_A}" rev-parse master)" = "$(git -C "${ORIGIN}" rev-parse master)" ]
  [ -z "$(git -C "${REPO_A}" status --porcelain)" ]

  # B gets it and has nothing to push.
  run raw_as b sync
  [ "$status" -eq 0 ]
  [ "$(cat "${CLAUDE_HOME_B}/CLAUDE.md")" = "edit two" ]
  refute git -C "${ORIGIN}" show-ref -q --verify refs/heads/sync/host-b
}

@test "after a merge-commit merge that kept the branch, the next change reuses it" {
  start_machine a
  echo "edit one" > "${CLAUDE_HOME_A}/CLAUDE.md"
  raw_as a sync >/dev/null
  merge_sync_pr "${ORIGIN}" host-a merge keep
  raw_as a sync >/dev/null
  refute git -C "${REPO_A}" show-ref -q --verify refs/heads/sync/host-a

  echo "edit two" > "${CLAUDE_HOME_A}/CLAUDE.md"
  run raw_as a sync
  [ "$status" -eq 0 ]
  [ "$(git -C "${ORIGIN}" show sync/host-a:dot-claude/CLAUDE.md)" = "edit two" ]
  [ "$(git -C "${ORIGIN}" rev-parse sync/host-a^)" = "$(git -C "${ORIGIN}" rev-parse master)" ]
}

@test "a fresh clone picks up the machine's open sync branch instead of dropping it" {
  start_machine a
  echo "pending edit" > "${CLAUDE_HOME_A}/CLAUDE.md"
  raw_as a sync >/dev/null
  local pushed; pushed="$(git -C "${ORIGIN}" rev-parse sync/host-a)"

  # As after install.sh --repair: a new clone, same live config.
  mv "${REPO_A}" "${BATS_TEST_TMPDIR}/old_repo_a"
  git clone -q "${ORIGIN}" "${REPO_A}" 2>/dev/null

  run raw_as a sync
  [ "$status" -eq 0 ]
  [[ "$output" == *"picked up this machine's open sync/host-a"* ]]
  [ "$(cat "${CLAUDE_HOME_A}/CLAUDE.md")" = "pending edit" ]
  [ "$(cat "${REPO_A}/dot-claude/CLAUDE.md")" = "pending edit" ]
  [ "$(git -C "${ORIGIN}" rev-parse sync/host-a)" = "${pushed}" ]
}

@test "a sync branch edited on GitHub is not overwritten" {
  start_machine a
  echo "edit one" > "${CLAUDE_HOME_A}/CLAUDE.md"
  raw_as a sync >/dev/null
  # Someone commits to the PR branch on GitHub.
  local w="${BATS_TEST_TMPDIR}/gh_edit"
  git clone -q -b sync/host-a "${ORIGIN}" "${w}" 2>/dev/null
  echo "reviewer note" > "${w}/memory/NOTE.md"
  git -C "${w}" add -A
  git -C "${w}" -c user.email=t@t -c user.name=t commit -q -m "review edit"
  git -C "${w}" push -q origin sync/host-a 2>/dev/null
  local edited; edited="$(git -C "${ORIGIN}" rev-parse sync/host-a)"

  echo "edit two" > "${CLAUDE_HOME_A}/CLAUDE.md"
  run raw_as a sync
  [ "$status" -ne 0 ]
  [[ "$output" == *"has commits this machine did not push"* ]]
  [ "$(git -C "${ORIGIN}" rev-parse sync/host-a)" = "${edited}" ]
}

@test "hand edits in the clone to a pending file are kept and go out with the PR" {
  start_machine a
  echo '{"theme":"solarized"}' > "${REPO_A}/machines/host-a.settings.json"
  raw_as a sync >/dev/null
  [ "$(jq -r .theme "${CLAUDE_HOME_A}/settings.json")" = "solarized" ]

  # The pending file is in the clone's working tree; edit it by hand.
  echo '{"theme":"nord"}' > "${REPO_A}/machines/host-a.settings.json"
  run raw_as a sync
  [ "$status" -eq 0 ]
  [ "$(jq -r .theme "${CLAUDE_HOME_A}/settings.json")" = "nord" ]
  [ "$(git -C "${ORIGIN}" show sync/host-a:machines/host-a.settings.json | jq -r .theme)" = "nord" ]
}

@test "pending changes are not staged, so a commit made by hand in the clone holds only its own file" {
  start_machine a
  echo "pending edit" > "${CLAUDE_HOME_A}/CLAUDE.md"
  raw_as a sync >/dev/null
  [ -z "$(git -C "${REPO_A}" diff --cached --name-only)" ]

  git -C "${REPO_A}" checkout -q -b some-feature
  echo "feature" > "${REPO_A}/feature.txt"
  git -C "${REPO_A}" add feature.txt
  git -C "${REPO_A}" -c user.email=t@t -c user.name=t commit -q -m feature
  [ "$(git -C "${REPO_A}" show --name-only --format= HEAD)" = "feature.txt" ]
}

@test "pull keeps this machine's unmerged changes in the live config" {
  start_machine a
  start_machine b
  echo "pending edit" > "${CLAUDE_HOME_A}/CLAUDE.md"
  raw_as a sync >/dev/null
  echo "from b" > "${CLAUDE_HOME_B}/agents/b-agent.md"
  raw_as b sync >/dev/null
  merge_sync_pr "${ORIGIN}" host-b
  local before; before="$(git -C "${ORIGIN}" rev-parse sync/host-a)"

  run raw_as a pull
  [ "$status" -eq 0 ]
  [ "$(cat "${CLAUDE_HOME_A}/CLAUDE.md")" = "pending edit" ]
  [ "$(cat "${CLAUDE_HOME_A}/agents/b-agent.md")" = "from b" ]
  # pull never pushes.
  [ "$(git -C "${ORIGIN}" rev-parse sync/host-a)" = "${before}" ]
}

@test "the default branch name is never used for a sync branch" {
  start_machine a
  CLAUDE_SYNC_HOSTNAME="../master" run env CLAUDE_SYNC_CLAUDE_HOME="${CLAUDE_HOME_A}" \
    CLAUDE_SYNC_AGENTS_HOME="${AGENTS_HOME_A}" "${REPO_A}/claude-sync.sh" push
  [ "$status" -ne 0 ]
  [[ "$output" == *"can't be used as this machine's sync branch name"* ]]
}

# Commits a change to origin's master directly, as a merged PR would.
commit_on_master() {  # commit_on_master FILE CONTENT
  local w; w="$(mktemp -d "${BATS_TEST_TMPDIR}/direct.XXXXXX")"
  git clone -q "${ORIGIN}" "${w}/c" 2>/dev/null
  printf '%s\n' "$2" > "${w}/c/$1"
  git -C "${w}/c" add -A
  git -C "${w}/c" -c user.email=t@t -c user.name=t commit -q -m "direct: $1"
  git -C "${w}/c" push -q origin master 2>/dev/null
}

@test "a squash merge that kept the remote branch converges after the default branch changes the same file" {
  start_machine a
  echo "edit one" > "${CLAUDE_HOME_A}/CLAUDE.md"
  raw_as a sync >/dev/null
  merge_sync_pr "${ORIGIN}" host-a squash keep
  raw_as a sync >/dev/null
  refute git -C "${REPO_A}" show-ref -q --verify refs/heads/sync/host-a
  git -C "${ORIGIN}" show-ref -q --verify refs/heads/sync/host-a

  commit_on_master dot-claude/CLAUDE.md "changed on master"
  local i
  for i in 1 2; do
    run raw_as a sync
    [ "$status" -eq 0 ]
    [[ "$output" != *"picked up"* ]]
  done
  [ "$(cat "${CLAUDE_HOME_A}/CLAUDE.md")" = "changed on master" ]
  refute git -C "${REPO_A}" show-ref -q --verify refs/heads/sync/host-a

  # The next change replaces the stale branch and opens a new PR.
  echo "edit two" > "${CLAUDE_HOME_A}/CLAUDE.md"
  run raw_as a sync
  [ "$status" -eq 0 ]
  [ "$(git -C "${ORIGIN}" show sync/host-a:dot-claude/CLAUDE.md)" = "edit two" ]
  [ "$(git -C "${ORIGIN}" rev-parse sync/host-a^)" = "$(git -C "${ORIGIN}" rev-parse master)" ]
  [ "$(pr_creates)" = "2" ]
}

@test "a fresh clone does not pick up a kept sync branch whose PR is no longer open" {
  start_machine a
  echo "edit one" > "${CLAUDE_HOME_A}/CLAUDE.md"
  raw_as a sync >/dev/null
  merge_sync_pr "${ORIGIN}" host-a squash keep
  commit_on_master dot-claude/CLAUDE.md "changed on master"

  mv "${REPO_A}" "${BATS_TEST_TMPDIR}/old_repo_a"
  git clone -q "${ORIGIN}" "${REPO_A}" 2>/dev/null
  rm -f "${CLAUDE_HOME_A}/.claude-sync-applied-commit"
  run raw_as a pull
  [ "$status" -eq 0 ]
  [[ "$output" == *"belongs to a PR that was merged or closed, so it was not picked up"* ]]
  [ "$(cat "${CLAUDE_HOME_A}/CLAUDE.md")" = "changed on master" ]
  refute git -C "${REPO_A}" show-ref -q --verify refs/heads/sync/host-a

  echo "edit two" > "${CLAUDE_HOME_A}/CLAUDE.md"
  run raw_as a sync
  [ "$status" -eq 0 ]
  [ "$(git -C "${ORIGIN}" show sync/host-a:dot-claude/CLAUDE.md)" = "edit two" ]
}

@test "a fresh clone picks up a branch that was pushed without a PR" {
  start_machine a
  echo "pending edit" > "${CLAUDE_HOME_A}/CLAUDE.md"
  FAKE_GH_AUTH_RC=1 run raw_as a sync
  [ "$status" -ne 0 ]
  local pushed; pushed="$(git -C "${ORIGIN}" rev-parse sync/host-a)"

  mv "${REPO_A}" "${BATS_TEST_TMPDIR}/old_repo_a"
  git clone -q "${ORIGIN}" "${REPO_A}" 2>/dev/null
  run raw_as a sync
  [ "$status" -eq 0 ]
  [[ "$output" == *"picked up this machine's open sync/host-a"* ]]
  [ "$(cat "${CLAUDE_HOME_A}/CLAUDE.md")" = "pending edit" ]
  [ "$(git -C "${ORIGIN}" rev-parse sync/host-a)" = "${pushed}" ]
  [ "$(pr_creates)" = "1" ]
}

@test "a run waits for the lock and fails loudly when another run holds it" {
  start_machine a
  echo "edit one" > "${CLAUDE_HOME_A}/CLAUDE.md"
  # The test shell itself holds the lock on its own open file.
  exec 8> "${REPO_A}/.git/claude-sync.flock"
  flock 8
  local sync_status sync_output
  CLAUDE_SYNC_LOCK_WAIT=1 run raw_as a sync
  sync_status="$status" sync_output="$output"
  CLAUDE_SYNC_LOCK_WAIT=1 run raw_as a doctor remediate
  exec 8>&-
  [ "${sync_status}" -ne 0 ]
  [[ "${sync_output}" == *"another claude-sync run is still working"* ]]
  [ "$status" -ne 0 ]
  [[ "$output" == *"another claude-sync run is still working"* ]]
  refute git -C "${ORIGIN}" show-ref -q --verify refs/heads/sync/host-a

  # Released once the holder closes it, with nothing left to clean up.
  run raw_as a sync
  [ "$status" -eq 0 ]
  git -C "${ORIGIN}" show-ref -q --verify refs/heads/sync/host-a
}

@test "without flock a weaker lock is used loudly, and a stale one is cleared" {
  start_machine a
  ln -s 999999 "${REPO_A}/.git/claude-sync.lockpid"
  echo "edit one" > "${CLAUDE_HOME_A}/CLAUDE.md"
  CLAUDE_SYNC_FLOCK="${BATS_TEST_TMPDIR}/no-such-flock" run raw_as a sync
  [ "$status" -eq 0 ]
  [[ "$output" == *"not found"*"weaker lock"* ]]
  [[ "$output" == *"stale lock left by process 999999"* ]]
  [ ! -L "${REPO_A}/.git/claude-sync.lockpid" ]

  # A lock held by a live process is respected.
  ln -s "$$" "${REPO_A}/.git/claude-sync.lockpid"
  echo "edit two" > "${CLAUDE_HOME_A}/CLAUDE.md"
  CLAUDE_SYNC_FLOCK="${BATS_TEST_TMPDIR}/no-such-flock" CLAUDE_SYNC_LOCK_WAIT=1 run raw_as a sync
  [ "$status" -ne 0 ]
  [[ "$output" == *"pid $$"*"still working"* ]]
  [ "$(readlink "${REPO_A}/.git/claude-sync.lockpid")" = "$$" ]
  rm -f "${REPO_A}/.git/claude-sync.lockpid"
}

@test "a missing last-applied commit gets its own explained refusal" {
  start_machine a
  git -C "${REPO_A}" show-ref -q --verify refs/claude-sync/applied
  echo 0123456789abcdef0123456789abcdef01234567 > "${CLAUDE_HOME_A}/.claude-sync-applied-commit"
  echo "local edit" > "${CLAUDE_HOME_A}/CLAUDE.md"
  local refs; refs="$(origin_refs)"
  run raw_as a sync
  [ "$status" -ne 0 ]
  [[ "$output" == *"is no longer in"* ]]
  [[ "$output" == *"delete ${CLAUDE_HOME_A}/.claude-sync-applied-commit"* ]]
  [ "$(cat "${CLAUDE_HOME_A}/CLAUDE.md")" = "local edit" ]
  [ "$(origin_refs)" = "${refs}" ]
}

# A git that reports VERSION and otherwise defers to the real one, except
# that it fails merge-tree when FAIL_MERGE_TREE is set.
install_fake_git() {  # install_fake_git VERSION
  local real; real="$(command -v git)"
  mkdir -p "${BATS_TEST_TMPDIR}/fake-git"
  cat > "${BATS_TEST_TMPDIR}/fake-git/git" << EOF
#!/usr/bin/env bash
[ "\$1" != "--version" ] || { echo "git version $1"; exit 0; }
if [ -n "\${FAIL_MERGE_TREE:-}" ] && [ "\${3:-}" = "merge-tree" ]; then
  echo "fatal: simulated merge-tree failure" >&2
  exit 128
fi
exec "${real}" "\$@"
EOF
  chmod +x "${BATS_TEST_TMPDIR}/fake-git/git"
}

@test "an old git is refused clearly before anything changes" {
  start_machine a
  install_fake_git 2.39.2
  echo "edit one" > "${CLAUDE_HOME_A}/CLAUDE.md"
  PATH="${BATS_TEST_TMPDIR}/fake-git:${PATH}" run raw_as a sync
  [ "$status" -ne 0 ]
  [[ "$output" == *"git 2.40 or newer is needed (found: git version 2.39.2)"* ]]
  refute git -C "${REPO_A}" show-ref -q --verify refs/heads/sync/host-a
}

@test "a merge-tree failure is reported as a git error, not a conflict" {
  start_machine a
  install_fake_git 2.53.0
  echo "edit one" > "${CLAUDE_HOME_A}/CLAUDE.md"
  FAIL_MERGE_TREE=1 PATH="${BATS_TEST_TMPDIR}/fake-git:${PATH}" run raw_as a sync
  [ "$status" -ne 0 ]
  [[ "$output" == *"git merge-tree failed (exit 128"* ]]
  [[ "$output" != *"conflict with changes merged"* ]]
  refute git -C "${ORIGIN}" show-ref -q --verify refs/heads/sync/host-a
}
