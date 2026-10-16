#!/usr/bin/env bash
# Provisions what the agent-memory plugin needs to start, since it comes via
# claude.ai plugin sync rather than this repo, so claude-sync.sh can't carry
# its dependencies. Its MCP server and hooks run `python` (Ubuntu only ships
# `python3`), and its server is written against the mcp 1.x API (mcp 2.x
# removed Server.list_tools, so the server crashes on start).
# install.sh calls this on every run; both steps no-op if already in place.

PIP_PACKAGES=(
  'mcp<2'
  aiosqlite
  pyyaml
)

ensure_python_alias() {
  if command -v python >/dev/null 2>&1; then
    echo "python already available."
    return 0
  fi

  if ! command -v apt-get >/dev/null 2>&1; then
    echo "No python command and no apt-get — link python to python3 manually." >&2
    return 0
  fi

  local sudo_cmd=""
  if [ "$(id -u 2>/dev/null || echo 1)" -ne 0 ] && command -v sudo >/dev/null 2>&1; then
    sudo_cmd="sudo"
  fi

  echo "python not found — installing python-is-python3..."
  if ! ${sudo_cmd} apt-get install -y python-is-python3; then
    echo "Failed to install python-is-python3 — continuing." >&2
  fi
  return 0
}

ensure_pip_packages() {
  if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 not found — skipping pip packages (${PIP_PACKAGES[*]})." >&2
    return 0
  fi

  echo "Ensuring pip packages: ${PIP_PACKAGES[*]}..."
  if ! python3 -m pip install --break-system-packages "${PIP_PACKAGES[@]}"; then
    echo "Failed to install pip packages — continuing." >&2
  fi
  return 0
}

ensure_python_alias
ensure_pip_packages
