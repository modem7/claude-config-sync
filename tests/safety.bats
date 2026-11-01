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

@test "assert_public_tree_clean passes on a clean tree" {
  mkdir -p "${BATS_TEST_TMPDIR}/pub/dot-agents/skills/find-skills" "${BATS_TEST_TMPDIR}/pub/.git/machines"
  echo "skill" > "${BATS_TEST_TMPDIR}/pub/dot-agents/skills/find-skills/SKILL.md"
  run assert_public_tree_clean "${BATS_TEST_TMPDIR}/pub"
  [ "$status" -eq 0 ]
}

@test "assert_public_tree_clean catches machines/, memory/ and a nested machines settings override" {
  mkdir -p "${BATS_TEST_TMPDIR}/p1/machines/host-a/skills"
  run assert_public_tree_clean "${BATS_TEST_TMPDIR}/p1"
  [ "$status" -eq 1 ]
  [[ "$output" == *"./machines"* ]]

  mkdir -p "${BATS_TEST_TMPDIR}/p2/memory"
  run assert_public_tree_clean "${BATS_TEST_TMPDIR}/p2"
  [ "$status" -eq 1 ]

  mkdir -p "${BATS_TEST_TMPDIR}/p3/docs/machines"
  echo '{}' > "${BATS_TEST_TMPDIR}/p3/docs/machines/host-a.settings.json"
  run assert_public_tree_clean "${BATS_TEST_TMPDIR}/p3"
  [ "$status" -eq 1 ]

  mkdir -p "${BATS_TEST_TMPDIR}/p4/private"
  echo 'notes' > "${BATS_TEST_TMPDIR}/p4/private/NOTES.md"
  run assert_public_tree_clean "${BATS_TEST_TMPDIR}/p4"
  [ "$status" -eq 1 ]
  [[ "$output" == *"./private"* ]]
}

@test "the files publish-public.sh whitelists pass the guard, and it runs the guard" {
  [ -f "${REPO_ROOT}/publish-public.sh" ] || skip "publish-public.sh is not part of the public mirror"
  local pub="${BATS_TEST_TMPDIR}/whitelisted" p
  mkdir -p "${pub}"
  while IFS= read -r p; do
    [ -e "${REPO_ROOT}/${p}" ] || continue
    mkdir -p "${pub}/$(dirname "${p}")"
    cp -a "${REPO_ROOT}/${p}" "${pub}/${p}"
  done < <(sed -n '/^PATHS=(/,/^)/{/^  /s/^  //p}' "${REPO_ROOT}/publish-public.sh")
  [ -f "${pub}/claude-sync.sh" ]
  run assert_public_tree_clean "${pub}"
  [ "$status" -eq 0 ]
  grep -q 'assert_public_tree_clean "${PUB_DIR}"' "${REPO_ROOT}/publish-public.sh"
  refute grep -q 'woodpecker' "${REPO_ROOT}/publish-public.sh"
}
