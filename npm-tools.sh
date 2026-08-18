#!/usr/bin/env bash
# Provisions npm-installed companion tools for Claude Code that live outside
# the plugin marketplace system, so claude-sync.sh has no way to carry them
# via git. install.sh calls this on every run: `npm install -g pkg@latest` is
# idempotent — installs if missing, upgrades if outdated, no-ops if current.

NPM_TOOLS=(
  ccusage
)

provision_npm_tools() {
  if ! command -v npm >/dev/null 2>&1; then
    echo "npm not found — skipping companion tool provisioning (${NPM_TOOLS[*]})." >&2
    return 0
  fi

  local tool
  for tool in "${NPM_TOOLS[@]}"; do
    echo "Ensuring ${tool}@latest via npm..."
    if ! npm install -g "${tool}@latest"; then
      echo "Failed to install/update ${tool} — continuing." >&2
    fi
  done
}

provision_npm_tools
