#!/usr/bin/env bash
# Reads and writes the per-machine PRIMARY_PROJECT_PATH config file that
# maps a machine's hostname to the local project whose memory dir is synced.

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
