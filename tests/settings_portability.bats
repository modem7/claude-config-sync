#!/usr/bin/env bats

setup() {
  load 'test_helper'
  SETTINGS="${REPO_ROOT}/dot-claude/settings.json"
}

# settings.json is applied verbatim on every machine, so a hook command
# pinned to one user's home directory silently stops running elsewhere.
@test "hook commands contain no hardcoded home directory" {
  run bash -c "jq -r '.. | .command? // empty' '${SETTINGS}' | grep -E '(^|[\"[:space:]])(/home/|/Users/|/root/)'"
  [ "$status" -eq 1 ]
}

@test "every hook script referenced by settings.json exists and is executable" {
  local script
  while read -r script; do
    [ -x "${REPO_ROOT}/dot-claude/hooks/${script}" ] || { echo "missing or not executable: ${script}"; return 1; }
  done < <(jq -r '.. | .command? // empty' "${SETTINGS}" | grep -oE '\.claude/hooks/[A-Za-z0-9_.-]+' | sed 's#.*/##' | sort -u)
}
