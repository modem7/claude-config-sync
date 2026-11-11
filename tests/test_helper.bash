#!/usr/bin/env bash
# Shared bats setup: exposes LIB_DIR so tests can source library files.

LIB_DIR="$(cd "${BATS_TEST_DIRNAME}/../lib" && pwd)"
REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/.." && pwd)"

# CI runners have no global git identity, and claude-sync.sh commits in fresh
# clones. Give every test a fixed one so results do not depend on the host.
export GIT_AUTHOR_NAME="test" GIT_AUTHOR_EMAIL="test@example.invalid"
export GIT_COMMITTER_NAME="test" GIT_COMMITTER_EMAIL="test@example.invalid"

# "! cmd" never fails a bats test unless it's the last line (errexit ignores
# negated commands), so assert a command fails with "refute cmd" instead.
refute() {
  if "$@"; then return 1; fi
}

# A stand-in for the GitHub CLI, so claude-sync.sh's PR step runs without a
# network. It logs every call to FAKE_GH_LOG and remembers open PRs per head
# branch under FAKE_GH_STATE. FAKE_GH_AUTH_RC / FAKE_GH_CREATE_RC make
# 'auth status' / 'pr create' fail. Exports CLAUDE_SYNC_GH and
# CLAUDE_SYNC_GH_REPO so every claude-sync.sh child uses it.
install_fake_gh() {
  FAKE_GH_DIR="${BATS_TEST_TMPDIR}/fake-gh"
  FAKE_GH_LOG="${FAKE_GH_DIR}/calls.log"
  FAKE_GH_STATE="${FAKE_GH_DIR}/state"
  mkdir -p "${FAKE_GH_STATE}"
  : > "${FAKE_GH_LOG}"
  cat > "${FAKE_GH_DIR}/gh" << 'EOF'
#!/usr/bin/env bash
echo "gh $*" >> "${FAKE_GH_LOG}"
head="" body="" state="" jq=""
args=("$@")
for ((i = 0; i < ${#args[@]}; i++)); do
  case "${args[i]}" in
    --head) head="${args[i+1]}" ;;
    --body-file) body="${args[i+1]}" ;;
    --state) state="${args[i+1]}" ;;
    --jq) jq="${args[i+1]}" ;;
  esac
done
key="${head//\//_}"
case "$1 $2" in
  "auth status")
    [ "${FAKE_GH_AUTH_RC:-0}" -eq 0 ] || { echo "You are not logged into any GitHub hosts." >&2; exit "${FAKE_GH_AUTH_RC}"; }
    echo "Logged in to github.com"
    ;;
  "pr list")
    if [ "${state}" = "all" ]; then
      # Mimics claude-sync's --jq: open, done (a merged PR had the tip
      # named in the query), or nopr.
      sha="$(printf '%s' "${jq}" | grep -o '[0-9a-f]\{40\}' | head -n1)"
      if [ -f "${FAKE_GH_STATE}/open-${key}" ]; then echo open
      elif [ -n "${sha}" ] && grep -qx "${sha}" "${FAKE_GH_STATE}/merged-${key}" 2>/dev/null; then echo done
      else echo nopr; fi
    else
      [ ! -f "${FAKE_GH_STATE}/open-${key}" ] || cat "${FAKE_GH_STATE}/open-${key}"
    fi
    ;;
  "pr create")
    [ "${FAKE_GH_CREATE_RC:-0}" -eq 0 ] || { echo "pull request create failed" >&2; exit "${FAKE_GH_CREATE_RC}"; }
    [ -f "${body}" ] && cp "${body}" "${FAKE_GH_STATE}/body-${key}"
    n=$(( $(cat "${FAKE_GH_STATE}/count" 2>/dev/null || echo 0) + 1 ))
    echo "${n}" > "${FAKE_GH_STATE}/count"
    echo "https://github.example/test/repo/pull/${n}" | tee "${FAKE_GH_STATE}/open-${key}"
    ;;
  *)
    echo "fake gh: unexpected call: $*" >&2
    exit 1
    ;;
esac
EOF
  chmod +x "${FAKE_GH_DIR}/gh"
  export FAKE_GH_LOG FAKE_GH_STATE
  export CLAUDE_SYNC_GH="${FAKE_GH_DIR}/gh"
  export CLAUDE_SYNC_GH_REPO="test/repo"
}

# Simulates merging machine HOST's sync PR on GitHub: squash (default) or a
# merge commit into ORIGIN's master, then deletes the branch (unless
# "keep") and marks its PR closed.
merge_sync_pr() {  # merge_sync_pr ORIGIN HOST [squash|merge] [keep]
  local origin="$1" host="$2" mode="${3:-squash}" keep="${4:-}" w
  w="$(mktemp -d "${BATS_TEST_TMPDIR}/merge.XXXXXX")"
  git clone -q "${origin}" "${w}/c" 2>/dev/null
  git -C "${w}/c" fetch -q origin "sync/${host}"
  [ -z "${FAKE_GH_STATE:-}" ] || git -C "${origin}" rev-parse "sync/${host}" >> "${FAKE_GH_STATE}/merged-sync_${host}"
  if [ "${mode}" = "merge" ]; then
    git -C "${w}/c" -c user.email=gh@test -c user.name=gh merge -q --no-ff -m "Merge sync/${host}" FETCH_HEAD
  else
    git -C "${w}/c" merge -q --squash FETCH_HEAD
    git -C "${w}/c" -c user.email=gh@test -c user.name=gh commit -q -m "sync: ${host} (squashed)"
  fi
  git -C "${w}/c" push -q origin master 2>/dev/null
  [ "${keep}" = "keep" ] || git -C "${origin}" update-ref -d "refs/heads/sync/${host}"
  rm -f "${FAKE_GH_STATE:-/nonexistent}/open-sync_${host}"
}

# After a repo-changing command on machine HOST succeeded: if it pushed a
# sync branch, merge that PR and let the machine sync once more, so the
# clone ends where it would once its changes are shared. RUNNER... is the
# command that runs claude-sync.sh as that machine (given "sync").
merge_and_converge() {  # merge_and_converge ORIGIN HOST RUNNER...
  local origin="$1" host="$2" log
  shift 2
  git -C "${origin}" show-ref -q --verify "refs/heads/sync/${host}" || return 0
  merge_sync_pr "${origin}" "${host}"
  log="${BATS_TEST_TMPDIR}/converge-${host}.log"
  if ! "$@" sync > "${log}" 2>&1; then
    echo "converging sync for ${host} failed:" >&2
    cat "${log}" >&2
    return 1
  fi
}

# What a file holds on machine REPO's sync branch, else its default branch.
effective_show() {  # effective_show REPO HOST PATH
  local ref="sync/$2"
  git -C "$1" show-ref -q --verify "refs/heads/${ref}" || ref=HEAD
  git -C "$1" show "${ref}:$3"
}
