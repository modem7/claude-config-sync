#!/usr/bin/env bats
# Per-machine profiles (machines/<profile>/). Placeholder hostnames only:
# this file is published to the public mirror.

setup() {
  load 'test_helper'
  install_fake_gh

  ORIGIN="${BATS_TEST_TMPDIR}/origin.git"
  git init --bare -q "${ORIGIN}"
  git -C "${ORIGIN}" symbolic-ref HEAD refs/heads/master

  SEED="${BATS_TEST_TMPDIR}/seed"
  git clone -q "${ORIGIN}" "${SEED}" 2>/dev/null
  mkdir -p "${SEED}/lib" "${SEED}/machines" "${SEED}/dot-claude/agents" "${SEED}/dot-claude/hooks" \
           "${SEED}/dot-agents/skills/shared-skill" "${SEED}/dot-agents/skills/other-skill" "${SEED}/memory"
  cp "${REPO_ROOT}/claude-sync.sh" "${SEED}/claude-sync.sh"
  cp "${REPO_ROOT}"/lib/*.sh "${SEED}/lib/"
  chmod +x "${SEED}/claude-sync.sh"
  touch "${SEED}/machines/.gitkeep" "${SEED}/dot-claude/agents/.gitkeep" \
        "${SEED}/dot-claude/hooks/.gitkeep" "${SEED}/memory/.gitkeep"
  printf '# shared instructions\n' > "${SEED}/dot-claude/CLAUDE.md"
  echo '{"theme":"dark"}' > "${SEED}/dot-claude/settings.json"
  echo "shared helper" > "${SEED}/dot-claude/agents/helper.md"
  echo "shared reviewer" > "${SEED}/dot-claude/agents/reviewer.md"
  printf '#!/bin/bash\necho shared guard\n' > "${SEED}/dot-claude/hooks/guard.sh"
  chmod +x "${SEED}/dot-claude/hooks/guard.sh"
  echo "shared skill" > "${SEED}/dot-agents/skills/shared-skill/SKILL.md"
  echo "only in the shared copy" > "${SEED}/dot-agents/skills/shared-skill/notes.md"
  echo "other skill" > "${SEED}/dot-agents/skills/other-skill/SKILL.md"
  echo "shared memory" > "${SEED}/memory/MEMORY.md"
  source "${LIB_DIR}/machine-conf.sh"
  write_machine_conf "${SEED}/machines/host-a.conf" "/home/user/project"
  write_machine_conf "${SEED}/machines/host-b.conf" "/home/user/project"
  git -C "${SEED}" add -A
  git -C "${SEED}" -c user.email=test@test -c user.name=test commit -q -m seed
  git -C "${SEED}" push -q origin master 2>/dev/null

  REPO_A="${BATS_TEST_TMPDIR}/repo_a"
  CLAUDE_HOME_A="${BATS_TEST_TMPDIR}/claude_home_a"
  AGENTS_HOME_A="${BATS_TEST_TMPDIR}/agents_home_a"
  REPO_B="${BATS_TEST_TMPDIR}/repo_b"
  CLAUDE_HOME_B="${BATS_TEST_TMPDIR}/claude_home_b"
  AGENTS_HOME_B="${BATS_TEST_TMPDIR}/agents_home_b"
  PA="${REPO_A}/machines/host-a"
}

raw_as() {  # raw_as a|b CMD [ARGS...]: runs claude-sync.sh as that machine
  local m="$1"; shift
  local home agents repo
  if [ "$m" = a ]; then home="${CLAUDE_HOME_A}" agents="${AGENTS_HOME_A}" repo="${REPO_A}"
  else home="${CLAUDE_HOME_B}" agents="${AGENTS_HOME_B}" repo="${REPO_B}"; fi
  CLAUDE_SYNC_CLAUDE_HOME="${home}" CLAUDE_SYNC_AGENTS_HOME="${agents}" CLAUDE_SYNC_HOSTNAME="host-${m}" \
  GIT_AUTHOR_NAME=test GIT_AUTHOR_EMAIL=test@test GIT_COMMITTER_NAME=test GIT_COMMITTER_EMAIL=test@test \
    "${repo}/claude-sync.sh" "$@"
}

# Like raw_as, but once a repo-changing command succeeds its sync PR is
# merged and the machine syncs again, as if the change was shared at once.
sync_as() {  # sync_as a|b CMD [ARGS...]
  local m="$1" rc=0
  shift
  raw_as "$m" "$@" || rc=$?
  [ "${rc}" -eq 0 ] || return "${rc}"
  case "${1:-}" in
    push|sync|bootstrap|override|profile) merge_and_converge "${ORIGIN}" "host-${m}" raw_as "$m" ;;
  esac
}

# Edits origin from a scratch clone, as another machine or a merged PR
# would: edit_origin, change files under ${EDIT}, then push_to_origin MSG.
edit_origin() {
  EDIT="${BATS_TEST_TMPDIR}/edit"
  [ -d "${EDIT}/.git" ] || git clone -q "${ORIGIN}" "${EDIT}" 2>/dev/null
  git -C "${EDIT}" pull -q 2>/dev/null
}
push_to_origin() {
  git -C "${EDIT}" add -A
  git -C "${EDIT}" -c user.email=test@test -c user.name=test commit -q -m "$1"
  git -C "${EDIT}" push -q origin master 2>/dev/null
}

# A machine with the shared config applied and no profile yet.
start_machine() {  # start_machine a|b
  local m="$1" repo home agents
  if [ "$m" = a ]; then repo="${REPO_A}" home="${CLAUDE_HOME_A}" agents="${AGENTS_HOME_A}"
  else repo="${REPO_B}" home="${CLAUDE_HOME_B}" agents="${AGENTS_HOME_B}"; fi
  [ -d "${repo}/.git" ] || git clone -q "${ORIGIN}" "${repo}" 2>/dev/null
  mkdir -p "${home}" "${agents}/skills"
  sync_as "$m" pull >/dev/null 2>&1
}

origin_has() { git -C "${ORIGIN}" cat-file -e "master:$1" 2>/dev/null; }
origin_cat() { git -C "${ORIGIN}" show "master:$1"; }

# ── Regression: capture-before-apply must not clobber a profile entry ────

@test "a restored machine's first sync keeps its unapplied profile entry instead of overwriting it with the shared one" {
  edit_origin
  mkdir -p "${EDIT}/machines/host-b/agents"
  echo "host-b helper" > "${EDIT}/machines/host-b/agents/helper.md"
  push_to_origin "host-b profile"

  # A rebuilt machine: same hostname, fresh clone, live files still hold the
  # shared version (restored from a backup), and no apply has run yet.
  git clone -q "${ORIGIN}" "${REPO_B}" 2>/dev/null
  mkdir -p "${CLAUDE_HOME_B}/agents" "${AGENTS_HOME_B}/skills"
  echo "shared helper" > "${CLAUDE_HOME_B}/agents/helper.md"

  sync_as b sync >/dev/null

  [ "$(cat "${REPO_B}/machines/host-b/agents/helper.md")" = "host-b helper" ]
  [ "$(origin_cat machines/host-b/agents/helper.md)" = "host-b helper" ]
  [ "$(origin_cat dot-claude/agents/helper.md)" = "shared helper" ]
  [ "$(cat "${CLAUDE_HOME_B}/agents/helper.md")" = "host-b helper" ]
}

# ── Apply: replace, add, disable ─────────────────────────────────────────

@test "agents: a profile replaces, adds and disables entries without leaking into dot-claude" {
  start_machine a
  mkdir -p "${PA}/agents"
  echo "host-a helper" > "${PA}/agents/helper.md"
  echo "host-a only" > "${PA}/agents/only-a.md"
  touch "${PA}/agents/reviewer.md.disabled"

  sync_as a sync >/dev/null

  [ "$(cat "${CLAUDE_HOME_A}/agents/helper.md")" = "host-a helper" ]
  [ "$(cat "${CLAUDE_HOME_A}/agents/only-a.md")" = "host-a only" ]
  [ ! -e "${CLAUDE_HOME_A}/agents/reviewer.md" ]
  [ "$(origin_cat dot-claude/agents/helper.md)" = "shared helper" ]
  [ "$(origin_cat dot-claude/agents/reviewer.md)" = "shared reviewer" ]
  refute origin_has dot-claude/agents/only-a.md
  origin_has machines/host-a/agents/reviewer.md.disabled

  # A second sync with nothing changed commits nothing and still leaks nothing.
  local before; before="$(git -C "${REPO_A}" rev-parse HEAD)"
  sync_as a sync >/dev/null
  [ "$(git -C "${REPO_A}" rev-parse HEAD)" = "${before}" ]
  [ ! -e "${REPO_A}/dot-claude/agents/only-a.md" ]
  [ "$(cat "${REPO_A}/dot-claude/agents/helper.md")" = "shared helper" ]

  run sync_as a doctor
  [ "$status" -eq 0 ]
  [[ "$output" == *"healthy"* ]]
}

@test "hooks: a profile hook replaces the shared one and keeps its executable bit; disabled hooks go" {
  start_machine a
  mkdir -p "${PA}/hooks"
  printf '#!/bin/bash\necho host-a guard\n' > "${PA}/hooks/guard.sh"
  printf '#!/bin/bash\necho host-a extra\n' > "${PA}/hooks/extra.sh"
  chmod +x "${PA}/hooks/guard.sh" "${PA}/hooks/extra.sh"

  sync_as a sync >/dev/null

  [ -x "${CLAUDE_HOME_A}/hooks/guard.sh" ]
  [ -x "${CLAUDE_HOME_A}/hooks/extra.sh" ]
  grep -q "host-a guard" "${CLAUDE_HOME_A}/hooks/guard.sh"
  grep -q "shared guard" "${REPO_A}/dot-claude/hooks/guard.sh"
  [ ! -e "${REPO_A}/dot-claude/hooks/extra.sh" ]
  [ "$(git -C "${ORIGIN}" ls-tree master machines/host-a/hooks/extra.sh | cut -c1-6)" = "100755" ]

  git -C "${REPO_A}" rm -q -r machines/host-a/hooks
  mkdir -p "${PA}/hooks"
  touch "${PA}/hooks/guard.sh.disabled"
  sync_as a sync >/dev/null
  [ ! -e "${CLAUDE_HOME_A}/hooks/guard.sh" ]
  [ ! -e "${CLAUDE_HOME_A}/hooks/extra.sh" ]
  [ -x "${REPO_A}/dot-claude/hooks/guard.sh" ]
  origin_has dot-claude/hooks/guard.sh
}

@test "skills: a profile skill replaces the whole shared directory, and a disabled skill loses its link" {
  start_machine a
  [ -L "${CLAUDE_HOME_A}/skills/other-skill" ]
  mkdir -p "${PA}/skills/shared-skill" "${PA}/skills/only-a"
  echo "host-a skill" > "${PA}/skills/shared-skill/SKILL.md"
  echo "host-a only skill" > "${PA}/skills/only-a/SKILL.md"
  touch "${PA}/skills/other-skill.disabled"

  sync_as a sync >/dev/null

  [ "$(cat "${AGENTS_HOME_A}/skills/shared-skill/SKILL.md")" = "host-a skill" ]
  # Replaced, never merged: the shared copy's extra file is gone.
  [ ! -e "${AGENTS_HOME_A}/skills/shared-skill/notes.md" ]
  [ "$(cat "${AGENTS_HOME_A}/skills/only-a/SKILL.md")" = "host-a only skill" ]
  [ -L "${CLAUDE_HOME_A}/skills/only-a" ]
  [ ! -e "${AGENTS_HOME_A}/skills/other-skill" ]
  [ ! -L "${CLAUDE_HOME_A}/skills/other-skill" ]

  [ "$(origin_cat dot-agents/skills/shared-skill/SKILL.md)" = "shared skill" ]
  origin_has dot-agents/skills/shared-skill/notes.md
  origin_has dot-agents/skills/other-skill/SKILL.md
  refute origin_has dot-agents/skills/only-a/SKILL.md
}

@test "CLAUDE.md: the append block is added after the marker and stripped before capture" {
  start_machine a
  mkdir -p "${PA}"
  printf 'host-a only\n' > "${PA}/CLAUDE.append.md"

  sync_as a sync >/dev/null

  printf '# shared instructions\n\n<!-- claude-sync:profile-append -->\nhost-a only\n' > "${BATS_TEST_TMPDIR}/want"
  cmp "${CLAUDE_HOME_A}/CLAUDE.md" "${BATS_TEST_TMPDIR}/want"
  origin_cat dot-claude/CLAUDE.md > "${BATS_TEST_TMPDIR}/shared"
  printf '# shared instructions\n' | cmp - "${BATS_TEST_TMPDIR}/shared"

  # A shared edit above the block still flows to the shared file.
  printf '# shared instructions\nmore\n\n<!-- claude-sync:profile-append -->\nhost-a only\n' > "${CLAUDE_HOME_A}/CLAUDE.md"
  sync_as a sync >/dev/null
  origin_cat dot-claude/CLAUDE.md > "${BATS_TEST_TMPDIR}/shared"
  printf '# shared instructions\nmore\n' | cmp - "${BATS_TEST_TMPDIR}/shared"
  [ "$(cat "${PA}/CLAUDE.append.md")" = "host-a only" ]
}

@test "CLAUDE.md round-trips byte for byte when the shared file has no trailing newline" {
  edit_origin
  printf 'no newline' > "${EDIT}/dot-claude/CLAUDE.md"
  push_to_origin "no trailing newline"
  start_machine a
  mkdir -p "${PA}"
  printf 'host-a only' > "${PA}/CLAUDE.append.md"

  sync_as a sync >/dev/null
  sync_as a sync >/dev/null

  origin_cat dot-claude/CLAUDE.md > "${BATS_TEST_TMPDIR}/shared"
  printf 'no newline' | cmp - "${BATS_TEST_TMPDIR}/shared"
  printf 'host-a only' | cmp - "${PA}/CLAUDE.append.md"
}

# ── Capture: local edits, removals ───────────────────────────────────────

@test "a local edit to an applied profile entry goes back into the profile, not the shared copy" {
  start_machine a
  mkdir -p "${PA}/agents"
  echo "host-a helper" > "${PA}/agents/helper.md"
  printf 'host-a only\n' > "${PA}/CLAUDE.append.md"
  sync_as a sync >/dev/null

  echo "host-a helper, edited" > "${CLAUDE_HOME_A}/agents/helper.md"
  printf '# shared instructions\n\n<!-- claude-sync:profile-append -->\nhost-a edited\n' > "${CLAUDE_HOME_A}/CLAUDE.md"
  sync_as a sync >/dev/null

  [ "$(origin_cat machines/host-a/agents/helper.md)" = "host-a helper, edited" ]
  [ "$(origin_cat dot-claude/agents/helper.md)" = "shared helper" ]
  [ "$(origin_cat machines/host-a/CLAUDE.append.md)" = "host-a edited" ]
  [ "$(origin_cat dot-claude/CLAUDE.md)" = "# shared instructions" ]

  # The written-back state now counts as applied: no churn.
  local before; before="$(git -C "${REPO_A}" rev-parse HEAD)"
  sync_as a sync >/dev/null
  [ "$(git -C "${REPO_A}" rev-parse HEAD)" = "${before}" ]
}

@test "an entry removed from the profile reverts to the shared version, and its stale copy is not captured" {
  start_machine a
  mkdir -p "${PA}/agents"
  echo "host-a helper" > "${PA}/agents/helper.md"
  echo "host-a only" > "${PA}/agents/only-a.md"
  sync_as a sync >/dev/null

  git -C "${REPO_A}" rm -q machines/host-a/agents/helper.md machines/host-a/agents/only-a.md
  sync_as a sync >/dev/null

  [ "$(cat "${CLAUDE_HOME_A}/agents/helper.md")" = "shared helper" ]
  [ ! -e "${CLAUDE_HOME_A}/agents/only-a.md" ]
  [ "$(origin_cat dot-claude/agents/helper.md)" = "shared helper" ]
  refute origin_has dot-claude/agents/only-a.md
}

@test "a disabled shared entry is left alone in the repo even though it is missing locally" {
  start_machine a
  mkdir -p "${PA}/agents"
  touch "${PA}/agents/reviewer.md.disabled"
  sync_as a sync >/dev/null
  git -C "${REPO_A}" rm -q machines/host-a/agents/reviewer.md.disabled
  sync_as a sync >/dev/null
  [ "$(cat "${CLAUDE_HOME_A}/agents/reviewer.md")" = "shared reviewer" ]
  [ "$(origin_cat dot-claude/agents/reviewer.md)" = "shared reviewer" ]
}

# ── Stale clone check (live_matches_commit) ──────────────────────────────

pull_newer_shared_hook() {
  edit_origin
  printf '#!/bin/bash\necho newer\n' > "${EDIT}/dot-claude/hooks/newer.sh"
  chmod +x "${EDIT}/dot-claude/hooks/newer.sh"
  push_to_origin "newer hook"
  git -C "${REPO_A}" pull -q --ff-only 2>/dev/null
}

@test "a clone that moved without an apply is not mistaken for local edits when a profile is applied" {
  start_machine a
  mkdir -p "${PA}/agents" "${PA}/skills/shared-skill"
  echo "host-a helper" > "${PA}/agents/helper.md"
  echo "host-a skill" > "${PA}/skills/shared-skill/SKILL.md"
  touch "${PA}/agents/reviewer.md.disabled"
  printf 'host-a only\n' > "${PA}/CLAUDE.append.md"
  sync_as a sync >/dev/null
  pull_newer_shared_hook

  run sync_as a sync
  [ "$status" -eq 0 ]
  [[ "$output" == *"skipping capture"* ]]
  [[ "$output" != *"refusing"* ]]
  [ -x "${CLAUDE_HOME_A}/hooks/newer.sh" ]
  [ "$(cat "${CLAUDE_HOME_A}/agents/helper.md")" = "host-a helper" ]
}

@test "after a profile edit is written back, a moved clone still skips capture rather than refusing" {
  start_machine a
  mkdir -p "${PA}/agents"
  echo "host-a helper" > "${PA}/agents/helper.md"
  sync_as a sync >/dev/null
  echo "host-a helper, edited" > "${CLAUDE_HOME_A}/agents/helper.md"
  sync_as a sync >/dev/null
  pull_newer_shared_hook

  run sync_as a sync
  [ "$status" -eq 0 ]
  [[ "$output" != *"refusing"* ]]
  [ "$(cat "${CLAUDE_HOME_A}/agents/helper.md")" = "host-a helper, edited" ]
}

@test "a moved clone plus an unsynced edit to a profile entry still refuses" {
  start_machine a
  mkdir -p "${PA}/agents"
  echo "host-a helper" > "${PA}/agents/helper.md"
  sync_as a sync >/dev/null
  pull_newer_shared_hook
  echo "unsynced edit" > "${CLAUDE_HOME_A}/agents/helper.md"

  run sync_as a sync
  [ "$status" -ne 0 ]
  [[ "$output" == *"refusing to capture"* ]]
  [ "$(cat "${CLAUDE_HOME_A}/agents/helper.md")" = "unsynced edit" ]
}

# ── PROFILE= pointer, settings override follows the profile ──────────────

@test "PROFILE= makes a machine follow another profile, including its settings override" {
  start_machine a
  mkdir -p "${PA}/agents"
  echo "host-a helper" > "${PA}/agents/helper.md"
  echo '{"theme":"light"}' > "${REPO_A}/machines/host-a.settings.json"
  sync_as a sync >/dev/null

  git clone -q "${ORIGIN}" "${REPO_B}" 2>/dev/null
  source "${LIB_DIR}/machine-conf.sh"
  write_machine_profile "${REPO_B}/machines/host-b.conf" host-a
  git -C "${REPO_B}" -c user.email=test@test -c user.name=test commit -q -am "host-b follows host-a"
  start_machine b

  [ "$(cat "${CLAUDE_HOME_B}/agents/helper.md")" = "host-a helper" ]
  [ "$(jq -r .theme "${CLAUDE_HOME_B}/settings.json")" = "light" ]

  # Edits on the follower go to the profile it follows.
  echo "edited on host-b" > "${CLAUDE_HOME_B}/agents/helper.md"
  sync_as b sync >/dev/null
  [ "$(origin_cat machines/host-a/agents/helper.md)" = "edited on host-b" ]
  [ "$(origin_cat dot-claude/agents/helper.md)" = "shared helper" ]
  refute origin_has machines/host-b
}

# ── profile list / show / use / copy ─────────────────────────────────────

seed_host_a_profile() {
  start_machine a
  mkdir -p "${PA}/agents" "${PA}/hooks"
  echo "host-a helper" > "${PA}/agents/helper.md"
  touch "${PA}/agents/reviewer.md.disabled"
  printf '#!/bin/bash\necho extra\n' > "${PA}/hooks/extra.sh"
  chmod +x "${PA}/hooks/extra.sh"
  printf 'host-a only\n' > "${PA}/CLAUDE.append.md"
  echo '{"theme":"light"}' > "${REPO_A}/machines/host-a.settings.json"
  sync_as a sync >/dev/null
  start_machine b
}

@test "profile list and show describe every entry, marker and append" {
  seed_host_a_profile

  run sync_as a profile list
  [ "$status" -eq 0 ]
  [[ "$output" == *"host-a uses its own profile 'host-a'"* ]]
  [[ "$output" == *"agents/helper.md  (replaces the shared one)"* ]]
  [[ "$output" == *"agents/reviewer.md  (disabled)"* ]]
  [[ "$output" == *"hooks/extra.sh  (machine-only)"* ]]
  [[ "$output" == *"CLAUDE.append.md"* ]]
  [[ "$output" == *"host-a.settings.json"* ]]

  run sync_as b profile show host-a
  [ "$status" -eq 0 ]
  [[ "$output" == *"agents/helper.md"* ]]

  run sync_as b profile list
  [[ "$output" == *"(nothing"* ]]
}

@test "profile use points the machine at another profile and applies it" {
  seed_host_a_profile

  run sync_as b profile use no-such-profile
  [ "$status" -ne 0 ]
  [[ "$output" == *"does not exist"* ]]

  run sync_as b profile use host-a
  [ "$status" -eq 0 ]
  grep -qx 'PROFILE=host-a' "${REPO_B}/machines/host-b.conf"
  origin_cat machines/host-b.conf | grep -qx 'PROFILE=host-a'
  [ "$(cat "${CLAUDE_HOME_B}/agents/helper.md")" = "host-a helper" ]
  [ "$(jq -r .theme "${CLAUDE_HOME_B}/settings.json")" = "light" ]

  run sync_as b profile list
  [[ "$output" == *"host-b follows profile 'host-a'"* ]]

  # Back to its own profile: the PROFILE= line goes.
  sync_as b profile use host-b >/dev/null
  refute grep -q '^PROFILE=' "${REPO_B}/machines/host-b.conf"
  [ "$(cat "${CLAUDE_HOME_B}/agents/helper.md")" = "shared helper" ]
}

@test "profile copy shows the hooks it copies, asks first, and refuses to overwrite without --force" {
  seed_host_a_profile

  run sync_as b profile copy host-a <<<"n"
  [ "$status" -ne 0 ]
  [[ "$output" == *"machines/host-a/hooks/extra.sh"* ]]
  [[ "$output" == *"Aborted"* ]]
  [ ! -e "${REPO_B}/machines/host-b" ]

  run sync_as b profile copy host-a <<<"y"
  [ "$status" -eq 0 ]
  [ "$(cat "${REPO_B}/machines/host-b/agents/helper.md")" = "host-a helper" ]
  [ -x "${REPO_B}/machines/host-b/hooks/extra.sh" ]
  [ "$(jq -c . "${REPO_B}/machines/host-b.settings.json")" = '{"theme":"light"}' ]
  origin_has machines/host-b/agents/helper.md
  [ "$(cat "${CLAUDE_HOME_B}/agents/helper.md")" = "host-a helper" ]
  [ "$(jq -r .theme "${CLAUDE_HOME_B}/settings.json")" = "light" ]

  # Independent: editing the copy leaves host-a's profile alone.
  echo "host-b's own helper" > "${CLAUDE_HOME_B}/agents/helper.md"
  sync_as b sync >/dev/null
  [ "$(origin_cat machines/host-b/agents/helper.md)" = "host-b's own helper" ]
  [ "$(origin_cat machines/host-a/agents/helper.md)" = "host-a helper" ]

  run sync_as b profile copy host-a --yes
  [ "$status" -ne 0 ]
  [[ "$output" == *"--force"* ]]

  run sync_as b profile copy host-a --force --yes
  [ "$status" -eq 0 ]
  [ "$(origin_cat machines/host-b/agents/helper.md)" = "host-a helper" ]
}

@test "profile copy drops a PROFILE= line so the copy takes effect" {
  seed_host_a_profile
  sync_as b profile use host-a >/dev/null
  run sync_as b profile copy host-a --yes
  [ "$status" -eq 0 ]
  refute grep -q '^PROFILE=' "${REPO_B}/machines/host-b.conf"
  [ -d "${REPO_B}/machines/host-b" ]
}

@test "profile copy and use are skipped on a feature branch; list still works" {
  seed_host_a_profile
  git -C "${REPO_B}" checkout -q -b some-feature
  run sync_as b profile use host-a
  [ "$status" -eq 0 ]
  [[ "$output" == *"not its default branch"* ]]
  refute grep -q '^PROFILE=' "${REPO_B}/machines/host-b.conf"
  run sync_as b profile list
  [[ "$output" == *"host-b uses its own profile"* ]]
}

# ── doctor ───────────────────────────────────────────────────────────────

@test "doctor reports a .disabled marker that hides nothing" {
  start_machine a
  mkdir -p "${PA}/agents"
  touch "${PA}/agents/no-such-agent.md.disabled"
  sync_as a sync >/dev/null

  run sync_as a doctor
  [ "$status" -ne 0 ]
  [[ "$output" == *"no-such-agent.md.disabled hides nothing"* ]]
}

@test "doctor reports a live/profile mismatch and remediate re-applies a pulled but unapplied profile" {
  start_machine a
  mkdir -p "${PA}/agents"
  echo "host-a helper" > "${PA}/agents/helper.md"
  sync_as a sync >/dev/null
  # As if a pull brought a newer profile entry but nothing applied it.
  echo "host-a helper v2" > "${PA}/agents/helper.md"

  run sync_as a doctor
  [ "$status" -ne 0 ]
  [[ "$output" == *"agents/helper.md"* ]]

  run sync_as a doctor remediate
  [[ "$output" == *"re-applied the shared config and profile"* ]]
  [ "$(cat "${CLAUDE_HOME_A}/agents/helper.md")" = "host-a helper v2" ]
}

@test "doctor remediate does not discard a local edit to a profile entry" {
  start_machine a
  mkdir -p "${PA}/agents"
  echo "host-a helper" > "${PA}/agents/helper.md"
  sync_as a sync >/dev/null
  echo "local edit" > "${CLAUDE_HOME_A}/agents/helper.md"

  run sync_as a doctor remediate
  [[ "$output" == *"not re-applied"* ]]
  [ "$(cat "${CLAUDE_HOME_A}/agents/helper.md")" = "local edit" ]
}

@test "doctor reports a settings.json hook command whose script does not exist" {
  edit_origin
  cat > "${EDIT}/dot-claude/settings.json" <<'EOF'
{"theme":"dark","hooks":{"PreToolUse":[{"matcher":"Bash","hooks":[{"type":"command","command":"\"$HOME/.claude/hooks/guard.sh\""}]}]}}
EOF
  push_to_origin "wire guard hook"
  start_machine a
  run sync_as a doctor
  [ "$status" -eq 0 ]

  mkdir -p "${PA}/hooks"
  touch "${PA}/hooks/guard.sh.disabled"
  sync_as a sync >/dev/null
  run sync_as a doctor
  [ "$status" -ne 0 ]
  [[ "$output" == *"hooks/guard.sh, which does not exist"* ]]
}

# ── install.sh ───────────────────────────────────────────────────────────

run_install() {  # run_install HOST REPO_DIR CLAUDE_HOME AGENTS_HOME [ARGS...]
  local host="$1" repo="$2" home="$3" agents="$4"; shift 4
  mkdir -p "${home}" "${agents}/skills"
  env CLAUDE_SYNC_REPO_URL="${ORIGIN}" CLAUDE_SYNC_REPO_DIR="${repo}" CLAUDE_SYNC_HOSTNAME="${host}" \
    CLAUDE_SYNC_PROJECT_PATH="/home/user/project" CLAUDE_SYNC_CLAUDE_HOME="${home}" \
    CLAUDE_SYNC_AGENTS_HOME="${agents}" HOME="${BATS_TEST_TMPDIR}" \
    GIT_AUTHOR_NAME=test GIT_AUTHOR_EMAIL=test@test GIT_COMMITTER_NAME=test GIT_COMMITTER_EMAIL=test@test \
    "${REPO_ROOT}/install.sh" "$@"
}

@test "install.sh --profile writes PROFILE= into a new machine's conf and applies that profile" {
  seed_host_a_profile
  local repo="${BATS_TEST_TMPDIR}/repo_c" home="${BATS_TEST_TMPDIR}/claude_home_c"

  run run_install host-c "${repo}" "${home}" "${BATS_TEST_TMPDIR}/agents_home_c" --profile host-a
  [ "$status" -eq 0 ]
  grep -qx 'PROFILE=host-a' "${repo}/machines/host-c.conf"
  [ "$(cat "${home}/agents/helper.md")" = "host-a helper" ]
  refute origin_has machines/host-c
  # Its first sync went out as a PR, which holds the conf but no profile dir.
  git -C "${ORIGIN}" cat-file -e sync/host-c:machines/host-c.conf
  refute git -C "${ORIGIN}" cat-file -e sync/host-c:machines/host-c
}

@test "install.sh --profile refuses a profile that does not exist" {
  run run_install host-c "${BATS_TEST_TMPDIR}/repo_c" "${BATS_TEST_TMPDIR}/claude_home_c" \
    "${BATS_TEST_TMPDIR}/agents_home_c" --profile no-such-profile
  [ "$status" -ne 0 ]
  [[ "$output" == *"does not exist"* ]]
  [ ! -f "${BATS_TEST_TMPDIR}/repo_c/machines/host-c.conf" ]
}

@test "CLAUDE_SYNC_PROFILE works like --profile" {
  seed_host_a_profile
  CLAUDE_SYNC_PROFILE=host-a run run_install host-c "${BATS_TEST_TMPDIR}/repo_c" \
    "${BATS_TEST_TMPDIR}/claude_home_c" "${BATS_TEST_TMPDIR}/agents_home_c"
  [ "$status" -eq 0 ]
  grep -qx 'PROFILE=host-a' "${BATS_TEST_TMPDIR}/repo_c/machines/host-c.conf"
}

@test "install.sh --repair keeps the machine's profile and settings override, and --profile restores the pointer" {
  seed_host_a_profile
  sync_as b profile use host-a >/dev/null

  run run_install host-a "${REPO_A}" "${CLAUDE_HOME_A}" "${AGENTS_HOME_A}" --repair
  [ "$status" -eq 0 ]
  origin_has machines/host-a/agents/helper.md
  origin_has machines/host-a.settings.json
  [ "$(cat "${CLAUDE_HOME_A}/agents/helper.md")" = "host-a helper" ]

  run run_install host-b "${REPO_B}" "${CLAUDE_HOME_B}" "${AGENTS_HOME_B}" --repair --profile host-a
  [ "$status" -eq 0 ]
  grep -qx 'PROFILE=host-a' "${REPO_B}/machines/host-b.conf"
  [ "$(cat "${CLAUDE_HOME_B}/agents/helper.md")" = "host-a helper" ]
  origin_has machines/host-a/agents/helper.md
}

@test "install.sh --repair on a machine that synced before sends its new conf out in the same run" {
  seed_host_a_profile
  sync_as b profile use host-a >/dev/null
  [ -f "${CLAUDE_HOME_B}/.claude-sync-applied-commit" ]
  grep -qx 'PROFILE=host-a' <(origin_cat machines/host-b.conf)

  # No --profile: the rewritten conf drops PROFILE=.
  run run_install host-b "${REPO_B}" "${CLAUDE_HOME_B}" "${AGENTS_HOME_B}" --repair
  [ "$status" -eq 0 ]
  run grep -c '^PROFILE=' "${REPO_B}/machines/host-b.conf"
  [ "$output" = "0" ]
  run bash -c "git -C '${ORIGIN}' show sync/host-b:machines/host-b.conf | grep -c '^PROFILE='"
  [ "$output" = "0" ]
  grep -qx 'PROFILE=host-a' <(origin_cat machines/host-b.conf)
}

@test "install.sh rejects an unknown option" {
  run "${REPO_ROOT}/install.sh" --bogus
  [ "$status" -ne 0 ]
  [[ "$output" == *"Unknown option"* ]]
}
