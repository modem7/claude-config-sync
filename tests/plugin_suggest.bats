#!/usr/bin/env bats

setup() {
  load 'test_helper'
  HOOK="${REPO_ROOT}/dot-claude/hooks/plugin-suggest.sh"
  export HOME="${BATS_TEST_TMPDIR}/home"
  mkdir -p "$HOME/.claude"
  PROJ="${BATS_TEST_TMPDIR}/proj"
  mkdir -p "$PROJ/.claude"
}

run_hook() {
  printf '{"cwd":"%s"}' "$PROJ" | "$HOOK"
}

@test "script has valid syntax" {
  run bash -n "$HOOK"
  [ "$status" -eq 0 ]
}

@test "Dockerfile suggests docker" {
  touch "$PROJ/Dockerfile"
  run run_hook
  [ "$status" -eq 0 ]
  [[ "$output" == "Plugin hint:"*"docker@claude-plugins-official"* ]]
  [ "$(printf '%s\n' "$output" | wc -l)" -eq 1 ]
}

@test "compose file suggests docker" {
  touch "$PROJ/compose.yaml"
  run run_hook
  [[ "$output" == *"docker@claude-plugins-official"* ]]
}

@test "workflow suggests deployment-engineer" {
  mkdir -p "$PROJ/.github/workflows" && touch "$PROJ/.github/workflows/ci.yml"
  run run_hook
  [[ "$output" == *"deployment-engineer@awesome-claude-code-plugins"* ]]
}

@test "acquis.yaml suggests crowdsec" {
  touch "$PROJ/acquis.yaml"
  run run_hook
  [[ "$output" == *"crowdsec@crowdsecurity"* ]]
}

@test "react package.json suggests frontend-design" {
  echo '{"dependencies":{"react":"18"}}' > "$PROJ/package.json"
  run run_hook
  [[ "$output" == *"frontend-design@claude-plugins-official"* ]]
}

@test "plain package.json does not match" {
  echo '{"dependencies":{"express":"4"}}' > "$PROJ/package.json"
  run run_hook
  [ -z "$output" ]
}

@test "two rules for one plugin are reported once" {
  echo '{"dependencies":{"vue":"3"}}' > "$PROJ/package.json"
  echo '<style>a{}</style>' > "$PROJ/index.html"
  run run_hook
  [ "$(grep -o 'frontend-design@claude-plugins-official' <<<"$output" | wc -l)" -eq 2 ]
  [ "$(printf '%s\n' "$output" | wc -l)" -eq 1 ]
}

@test "multiple matches give one line" {
  touch "$PROJ/Dockerfile" "$PROJ/acquis.yaml"
  run run_hook
  [[ "$output" == *"docker@"*"crowdsec@"* ]]
  [ "$(printf '%s\n' "$output" | wc -l)" -eq 1 ]
}

@test "no markers prints nothing" {
  run run_hook
  [ "$status" -eq 0 ]
  [ -z "$output" ]
}

@test "already enabled in project settings is skipped" {
  touch "$PROJ/Dockerfile"
  echo '{"enabledPlugins":{"docker@claude-plugins-official":true}}' > "$PROJ/.claude/settings.json"
  run run_hook
  [ -z "$output" ]
}

@test "enabled in settings.local.json is skipped" {
  touch "$PROJ/Dockerfile"
  echo '{"enabledPlugins":{"docker@claude-plugins-official":true}}' > "$PROJ/.claude/settings.local.json"
  run run_hook
  [ -z "$output" ]
}

@test "explicit false in project settings counts as opt-out" {
  touch "$PROJ/Dockerfile"
  echo '{"enabledPlugins":{"docker@claude-plugins-official":false}}' > "$PROJ/.claude/settings.json"
  run run_hook
  [ -z "$output" ]
}

@test "globally enabled plugin is skipped" {
  touch "$PROJ/Dockerfile"
  echo '{"enabledPlugins":{"docker@claude-plugins-official":true}}' > "$HOME/.claude/settings.json"
  run run_hook
  [ -z "$output" ]
}

@test "ignore file opts out one plugin but not others" {
  touch "$PROJ/Dockerfile" "$PROJ/acquis.yaml"
  echo 'docker@claude-plugins-official' > "$PROJ/.claude/plugin-suggest-ignore"
  run run_hook
  [[ "$output" == *"crowdsec@crowdsecurity"* ]]
  [[ "$output" != *"docker@"* ]]
}

@test "ignore file with 'all' silences everything" {
  touch "$PROJ/Dockerfile" "$PROJ/acquis.yaml"
  echo 'all' > "$PROJ/.claude/plugin-suggest-ignore"
  run run_hook
  [ -z "$output" ]
}

@test "CC_PLUGIN_SUGGEST=0 disables the hook" {
  touch "$PROJ/Dockerfile"
  CC_PLUGIN_SUGGEST=0 run run_hook
  [ -z "$output" ]
}

@test "garbage stdin exits 0 silently" {
  run bash -c "printf 'not json {{{' | '$HOOK'"
  [ "$status" -eq 0 ]
  [ -z "$output" ]
}

@test "empty stdin exits 0 silently" {
  run bash -c "'$HOOK' < /dev/null"
  [ "$status" -eq 0 ]
  [ -z "$output" ]
}

@test "non-object JSON and non-string cwd exit 0 silently" {
  run bash -c "printf '[1,2]' | '$HOOK'"
  [ "$status" -eq 0 ] && [ -z "$output" ]
  run bash -c "printf '{\"cwd\":42}' | '$HOOK'"
  [ "$status" -eq 0 ] && [ -z "$output" ]
}

@test "missing or relative cwd exits 0 silently" {
  run bash -c "printf '{\"cwd\":\"/no/such/dir\"}' | '$HOOK'"
  [ "$status" -eq 0 ] && [ -z "$output" ]
  run bash -c "printf '{\"cwd\":\"relative\"}' | '$HOOK'"
  [ "$status" -eq 0 ] && [ -z "$output" ]
}

@test "garbage settings.json does not break the hook" {
  touch "$PROJ/Dockerfile"
  echo 'garbage{' > "$PROJ/.claude/settings.json"
  echo '{"enabledPlugins":[1,2]}' > "$PROJ/.claude/settings.local.json"
  run run_hook
  [ "$status" -eq 0 ]
  [[ "$output" == *"docker@claude-plugins-official"* ]]
}

@test "runs in well under 200 ms" {
  touch "$PROJ/Dockerfile" "$PROJ/acquis.yaml"
  echo '{"dependencies":{"react":"18"}}' > "$PROJ/package.json"
  local start end
  start=$(date +%s%N)
  run_hook >/dev/null
  end=$(date +%s%N)
  [ $(( (end - start) / 1000000 )) -lt 200 ]
}

@test "hook is registered under SessionStart in settings.json" {
  run jq -e '.hooks.SessionStart[].hooks[] | select(.command | contains("plugin-suggest.sh"))' "${REPO_ROOT}/dot-claude/settings.json"
  [ "$status" -eq 0 ]
}
