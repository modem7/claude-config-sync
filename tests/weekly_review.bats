#!/usr/bin/env bats
# Drives the weekly-review Python tests. weekly-review/ is private and never
# published, so this file skips cleanly on the public mirror.

setup() {
  load 'test_helper'
  WR_DIR="${REPO_ROOT}/weekly-review"
  [ -d "${WR_DIR}" ] || skip "weekly-review/ is private and not in this tree"
  command -v python3 >/dev/null 2>&1 || skip "python3 is not installed"
  export HOME="${BATS_TEST_TMPDIR}/home"
  export XDG_STATE_HOME="${BATS_TEST_TMPDIR}/state"
  export PYTHONDONTWRITEBYTECODE=1
  mkdir -p "${HOME}/.claude/projects"
}

@test "weekly-review unit tests pass" {
  cd "${WR_DIR}"
  run python3 -m unittest discover -s tests -t .
  [ "$status" -eq 0 ] || { echo "$output"; return 1; }
  [[ "$output" == *"OK"* ]]
}

@test "weekly-review dry run calls no gh, curl or claude and writes no state" {
  local bin="${BATS_TEST_TMPDIR}/bin" marker="${BATS_TEST_TMPDIR}/called" tool
  mkdir -p "${bin}"
  for tool in gh curl wget claude git; do
    printf '#!/bin/sh\necho "%s" >> "%s"\n' "${tool}" "${marker}" > "${bin}/${tool}"
    chmod +x "${bin}/${tool}"
  done
  mkdir -p "${HOME}/.claude/projects/-tmp-demo"
  printf '%s\n' \
    '{"type":"user","timestamp":"2030-06-14T10:00:00Z","message":{"content":"start"}}' \
    'not json' \
    > "${HOME}/.claude/projects/-tmp-demo/s1.jsonl"
  PATH="${bin}:${PATH}" run python3 "${WR_DIR}/run.py" --now 2030-06-15T00:00:00Z
  [ "$status" -eq 0 ]
  [[ "$output" == *"# Weekly review (dry run)"* ]]
  [[ "$output" == *"Sessions in window: 1"* ]]
  [[ "$output" == *"unparseable lines skipped: 1"* ]]
  [ ! -e "${marker}" ]
  [ ! -e "${XDG_STATE_HOME}" ]
}

@test "weekly-review refuses a state dir inside ~/.claude" {
  run python3 "${WR_DIR}/run.py" --state-dir "${HOME}/.claude/wr"
  [ "$status" -eq 1 ]
  [[ "$output" == *"inside ~/.claude"* ]]
  [ ! -e "${HOME}/.claude/wr" ]
}

@test "weekly-review --save-state writes a mode 700 state dir" {
  run python3 "${WR_DIR}/run.py" --save-state --now 2030-06-15T00:00:00Z
  [ "$status" -eq 0 ]
  [ -f "${XDG_STATE_HOME}/claude-weekly-review/watermark.json" ]
  [[ "$(ls -ld "${XDG_STATE_HOME}/claude-weekly-review")" == drwx------* ]]
}

@test "weekly-review --triage and --post are refused without approval files" {
  local bin="${BATS_TEST_TMPDIR}/bin" marker="${BATS_TEST_TMPDIR}/called" tool
  mkdir -p "${bin}"
  for tool in gh claude; do
    printf '#!/bin/sh\necho "%s" >> "%s"\n' "${tool}" "${marker}" > "${bin}/${tool}"
    chmod +x "${bin}/${tool}"
  done
  PATH="${bin}:${PATH}" run python3 "${WR_DIR}/run.py" --triage --post --issue 1
  [ "$status" -eq 3 ]
  [[ "$output" == *"approved-triage does not exist"* ]]
  [[ "$output" == *"approved-post does not exist"* ]]
  [ ! -e "${marker}" ]
  [ ! -e "${XDG_STATE_HOME}" ]
}
