#!/usr/bin/env bash
# Shim: runs the Python installer, `python3 -m claude_sync.install`, with
# every argument passed through. It runs from the clone, so the package
# imports from here.
#
# install.sh [--repair|--reinstall] [--profile NAME] [--project-path PATH]
set -euo pipefail

dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if ! command -v python3 >/dev/null 2>&1; then
  echo "claude-sync: needs python3 (3.12+)" >&2
  exit 1
fi
# The clone first on the import path, so a PYTHONSAFEPATH in the
# environment (which drops the folder from sys.path) cannot break it.
export PYTHONPATH="${dir}${PYTHONPATH:+:${PYTHONPATH}}"
cd "${dir}"
exec python3 -m claude_sync.install "$@"
