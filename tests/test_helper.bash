#!/usr/bin/env bash
# Shared bats setup: exposes LIB_DIR so tests can source library files.

LIB_DIR="$(cd "${BATS_TEST_DIRNAME}/../lib" && pwd)"
REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/.." && pwd)"

# "! cmd" never fails a bats test unless it's the last line (errexit ignores
# negated commands), so assert a command fails with "refute cmd" instead.
refute() {
  if "$@"; then return 1; fi
}
