#!/usr/bin/env bash
# Claude Code discovers skills under ~/.claude/skills/, but only
# ~/.agents/skills/ content is git-tracked. This wires a discovery symlink
# for each tracked skill without disturbing real (non-symlink) directories
# that already live under ~/.claude/skills/ (built-in or plugin-managed).

wire_skill_symlinks() {
  local repo_dir="$1"
  local claude_home="$2"
  local agents_home="$3"
  local skill_dir name link

  mkdir -p "${claude_home}/skills"

  for skill_dir in "${repo_dir}"/dot-agents/skills/*/; do
    [ -d "${skill_dir}" ] || continue
    name="$(basename "${skill_dir}")"
    link="${claude_home}/skills/${name}"

    if [ -L "${link}" ] || [ ! -e "${link}" ]; then
      ln -sfn "${agents_home}/skills/${name}" "${link}"
    fi
  done
}
