#!/usr/bin/env bash
# Resolves the on-disk memory directory Claude Code uses for a given
# working-directory path (Claude Code encodes cwd by replacing / with -).

encode_project_path() {
  local path="$1"
  echo "${path//\//-}"
}

resolve_memory_dir() {
  local claude_home="$1"
  local project_path="$2"
  local encoded
  encoded="$(encode_project_path "${project_path}")"
  echo "${claude_home}/projects/${encoded}/memory"
}
