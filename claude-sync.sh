#!/usr/bin/env bash
# Shim: runs the Python port, `python3 -m claude_sync`, with every argument
# passed through. It runs from the clone, so the package imports from here.
# CLAUDE_SYNC_IMPL=bash runs the old bash script in legacy/ instead (kept
# until phase 3 of the port). The usage text is USAGE in claude_sync/cli.py.
set -euo pipefail

dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ "${CLAUDE_SYNC_IMPL:-}" = "bash" ]; then
  exec "${dir}/legacy/claude-sync.sh" "$@"
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "claude-sync: needs python3 (3.12+); or set CLAUDE_SYNC_IMPL=bash" >&2
  exit 1
fi
# The clone first on the import path, so a PYTHONSAFEPATH in the
# environment (which drops the folder from sys.path) cannot break it.
export PYTHONPATH="${dir}${PYTHONPATH:+:${PYTHONPATH}}"
cd "${dir}"
exec python3 -m claude_sync "$@"
