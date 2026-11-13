#!/usr/bin/env bash
# Shared bats setup for the tests of the provisioning scripts and hooks.

REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/.." && pwd)"

# CI runners have no global git identity. Give every test a fixed one so
# results do not depend on the host.
export GIT_AUTHOR_NAME="test" GIT_AUTHOR_EMAIL="test@example.invalid"
export GIT_COMMITTER_NAME="test" GIT_COMMITTER_EMAIL="test@example.invalid"
