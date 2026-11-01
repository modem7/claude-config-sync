#!/usr/bin/env bash
# Claude Code discovers skills under ~/.claude/skills/, but only
# ~/.agents/skills/ content is git-tracked. This wires a discovery symlink
# for each tracked skill without disturbing real (non-symlink) directories
# that already live under ~/.claude/skills/ (built-in or plugin-managed).
# It also removes links into ~/.agents/skills/ whose target no longer exists
# (a skill deleted from the repo), and nothing else.
#
# With a machine profile directory (optional 4th argument), the tracked set
# is the effective one: shared skills the profile doesn't disable, plus the
# profile's own skills. A disabled skill's discovery link is removed.

wire_skill_symlinks() {
  local repo_dir="$1"
  local claude_home="$2"
  local agents_home="$3"
  local profile_dir="${4:-}"
  local skill_dir name link target root
  local roots=("${repo_dir}/dot-agents/skills")
  [ -n "${profile_dir}" ] && roots+=("${profile_dir}/skills")

  mkdir -p "${claude_home}/skills"

  for root in "${roots[@]}"; do
    for skill_dir in "${root}"/*/; do
      [ -d "${skill_dir}" ] || continue
      name="$(basename "${skill_dir}")"
      [ -n "${profile_dir}" ] && [ -e "${profile_dir}/skills/${name}.disabled" ] && continue
      link="${claude_home}/skills/${name}"

      if [ -L "${link}" ] || [ ! -e "${link}" ]; then
        ln -sfn "${agents_home}/skills/${name}" "${link}"
      fi
    done
  done

  if [ -n "${profile_dir}" ]; then
    for target in "${profile_dir}"/skills/*.disabled; do
      [ -e "${target}" ] || continue
      name="$(basename "${target}" .disabled)"
      link="${claude_home}/skills/${name}"
      if [ -L "${link}" ] && [ "$(readlink "${link}")" = "${agents_home}/skills/${name}" ]; then
        rm -f "${link}"
      fi
    done
  fi

  for link in "${claude_home}"/skills/*; do
    [ -L "${link}" ] || continue
    [ -e "${link}" ] && continue
    target="$(readlink "${link}")"
    case "${target}" in
      "${agents_home}/skills/"*) rm -f "${link}" ;;
    esac
  done
}
