#!/usr/bin/env bash
# Reads and writes the per-machine config file (machines/<hostname>.conf)
# that maps a machine's hostname to the local project whose memory dir is
# synced, and optionally to the machine profile it follows.

machine_conf_path() {
  local repo_dir="$1"
  local hostname="$2"
  echo "${repo_dir}/machines/${hostname}.conf"
}

write_machine_conf() {
  local conf_file="$1"
  local project_path="$2"
  mkdir -p "$(dirname "${conf_file}")"
  printf 'PRIMARY_PROJECT_PATH=%s\n' "${project_path}" > "${conf_file}"
}

read_primary_project_path() {
  local conf_file="$1"
  if [ ! -f "${conf_file}" ]; then
    return 1
  fi
  local line
  line="$(grep -m1 '^PRIMARY_PROJECT_PATH=' "${conf_file}")"
  echo "${line#PRIMARY_PROJECT_PATH=}"
}

# The optional PROFILE= line: the machine profile (machines/<name>/) this
# machine follows instead of its own. Fails when there isn't one.
read_machine_profile() {
  local conf_file="$1"
  local line
  [ -f "${conf_file}" ] || return 1
  line="$(grep -m1 '^PROFILE=' "${conf_file}")" || return 1
  [ -n "${line#PROFILE=}" ] || return 1
  echo "${line#PROFILE=}"
}

# Sets the PROFILE= line, or removes it when NAME is empty. Other lines are
# kept as they are.
write_machine_profile() {
  local conf_file="$1"
  local name="$2"
  local tmp
  tmp="$(mktemp)"
  grep -v '^PROFILE=' "${conf_file}" > "${tmp}" || true
  [ -z "${name}" ] || printf 'PROFILE=%s\n' "${name}" >> "${tmp}"
  cat "${tmp}" > "${conf_file}"
  rm -f "${tmp}"
}
