#!/usr/bin/env bats

setup() {
  load 'test_helper'
  FAKE_BIN="${BATS_TEST_TMPDIR}/fakebin"
  mkdir -p "${FAKE_BIN}"
  CALLS_LOG="${BATS_TEST_TMPDIR}/calls.log"
}

# Absolute shebangs: under the fully-restricted PATH used below,
# "#!/usr/bin/env bash" would itself fail.
fake_cmd() {
  cat > "${FAKE_BIN}/$1" << EOF
#!/bin/bash
echo "$1 \$*" >> "${CALLS_LOG}"
exit ${2:-0}
EOF
  chmod +x "${FAKE_BIN}/$1"
}

@test "skips apt-get when python is already available" {
  fake_cmd python
  fake_cmd python3
  fake_cmd apt-get

  run env -i PATH="${FAKE_BIN}" HOME="${HOME}" /bin/bash "${REPO_ROOT}/python-tools.sh"

  [ "$status" -eq 0 ]
  [[ "$output" == *"python already available"* ]]
  ! grep -q "apt-get" "${CALLS_LOG}"
}

@test "installs python-is-python3 when python is missing" {
  fake_cmd python3
  fake_cmd apt-get

  run env -i PATH="${FAKE_BIN}" HOME="${HOME}" /bin/bash "${REPO_ROOT}/python-tools.sh"

  [ "$status" -eq 0 ]
  grep -q "apt-get install -y python-is-python3" "${CALLS_LOG}"
}

@test "installs the pinned pip packages with --break-system-packages" {
  fake_cmd python
  fake_cmd python3

  run env -i PATH="${FAKE_BIN}" HOME="${HOME}" /bin/bash "${REPO_ROOT}/python-tools.sh"

  [ "$status" -eq 0 ]
  grep -q "python3 -m pip install --break-system-packages mcp<2 aiosqlite pyyaml" "${CALLS_LOG}"
}

@test "continues without aborting when installs fail" {
  fake_cmd python3 1
  fake_cmd apt-get 1

  run env -i PATH="${FAKE_BIN}" HOME="${HOME}" /bin/bash "${REPO_ROOT}/python-tools.sh"

  [ "$status" -eq 0 ]
  [[ "$output" == *"Failed to install python-is-python3"* ]]
  [[ "$output" == *"Failed to install pip packages"* ]]
}

@test "skips gracefully when neither python3 nor apt-get exist" {
  run env -i PATH="${FAKE_BIN}" HOME="${HOME}" /bin/bash "${REPO_ROOT}/python-tools.sh"

  [ "$status" -eq 0 ]
  [[ "$output" == *"link python to python3 manually"* ]]
  [[ "$output" == *"python3 not found"* ]]
}
