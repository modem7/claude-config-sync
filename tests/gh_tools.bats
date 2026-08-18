#!/usr/bin/env bats

setup() {
  load 'test_helper'
}

@test "does nothing if gh is already installed" {
  FAKE_BIN="${BATS_TEST_TMPDIR}/fakebin"
  mkdir -p "${FAKE_BIN}"
  APT_CALLS_LOG="${BATS_TEST_TMPDIR}/apt-calls.log"
  cat > "${FAKE_BIN}/gh" << 'EOF'
#!/usr/bin/env bash
exit 0
EOF
  chmod +x "${FAKE_BIN}/gh"
  cat > "${FAKE_BIN}/apt-get" << EOF
#!/usr/bin/env bash
echo "apt-get \$*" >> "${APT_CALLS_LOG}"
exit 0
EOF
  chmod +x "${FAKE_BIN}/apt-get"

  run env PATH="${FAKE_BIN}:${PATH}" bash "${REPO_ROOT}/gh-tools.sh"

  [ "$status" -eq 0 ]
  [[ "$output" == *"already installed"* ]]
  [ ! -f "${APT_CALLS_LOG}" ]
}

@test "installs via apt-get when gh is missing and apt-get is available" {
  FAKE_BIN="${BATS_TEST_TMPDIR}/fakebin"
  mkdir -p "${FAKE_BIN}"
  APT_CALLS_LOG="${BATS_TEST_TMPDIR}/apt-calls.log"
  # Absolute shebang: under the fully-restricted PATH below, "#!/usr/bin/env
  # bash" would itself fail (env can't find bash via that PATH either).
  cat > "${FAKE_BIN}/apt-get" << EOF
#!/bin/bash
echo "apt-get \$*" >> "${APT_CALLS_LOG}"
exit 0
EOF
  chmod +x "${FAKE_BIN}/apt-get"

  run env -i PATH="${FAKE_BIN}" HOME="${HOME}" /bin/bash "${REPO_ROOT}/gh-tools.sh"

  [ "$status" -eq 0 ]
  grep -q "apt-get update -y" "${APT_CALLS_LOG}"
  grep -q "apt-get install -y gh" "${APT_CALLS_LOG}"
}

@test "continues without aborting when the package manager install fails" {
  FAKE_BIN="${BATS_TEST_TMPDIR}/fakebin"
  mkdir -p "${FAKE_BIN}"
  cat > "${FAKE_BIN}/apt-get" << 'EOF'
#!/bin/bash
exit 1
EOF
  chmod +x "${FAKE_BIN}/apt-get"

  run env -i PATH="${FAKE_BIN}" HOME="${HOME}" /bin/bash "${REPO_ROOT}/gh-tools.sh"

  [ "$status" -eq 0 ]
  [[ "$output" == *"Install manually: https://cli.github.com"* ]]
}

@test "skips gracefully with a clear message when no package manager is found" {
  EMPTY_BIN="${BATS_TEST_TMPDIR}/emptybin"
  mkdir -p "${EMPTY_BIN}"

  run env -i PATH="${EMPTY_BIN}" HOME="${HOME}" /bin/bash "${REPO_ROOT}/gh-tools.sh"

  [ "$status" -eq 0 ]
  [[ "$output" == *"Could not auto-install gh"* ]]
}
