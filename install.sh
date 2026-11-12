#!/usr/bin/env bash
# Shim: runs the Python installer, `python3 -m claude_sync.install`, with
# every argument passed through. It runs from the clone, so the package
# imports from here. CLAUDE_SYNC_IMPL=bash runs the old bash installer in
# legacy/ instead (kept until phase 3 of the port).
#
# install.sh [--repair|--reinstall] [--profile NAME] [--project-path PATH]
set -euo pipefail

dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ "${CLAUDE_SYNC_IMPL:-}" = "bash" ]; then
  exec "${dir}/legacy/install.sh" "$@"
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "claude-sync: needs python3 (3.12+); or set CLAUDE_SYNC_IMPL=bash" >&2
  exit 1
fi
# The clone first on the import path, so a PYTHONSAFEPATH in the
# environment (which drops the folder from sys.path) cannot break it.
export PYTHONPATH="${dir}${PYTHONPATH:+:${PYTHONPATH}}"
cd "${dir}"
exec python3 -m claude_sync.install "$@"
