#!/usr/bin/env bash
# Guards against ever committing account/auth secrets into the repo.

DENYLISTED_FILENAMES=(".credentials.json" ".claude.json")

assert_no_denylisted_files() {
  local repo_dir="$1"
  local name found

  for name in "${DENYLISTED_FILENAMES[@]}"; do
    found="$(find "${repo_dir}" -name "${name}" -print -quit)"
    if [ -n "${found}" ]; then
      echo "Denylisted file found in repo tree: ${found}" >&2
      return 1
    fi
  done
  return 0
}
