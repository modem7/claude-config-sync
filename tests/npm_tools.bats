#!/usr/bin/env bats

setup() {
  load 'test_helper'
}

@test "provisions each declared tool via npm install -g <pkg>@latest" {
  FAKE_BIN="${BATS_TEST_TMPDIR}/fakebin"
  mkdir -p "${FAKE_BIN}"
  NPM_CALLS_LOG="${BATS_TEST_TMPDIR}/npm-calls.log"
  cat > "${FAKE_BIN}/npm" << EOF
#!/usr/bin/env bash
echo "npm \$*" >> "${NPM_CALLS_LOG}"
exit 0
EOF
  chmod +x "${FAKE_BIN}/npm"

  PATH="${FAKE_BIN}:${PATH}" bash "${REPO_ROOT}/npm-tools.sh"

  grep -q "npm install -g ccusage@latest" "${NPM_CALLS_LOG}"
}

@test "continues past a failed tool install rather than aborting" {
  FAKE_BIN="${BATS_TEST_TMPDIR}/fakebin"
  mkdir -p "${FAKE_BIN}"
  cat > "${FAKE_BIN}/npm" << 'EOF'
#!/usr/bin/env bash
exit 1
EOF
  chmod +x "${FAKE_BIN}/npm"

  run env PATH="${FAKE_BIN}:${PATH}" bash "${REPO_ROOT}/npm-tools.sh"

  [ "$status" -eq 0 ]
  [[ "$output" == *"Failed to install/update ccusage"* ]]
}

@test "skips gracefully with a clear message when npm is not available" {
  EMPTY_BIN="${BATS_TEST_TMPDIR}/emptybin"
  mkdir -p "${EMPTY_BIN}"

  run env -i PATH="${EMPTY_BIN}" /bin/bash "${REPO_ROOT}/npm-tools.sh"

  [ "$status" -eq 0 ]
  [[ "$output" == *"npm not found"* ]]
}
