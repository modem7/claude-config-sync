#!/usr/bin/env bash
# Shared bats setup: exposes LIB_DIR so tests can source library files.

LIB_DIR="$(cd "${BATS_TEST_DIRNAME}/../lib" && pwd)"
REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/.." && pwd)"
