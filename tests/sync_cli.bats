#!/usr/bin/env bats

setup() {
  load 'test_helper'

  ORIGIN="${BATS_TEST_TMPDIR}/origin.git"
  git init --bare -q "${ORIGIN}"
  # Pin the bare origin's default branch to master regardless of this
  # machine's init.defaultBranch config. Without this, ORIGIN's HEAD
  # symref points at whatever the local default is (e.g. "main"); once
  # commits land on "master" instead, every later clone (SEED, REPO_A,
  # REPO_B, CHECK) hits "remote HEAD refers to nonexistent ref" and
  # checks out nothing. Setting this before ORIGIN has any commits lets
  # every subsequent clone (starting with SEED) correctly inherit
  # "master" as its checked-out branch.
  git -C "${ORIGIN}" symbolic-ref HEAD refs/heads/master

  SEED="${BATS_TEST_TMPDIR}/seed"
  git clone -q "${ORIGIN}" "${SEED}"
  mkdir -p "${SEED}/lib" "${SEED}/machines" "${SEED}/dot-claude/agents" "${SEED}/dot-agents/skills" "${SEED}/memory"
  cp "${REPO_ROOT}/claude-sync.sh" "${SEED}/claude-sync.sh"
  cp "${REPO_ROOT}/npm-tools.sh" "${SEED}/npm-tools.sh"
  cp "${REPO_ROOT}/gh-tools.sh" "${SEED}/gh-tools.sh"
  cp "${REPO_ROOT}"/lib/*.sh "${SEED}/lib/"
  chmod +x "${SEED}/claude-sync.sh" "${SEED}/npm-tools.sh" "${SEED}/gh-tools.sh"
  touch "${SEED}/machines/.gitkeep" "${SEED}/dot-claude/agents/.gitkeep" \
        "${SEED}/dot-agents/skills/.gitkeep" "${SEED}/memory/.gitkeep"
  git -C "${SEED}" add -A
  git -C "${SEED}" -c user.email=test@test -c user.name=test commit -q -m seed
  git -C "${SEED}" push -q origin master

  # Machine A
  REPO_A="${BATS_TEST_TMPDIR}/repo_a"
  git clone -q "${ORIGIN}" "${REPO_A}"
  CLAUDE_HOME_A="${BATS_TEST_TMPDIR}/claude_home_a"
  AGENTS_HOME_A="${BATS_TEST_TMPDIR}/agents_home_a"
  mkdir -p "${CLAUDE_HOME_A}/projects/-home-modem7-project/memory" "${AGENTS_HOME_A}/skills"
  echo "# from machine A" > "${CLAUDE_HOME_A}/CLAUDE.md"
  echo '{"theme":"dark"}' > "${CLAUDE_HOME_A}/settings.json"
  echo "machine A memory" > "${CLAUDE_HOME_A}/projects/-home-modem7-project/memory/MEMORY.md"
  source "${LIB_DIR}/machine-conf.sh"
  write_machine_conf "${REPO_A}/machines/host-a.conf" "/home/modem7/project"
  git -C "${REPO_A}" -c user.email=test@test -c user.name=test add -A
  git -C "${REPO_A}" -c user.email=test@test -c user.name=test commit -q -m "seed machine conf" --allow-empty

  # Machine B
  REPO_B="${BATS_TEST_TMPDIR}/repo_b"
  CLAUDE_HOME_B="${BATS_TEST_TMPDIR}/claude_home_b"
  AGENTS_HOME_B="${BATS_TEST_TMPDIR}/agents_home_b"
}

run_as_machine_a() {
  CLAUDE_SYNC_CLAUDE_HOME="${CLAUDE_HOME_A}" \
  CLAUDE_SYNC_AGENTS_HOME="${AGENTS_HOME_A}" \
  CLAUDE_SYNC_HOSTNAME="host-a" \
  git -C "${REPO_A}" -c user.email=test@test -c user.name=test "$@" 2>/dev/null || true
}

@test "push captures local config, commits, and pushes to origin" {
  CLAUDE_SYNC_CLAUDE_HOME="${CLAUDE_HOME_A}" \
  CLAUDE_SYNC_AGENTS_HOME="${AGENTS_HOME_A}" \
  CLAUDE_SYNC_HOSTNAME="host-a" \
  GIT_AUTHOR_NAME=test GIT_AUTHOR_EMAIL=test@test GIT_COMMITTER_NAME=test GIT_COMMITTER_EMAIL=test@test \
    "${REPO_A}/claude-sync.sh" push

  [ "$(cat "${REPO_A}/dot-claude/CLAUDE.md")" = "# from machine A" ]

  CHECK="${BATS_TEST_TMPDIR}/check"
  git clone -q "${ORIGIN}" "${CHECK}"
  [ "$(cat "${CHECK}/dot-claude/CLAUDE.md")" = "# from machine A" ]
  [ "$(cat "${CHECK}/memory/MEMORY.md")" = "machine A memory" ]
}

@test "push/sync/bootstrap skip without committing when the repo clone is on a feature branch" {
  git -C "${REPO_A}" checkout -q -b some-feature-branch

  run env \
    CLAUDE_SYNC_CLAUDE_HOME="${CLAUDE_HOME_A}" \
    CLAUDE_SYNC_AGENTS_HOME="${AGENTS_HOME_A}" \
    CLAUDE_SYNC_HOSTNAME="host-a" \
    GIT_AUTHOR_NAME=test GIT_AUTHOR_EMAIL=test@test GIT_COMMITTER_NAME=test GIT_COMMITTER_EMAIL=test@test \
    "${REPO_A}/claude-sync.sh" sync

  [ "$status" -eq 0 ]
  [[ "$output" == *"not its default branch"* ]]
  # Nothing got captured/committed onto the feature branch - the seed
  # commit is still HEAD.
  [ "$(git -C "${REPO_A}" log -1 --format=%s)" = "seed machine conf" ]
  [ ! -f "${REPO_A}/dot-claude/CLAUDE.md" ]
}

@test "pull on a fresh machine applies what a different machine pushed" {
  CLAUDE_SYNC_CLAUDE_HOME="${CLAUDE_HOME_A}" \
  CLAUDE_SYNC_AGENTS_HOME="${AGENTS_HOME_A}" \
  CLAUDE_SYNC_HOSTNAME="host-a" \
  GIT_AUTHOR_NAME=test GIT_AUTHOR_EMAIL=test@test GIT_COMMITTER_NAME=test GIT_COMMITTER_EMAIL=test@test \
    "${REPO_A}/claude-sync.sh" push

  git clone -q "${ORIGIN}" "${REPO_B}"
  source "${LIB_DIR}/machine-conf.sh"
  write_machine_conf "${REPO_B}/machines/host-b.conf" "/home/modem7/work"

  CLAUDE_SYNC_CLAUDE_HOME="${CLAUDE_HOME_B}" \
  CLAUDE_SYNC_AGENTS_HOME="${AGENTS_HOME_B}" \
  CLAUDE_SYNC_HOSTNAME="host-b" \
    "${REPO_B}/claude-sync.sh" pull

  [ "$(cat "${CLAUDE_HOME_B}/CLAUDE.md")" = "# from machine A" ]
  [ "$(cat "${CLAUDE_HOME_B}/projects/-home-modem7-work/memory/MEMORY.md")" = "machine A memory" ]
}

@test "push refuses to run without a machine conf" {
  git clone -q "${ORIGIN}" "${REPO_B}"
  run env CLAUDE_SYNC_CLAUDE_HOME="${CLAUDE_HOME_B}" CLAUDE_SYNC_AGENTS_HOME="${AGENTS_HOME_B}" \
      CLAUDE_SYNC_HOSTNAME="host-b" "${REPO_B}/claude-sync.sh" push
  [ "$status" -eq 1 ]
}

@test "push succeeds on the very first push of a brand-new branch with no upstream" {
  EMPTY_ORIGIN="${BATS_TEST_TMPDIR}/empty_origin.git"
  git init --bare -q "${EMPTY_ORIGIN}"

  FIRST_REPO="${BATS_TEST_TMPDIR}/first_repo"
  mkdir -p "${FIRST_REPO}/lib" "${FIRST_REPO}/machines" "${FIRST_REPO}/dot-claude/agents" \
           "${FIRST_REPO}/dot-agents/skills" "${FIRST_REPO}/memory"
  git -C "${FIRST_REPO}" init -q
  git -C "${FIRST_REPO}" remote add origin "${EMPTY_ORIGIN}"
  cp "${REPO_ROOT}/claude-sync.sh" "${FIRST_REPO}/claude-sync.sh"
  cp "${REPO_ROOT}"/lib/*.sh "${FIRST_REPO}/lib/"
  chmod +x "${FIRST_REPO}/claude-sync.sh"
  touch "${FIRST_REPO}/machines/.gitkeep" "${FIRST_REPO}/dot-claude/agents/.gitkeep" \
        "${FIRST_REPO}/dot-agents/skills/.gitkeep" "${FIRST_REPO}/memory/.gitkeep"
  git -C "${FIRST_REPO}" add -A
  git -C "${FIRST_REPO}" -c user.email=test@test -c user.name=test commit -q -m seed

  CLAUDE_HOME_FIRST="${BATS_TEST_TMPDIR}/claude_home_first"
  AGENTS_HOME_FIRST="${BATS_TEST_TMPDIR}/agents_home_first"
  mkdir -p "${CLAUDE_HOME_FIRST}" "${AGENTS_HOME_FIRST}/skills"
  echo "# from first machine" > "${CLAUDE_HOME_FIRST}/CLAUDE.md"
  echo '{}' > "${CLAUDE_HOME_FIRST}/settings.json"
  source "${LIB_DIR}/machine-conf.sh"
  write_machine_conf "${FIRST_REPO}/machines/host-first.conf" "/home/modem7/project"
  git -C "${FIRST_REPO}" add -A
  git -C "${FIRST_REPO}" -c user.email=test@test -c user.name=test commit -q -m "seed machine conf"

  run env CLAUDE_SYNC_CLAUDE_HOME="${CLAUDE_HOME_FIRST}" CLAUDE_SYNC_AGENTS_HOME="${AGENTS_HOME_FIRST}" \
      CLAUDE_SYNC_HOSTNAME="host-first" \
      GIT_AUTHOR_NAME=test GIT_AUTHOR_EMAIL=test@test GIT_COMMITTER_NAME=test GIT_COMMITTER_EMAIL=test@test \
      "${FIRST_REPO}/claude-sync.sh" push
  [ "$status" -eq 0 ]

  git -C "${FIRST_REPO}" rev-parse --abbrev-ref --symbolic-full-name '@{u}'
}

@test "install.sh clones, creates machine conf, and seeds the repo on a fresh machine" {
  FRESH_REPO_DIR="${BATS_TEST_TMPDIR}/fresh_repo_dir"
  FRESH_CLAUDE_HOME="${BATS_TEST_TMPDIR}/fresh_install_claude_home"
  FRESH_AGENTS_HOME="${BATS_TEST_TMPDIR}/fresh_install_agents_home"
  mkdir -p "${FRESH_CLAUDE_HOME}" "${FRESH_AGENTS_HOME}"
  echo "# fresh machine" > "${FRESH_CLAUDE_HOME}/CLAUDE.md"
  echo '{}' > "${FRESH_CLAUDE_HOME}/settings.json"

  FAKE_BIN="${BATS_TEST_TMPDIR}/install_fakebin"
  mkdir -p "${FAKE_BIN}"
  NPM_CALLS_LOG="${BATS_TEST_TMPDIR}/install-npm-calls.log"
  cat > "${FAKE_BIN}/npm" << EOF
#!/usr/bin/env bash
echo "npm \$*" >> "${NPM_CALLS_LOG}"
exit 0
EOF
  chmod +x "${FAKE_BIN}/npm"

  CLAUDE_SYNC_REPO_URL="${ORIGIN}" \
  CLAUDE_SYNC_REPO_DIR="${FRESH_REPO_DIR}" \
  CLAUDE_SYNC_HOSTNAME="host-fresh" \
  CLAUDE_SYNC_PROJECT_PATH="/home/modem7/fresh" \
  CLAUDE_SYNC_CLAUDE_HOME="${FRESH_CLAUDE_HOME}" \
  CLAUDE_SYNC_AGENTS_HOME="${FRESH_AGENTS_HOME}" \
  GIT_AUTHOR_NAME=test GIT_AUTHOR_EMAIL=test@test GIT_COMMITTER_NAME=test GIT_COMMITTER_EMAIL=test@test \
  PATH="${FAKE_BIN}:${PATH}" \
    "${REPO_ROOT}/install.sh"

  [ -f "${FRESH_REPO_DIR}/claude-sync.sh" ]
  [ -f "${FRESH_REPO_DIR}/machines/host-fresh.conf" ]
  [ -f "${FRESH_REPO_DIR}/dot-claude/CLAUDE.md" ]
  grep -q "npm install -g ccusage@latest" "${NPM_CALLS_LOG}"
}

@test "install.sh defaults the project path to \$HOME/project when the prompt is left empty" {
  DEFAULT_REPO_DIR="${BATS_TEST_TMPDIR}/default_repo_dir"
  DEFAULT_CLAUDE_HOME="${BATS_TEST_TMPDIR}/default_claude_home"
  DEFAULT_AGENTS_HOME="${BATS_TEST_TMPDIR}/default_agents_home"
  DEFAULT_HOME="${BATS_TEST_TMPDIR}/default_home"
  mkdir -p "${DEFAULT_CLAUDE_HOME}" "${DEFAULT_AGENTS_HOME}" "${DEFAULT_HOME}"
  echo "# fresh machine" > "${DEFAULT_CLAUDE_HOME}/CLAUDE.md"
  echo '{}' > "${DEFAULT_CLAUDE_HOME}/settings.json"

  FAKE_BIN="${BATS_TEST_TMPDIR}/default_fakebin"
  mkdir -p "${FAKE_BIN}"
  cat > "${FAKE_BIN}/npm" << 'EOF'
#!/usr/bin/env bash
exit 0
EOF
  chmod +x "${FAKE_BIN}/npm"

  # No CLAUDE_SYNC_PROJECT_PATH set, and an empty line fed to the prompt -
  # asserts the default (\$HOME/project) is used rather than an empty path.
  env \
    CLAUDE_SYNC_REPO_URL="${ORIGIN}" \
    CLAUDE_SYNC_REPO_DIR="${DEFAULT_REPO_DIR}" \
    CLAUDE_SYNC_HOSTNAME="host-default" \
    CLAUDE_SYNC_CLAUDE_HOME="${DEFAULT_CLAUDE_HOME}" \
    CLAUDE_SYNC_AGENTS_HOME="${DEFAULT_AGENTS_HOME}" \
    HOME="${DEFAULT_HOME}" \
    GIT_AUTHOR_NAME=test GIT_AUTHOR_EMAIL=test@test GIT_COMMITTER_NAME=test GIT_COMMITTER_EMAIL=test@test \
    PATH="${FAKE_BIN}:${PATH}" \
    bash -c "echo '' | '${REPO_ROOT}/install.sh'"

  grep -q "PRIMARY_PROJECT_PATH=${DEFAULT_HOME}/project" "${DEFAULT_REPO_DIR}/machines/host-default.conf"
}

@test "install.sh --repair resets a misconfigured machine and lets it re-bootstrap with the correct project path" {
  BROKEN_REPO_DIR="${BATS_TEST_TMPDIR}/broken_repo_dir"
  BROKEN_CLAUDE_HOME="${BATS_TEST_TMPDIR}/broken_claude_home"
  BROKEN_AGENTS_HOME="${BATS_TEST_TMPDIR}/broken_agents_home"
  mkdir -p "${BROKEN_CLAUDE_HOME}" "${BROKEN_AGENTS_HOME}/skills"
  echo "# broken machine" > "${BROKEN_CLAUDE_HOME}/CLAUDE.md"
  echo '{}' > "${BROKEN_CLAUDE_HOME}/settings.json"

  # Simulate a machine that already ran once but ended up with the wrong
  # project path committed and pushed to the shared repo.
  git clone -q "${ORIGIN}" "${BROKEN_REPO_DIR}"
  source "${LIB_DIR}/machine-conf.sh"
  write_machine_conf "${BROKEN_REPO_DIR}/machines/host-broken.conf" "/wrong/path"
  git -C "${BROKEN_REPO_DIR}" -c user.email=test@test -c user.name=test add -A
  git -C "${BROKEN_REPO_DIR}" -c user.email=test@test -c user.name=test commit -q -m "seed broken conf"
  git -C "${BROKEN_REPO_DIR}" push -q origin master

  # And a stale skill-discovery symlink this tool created on a prior run.
  mkdir -p "${BROKEN_AGENTS_HOME}/skills/stale-skill" "${BROKEN_CLAUDE_HOME}/skills"
  ln -s "${BROKEN_AGENTS_HOME}/skills/stale-skill" "${BROKEN_CLAUDE_HOME}/skills/stale-skill"

  FAKE_BIN="${BATS_TEST_TMPDIR}/repair_fakebin"
  mkdir -p "${FAKE_BIN}"
  cat > "${FAKE_BIN}/npm" << 'EOF'
#!/usr/bin/env bash
exit 0
EOF
  chmod +x "${FAKE_BIN}/npm"

  CLAUDE_SYNC_REPO_URL="${ORIGIN}" \
  CLAUDE_SYNC_REPO_DIR="${BROKEN_REPO_DIR}" \
  CLAUDE_SYNC_HOSTNAME="host-broken" \
  CLAUDE_SYNC_PROJECT_PATH="/correct/path" \
  CLAUDE_SYNC_CLAUDE_HOME="${BROKEN_CLAUDE_HOME}" \
  CLAUDE_SYNC_AGENTS_HOME="${BROKEN_AGENTS_HOME}" \
  GIT_AUTHOR_NAME=test GIT_AUTHOR_EMAIL=test@test GIT_COMMITTER_NAME=test GIT_COMMITTER_EMAIL=test@test \
  PATH="${FAKE_BIN}:${PATH}" \
    "${REPO_ROOT}/install.sh" --repair

  # Local: the conf now has the corrected path, not the old broken one.
  grep -q "PRIMARY_PROJECT_PATH=/correct/path" "${BROKEN_REPO_DIR}/machines/host-broken.conf"

  # Remote: a fresh clone confirms the correction actually made it to
  # origin, not just the local working copy.
  CHECK_REPO="${BATS_TEST_TMPDIR}/repair_check"
  git clone -q "${ORIGIN}" "${CHECK_REPO}"
  grep -q "PRIMARY_PROJECT_PATH=/correct/path" "${CHECK_REPO}/machines/host-broken.conf"

  # The stale symlink from the prior, broken run is gone.
  [ ! -e "${BROKEN_CLAUDE_HOME}/skills/stale-skill" ]
}

@test "install.sh --repair succeeds when invoked from a shell whose cwd is inside the repo dir being deleted" {
  CWD_REPO_DIR="${BATS_TEST_TMPDIR}/cwd_repo_dir"
  CWD_CLAUDE_HOME="${BATS_TEST_TMPDIR}/cwd_claude_home"
  CWD_AGENTS_HOME="${BATS_TEST_TMPDIR}/cwd_agents_home"
  mkdir -p "${CWD_CLAUDE_HOME}" "${CWD_AGENTS_HOME}"

  git clone -q "${ORIGIN}" "${CWD_REPO_DIR}"

  FAKE_BIN="${BATS_TEST_TMPDIR}/cwd_fakebin"
  mkdir -p "${FAKE_BIN}"
  cat > "${FAKE_BIN}/npm" << 'EOF'
#!/usr/bin/env bash
exit 0
EOF
  chmod +x "${FAKE_BIN}/npm"

  # The natural way to invoke ~/claude-config/install.sh is from a shell
  # sitting inside ~/claude-config itself — assert repair survives that,
  # rather than the rm -rf breaking getcwd() for the rest of the script.
  run env \
    CLAUDE_SYNC_REPO_URL="${ORIGIN}" \
    CLAUDE_SYNC_REPO_DIR="${CWD_REPO_DIR}" \
    CLAUDE_SYNC_HOSTNAME="host-cwd" \
    CLAUDE_SYNC_PROJECT_PATH="/some/path" \
    CLAUDE_SYNC_CLAUDE_HOME="${CWD_CLAUDE_HOME}" \
    CLAUDE_SYNC_AGENTS_HOME="${CWD_AGENTS_HOME}" \
    GIT_AUTHOR_NAME=test GIT_AUTHOR_EMAIL=test@test GIT_COMMITTER_NAME=test GIT_COMMITTER_EMAIL=test@test \
    PATH="${FAKE_BIN}:${PATH}" \
    HOME="${BATS_TEST_TMPDIR}" \
    bash -c "cd '${CWD_REPO_DIR}' && exec '${REPO_ROOT}/install.sh' --repair"

  [ "$status" -eq 0 ]
  [[ "$output" != *"Unable to read current working directory"* ]]
  [ -f "${CWD_REPO_DIR}/machines/host-cwd.conf" ]
}

@test "install.sh --repair refuses when the local clone has uncommitted changes" {
  DIRTY_REPO_DIR="${BATS_TEST_TMPDIR}/dirty_repo_dir"
  git clone -q "${ORIGIN}" "${DIRTY_REPO_DIR}"
  echo "# local edit" >> "${DIRTY_REPO_DIR}/claude-sync.sh"

  run env \
    CLAUDE_SYNC_REPO_URL="${ORIGIN}" \
    CLAUDE_SYNC_REPO_DIR="${DIRTY_REPO_DIR}" \
    CLAUDE_SYNC_HOSTNAME="host-dirty" \
    "${REPO_ROOT}/install.sh" --repair

  [ "$status" -eq 1 ]
  [[ "$output" == *"uncommitted or untracked changes"* ]]
  # The dirty clone must survive untouched — repair refused, not repaired.
  [ -d "${DIRTY_REPO_DIR}/.git" ]
}

@test "install.sh --repair on a machine with no prior local state behaves like a normal fresh install" {
  NEVER_RUN_REPO_DIR="${BATS_TEST_TMPDIR}/never_run_repo_dir"
  NEVER_RUN_CLAUDE_HOME="${BATS_TEST_TMPDIR}/never_run_claude_home"
  NEVER_RUN_AGENTS_HOME="${BATS_TEST_TMPDIR}/never_run_agents_home"
  mkdir -p "${NEVER_RUN_CLAUDE_HOME}" "${NEVER_RUN_AGENTS_HOME}"
  echo "# never run before" > "${NEVER_RUN_CLAUDE_HOME}/CLAUDE.md"
  echo '{}' > "${NEVER_RUN_CLAUDE_HOME}/settings.json"

  FAKE_BIN="${BATS_TEST_TMPDIR}/never_run_fakebin"
  mkdir -p "${FAKE_BIN}"
  cat > "${FAKE_BIN}/npm" << 'EOF'
#!/usr/bin/env bash
exit 0
EOF
  chmod +x "${FAKE_BIN}/npm"

  CLAUDE_SYNC_REPO_URL="${ORIGIN}" \
  CLAUDE_SYNC_REPO_DIR="${NEVER_RUN_REPO_DIR}" \
  CLAUDE_SYNC_HOSTNAME="host-never-run" \
  CLAUDE_SYNC_PROJECT_PATH="/some/path" \
  CLAUDE_SYNC_CLAUDE_HOME="${NEVER_RUN_CLAUDE_HOME}" \
  CLAUDE_SYNC_AGENTS_HOME="${NEVER_RUN_AGENTS_HOME}" \
  GIT_AUTHOR_NAME=test GIT_AUTHOR_EMAIL=test@test GIT_COMMITTER_NAME=test GIT_COMMITTER_EMAIL=test@test \
  PATH="${FAKE_BIN}:${PATH}" \
    "${REPO_ROOT}/install.sh" --repair

  [ -f "${NEVER_RUN_REPO_DIR}/claude-sync.sh" ]
  [ -f "${NEVER_RUN_REPO_DIR}/machines/host-never-run.conf" ]
}

@test "bootstrap on a machine's first sync adopts shared config before contributing local-only content" {
  # Machine A has already synced real config to $ORIGIN, including a skill.
  mkdir -p "${AGENTS_HOME_A}/skills/shared-skill"
  echo "shared skill from A" > "${AGENTS_HOME_A}/skills/shared-skill/SKILL.md"

  CLAUDE_SYNC_CLAUDE_HOME="${CLAUDE_HOME_A}" \
  CLAUDE_SYNC_AGENTS_HOME="${AGENTS_HOME_A}" \
  CLAUDE_SYNC_HOSTNAME="host-a" \
  GIT_AUTHOR_NAME=test GIT_AUTHOR_EMAIL=test@test GIT_COMMITTER_NAME=test GIT_COMMITTER_EMAIL=test@test \
    "${REPO_A}/claude-sync.sh" push

  [ "$(cat "${REPO_A}/dot-agents/skills/shared-skill/SKILL.md")" = "shared skill from A" ]

  # Machine B: fresh clone of origin, with its own pre-existing local-only
  # skill that machine A's repo state does not have, plus a freshly created
  # machine conf (as install.sh would produce on first-time setup).
  git clone -q "${ORIGIN}" "${REPO_B}"
  mkdir -p "${CLAUDE_HOME_B}" "${AGENTS_HOME_B}/skills/local-only-b"
  echo "local only skill from B" > "${AGENTS_HOME_B}/skills/local-only-b/SKILL.md"
  source "${LIB_DIR}/machine-conf.sh"
  write_machine_conf "${REPO_B}/machines/host-b.conf" "/home/modem7/work"

  CLAUDE_SYNC_CLAUDE_HOME="${CLAUDE_HOME_B}" \
  CLAUDE_SYNC_AGENTS_HOME="${AGENTS_HOME_B}" \
  CLAUDE_SYNC_HOSTNAME="host-b" \
  GIT_AUTHOR_NAME=test GIT_AUTHOR_EMAIL=test@test GIT_COMMITTER_NAME=test GIT_COMMITTER_EMAIL=test@test \
    "${REPO_B}/claude-sync.sh" bootstrap

  # Machine B's real local paths now contain BOTH machine A's shared config
  # and machine B's own pre-existing local-only skill: nothing local-only
  # was deleted by the adopt step.
  [ "$(cat "${CLAUDE_HOME_B}/CLAUDE.md")" = "# from machine A" ]
  [ "$(cat "${AGENTS_HOME_B}/skills/shared-skill/SKILL.md")" = "shared skill from A" ]
  [ "$(cat "${AGENTS_HOME_B}/skills/local-only-b/SKILL.md")" = "local only skill from B" ]

  # After bootstrap pushes, a fresh clone of origin shows machine B's
  # local-only skill was captured and shared too, not just preserved locally.
  CHECK="${BATS_TEST_TMPDIR}/check_bootstrap"
  git clone -q "${ORIGIN}" "${CHECK}"
  [ "$(cat "${CHECK}/dot-agents/skills/local-only-b/SKILL.md")" = "local only skill from B" ]
  [ "$(cat "${CHECK}/dot-agents/skills/shared-skill/SKILL.md")" = "shared skill from A" ]
}

sync_as_machine_a() {
  CLAUDE_SYNC_CLAUDE_HOME="${CLAUDE_HOME_A}" \
  CLAUDE_SYNC_AGENTS_HOME="${AGENTS_HOME_A}" \
  CLAUDE_SYNC_HOSTNAME="host-a" \
  GIT_AUTHOR_NAME=test GIT_AUTHOR_EMAIL=test@test GIT_COMMITTER_NAME=test GIT_COMMITTER_EMAIL=test@test \
    "${REPO_A}/claude-sync.sh" "$@"
}

doctor_as_machine_a() {
  CLAUDE_SYNC_CLAUDE_HOME="${CLAUDE_HOME_A}" \
  CLAUDE_SYNC_AGENTS_HOME="${AGENTS_HOME_A}" \
  CLAUDE_SYNC_HOSTNAME="host-a" \
  GIT_AUTHOR_NAME=test GIT_AUTHOR_EMAIL=test@test GIT_COMMITTER_NAME=test GIT_COMMITTER_EMAIL=test@test \
    "${REPO_A}/claude-sync.sh" doctor "$@"
}

@test "doctor reports healthy on a repo that's fully in sync" {
  sync_as_machine_a sync

  run doctor_as_machine_a
  [ "$status" -eq 0 ]
  [[ "$output" == *"No issues found"* ]]
}

@test "doctor detects a stuck rebase without touching it unless told to remediate" {
  sync_as_machine_a sync

  # Create a genuine stuck rebase (not a fake empty directory - git rebase
  # --abort only cleans up a real one): both sides edit CLAUDE.md's same
  # line, then rebase directly (bypassing do_integrate's own auto-abort) so
  # it pauses mid-conflict for real.
  REMOTE_EDIT="${BATS_TEST_TMPDIR}/remote_edit_rebase"
  git clone -q "${ORIGIN}" "${REMOTE_EDIT}"
  echo "# from origin" > "${REMOTE_EDIT}/dot-claude/CLAUDE.md"
  git -C "${REMOTE_EDIT}" -c user.email=test@test -c user.name=test commit -q -am "remote CLAUDE.md change"
  git -C "${REMOTE_EDIT}" push -q origin master

  echo "# from stuck local edit" > "${REPO_A}/dot-claude/CLAUDE.md"
  git -C "${REPO_A}" -c user.email=test@test -c user.name=test commit -q -am "local CLAUDE.md change"
  git -C "${REPO_A}" fetch -q origin
  run git -C "${REPO_A}" rebase origin/master
  [ "$status" -ne 0 ]
  [ -d "${REPO_A}/.git/rebase-merge" ]

  run doctor_as_machine_a
  [ "$status" -ne 0 ]
  [[ "$output" == *"mid-rebase"* ]]
  [ -d "${REPO_A}/.git/rebase-merge" ]
}

@test "doctor remediate clears a stuck rebase-merge directory" {
  sync_as_machine_a sync

  REMOTE_EDIT="${BATS_TEST_TMPDIR}/remote_edit_rebase"
  git clone -q "${ORIGIN}" "${REMOTE_EDIT}"
  echo "# from origin" > "${REMOTE_EDIT}/dot-claude/CLAUDE.md"
  git -C "${REMOTE_EDIT}" -c user.email=test@test -c user.name=test commit -q -am "remote CLAUDE.md change"
  git -C "${REMOTE_EDIT}" push -q origin master

  echo "# from stuck local edit" > "${REPO_A}/dot-claude/CLAUDE.md"
  git -C "${REPO_A}" -c user.email=test@test -c user.name=test commit -q -am "local CLAUDE.md change"
  git -C "${REPO_A}" fetch -q origin
  git -C "${REPO_A}" rebase origin/master || true
  [ -d "${REPO_A}/.git/rebase-merge" ]

  run doctor_as_machine_a remediate
  [[ "$output" == *"aborted the stuck rebase"* ]]
  [ ! -d "${REPO_A}/.git/rebase-merge" ]
}

@test "doctor remediate refuses to silently discard a genuine content conflict" {
  # Baseline: machine A pushes settings.json with theme=dark.
  sync_as_machine_a sync
  [ "$(cat "${CLAUDE_HOME_A}/settings.json")" = '{"theme":"dark"}' ]

  # Simulate a PR merged straight to origin (bypassing claude-sync), changing
  # the same "theme" key - exactly what happened repeatedly this session.
  REMOTE_EDIT="${BATS_TEST_TMPDIR}/remote_edit"
  git clone -q "${ORIGIN}" "${REMOTE_EDIT}"
  echo '{"theme":"light"}' > "${REMOTE_EDIT}/dot-claude/settings.json"
  git -C "${REMOTE_EDIT}" -c user.email=test@test -c user.name=test commit -q -am "remote theme change"
  git -C "${REMOTE_EDIT}" push -q origin master

  # Machine A independently edits the same key differently, then syncs -
  # do_capture_and_commit commits it locally, do_integrate's rebase hits a
  # real conflict against the remote change above and aborts.
  echo '{"theme":"custom"}' > "${CLAUDE_HOME_A}/settings.json"
  run sync_as_machine_a sync
  [ "$status" -ne 0 ]

  # Confirm the deadlock actually exists: ahead of and behind the upstream.
  run doctor_as_machine_a
  [ "$status" -ne 0 ]
  [[ "$output" == *"ahead"* ]]
  [[ "$output" == *"behind"* ]]

  run doctor_as_machine_a remediate
  [[ "$output" == *"real conflict"* ]]
  [[ "$output" == *"Resolve manually"* ]]
  # Local file was never silently overwritten with either side's content.
  [ "$(cat "${CLAUDE_HOME_A}/settings.json")" = '{"theme":"custom"}' ]
}

@test "doctor remediate refreshes local settings.json when only local is stale" {
  sync_as_machine_a sync
  # Local file drifts (e.g. a manual local edit) without a sync capturing it -
  # the repo clone's dot-claude/settings.json is untouched and still correct.
  echo '{"theme":"stale-local-edit"}' > "${CLAUDE_HOME_A}/settings.json"

  run doctor_as_machine_a
  [ "$status" -ne 0 ]
  [[ "$output" == *"differs from the repo's"* ]]

  run doctor_as_machine_a remediate
  [[ "$output" == *"re-applied the repo's settings.json"* ]]
  [ "$(cat "${CLAUDE_HOME_A}/settings.json")" = '{"theme":"dark"}' ]
}
