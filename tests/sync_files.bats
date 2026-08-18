#!/usr/bin/env bats

setup() {
  load 'test_helper'
  source "${LIB_DIR}/sync-files.sh"

  REPO="${BATS_TEST_TMPDIR}/repo"
  CLAUDE_HOME="${BATS_TEST_TMPDIR}/claude_home"
  AGENTS_HOME="${BATS_TEST_TMPDIR}/agents_home"
  MEMORY_DIR="${BATS_TEST_TMPDIR}/claude_home/projects/-home-modem7-project/memory"

  mkdir -p "${REPO}/dot-claude/agents" "${REPO}/dot-claude/hooks" "${REPO}/dot-agents/skills" "${REPO}/memory"
  mkdir -p "${CLAUDE_HOME}/hooks" "${AGENTS_HOME}/skills/release" "${MEMORY_DIR}"

  echo "# global instructions" > "${CLAUDE_HOME}/CLAUDE.md"
  echo '{"theme":"dark"}' > "${CLAUDE_HOME}/settings.json"
  echo "release skill body" > "${AGENTS_HOME}/skills/release/SKILL.md"
  echo "# Memory Index" > "${MEMORY_DIR}/MEMORY.md"

  printf '#!/bin/bash\necho hook\n' > "${CLAUDE_HOME}/hooks/example-guard.sh"
  chmod +x "${CLAUDE_HOME}/hooks/example-guard.sh"
}

@test "sync_capture copies CLAUDE.md and settings.json into the repo" {
  sync_capture "${REPO}" "${CLAUDE_HOME}" "${AGENTS_HOME}" "${MEMORY_DIR}"
  [ "$(cat "${REPO}/dot-claude/CLAUDE.md")" = "# global instructions" ]
  [ "$(cat "${REPO}/dot-claude/settings.json")" = '{"theme":"dark"}' ]
}

@test "sync_capture copies personal skills into dot-agents/skills" {
  sync_capture "${REPO}" "${CLAUDE_HOME}" "${AGENTS_HOME}" "${MEMORY_DIR}"
  [ "$(cat "${REPO}/dot-agents/skills/release/SKILL.md")" = "release skill body" ]
}

@test "sync_capture copies memory content into the repo" {
  sync_capture "${REPO}" "${CLAUDE_HOME}" "${AGENTS_HOME}" "${MEMORY_DIR}"
  [ "$(cat "${REPO}/memory/MEMORY.md")" = "# Memory Index" ]
}

@test "sync_capture copies hook scripts into dot-claude/hooks, preserving the executable bit" {
  sync_capture "${REPO}" "${CLAUDE_HOME}" "${AGENTS_HOME}" "${MEMORY_DIR}"
  [ "$(cat "${REPO}/dot-claude/hooks/example-guard.sh")" = "$(printf '#!/bin/bash\necho hook')" ]
  [ -x "${REPO}/dot-claude/hooks/example-guard.sh" ]
}

@test "sync_apply copies repo content back out to a fresh machine" {
  sync_capture "${REPO}" "${CLAUDE_HOME}" "${AGENTS_HOME}" "${MEMORY_DIR}"

  FRESH_CLAUDE_HOME="${BATS_TEST_TMPDIR}/fresh_claude_home"
  FRESH_AGENTS_HOME="${BATS_TEST_TMPDIR}/fresh_agents_home"
  FRESH_MEMORY_DIR="${BATS_TEST_TMPDIR}/fresh_claude_home/projects/-home-modem7-work/memory"

  sync_apply "${REPO}" "${FRESH_CLAUDE_HOME}" "${FRESH_AGENTS_HOME}" "${FRESH_MEMORY_DIR}"

  [ "$(cat "${FRESH_CLAUDE_HOME}/CLAUDE.md")" = "# global instructions" ]
  [ "$(cat "${FRESH_AGENTS_HOME}/skills/release/SKILL.md")" = "release skill body" ]
  [ "$(cat "${FRESH_MEMORY_DIR}/MEMORY.md")" = "# Memory Index" ]
  [ -x "${FRESH_CLAUDE_HOME}/hooks/example-guard.sh" ]
}

@test "sync_capture does not fail when claude_home/agents does not exist yet" {
  rm -rf "${CLAUDE_HOME:?}/agents"
  run sync_capture "${REPO}" "${CLAUDE_HOME}" "${AGENTS_HOME}" "${MEMORY_DIR}"
  [ "$status" -eq 0 ]
}

@test "sync_capture does not fail when claude_home/hooks does not exist yet" {
  rm -rf "${CLAUDE_HOME:?}/hooks"
  run sync_capture "${REPO}" "${CLAUDE_HOME}" "${AGENTS_HOME}" "${MEMORY_DIR}"
  [ "$status" -eq 0 ]
}

@test "sync_apply with mirror=true deletes a local-only hook script absent from the repo" {
  sync_capture "${REPO}" "${CLAUDE_HOME}" "${AGENTS_HOME}" "${MEMORY_DIR}"
  echo "local only hook" > "${CLAUDE_HOME}/hooks/local-only.sh"

  sync_apply "${REPO}" "${CLAUDE_HOME}" "${AGENTS_HOME}" "${MEMORY_DIR}" "true"

  [ ! -f "${CLAUDE_HOME}/hooks/local-only.sh" ]
  [ -f "${CLAUDE_HOME}/hooks/example-guard.sh" ]
}

@test "sync_apply with mirror=false does not delete a local-only hook script absent from the repo" {
  sync_capture "${REPO}" "${CLAUDE_HOME}" "${AGENTS_HOME}" "${MEMORY_DIR}"
  echo "local only hook" > "${CLAUDE_HOME}/hooks/local-only.sh"

  sync_apply "${REPO}" "${CLAUDE_HOME}" "${AGENTS_HOME}" "${MEMORY_DIR}" "false"

  [ -f "${CLAUDE_HOME}/hooks/local-only.sh" ]
}

@test "sync_apply with mirror=false does not delete a local-only skill absent from the repo" {
  mkdir -p "${AGENTS_HOME}/skills/local-only"
  echo "local only skill" > "${AGENTS_HOME}/skills/local-only/SKILL.md"

  sync_apply "${REPO}" "${CLAUDE_HOME}" "${AGENTS_HOME}" "${MEMORY_DIR}" "false"

  [ -d "${AGENTS_HOME}/skills/local-only" ]
  [ "$(cat "${AGENTS_HOME}/skills/local-only/SKILL.md")" = "local only skill" ]
}

@test "sync_apply with mirror=true (explicit) deletes a local-only skill absent from the repo" {
  mkdir -p "${AGENTS_HOME}/skills/local-only"
  echo "local only skill" > "${AGENTS_HOME}/skills/local-only/SKILL.md"

  sync_apply "${REPO}" "${CLAUDE_HOME}" "${AGENTS_HOME}" "${MEMORY_DIR}" "true"

  [ ! -d "${AGENTS_HOME}/skills/local-only" ]
}

@test "sync_apply with mirror omitted (default) still deletes a local-only skill absent from the repo" {
  mkdir -p "${AGENTS_HOME}/skills/local-only"
  echo "local only skill" > "${AGENTS_HOME}/skills/local-only/SKILL.md"

  sync_apply "${REPO}" "${CLAUDE_HOME}" "${AGENTS_HOME}" "${MEMORY_DIR}"

  [ ! -d "${AGENTS_HOME}/skills/local-only" ]
}
