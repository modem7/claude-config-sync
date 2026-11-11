#!/usr/bin/env bash
# Guards against ever committing account/auth secrets into the repo, and
# against publishing per-machine or personal content to the public mirror.

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

# assert_public_tree_clean TREE
# The public mirror must never get machines/ (hostnames, paths, machine
# profiles and notes, settings overrides), memory/ (personal auto-memory),
# private/ (repo-wide private notes) or weekly-review/ (transcript review job).
# Fails, listing what leaked, if TREE has any path under them, or any
# *.settings.json under a machines directory at any depth.
assert_public_tree_clean() {
  local tree="$1"
  local leaked
  leaked="$(cd "${tree}" && find . -path ./.git -prune -o \( \
      -path ./machines -o -path './machines/*' -o -path ./memory -o -path './memory/*' \
      -o -path ./private -o -path './private/*' \
      -o -path ./weekly-review -o -path './weekly-review/*' \
      -o \( -path '*/machines/*' -name '*.settings.json' \) \) -print)"
  if [ -n "${leaked}" ]; then
    echo "Refusing to publish: per-machine or personal content is in the public tree:" >&2
    echo "${leaked}" >&2
    return 1
  fi
  return 0
}
