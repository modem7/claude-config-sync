#!/usr/bin/env bats

setup() {
  load 'test_helper'
  source "${LIB_DIR}/memory-path.sh"
}

@test "encode_project_path replaces every slash with a dash" {
  result="$(encode_project_path "/home/modem7/project")"
  [ "$result" = "-home-modem7-project" ]
}

@test "encode_project_path handles nested subproject paths" {
  result="$(encode_project_path "/home/modem7/project/christmas-countdown")"
  [ "$result" = "-home-modem7-project-christmas-countdown" ]
}

@test "resolve_memory_dir builds the full memory path under claude_home" {
  result="$(resolve_memory_dir "/home/modem7/.claude" "/home/modem7/project")"
  [ "$result" = "/home/modem7/.claude/projects/-home-modem7-project/memory" ]
}
