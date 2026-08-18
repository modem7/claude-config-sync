#!/usr/bin/env bats

setup() {
  load 'test_helper'
  source "${LIB_DIR}/safety.sh"
}

@test "assert_no_denylisted_files passes on a clean tree" {
  mkdir -p "${BATS_TEST_TMPDIR}/clean/dot-claude"
  echo "hello" > "${BATS_TEST_TMPDIR}/clean/dot-claude/CLAUDE.md"
  run assert_no_denylisted_files "${BATS_TEST_TMPDIR}/clean"
  [ "$status" -eq 0 ]
}

@test "assert_no_denylisted_files catches a leaked .credentials.json" {
  mkdir -p "${BATS_TEST_TMPDIR}/dirty/dot-claude"
  echo "secret" > "${BATS_TEST_TMPDIR}/dirty/dot-claude/.credentials.json"
  run assert_no_denylisted_files "${BATS_TEST_TMPDIR}/dirty"
  [ "$status" -eq 1 ]
}

@test "assert_no_denylisted_files catches a leaked .claude.json anywhere in the tree" {
  mkdir -p "${BATS_TEST_TMPDIR}/dirty2/nested/deep"
  echo "secret" > "${BATS_TEST_TMPDIR}/dirty2/nested/deep/.claude.json"
  run assert_no_denylisted_files "${BATS_TEST_TMPDIR}/dirty2"
  [ "$status" -eq 1 ]
}
