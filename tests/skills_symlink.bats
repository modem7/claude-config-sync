#!/usr/bin/env bats

setup() {
  load 'test_helper'
  source "${LIB_DIR}/skills-symlink.sh"

  REPO="${BATS_TEST_TMPDIR}/repo"
  CLAUDE_HOME="${BATS_TEST_TMPDIR}/claude_home"
  AGENTS_HOME="${BATS_TEST_TMPDIR}/agents_home"

  mkdir -p "${REPO}/dot-agents/skills/release"
  mkdir -p "${AGENTS_HOME}/skills/release"
  mkdir -p "${CLAUDE_HOME}/skills"
}

@test "wire_skill_symlinks creates a symlink for a tracked skill" {
  wire_skill_symlinks "${REPO}" "${CLAUDE_HOME}" "${AGENTS_HOME}"
  [ -L "${CLAUDE_HOME}/skills/release" ]
  [ "$(readlink "${CLAUDE_HOME}/skills/release")" = "${AGENTS_HOME}/skills/release" ]
}

@test "wire_skill_symlinks does not touch a pre-existing real directory" {
  mkdir -p "${CLAUDE_HOME}/skills/find-skills"
  echo "builtin content" > "${CLAUDE_HOME}/skills/find-skills/marker.txt"

  wire_skill_symlinks "${REPO}" "${CLAUDE_HOME}" "${AGENTS_HOME}"

  [ ! -L "${CLAUDE_HOME}/skills/find-skills" ]
  [ "$(cat "${CLAUDE_HOME}/skills/find-skills/marker.txt")" = "builtin content" ]
}

@test "wire_skill_symlinks is idempotent when re-run" {
  wire_skill_symlinks "${REPO}" "${CLAUDE_HOME}" "${AGENTS_HOME}"
  wire_skill_symlinks "${REPO}" "${CLAUDE_HOME}" "${AGENTS_HOME}"
  [ -L "${CLAUDE_HOME}/skills/release" ]
}
