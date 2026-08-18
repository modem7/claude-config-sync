#!/usr/bin/env bash
# Copies tracked Claude Code config between the repo working tree and the
# real local paths. Copy-based (not symlinked) so a tool writing a file via
# write-temp-then-rename can never silently break the sync.

sync_capture() {
  local repo_dir="$1"
  local claude_home="$2"
  local agents_home="$3"
  local memory_dir="$4"

  mkdir -p "${repo_dir}/dot-claude/agents" "${repo_dir}/dot-claude/hooks" "${repo_dir}/dot-agents/skills" "${repo_dir}/memory"

  [ -f "${claude_home}/CLAUDE.md" ] && cp "${claude_home}/CLAUDE.md" "${repo_dir}/dot-claude/CLAUDE.md"
  [ -f "${claude_home}/settings.json" ] && cp "${claude_home}/settings.json" "${repo_dir}/dot-claude/settings.json"
  [ -d "${claude_home}/agents" ] && rsync -a --delete --exclude=.gitkeep "${claude_home}/agents/" "${repo_dir}/dot-claude/agents/"
  [ -d "${claude_home}/hooks" ] && rsync -a --delete --exclude=.gitkeep "${claude_home}/hooks/" "${repo_dir}/dot-claude/hooks/"
  [ -d "${agents_home}/skills" ] && rsync -a --delete --exclude=.gitkeep "${agents_home}/skills/" "${repo_dir}/dot-agents/skills/"
  [ -d "${memory_dir}" ] && rsync -a --delete --exclude=.gitkeep "${memory_dir}/" "${repo_dir}/memory/"
  # Guards above may be false as the last statement; force success so a
  # missing directory doesn't abort the whole script under set -e.
  return 0
}

sync_apply() {
  local repo_dir="$1"
  local claude_home="$2"
  local agents_home="$3"
  local memory_dir="$4"
  # mirror="true" (default) makes the three directory syncs an exact mirror
  # of the repo, deleting local entries the repo doesn't have. This is what
  # correctly propagates deletions between machines in steady state.
  # mirror="false" merges the repo in without deleting anything local-only;
  # used only for a machine's first-ever sync so it adopts shared config
  # without clobbering content unique to that machine. See bootstrap in
  # claude-sync.sh.
  local mirror="${5:-true}"

  mkdir -p "${claude_home}/agents" "${claude_home}/hooks" "${agents_home}/skills" "${memory_dir}"

  local rsync_flags=(-a)
  [ "${mirror}" = "true" ] && rsync_flags+=(--delete)

  [ -f "${repo_dir}/dot-claude/CLAUDE.md" ] && cp "${repo_dir}/dot-claude/CLAUDE.md" "${claude_home}/CLAUDE.md"
  [ -f "${repo_dir}/dot-claude/settings.json" ] && cp "${repo_dir}/dot-claude/settings.json" "${claude_home}/settings.json"
  [ -d "${repo_dir}/dot-claude/agents" ] && rsync "${rsync_flags[@]}" "${repo_dir}/dot-claude/agents/" "${claude_home}/agents/"
  [ -d "${repo_dir}/dot-claude/hooks" ] && rsync "${rsync_flags[@]}" "${repo_dir}/dot-claude/hooks/" "${claude_home}/hooks/"
  [ -d "${repo_dir}/dot-agents/skills" ] && rsync "${rsync_flags[@]}" "${repo_dir}/dot-agents/skills/" "${agents_home}/skills/"
  [ -d "${repo_dir}/memory" ] && rsync "${rsync_flags[@]}" "${repo_dir}/memory/" "${memory_dir}/"
  # Guards above may be false as the last statement; force success so a
  # missing directory doesn't abort the whole script under set -e.
  return 0
}
