#!/usr/bin/env bash
# Ensures the gh CLI is present, since Claude Code is configured to drive
# GitHub via gh directly (see dot-claude/settings.json — the github MCP
# plugin is intentionally disabled now that gh covers the same ground).
# install.sh calls this on every run; no-ops if gh is already installed.

ensure_gh() {
  if command -v gh >/dev/null 2>&1; then
    echo "gh already installed."
    return 0
  fi

  echo "gh not found — attempting to install..."

  local sudo_cmd=""
  if [ "$(id -u 2>/dev/null || echo 1)" -ne 0 ] && command -v sudo >/dev/null 2>&1; then
    sudo_cmd="sudo"
  fi

  if command -v brew >/dev/null 2>&1; then
    brew install gh && return 0
  elif command -v apt-get >/dev/null 2>&1; then
    ${sudo_cmd} apt-get update -y && ${sudo_cmd} apt-get install -y gh && return 0
  elif command -v dnf >/dev/null 2>&1; then
    ${sudo_cmd} dnf install -y gh && return 0
  elif command -v pacman >/dev/null 2>&1; then
    ${sudo_cmd} pacman -Sy --noconfirm github-cli && return 0
  elif command -v apk >/dev/null 2>&1; then
    ${sudo_cmd} apk add github-cli && return 0
  fi

  echo "Could not auto-install gh (no supported package manager found, or the install failed)." >&2
  echo "Install manually: https://cli.github.com" >&2
  return 0
}

ensure_gh
