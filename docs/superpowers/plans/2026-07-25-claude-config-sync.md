# Claude Config Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the `claude-config` repo's bootstrap scaffolding and a copy-based `install.sh`/`claude-sync.sh` tool that keeps Claude Code's global config (CLAUDE.md, settings.json, personal skills, "global" auto-memory) synced across machines via `github.com/modem7/claude-config`.

**Architecture:** Small bash CLI split into single-responsibility library files under `lib/` (path encoding, machine-config I/O, denylist safety check, file capture/apply, skill-symlink wiring), a thin `claude-sync.sh` dispatcher that wires them together with git plumbing, and an `install.sh` first-run wrapper. Every path the libraries touch is parameterized via env vars so the bats-core test suite can run the real code against a fake `$HOME` and a local bare git repo instead of the real machine or GitHub.

**Tech Stack:** bash, git, jq, rsync, bats-core (1.10.0), shellcheck (0.9.0) — all already installed on this VM. No Windows-native support needed (confirmed: Windows machines only ever use Claude Desktop's Remote Control against a Linux box; actual sync always runs on Linux).

## Global Constraints

- Never sync `.credentials.json`, `~/.claude.json`, or any plugin cache/session-state directory (`sessions/`, `session-env/`, `shell-snapshots/`, `security/`, `tasks/`, `teams/`, `remote/`) — per the design spec's "Never touched" list.
- In-repo directory names are `dot-claude/` and `dot-agents/`, never `.claude/`/`.agents/` — this deliberately avoids colliding with the inherited `.gitignore` rule that blanket-ignores `.claude/`.
- Default branch is `master`; **never push directly to it** — all work lands via a feature branch + PR (repo owner's stored global preference). This plan already moved past commits onto branch `claude-config-bootstrap`.
- **Never add a `Co-Authored-By` trailer to any commit** (repo owner's stored global preference).
- Copy-based sync only (Approach B from the spec) — no live symlinks for tracked config content, since Claude Code may write files via write-temp-then-rename, which would silently break a symlink. The only symlinks this tool creates are the local `~/.claude/skills/<name>` discovery links, which are machine-local plumbing, not synced content.
- Every path a library function touches must come from a parameter or an overridable env var — never a hardcoded `$HOME` — so tests can point everything at a temp directory.
- Existing directories that are real (not symlinks) — `~/.claude/skills/find-skills`, `~/.claude/skills/webapp-testing` — must never be overwritten by the skill-symlink wiring step.

---

## File Structure

```
claude-config/
├── README.md                          # Task 9
├── LICENSE                             # already present, untouched
├── CONTRIBUTING.md                     # Task 1
├── .editorconfig                       # Task 1
├── .gitattributes                      # Task 1
├── .gitignore                          # Task 1
├── .wakatime-project                   # Task 1
├── renovate.json                       # Task 1
├── install.sh                          # Task 8
├── claude-sync.sh                      # Task 7
├── lib/
│   ├── memory-path.sh                  # Task 2
│   ├── machine-conf.sh                 # Task 3
│   ├── safety.sh                       # Task 4
│   ├── sync-files.sh                   # Task 5
│   └── skills-symlink.sh               # Task 6
├── .github/
│   ├── settings.yml                    # Task 1
│   ├── CODEOWNERS                      # Task 1
│   ├── PULL_REQUEST_TEMPLATE.md        # Task 1
│   ├── ISSUE_TEMPLATE/                 # Task 1
│   │   ├── bug_report.yml
│   │   ├── feature_request.yml
│   │   └── config.yml
│   └── workflows/
│       ├── autoassign.yml              # Task 1
│       └── ci.yml                      # Task 10
├── tests/
│   ├── test_helper.bash                # Task 2
│   ├── memory_path.bats                # Task 2
│   ├── machine_conf.bats               # Task 3
│   ├── safety.bats                     # Task 4
│   ├── sync_files.bats                 # Task 5
│   ├── skills_symlink.bats             # Task 6
│   └── sync_cli.bats                   # Task 7 + Task 8
├── machines/.gitkeep                   # Task 1
├── dot-claude/agents/.gitkeep          # Task 1
├── dot-agents/skills/.gitkeep          # Task 1
└── memory/.gitkeep                     # Task 1
```

`dot-claude/CLAUDE.md`, `dot-claude/settings.json`, and real content under
`dot-agents/skills/`/`memory/` are **not** authored by hand — they get
populated by Task 11 when `claude-sync.sh sync` actually runs against this
real machine for the first time.

---

### Task 1: Repo scaffolding (branch safety + DefaultRepo-derived template files)

**Files:**
- Create: `.editorconfig`, `.gitattributes`, `CONTRIBUTING.md`, `.wakatime-project`, `.gitignore`, `renovate.json`
- Create: `.github/settings.yml`, `.github/CODEOWNERS`, `.github/PULL_REQUEST_TEMPLATE.md`, `.github/ISSUE_TEMPLATE/bug_report.yml`, `.github/ISSUE_TEMPLATE/feature_request.yml`, `.github/ISSUE_TEMPLATE/config.yml`, `.github/workflows/autoassign.yml`
- Create: `machines/.gitkeep`, `dot-claude/agents/.gitkeep`, `dot-agents/skills/.gitkeep`, `memory/.gitkeep`

**Interfaces:**
- Produces: the directory skeleton every later task's tests and scripts assume exists (`lib/`, `tests/`, `machines/`, `dot-claude/agents/`, `dot-agents/skills/`, `memory/`).

- [ ] **Step 1: Confirm working on the feature branch, not master**

```bash
cd ~/claude-config
git branch --show-current
```
Expected: `claude-config-bootstrap` (this was already set up before this plan was written — if it prints `master`, stop and run `git checkout claude-config-bootstrap` before continuing).

- [ ] **Step 2: Copy verbatim files from DefaultRepo**

```bash
cd ~/claude-config
mkdir -p .github/ISSUE_TEMPLATE
for f in .editorconfig .gitattributes CONTRIBUTING.md \
         .github/CODEOWNERS .github/PULL_REQUEST_TEMPLATE.md \
         .github/ISSUE_TEMPLATE/bug_report.yml \
         .github/ISSUE_TEMPLATE/feature_request.yml \
         .github/ISSUE_TEMPLATE/config.yml \
         .github/workflows/autoassign.yml; do
  mkdir -p "$(dirname "$f")"
  gh api "repos/modem7/DefaultRepo/contents/$f" --jq '.content' | base64 -d > "$f"
done
```

- [ ] **Step 3: Write `.wakatime-project`**

```bash
echo "claude-config" > .wakatime-project
```

- [ ] **Step 4: Write `.gitignore`**

Create `.gitignore`:

```
# ========================
# OS / System
# ========================
.DS_Store
.DS_Store?
._*
.Spotlight-V100
.Trashes
Thumbs.db
Desktop.ini

# ========================
# Claude
# ========================
.claude/

# ========================
# Editor / IDE
# ========================
.idea/
*.iml
.vscode/
*.swp
*.swo
*~
.project
.classpath
.settings/

# ========================
# Secrets & Credentials
# ========================
.env
.env.*
!.env.example
*.pem
*.key
*.p12
*.pfx
secrets/
credentials/
.credentials.json
.claude.json

# ========================
# Build Artifacts
# ========================
dist/
build/
out/
target/
*.o
*.a
*.so
*.dylib
*.exe
*.out

# ========================
# Logs & Temp
# ========================
*.log
```

The `.claude/` rule is inherited unchanged from `DefaultRepo` — this repo
never creates a directory literally named `.claude/` at its root (it uses
`dot-claude/`), so the rule can't accidentally hide tracked content. The
`.credentials.json` and `.claude.json` lines are added specifically because
those exact filenames aren't covered by the generic secret patterns above
them.

- [ ] **Step 5: Write `renovate.json`**

```json
{
  "$schema": "https://docs.renovatebot.com/renovate-schema.json",
  "extends": ["github>modem7/renovate-config:shared"]
}
```

- [ ] **Step 6: Write `.github/settings.yml`**

```yaml
# https://github.com/apps/settings
# https://github.com/repository-settings/app

repository:
  description: Private sync repo for Claude Code config (CLAUDE.md, settings, personal skills, memory) across machines
  homepage: https://modem7.com
  topics: claude-code, dotfiles, config-sync

  private: true
  has_issues: true
  has_wiki: false
  has_downloads: true
  has_projects: false
  has_discussions: false

  default_branch: master

  allow_squash_merge: true
  allow_rebase_merge: true
  allow_merge_commit: true

  delete_branch_on_merge: true
  allow_update_branch: true

  enable_automated_security_fixes: true
  enable_vulnerability_alerts: true

labels:
  - name: bug
    color: d73a4a
    description: "Something isn't working"
  - name: dependencies
    color: C62109
    description: "Pull requests that update a dependency file"
  - name: documentation
    color: "0075ca"
    description: "Improvements or additions to documentation"
  - name: duplicate
    color: cfd3d7
    description: "This issue or pull request already exists"
  - name: enhancement
    color: a2eeef
    description: "New feature or request"
  - name: good first issue
    color: "7057ff"
    description: "Good for newcomers"
  - name: help wanted
    color: "008672"
    description: "Extra attention is needed"
  - name: invalid
    color: e4e669
    description: "This doesn't seem right"
  - name: question
    color: d876e3
    description: "Further information is requested"
  - name: wontfix
    color: ffffff
    description: "This will not be worked on"
```

- [ ] **Step 7: Create the directory skeleton with placeholders**

```bash
cd ~/claude-config
mkdir -p lib tests machines dot-claude/agents dot-agents/skills memory
touch machines/.gitkeep dot-claude/agents/.gitkeep dot-agents/skills/.gitkeep memory/.gitkeep
```

- [ ] **Step 8: Validate syntax**

```bash
cd ~/claude-config
jq empty renovate.json && echo "renovate.json OK"
python3 -c "import yaml; yaml.safe_load(open('.github/settings.yml')); print('settings.yml OK')"
python3 -c "import yaml; yaml.safe_load(open('.github/workflows/autoassign.yml')); print('autoassign.yml OK')"
```
Expected: all three print `OK`.

- [ ] **Step 9: Commit**

```bash
cd ~/claude-config
git add .editorconfig .gitattributes CONTRIBUTING.md .wakatime-project \
        .gitignore renovate.json .github machines dot-claude dot-agents memory
git commit -m "Scaffold repo from DefaultRepo template"
```

---

### Task 2: `lib/memory-path.sh` — memory directory path resolution

**Files:**
- Create: `lib/memory-path.sh`
- Create: `tests/test_helper.bash`
- Test: `tests/memory_path.bats`

**Interfaces:**
- Produces: `encode_project_path(path)` → prints encoded string to stdout. `resolve_memory_dir(claude_home, project_path)` → prints full memory directory path to stdout.

- [ ] **Step 1: Write `tests/test_helper.bash`**

```bash
#!/usr/bin/env bash
# Shared bats setup: exposes LIB_DIR so tests can source library files.

LIB_DIR="$(cd "${BATS_TEST_DIRNAME}/../lib" && pwd)"
REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/.." && pwd)"
```

- [ ] **Step 2: Write the failing test — `tests/memory_path.bats`**

```bash
#!/usr/bin/env bats

setup() {
  load 'test_helper'
  source "${LIB_DIR}/memory-path.sh"
}

@test "encode_project_path replaces every slash with a dash" {
  result="$(encode_project_path "/home/modem7/project")"
  [ "$result" = "-home-modem7-project" ]
}

@test "encode_project_path handles nested subproject paths" {
  result="$(encode_project_path "/home/modem7/project/christmas-countdown")"
  [ "$result" = "-home-modem7-project-christmas-countdown" ]
}

@test "resolve_memory_dir builds the full memory path under claude_home" {
  result="$(resolve_memory_dir "/home/modem7/.claude" "/home/modem7/project")"
  [ "$result" = "/home/modem7/.claude/projects/-home-modem7-project/memory" ]
}
```

- [ ] **Step 3: Run the test suite to verify it fails**

```bash
cd ~/claude-config
bats tests/memory_path.bats
```
Expected: FAIL — `source: lib/memory-path.sh: No such file or directory` (or similar "command not found" for `encode_project_path`).

- [ ] **Step 4: Write `lib/memory-path.sh`**

```bash
#!/usr/bin/env bash
# Resolves the on-disk memory directory Claude Code uses for a given
# working-directory path (Claude Code encodes cwd by replacing / with -).

encode_project_path() {
  local path="$1"
  echo "${path//\//-}"
}

resolve_memory_dir() {
  local claude_home="$1"
  local project_path="$2"
  local encoded
  encoded="$(encode_project_path "${project_path}")"
  echo "${claude_home}/projects/${encoded}/memory"
}
```

- [ ] **Step 5: Run the test suite to verify it passes**

```bash
cd ~/claude-config
bats tests/memory_path.bats
```
Expected: `3 tests, 0 failures`.

- [ ] **Step 6: shellcheck the new library file**

```bash
cd ~/claude-config
shellcheck lib/memory-path.sh
```
Expected: no output (clean).

- [ ] **Step 7: Commit**

```bash
cd ~/claude-config
git add lib/memory-path.sh tests/test_helper.bash tests/memory_path.bats
git commit -m "Add memory directory path resolution"
```

---

### Task 3: `lib/machine-conf.sh` — per-machine config file I/O

**Files:**
- Create: `lib/machine-conf.sh`
- Test: `tests/machine_conf.bats`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `machine_conf_path(repo_dir, hostname)` → prints path to stdout. `write_machine_conf(conf_file, project_path)` → writes the file. `read_primary_project_path(conf_file)` → prints `PRIMARY_PROJECT_PATH` value to stdout, returns 1 if the file doesn't exist.

- [ ] **Step 1: Write the failing test — `tests/machine_conf.bats`**

```bash
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
```

- [ ] **Step 2: Run the test suite to verify it fails**

```bash
cd ~/claude-config
bats tests/machine_conf.bats
```
Expected: FAIL — `machine_conf_path: command not found`.

- [ ] **Step 3: Write `lib/machine-conf.sh`**

```bash
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
  # shellcheck disable=SC1090
  source "${conf_file}"
  echo "${PRIMARY_PROJECT_PATH}"
}
```

- [ ] **Step 4: Run the test suite to verify it passes**

```bash
cd ~/claude-config
bats tests/machine_conf.bats
```
Expected: `3 tests, 0 failures`.

- [ ] **Step 5: shellcheck**

```bash
cd ~/claude-config
shellcheck lib/machine-conf.sh
```
Expected: no output.

- [ ] **Step 6: Commit**

```bash
cd ~/claude-config
git add lib/machine-conf.sh tests/machine_conf.bats
git commit -m "Add per-machine config file I/O"
```

---

### Task 4: `lib/safety.sh` — denylisted-filename guard

**Files:**
- Create: `lib/safety.sh`
- Test: `tests/safety.bats`

**Interfaces:**
- Produces: `assert_no_denylisted_files(repo_dir)` → returns 0 if clean, returns 1 and prints the offending filename to stderr otherwise.

- [ ] **Step 1: Write the failing test — `tests/safety.bats`**

```bash
#!/usr/bin/env bats

setup() {
  load 'test_helper'
  source "${LIB_DIR}/safety.sh"
}

@test "assert_no_denylisted_files passes on a clean tree" {
  mkdir -p "${BATS_TEST_TMPDIR}/clean/dot-claude"
  echo "hello" > "${BATS_TEST_TMPDIR}/clean/dot-claude/CLAUDE.md"
  run assert_no_denylisted_files "${BATS_TEST_TMPDIR}/clean"
  [ "$status" -eq 0 ]
}

@test "assert_no_denylisted_files catches a leaked .credentials.json" {
  mkdir -p "${BATS_TEST_TMPDIR}/dirty/dot-claude"
  echo "secret" > "${BATS_TEST_TMPDIR}/dirty/dot-claude/.credentials.json"
  run assert_no_denylisted_files "${BATS_TEST_TMPDIR}/dirty"
  [ "$status" -eq 1 ]
}

@test "assert_no_denylisted_files catches a leaked .claude.json anywhere in the tree" {
  mkdir -p "${BATS_TEST_TMPDIR}/dirty2/nested/deep"
  echo "secret" > "${BATS_TEST_TMPDIR}/dirty2/nested/deep/.claude.json"
  run assert_no_denylisted_files "${BATS_TEST_TMPDIR}/dirty2"
  [ "$status" -eq 1 ]
}
```

- [ ] **Step 2: Run the test suite to verify it fails**

```bash
cd ~/claude-config
bats tests/safety.bats
```
Expected: FAIL — `assert_no_denylisted_files: command not found`.

- [ ] **Step 3: Write `lib/safety.sh`**

```bash
#!/usr/bin/env bash
# Guards against ever committing account/auth secrets into the repo.

DENYLISTED_FILENAMES=(".credentials.json" ".claude.json")

assert_no_denylisted_files() {
  local repo_dir="$1"
  local name found

  for name in "${DENYLISTED_FILENAMES[@]}"; do
    found="$(find "${repo_dir}" -name "${name}" -print -quit)"
    if [ -n "${found}" ]; then
      echo "Denylisted file found in repo tree: ${found}" >&2
      return 1
    fi
  done
  return 0
}
```

- [ ] **Step 4: Run the test suite to verify it passes**

```bash
cd ~/claude-config
bats tests/safety.bats
```
Expected: `3 tests, 0 failures`.

- [ ] **Step 5: shellcheck**

```bash
cd ~/claude-config
shellcheck lib/safety.sh
```
Expected: no output.

- [ ] **Step 6: Commit**

```bash
cd ~/claude-config
git add lib/safety.sh tests/safety.bats
git commit -m "Add denylisted-filename safety guard"
```

---

### Task 5: `lib/sync-files.sh` — capture/apply file copying

**Files:**
- Create: `lib/sync-files.sh`
- Test: `tests/sync_files.bats`

**Interfaces:**
- Consumes: nothing from earlier tasks (pure filesystem operations).
- Produces: `sync_capture(repo_dir, claude_home, agents_home, memory_dir)` — copies real→repo. `sync_apply(repo_dir, claude_home, agents_home, memory_dir)` — copies repo→real. Both are used unmodified by Task 7's `claude-sync.sh`.

- [ ] **Step 1: Write the failing test — `tests/sync_files.bats`**

```bash
#!/usr/bin/env bats

setup() {
  load 'test_helper'
  source "${LIB_DIR}/sync-files.sh"

  REPO="${BATS_TEST_TMPDIR}/repo"
  CLAUDE_HOME="${BATS_TEST_TMPDIR}/claude_home"
  AGENTS_HOME="${BATS_TEST_TMPDIR}/agents_home"
  MEMORY_DIR="${BATS_TEST_TMPDIR}/claude_home/projects/-home-modem7-project/memory"

  mkdir -p "${REPO}/dot-claude/agents" "${REPO}/dot-agents/skills" "${REPO}/memory"
  mkdir -p "${CLAUDE_HOME}" "${AGENTS_HOME}/skills/release" "${MEMORY_DIR}"

  echo "# global instructions" > "${CLAUDE_HOME}/CLAUDE.md"
  echo '{"theme":"dark"}' > "${CLAUDE_HOME}/settings.json"
  echo "release skill body" > "${AGENTS_HOME}/skills/release/SKILL.md"
  echo "# Memory Index" > "${MEMORY_DIR}/MEMORY.md"
}

@test "sync_capture copies CLAUDE.md and settings.json into the repo" {
  sync_capture "${REPO}" "${CLAUDE_HOME}" "${AGENTS_HOME}" "${MEMORY_DIR}"
  [ "$(cat "${REPO}/dot-claude/CLAUDE.md")" = "# global instructions" ]
  [ "$(cat "${REPO}/dot-claude/settings.json")" = '{"theme":"dark"}' ]
}

@test "sync_capture copies personal skills into dot-agents/skills" {
  sync_capture "${REPO}" "${CLAUDE_HOME}" "${AGENTS_HOME}" "${MEMORY_DIR}"
  [ "$(cat "${REPO}/dot-agents/skills/release/SKILL.md")" = "release skill body" ]
}

@test "sync_capture copies memory content into the repo" {
  sync_capture "${REPO}" "${CLAUDE_HOME}" "${AGENTS_HOME}" "${MEMORY_DIR}"
  [ "$(cat "${REPO}/memory/MEMORY.md")" = "# Memory Index" ]
}

@test "sync_apply copies repo content back out to a fresh machine" {
  sync_capture "${REPO}" "${CLAUDE_HOME}" "${AGENTS_HOME}" "${MEMORY_DIR}"

  FRESH_CLAUDE_HOME="${BATS_TEST_TMPDIR}/fresh_claude_home"
  FRESH_AGENTS_HOME="${BATS_TEST_TMPDIR}/fresh_agents_home"
  FRESH_MEMORY_DIR="${BATS_TEST_TMPDIR}/fresh_claude_home/projects/-home-modem7-work/memory"

  sync_apply "${REPO}" "${FRESH_CLAUDE_HOME}" "${FRESH_AGENTS_HOME}" "${FRESH_MEMORY_DIR}"

  [ "$(cat "${FRESH_CLAUDE_HOME}/CLAUDE.md")" = "# global instructions" ]
  [ "$(cat "${FRESH_AGENTS_HOME}/skills/release/SKILL.md")" = "release skill body" ]
  [ "$(cat "${FRESH_MEMORY_DIR}/MEMORY.md")" = "# Memory Index" ]
}

@test "sync_capture does not fail when claude_home/agents does not exist yet" {
  rm -rf "${CLAUDE_HOME:?}/agents"
  run sync_capture "${REPO}" "${CLAUDE_HOME}" "${AGENTS_HOME}" "${MEMORY_DIR}"
  [ "$status" -eq 0 ]
}
```

- [ ] **Step 2: Run the test suite to verify it fails**

```bash
cd ~/claude-config
bats tests/sync_files.bats
```
Expected: FAIL — `sync_capture: command not found`.

- [ ] **Step 3: Write `lib/sync-files.sh`**

```bash
#!/usr/bin/env bash
# Copies tracked Claude Code config between the repo working tree and the
# real local paths. Copy-based (not symlinked) so a tool writing a file via
# write-temp-then-rename can never silently break the sync.

sync_capture() {
  local repo_dir="$1"
  local claude_home="$2"
  local agents_home="$3"
  local memory_dir="$4"

  mkdir -p "${repo_dir}/dot-claude/agents" "${repo_dir}/dot-agents/skills" "${repo_dir}/memory"

  [ -f "${claude_home}/CLAUDE.md" ] && cp "${claude_home}/CLAUDE.md" "${repo_dir}/dot-claude/CLAUDE.md"
  [ -f "${claude_home}/settings.json" ] && cp "${claude_home}/settings.json" "${repo_dir}/dot-claude/settings.json"
  [ -d "${claude_home}/agents" ] && rsync -a --delete "${claude_home}/agents/" "${repo_dir}/dot-claude/agents/"
  [ -d "${agents_home}/skills" ] && rsync -a --delete "${agents_home}/skills/" "${repo_dir}/dot-agents/skills/"
  [ -d "${memory_dir}" ] && rsync -a --delete "${memory_dir}/" "${repo_dir}/memory/"
}

sync_apply() {
  local repo_dir="$1"
  local claude_home="$2"
  local agents_home="$3"
  local memory_dir="$4"

  mkdir -p "${claude_home}/agents" "${agents_home}/skills" "${memory_dir}"

  [ -f "${repo_dir}/dot-claude/CLAUDE.md" ] && cp "${repo_dir}/dot-claude/CLAUDE.md" "${claude_home}/CLAUDE.md"
  [ -f "${repo_dir}/dot-claude/settings.json" ] && cp "${repo_dir}/dot-claude/settings.json" "${claude_home}/settings.json"
  rsync -a --delete "${repo_dir}/dot-claude/agents/" "${claude_home}/agents/"
  rsync -a --delete "${repo_dir}/dot-agents/skills/" "${agents_home}/skills/"
  rsync -a --delete "${repo_dir}/memory/" "${memory_dir}/"
}
```

- [ ] **Step 4: Run the test suite to verify it passes**

```bash
cd ~/claude-config
bats tests/sync_files.bats
```
Expected: `5 tests, 0 failures`.

- [ ] **Step 5: shellcheck**

```bash
cd ~/claude-config
shellcheck lib/sync-files.sh
```
Expected: no output.

- [ ] **Step 6: Commit**

```bash
cd ~/claude-config
git add lib/sync-files.sh tests/sync_files.bats
git commit -m "Add capture/apply file sync logic"
```

---

### Task 6: `lib/skills-symlink.sh` — skill discovery wiring

**Files:**
- Create: `lib/skills-symlink.sh`
- Test: `tests/skills_symlink.bats`

**Interfaces:**
- Produces: `wire_skill_symlinks(repo_dir, claude_home, agents_home)` — for each directory under `repo_dir/dot-agents/skills/`, ensures `claude_home/skills/<name>` is a symlink to `agents_home/skills/<name>`, without ever touching a `claude_home/skills/<name>` that already exists as a real (non-symlink) directory.

- [ ] **Step 1: Write the failing test — `tests/skills_symlink.bats`**

```bash
#!/usr/bin/env bats

setup() {
  load 'test_helper'
  source "${LIB_DIR}/skills-symlink.sh"

  REPO="${BATS_TEST_TMPDIR}/repo"
  CLAUDE_HOME="${BATS_TEST_TMPDIR}/claude_home"
  AGENTS_HOME="${BATS_TEST_TMPDIR}/agents_home"

  mkdir -p "${REPO}/dot-agents/skills/release"
  mkdir -p "${AGENTS_HOME}/skills/release"
  mkdir -p "${CLAUDE_HOME}/skills"
}

@test "wire_skill_symlinks creates a symlink for a tracked skill" {
  wire_skill_symlinks "${REPO}" "${CLAUDE_HOME}" "${AGENTS_HOME}"
  [ -L "${CLAUDE_HOME}/skills/release" ]
  [ "$(readlink "${CLAUDE_HOME}/skills/release")" = "${AGENTS_HOME}/skills/release" ]
}

@test "wire_skill_symlinks does not touch a pre-existing real directory" {
  mkdir -p "${CLAUDE_HOME}/skills/find-skills"
  echo "builtin content" > "${CLAUDE_HOME}/skills/find-skills/marker.txt"

  wire_skill_symlinks "${REPO}" "${CLAUDE_HOME}" "${AGENTS_HOME}"

  [ ! -L "${CLAUDE_HOME}/skills/find-skills" ]
  [ "$(cat "${CLAUDE_HOME}/skills/find-skills/marker.txt")" = "builtin content" ]
}

@test "wire_skill_symlinks is idempotent when re-run" {
  wire_skill_symlinks "${REPO}" "${CLAUDE_HOME}" "${AGENTS_HOME}"
  wire_skill_symlinks "${REPO}" "${CLAUDE_HOME}" "${AGENTS_HOME}"
  [ -L "${CLAUDE_HOME}/skills/release" ]
}
```

- [ ] **Step 2: Run the test suite to verify it fails**

```bash
cd ~/claude-config
bats tests/skills_symlink.bats
```
Expected: FAIL — `wire_skill_symlinks: command not found`.

- [ ] **Step 3: Write `lib/skills-symlink.sh`**

```bash
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
```

- [ ] **Step 4: Run the test suite to verify it passes**

```bash
cd ~/claude-config
bats tests/skills_symlink.bats
```
Expected: `3 tests, 0 failures`.

- [ ] **Step 5: shellcheck**

```bash
cd ~/claude-config
shellcheck lib/skills-symlink.sh
```
Expected: no output.

- [ ] **Step 6: Commit**

```bash
cd ~/claude-config
git add lib/skills-symlink.sh tests/skills_symlink.bats
git commit -m "Add skill discovery symlink wiring"
```

---

### Task 7: `claude-sync.sh` — CLI dispatcher (push/pull/sync)

**Files:**
- Create: `claude-sync.sh`
- Test: `tests/sync_cli.bats` (first half — push/pull/sync round trip)

**Interfaces:**
- Consumes: `encode_project_path`, `resolve_memory_dir` (Task 2); `machine_conf_path`, `write_machine_conf`, `read_primary_project_path` (Task 3); `assert_no_denylisted_files` (Task 4); `sync_capture`, `sync_apply` (Task 5); `wire_skill_symlinks` (Task 6).
- Produces: `claude-sync.sh push|pull|sync` CLI, invoked as `<repo_dir>/claude-sync.sh <subcommand>`. Honors env var overrides `CLAUDE_SYNC_CLAUDE_HOME` (default `${HOME}/.claude`), `CLAUDE_SYNC_AGENTS_HOME` (default `${HOME}/.agents`), `CLAUDE_SYNC_HOSTNAME` (default `$(hostname)`) — this is what Task 8's `install.sh` and all bats tests rely on to run against a fake `$HOME`.

- [ ] **Step 1: Write the failing test — `tests/sync_cli.bats`**

This test builds a local bare git repo to stand in for GitHub, seeds it with
a real checkout containing the scripts under test, then drives two
simulated machines through `push` and `pull`.

```bash
#!/usr/bin/env bats

setup() {
  load 'test_helper'

  ORIGIN="${BATS_TEST_TMPDIR}/origin.git"
  git init --bare -q "${ORIGIN}"
  # Pin the bare origin's default branch to master regardless of this
  # machine's init.defaultBranch config. Without this, ORIGIN's HEAD
  # symref points at whatever the local default is (e.g. "main"); once
  # commits land on "master" instead, every later clone (SEED, REPO_A,
  # REPO_B, CHECK) hits "remote HEAD refers to nonexistent ref" and
  # checks out nothing. Setting this before ORIGIN has any commits lets
  # every subsequent clone (starting with SEED) correctly inherit
  # "master" as its checked-out branch.
  git -C "${ORIGIN}" symbolic-ref HEAD refs/heads/master

  SEED="${BATS_TEST_TMPDIR}/seed"
  git clone -q "${ORIGIN}" "${SEED}"
  mkdir -p "${SEED}/lib" "${SEED}/machines" "${SEED}/dot-claude/agents" "${SEED}/dot-agents/skills" "${SEED}/memory"
  cp "${REPO_ROOT}/claude-sync.sh" "${SEED}/claude-sync.sh"
  cp "${REPO_ROOT}"/lib/*.sh "${SEED}/lib/"
  chmod +x "${SEED}/claude-sync.sh"
  touch "${SEED}/machines/.gitkeep" "${SEED}/dot-claude/agents/.gitkeep" \
        "${SEED}/dot-agents/skills/.gitkeep" "${SEED}/memory/.gitkeep"
  git -C "${SEED}" add -A
  git -C "${SEED}" -c user.email=test@test -c user.name=test commit -q -m seed
  git -C "${SEED}" push -q origin master

  # Machine A
  REPO_A="${BATS_TEST_TMPDIR}/repo_a"
  git clone -q "${ORIGIN}" "${REPO_A}"
  CLAUDE_HOME_A="${BATS_TEST_TMPDIR}/claude_home_a"
  AGENTS_HOME_A="${BATS_TEST_TMPDIR}/agents_home_a"
  mkdir -p "${CLAUDE_HOME_A}/projects/-home-modem7-project/memory" "${AGENTS_HOME_A}/skills"
  echo "# from machine A" > "${CLAUDE_HOME_A}/CLAUDE.md"
  echo '{"theme":"dark"}' > "${CLAUDE_HOME_A}/settings.json"
  echo "machine A memory" > "${CLAUDE_HOME_A}/projects/-home-modem7-project/memory/MEMORY.md"
  source "${LIB_DIR}/machine-conf.sh"
  write_machine_conf "${REPO_A}/machines/host-a.conf" "/home/modem7/project"
  git -C "${REPO_A}" -c user.email=test@test -c user.name=test add -A
  git -C "${REPO_A}" -c user.email=test@test -c user.name=test commit -q -m "seed machine conf" --allow-empty

  # Machine B
  REPO_B="${BATS_TEST_TMPDIR}/repo_b"
  CLAUDE_HOME_B="${BATS_TEST_TMPDIR}/claude_home_b"
  AGENTS_HOME_B="${BATS_TEST_TMPDIR}/agents_home_b"
}

run_as_machine_a() {
  CLAUDE_SYNC_CLAUDE_HOME="${CLAUDE_HOME_A}" \
  CLAUDE_SYNC_AGENTS_HOME="${AGENTS_HOME_A}" \
  CLAUDE_SYNC_HOSTNAME="host-a" \
  git -C "${REPO_A}" -c user.email=test@test -c user.name=test "$@" 2>/dev/null || true
}

@test "push captures local config, commits, and pushes to origin" {
  CLAUDE_SYNC_CLAUDE_HOME="${CLAUDE_HOME_A}" \
  CLAUDE_SYNC_AGENTS_HOME="${AGENTS_HOME_A}" \
  CLAUDE_SYNC_HOSTNAME="host-a" \
  GIT_AUTHOR_NAME=test GIT_AUTHOR_EMAIL=test@test GIT_COMMITTER_NAME=test GIT_COMMITTER_EMAIL=test@test \
    "${REPO_A}/claude-sync.sh" push

  [ "$(cat "${REPO_A}/dot-claude/CLAUDE.md")" = "# from machine A" ]

  CHECK="${BATS_TEST_TMPDIR}/check"
  git clone -q "${ORIGIN}" "${CHECK}"
  [ "$(cat "${CHECK}/dot-claude/CLAUDE.md")" = "# from machine A" ]
  [ "$(cat "${CHECK}/memory/MEMORY.md")" = "machine A memory" ]
}

@test "pull on a fresh machine applies what a different machine pushed" {
  CLAUDE_SYNC_CLAUDE_HOME="${CLAUDE_HOME_A}" \
  CLAUDE_SYNC_AGENTS_HOME="${AGENTS_HOME_A}" \
  CLAUDE_SYNC_HOSTNAME="host-a" \
  GIT_AUTHOR_NAME=test GIT_AUTHOR_EMAIL=test@test GIT_COMMITTER_NAME=test GIT_COMMITTER_EMAIL=test@test \
    "${REPO_A}/claude-sync.sh" push

  git clone -q "${ORIGIN}" "${REPO_B}"
  source "${LIB_DIR}/machine-conf.sh"
  write_machine_conf "${REPO_B}/machines/host-b.conf" "/home/modem7/work"

  CLAUDE_SYNC_CLAUDE_HOME="${CLAUDE_HOME_B}" \
  CLAUDE_SYNC_AGENTS_HOME="${AGENTS_HOME_B}" \
  CLAUDE_SYNC_HOSTNAME="host-b" \
    "${REPO_B}/claude-sync.sh" pull

  [ "$(cat "${CLAUDE_HOME_B}/CLAUDE.md")" = "# from machine A" ]
  [ "$(cat "${CLAUDE_HOME_B}/projects/-home-modem7-work/memory/MEMORY.md")" = "machine A memory" ]
}

@test "push refuses to run without a machine conf" {
  git clone -q "${ORIGIN}" "${REPO_B}"
  run env CLAUDE_SYNC_CLAUDE_HOME="${CLAUDE_HOME_B}" CLAUDE_SYNC_AGENTS_HOME="${AGENTS_HOME_B}" \
      CLAUDE_SYNC_HOSTNAME="host-b" "${REPO_B}/claude-sync.sh" push
  [ "$status" -eq 1 ]
}
```

- [ ] **Step 2: Run the test suite to verify it fails**

```bash
cd ~/claude-config
bats tests/sync_cli.bats
```
Expected: FAIL — `claude-sync.sh: No such file or directory`.

- [ ] **Step 3: Write `claude-sync.sh`**

```bash
#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/memory-path.sh
source "${SCRIPT_DIR}/lib/memory-path.sh"
# shellcheck source=lib/machine-conf.sh
source "${SCRIPT_DIR}/lib/machine-conf.sh"
# shellcheck source=lib/safety.sh
source "${SCRIPT_DIR}/lib/safety.sh"
# shellcheck source=lib/sync-files.sh
source "${SCRIPT_DIR}/lib/sync-files.sh"
# shellcheck source=lib/skills-symlink.sh
source "${SCRIPT_DIR}/lib/skills-symlink.sh"

REPO_DIR="${SCRIPT_DIR}"
CLAUDE_HOME="${CLAUDE_SYNC_CLAUDE_HOME:-${HOME}/.claude}"
AGENTS_HOME="${CLAUDE_SYNC_AGENTS_HOME:-${HOME}/.agents}"
HOSTNAME_VAL="${CLAUDE_SYNC_HOSTNAME:-$(hostname)}"

require_project_path() {
  local conf_file
  conf_file="$(machine_conf_path "${REPO_DIR}" "${HOSTNAME_VAL}")"
  if ! read_primary_project_path "${conf_file}"; then
    echo "No machine config at ${conf_file}. Run install.sh first." >&2
    exit 1
  fi
}

do_capture_and_commit() {
  local project_path memory_dir
  project_path="$(require_project_path)"
  memory_dir="$(resolve_memory_dir "${CLAUDE_HOME}" "${project_path}")"

  sync_capture "${REPO_DIR}" "${CLAUDE_HOME}" "${AGENTS_HOME}" "${memory_dir}"

  if ! assert_no_denylisted_files "${REPO_DIR}"; then
    echo "Refusing to commit: denylisted file present in repo tree." >&2
    exit 1
  fi

  git -C "${REPO_DIR}" add -A
  if ! git -C "${REPO_DIR}" diff --cached --quiet; then
    git -C "${REPO_DIR}" commit -m "sync: ${HOSTNAME_VAL} $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  fi
}

do_integrate() {
  if ! git -C "${REPO_DIR}" pull --rebase; then
    git -C "${REPO_DIR}" rebase --abort || true
    echo "git pull --rebase failed (conflict). Resolve manually in ${REPO_DIR} and re-run." >&2
    exit 1
  fi
}

do_push() {
  git -C "${REPO_DIR}" push
}

do_apply() {
  local project_path memory_dir
  project_path="$(require_project_path)"
  memory_dir="$(resolve_memory_dir "${CLAUDE_HOME}" "${project_path}")"

  sync_apply "${REPO_DIR}" "${CLAUDE_HOME}" "${AGENTS_HOME}" "${memory_dir}"
  wire_skill_symlinks "${REPO_DIR}" "${CLAUDE_HOME}" "${AGENTS_HOME}"
}

usage() {
  echo "Usage: claude-sync.sh [push|pull|sync]" >&2
  exit 1
}

main() {
  if [ ! -d "${REPO_DIR}/.git" ]; then
    echo "No git repo at ${REPO_DIR}. Run install.sh first." >&2
    exit 1
  fi

  case "${1:-sync}" in
    push)
      do_capture_and_commit
      do_integrate
      do_push
      ;;
    pull)
      do_integrate
      do_apply
      ;;
    sync)
      do_capture_and_commit
      do_integrate
      do_push
      do_apply
      ;;
    *)
      usage
      ;;
  esac
}

main "$@"
```

- [ ] **Step 4: Make it executable and run the test suite to verify it passes**

```bash
cd ~/claude-config
chmod +x claude-sync.sh
bats tests/sync_cli.bats
```
Expected: `3 tests, 0 failures`.

- [ ] **Step 5: shellcheck**

```bash
cd ~/claude-config
shellcheck claude-sync.sh
```
Expected: no output.

- [ ] **Step 6: Commit**

```bash
cd ~/claude-config
git add claude-sync.sh tests/sync_cli.bats
git commit -m "Add claude-sync.sh CLI dispatcher"
```

---

### Task 8: `install.sh` — first-run bootstrap

**Files:**
- Create: `install.sh`
- Modify: `tests/sync_cli.bats` (append install.sh scenario)

**Interfaces:**
- Consumes: `claude-sync.sh sync` (Task 7) as its final step.
- Produces: `install.sh` executable, honors env var overrides `CLAUDE_SYNC_REPO_URL` (default `git@github.com:modem7/claude-config.git`), `CLAUDE_SYNC_REPO_DIR` (default `${HOME}/claude-config`), `CLAUDE_SYNC_HOSTNAME`, `CLAUDE_SYNC_PROJECT_PATH` (skips the interactive prompt when set — required for tests and for scripted/non-interactive installs).

- [ ] **Step 1: Write the failing test — append to `tests/sync_cli.bats`**

```bash
@test "install.sh clones, creates machine conf, and seeds the repo on a fresh machine" {
  FRESH_REPO_DIR="${BATS_TEST_TMPDIR}/fresh_repo_dir"
  FRESH_CLAUDE_HOME="${BATS_TEST_TMPDIR}/fresh_install_claude_home"
  FRESH_AGENTS_HOME="${BATS_TEST_TMPDIR}/fresh_install_agents_home"
  mkdir -p "${FRESH_CLAUDE_HOME}" "${FRESH_AGENTS_HOME}"
  echo "# fresh machine" > "${FRESH_CLAUDE_HOME}/CLAUDE.md"
  echo '{}' > "${FRESH_CLAUDE_HOME}/settings.json"

  CLAUDE_SYNC_REPO_URL="${ORIGIN}" \
  CLAUDE_SYNC_REPO_DIR="${FRESH_REPO_DIR}" \
  CLAUDE_SYNC_HOSTNAME="host-fresh" \
  CLAUDE_SYNC_PROJECT_PATH="/home/modem7/fresh" \
  CLAUDE_SYNC_CLAUDE_HOME="${FRESH_CLAUDE_HOME}" \
  CLAUDE_SYNC_AGENTS_HOME="${FRESH_AGENTS_HOME}" \
  GIT_AUTHOR_NAME=test GIT_AUTHOR_EMAIL=test@test GIT_COMMITTER_NAME=test GIT_COMMITTER_EMAIL=test@test \
    "${REPO_ROOT}/install.sh"

  [ -f "${FRESH_REPO_DIR}/claude-sync.sh" ]
  [ -f "${FRESH_REPO_DIR}/machines/host-fresh.conf" ]
  [ -f "${FRESH_REPO_DIR}/dot-claude/CLAUDE.md" ]
}
```

- [ ] **Step 2: Run the test suite to verify it fails**

```bash
cd ~/claude-config
bats tests/sync_cli.bats
```
Expected: FAIL — `install.sh: No such file or directory`.

- [ ] **Step 3: Write `install.sh`**

```bash
#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${CLAUDE_SYNC_REPO_URL:-git@github.com:modem7/claude-config.git}"
REPO_DIR="${CLAUDE_SYNC_REPO_DIR:-${HOME}/claude-config}"
HOSTNAME_VAL="${CLAUDE_SYNC_HOSTNAME:-$(hostname)}"

if [ ! -d "${REPO_DIR}/.git" ]; then
  git clone "${REPO_URL}" "${REPO_DIR}"
fi

CONF_FILE="${REPO_DIR}/machines/${HOSTNAME_VAL}.conf"
if [ ! -f "${CONF_FILE}" ]; then
  if [ -n "${CLAUDE_SYNC_PROJECT_PATH:-}" ]; then
    project_path="${CLAUDE_SYNC_PROJECT_PATH}"
  else
    read -r -p "Primary Claude Code working directory on this machine (e.g. /home/modem7/project): " project_path
  fi
  mkdir -p "${REPO_DIR}/machines"
  printf 'PRIMARY_PROJECT_PATH=%s\n' "${project_path}" > "${CONF_FILE}"
fi

exec "${REPO_DIR}/claude-sync.sh" sync
```

- [ ] **Step 4: Make it executable and run the test suite to verify it passes**

```bash
cd ~/claude-config
chmod +x install.sh
bats tests/sync_cli.bats
```
Expected: `4 tests, 0 failures`.

- [ ] **Step 5: shellcheck**

```bash
cd ~/claude-config
shellcheck install.sh
```
Expected: no output.

- [ ] **Step 6: Commit**

```bash
cd ~/claude-config
git add install.sh tests/sync_cli.bats
git commit -m "Add install.sh first-run bootstrap"
```

---

### Task 9: `README.md`

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: nothing (documentation only). Lists the plugins currently in `dot-claude/settings.json`'s `enabledPlugins` at time of writing — this is a point-in-time reference, not synced automatically; `dot-claude/settings.json` (once populated by Task 11) is the source of truth going forward.

- [ ] **Step 1: Write `README.md`**

```markdown
# claude-config

Private repo that syncs Claude Code's global configuration — `CLAUDE.md`,
`settings.json`, personal skills, and "global" auto-memory — across every
machine the owner runs `claude` on.

## Why

Claude Code stores its config under `~/.claude/` and personal skills under
`~/.agents/`. This repo is the single source of truth those directories get
synced from, via `install.sh` (first run on a new machine) and
`claude-sync.sh` (subsequent runs).

## Quick start on a new machine

```bash
git clone git@github.com:modem7/claude-config.git ~/claude-config
~/claude-config/install.sh
```

`install.sh` will ask for the primary working directory you'll run `claude`
from on this machine (used to locate its Claude Code memory folder — see
"Memory scoping" below), then does a full sync.

## Everyday use

```bash
~/claude-config/claude-sync.sh sync   # capture local changes, merge with remote, apply back — the default
~/claude-config/claude-sync.sh push   # capture + commit + push only
~/claude-config/claude-sync.sh pull   # pull + apply only
```

## What's synced

| Repo path | Local path |
|---|---|
| `dot-claude/CLAUDE.md` | `~/.claude/CLAUDE.md` |
| `dot-claude/settings.json` | `~/.claude/settings.json` |
| `dot-claude/agents/` | `~/.claude/agents/` |
| `dot-agents/skills/` | `~/.agents/skills/` |
| `memory/` | this machine's primary-project memory folder |

**Never synced:** `.credentials.json`, `~/.claude.json`, plugin caches, or
any session/task/security state — see
`docs/superpowers/specs/2026-07-25-claude-config-sync-design.md` for the
full list and rationale.

## Memory scoping

Claude Code keys its auto-memory storage off the exact working directory
path (`~/.claude/projects/<encoded-cwd>/memory/`), which differs machine to
machine. Each machine records its primary project path in
`machines/<hostname>.conf`, and that's the memory folder treated as
"global" for sync purposes. This repo's memory content is currently all
global-flavored (feedback/reference-type entries) — if project-specific
memory entries start accumulating, they'll sync as if global too; see the
design spec's "Known limitation" for details.

## Currently installed plugins

(Point-in-time snapshot — `dot-claude/settings.json`'s `enabledPlugins` is
the source of truth going forward.)

- `frontend-design` (claude-plugins-official)
- `code-review` (claude-plugins-official)
- `commit-commands` (claude-plugins-official)
- `security-guidance` (claude-plugins-official)
- `context7` (claude-plugins-official)
- `docker` (claude-plugins-official)
- `github` (claude-plugins-official)
- `superpowers` (superpowers-marketplace)
- `deployment-engineer` (awesome-claude-code-plugins)
- `crowdsec` (crowdsecurity)

## Design

See `docs/superpowers/specs/2026-07-25-claude-config-sync-design.md` for
the full design rationale, and `docs/superpowers/plans/2026-07-25-claude-config-sync.md`
for the implementation plan.
```

- [ ] **Step 2: Commit**

```bash
cd ~/claude-config
git add README.md
git commit -m "Write README"
```

---

### Task 10: `.github/workflows/ci.yml`

**Files:**
- Create: `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: `install.sh`, `claude-sync.sh`, `lib/*.sh`, `tests/*.bats` (all prior tasks) — runs shellcheck/jq/gitleaks/bats against them on every push and PR.

- [ ] **Step 1: Write `.github/workflows/ci.yml`**

```yaml
name: CI

on:
  push:
  pull_request:

jobs:
  lint-and-test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: ShellCheck
        run: shellcheck install.sh claude-sync.sh lib/*.sh

      - name: Validate renovate.json is valid JSON
        run: jq empty renovate.json

      - name: Validate dot-claude/settings.json is valid JSON (if present)
        run: |
          if [ -f dot-claude/settings.json ]; then
            jq empty dot-claude/settings.json
          fi

      - name: Install gitleaks
        env:
          GITLEAKS_VERSION: 8.21.2 # renovate: datasource=github-releases depName=gitleaks/gitleaks
        run: |
          curl -sSL "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz" | tar -xz gitleaks
          sudo mv gitleaks /usr/local/bin/

      - name: Run gitleaks
        run: gitleaks detect --source . -v

      - name: Run bats test suite
        run: bats tests/
```

- [ ] **Step 2: Validate workflow YAML syntax**

```bash
cd ~/claude-config
python3 -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml')); print('ci.yml OK')"
```
Expected: `ci.yml OK`.

- [ ] **Step 3: Commit**

```bash
cd ~/claude-config
git add .github/workflows/ci.yml
git commit -m "Add CI workflow: shellcheck, JSON validation, gitleaks, bats"
```

---

### Task 11: First real sync + open PR

**Files:** none created — this task runs the tool against this actual machine and opens the PR.

**Interfaces:** none (integration/delivery task).

- [ ] **Step 1: Run the full local test suite one more time**

```bash
cd ~/claude-config
shellcheck install.sh claude-sync.sh lib/*.sh
bats tests/
```
Expected: all shellcheck output clean, all bats tests pass.

- [ ] **Step 2: Run the real sync against this machine (Claude-Code VM)**

```bash
cd ~/claude-config
chmod +x install.sh claude-sync.sh
CLAUDE_SYNC_PROJECT_PATH=/home/modem7/project HOSTNAME=Claude-Code ./install.sh
```
This seeds `dot-claude/CLAUDE.md`, `dot-claude/settings.json`,
`dot-agents/skills/`, and `memory/` from this machine's real config, writes
`machines/Claude-Code.conf`, and — because `install.sh`'s last step is
`claude-sync.sh sync` — commits that capture locally (it will NOT push yet;
`git push` inside `claude-sync.sh` targets whatever remote/branch this repo
is currently on, which is `claude-config-bootstrap`, not `master`).

- [ ] **Step 3: Verify no secrets ended up in the tree**

```bash
cd ~/claude-config
find . -name ".credentials.json" -o -name ".claude.json"
git status
git diff --cached --stat
```
Expected: the `find` command prints nothing; review the diff stat to
confirm only expected paths (`dot-claude/`, `dot-agents/skills/`, `memory/`,
`machines/Claude-Code.conf`) changed.

- [ ] **Step 4: Push the branch and open a PR**

```bash
cd ~/claude-config
git push -u origin claude-config-bootstrap
gh pr create --title "Bootstrap claude-config sync repo" --body "$(cat <<'EOF'
## Summary
- Scaffolds the repo from the DefaultRepo template (settings.yml, renovate.json using the shared preset, CONTRIBUTING.md, editorconfig, gitattributes, gitignore, issue/PR templates, CODEOWNERS, autoassign workflow)
- Adds install.sh / claude-sync.sh with a bats-tested lib/ (memory path resolution, machine-conf I/O, denylist safety check, capture/apply file sync, skill-symlink wiring)
- Adds CI (shellcheck, JSON validation, gitleaks, bats)
- Seeds dot-claude/, dot-agents/skills/, memory/ from this machine's real config

## Testing
- [ ] `shellcheck install.sh claude-sync.sh lib/*.sh` clean
- [ ] `bats tests/` all passing
- [ ] No denylisted filenames in the tree
EOF
)"
```

- [ ] **Step 5: Report the PR URL to the user**

No code changes — just confirm the `gh pr create` output includes the PR
URL, so it can be shared back.

---

## Self-Review Notes

- **Spec coverage:** Non-goals (Task: none needed, nothing built for them by design), Repository/Layout (Task 1), File inventory (Tasks 5, 9), Memory resolution (Task 2), Repo bootstrap (Task 1), Skill discovery wiring (Task 6), Sync mechanism (Tasks 7–8), CI & safety checks (Tasks 4, 10), Error handling (Tasks 7–8 — missing conf, missing clone, rebase conflict all implemented in `claude-sync.sh`), Out-of-scope items (not built — confirmed absent from file structure).
- **Placeholder scan:** no TBD/TODO; every step has runnable code and expected output.
- **Type/interface consistency:** checked that `sync_capture`/`sync_apply` signatures in Task 5 match the calls in Task 7's `claude-sync.sh`; `machine_conf_path`/`write_machine_conf`/`read_primary_project_path` signatures match between Task 3 and their use in Task 7's tests and `require_project_path`; `wire_skill_symlinks` signature matches between Task 6 and Task 7.
