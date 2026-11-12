#!/usr/bin/env bats

setup() {
  load 'test_helper'
  [ -f "${REPO_ROOT}/publish-public.sh" ] || skip "publish-public.sh is not part of the public mirror"
  SRC="${BATS_TEST_TMPDIR}/src"
  PUBLIC="${BATS_TEST_TMPDIR}/public.git"
  git init -q --bare -b main "${PUBLIC}"
  git init -q -b master "${SRC}"
  mkdir -p "${SRC}/lib" "${SRC}/dot-claude/hooks"
  cp "${REPO_ROOT}/publish-public.sh" "${SRC}/"
  cp "${REPO_ROOT}/lib/safety.sh" "${SRC}/lib/"
  printf '#!/bin/bash\necho sync\n' > "${SRC}/claude-sync.sh"
  printf '#!/bin/bash\necho hook\n' > "${SRC}/dot-claude/hooks/guard.sh"
  printf '# shared\n' > "${SRC}/dot-claude/CLAUDE.md"
  printf '# public readme\n' > "${SRC}/README.public.md"
  chmod 755 "${SRC}/claude-sync.sh" "${SRC}/dot-claude/hooks/guard.sh" "${SRC}/publish-public.sh"
  git -C "${SRC}" add -A
  git -C "${SRC}" update-index --chmod=+x -- claude-sync.sh dot-claude/hooks/guard.sh publish-public.sh
  git -C "${SRC}" update-index --chmod=-x -- dot-claude/CLAUDE.md
  git -C "${SRC}" commit -q -m seed
}

# Mode of PATH on the public main branch.
public_mode() {
  git -C "${PUBLIC}" ls-tree main -- "$1" | cut -d' ' -f1
}

@test "publish-public.sh keeps HEAD's modes when git has no file mode (as on Windows)" {
  # No file mode for every git call, as Git for Windows; the disk modes
  # are the wrong way round, so only HEAD can give the right ones.
  export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.filemode GIT_CONFIG_VALUE_0=false
  chmod 644 "${SRC}/claude-sync.sh" "${SRC}/dot-claude/hooks/guard.sh"
  chmod 755 "${SRC}/dot-claude/CLAUDE.md"
  [ -z "$(git -C "${SRC}" status --porcelain)" ]
  CLAUDE_CONFIG_PUBLIC_REPO_URL="${PUBLIC}" run bash "${SRC}/publish-public.sh"
  [ "$status" -eq 0 ]
  [ "$(public_mode claude-sync.sh)" = "100755" ]
  [ "$(public_mode dot-claude/hooks/guard.sh)" = "100755" ]
  [ "$(public_mode dot-claude/CLAUDE.md)" = "100644" ]
  [ "$(public_mode README.md)" = "100644" ]
}

@test "publish-public.sh keeps modes as they are with a file mode" {
  CLAUDE_CONFIG_PUBLIC_REPO_URL="${PUBLIC}" run bash "${SRC}/publish-public.sh"
  [ "$status" -eq 0 ]
  [ "$(public_mode claude-sync.sh)" = "100755" ]
  [ "$(public_mode dot-claude/hooks/guard.sh)" = "100755" ]
  [ "$(public_mode dot-claude/CLAUDE.md)" = "100644" ]
}
