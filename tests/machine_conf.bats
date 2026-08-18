#!/usr/bin/env bats

setup() {
  load 'test_helper'
  source "${LIB_DIR}/machine-conf.sh"
}

@test "machine_conf_path builds path from repo_dir and hostname" {
  result="$(machine_conf_path "/tmp/repo" "my-host")"
  [ "$result" = "/tmp/repo/machines/my-host.conf" ]
}

@test "write_machine_conf then read_primary_project_path round-trips" {
  conf_file="${BATS_TEST_TMPDIR}/my-host.conf"
  write_machine_conf "${conf_file}" "/home/modem7/project"
  result="$(read_primary_project_path "${conf_file}")"
  [ "$result" = "/home/modem7/project" ]
}

@test "read_primary_project_path fails when the conf file is missing" {
  run read_primary_project_path "${BATS_TEST_TMPDIR}/does-not-exist.conf"
  [ "$status" -eq 1 ]
}
