# Python Port, Phase 1 (Sync Core) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the bash sync tool (`claude-sync.sh`, `lib/*.sh`, `install.sh`, tool installers, `publish-public.sh`) with a standard-library Python package that behaves identically on Linux and Windows.

**Architecture:** A `claude_sync/` package, one module per bash file (see the spec's module table), run as `python -m claude_sync`. Each module is ported test-first from its bats file, which is the spec for behaviour. `claude-sync.sh` and `install.sh` become thin shims, switched only after pytest matches bats. Phases 2 (hooks) and 3 (remove bash) get their own plans.

**Tech Stack:** Python 3.12+ standard library only; pytest for tests (dev only); git 2.40+; gh.

**Spec:** `docs/superpowers/specs/2026-11-11-python-port-design.md` (read it first; "Behaviour the port must reproduce" is binding). Bash being ported: `claude-sync.sh`, `lib/*.sh`, `install.sh`, `npm-tools.sh`, `gh-tools.sh`, `python-tools.sh`, `publish-public.sh`. Specs: `tests/*.bats`.

## Global Constraints

- Python 3.12+, standard library only; no third-party runtime dependency.
- Needs git 2.40+ and `gh`; no WSL, `rsync`, `flock`, `jq` or Git Bash for the sync tool.
- Never commit or push to the default branch; one commit on `sync/<hostname>`; copy-based, no symlinks in the repo.
- Every failure prints what to do on stderr and exits non-zero.
- Env vars keep their names: `CLAUDE_SYNC_REPO_URL`, `_REPO_DIR`, `_HOSTNAME`, `_CLAUDE_HOME`, `_AGENTS_HOME`, `_PROFILE`, `_PROJECT_PATH`, `_GH`, `_LOCK_WAIT`. `CLAUDE_SYNC_FLOCK` is dropped and noted in the docs.
- Lock file stays `.git/claude-sync.flock`, default wait 10s; child processes must not inherit the lock handle.
- Synced file content is read and written as bytes; line endings are never altered.
- Code, tests and docs outside `machines/`, `memory/`, `private/`, `weekly-review/` stay generic (`host-a`, `host-b`, no personal paths or names). `/z/projects` appears only as an example.
- Tests use a fake HOME via `CLAUDE_SYNC_*` and never touch the real HOME; never run `claude-sync.sh` or `install.sh` against the real HOME.
- Work in the worktree on branch `docs/python-port-design` or a branch cut from `origin/master`; open a PR; never push to `master`. `shellcheck` and `bats tests` keep passing until phase 3.
- Run `docs-sync` and follow `docs/doc-map.md` before the PR.

## Review Focus

1. Interrupted sync (Ctrl-C, killed process, power loss) mid-apply: `~/.claude` is never left half-written and the next run recovers or says what to do.
2. Windows paths: spaces in paths, drive letters, CRLF files, and a project path that does not exist (fallback to the home directory).
3. Two runs at once, including one bash and one Python run during the cutover: the second waits then fails with the "another claude-sync run is still working" message.
4. A settings file or override that is empty, not an object, invalid JSON, or has non-ASCII text: a clear error, no data loss, no mangled characters.
5. A user with no `gh` login, no `origin`, or an old git: a specific message, not a traceback.

---

## File Structure

- `claude_sync/__init__.py`, `__main__.py`: entry (`python -m claude_sync`).
- `claude_sync/util.py`: `Fail`, `run()`, env helpers.
- `claude_sync/{memorypath,machineconf,lock,syncfiles,overlay,safety,skilllinks,profile,gitflow,pr,doctor,cli,install,tools,publish}.py`: as in the spec table.
- `tests/py/conftest.py`: shared fixtures. `tests/py/test_<module>.py`: one per bats file.
- `claude-sync.sh`, `claude-sync.cmd`, `install.sh`, `install.cmd`: shims (last).

---

### Task 1: Package scaffold, `run()` and fixtures

**Files:**
- Create: `claude_sync/__init__.py`, `claude_sync/__main__.py`, `claude_sync/util.py`, `tests/py/conftest.py`, `tests/py/test_util.py`, `pytest.ini`
- Modify: `.github/workflows/ci.yml` (add a `pytest` job on a matrix of `ubuntu-latest` and `windows-latest`, Python 3.12)

**Interfaces:**
- Produces: `class Fail(Exception)` with `.message: str` and `.hint: str`; `run(args: list[str], *, cwd: Path | None = None, check: bool = False, env: dict | None = None) -> subprocess.CompletedProcess[str]` (list args, no shell, UTF-8, `close_fds=True`); fixtures `fake_home(tmp_path)` returning an object with `.claude`, `.agents`, `.repo`, `.env` (dict of `CLAUDE_SYNC_*` pointing into `tmp_path`), `fake_gh(tmp_path)` returning a stub `gh` script path that records its argv to a file, and `git_repo(tmp_path)` creating a bare origin plus clone on branch `master`.

- [ ] **Step 1: Write failing tests** `test_run_never_uses_shell` (args with `;` and spaces are passed literally), `test_run_decodes_utf8`, `test_fail_carries_hint`, `test_fake_home_never_equals_real_home` (asserts `fake_home.claude` is under `tmp_path`).
- [ ] **Step 2: Run** `python -m pytest tests/py/test_util.py -v`. Expected: FAIL (module missing).
- [ ] **Step 3: Implement** `util.py` and the fixtures. The fake `gh` stub must be a Python script runnable on Windows through `sys.executable`, since `CLAUDE_SYNC_GH` takes a path.
- [ ] **Step 4: Run** the same command. Expected: PASS on Linux.
- [ ] **Step 5: Commit** `feat: python package scaffold and test fixtures`.

### Task 2: `memorypath` and `machineconf`

**Files:** Create `claude_sync/memorypath.py`, `claude_sync/machineconf.py`, `tests/py/test_memorypath.py`, `tests/py/test_machineconf.py`. Specs: `tests/memory_path.bats`, `tests/machine_conf.bats`.

**Interfaces:**
- Produces: `encode_project_path(path: str) -> str`; `resolve_memory_dir(claude_home: Path, project_path: str) -> Path`; `machine_conf_path(repo: Path, hostname: str) -> Path`; `write_machine_conf(repo, hostname, project_path)`; `read_primary_project_path(repo, hostname) -> str | None`; `read_machine_profile(repo, hostname) -> str | None`; `write_machine_profile(repo, hostname, profile)`.

- [ ] **Step 1: Port the bats cases** to pytest one for one, plus `test_encode_windows_drive_path` (`Z:\projects\x` and `/z/projects/x` both encode the way the plan's Windows rule decides; write the rule into the test first, default: `\`, `/` and `:` each become `-`) and `test_conf_roundtrip_preserves_other_lines`.
- [ ] **Step 2: Run** `python -m pytest tests/py/test_memorypath.py tests/py/test_machineconf.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** with `pathlib`; `.conf` files are `KEY=value` lines read and written as bytes so unknown lines survive.
- [ ] **Step 4: Run** same command. Expected: PASS.
- [ ] **Step 5: Commit** `feat: port memory-path and machine-conf`.

### Task 3: `lock`

**Files:** Create `claude_sync/lock.py`, `tests/py/test_lock.py`. Spec: spec "Constraints" and the lock tests in `tests/sync_cli.bats`.

**Interfaces:**
- Produces: `class SyncLock` context manager: `SyncLock(path: Path, wait: float)`; raises `Fail` with the existing message "another claude-sync run is still working on ..." after `wait` seconds. `lock_wait_from_env(env) -> float` (default 10).

- [ ] **Step 1: Write failing tests** `test_second_holder_times_out_with_message` (hold in a subprocess, `wait=0.2`), `test_lock_released_when_holder_killed` (kill the holder with SIGKILL, the next acquire succeeds), `test_lock_handle_not_inherited_by_child` (a child spawned while locked can outlive the parent without holding the lock), `test_bash_flock_excludes_python` (skip when `flock` missing; start `flock <path> sleep 2`, Python must wait).
- [ ] **Step 2: Run** `python -m pytest tests/py/test_lock.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** with `fcntl.flock(LOCK_EX|LOCK_NB)` polling on Linux and `msvcrt.locking` on one byte on Windows; open the file non-inheritable.
- [ ] **Step 4: Run** same. Expected: PASS (Windows CI runs the non-skipped ones).
- [ ] **Step 5: Commit** `feat: cross-platform sync lock`.

### Task 4: `syncfiles` (mirror copy, capture, apply)

**Files:** Create `claude_sync/syncfiles.py`, `tests/py/test_syncfiles.py`. Spec: `tests/sync_files.bats`; spec section "Mirror copy".

**Interfaces:**
- Produces: `mirror(src: Path, dst: Path, *, delete: bool, exclude: set[str] = frozenset()) -> None` (top-level exclude names, never deleted on the receiver; copy to temp name then `os.replace`; skip when size and mtime match; keep exec bit on POSIX); `sync_capture(claude_home, agents_home, repo, profile_excludes)`; `sync_apply(repo, claude_home, agents_home, *, mirror: bool, profile_excludes)`.

- [ ] **Step 1: Write failing tests** ported from `sync_files.bats`, plus `test_mirror_deletes_only_missing`, `test_exclude_is_top_level_only`, `test_excluded_name_survives_delete`, `test_gitkeep_excluded_on_capture`, `test_no_delete_when_mirror_false`, `test_crlf_bytes_preserved`, `test_path_with_spaces`, `test_same_size_and_mtime_different_content_is_detected` (decides the stale-file issue: compare content when size matches), `test_interrupted_copy_leaves_no_partial_file` (patch `os.replace` to raise).
- [ ] **Step 2: Run** `python -m pytest tests/py/test_syncfiles.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the signatures above; the synced path list is copied from `lib/sync-files.sh`.
- [ ] **Step 4: Run** same. Expected: PASS.
- [ ] **Step 5: Commit** `feat: port sync-files with python mirror copy`.

### Task 5: `overlay` and the jq-path parser

**Files:** Create `claude_sync/overlay.py`, `tests/py/test_overlay.py`. Specs: `tests/settings_overlay.bats`, `tests/settings_portability.bats`; spec section "Settings overlay" and "Override paths".

**Interfaces:**
- Produces: `parse_path(expr: str) -> tuple[str | int, ...]` accepting exactly the syntax the CLI documents (`.a.b`, `.a["x@y"]`); `overlay_merge(shared: dict, override: dict) -> dict`; `overlay_split(local: dict, shared_before: dict, override_before: dict, ...) -> tuple[dict, dict]` (match the five bash inputs; read `lib/settings-overlay.sh` for the order); `overlay_add/remove/list(...)`; `dump_json(obj) -> bytes` (2-space indent, `ensure_ascii=False`, trailing newline).

- [ ] **Step 1: Write failing tests** ported from both bats files, plus `test_null_deletes_leaf`, `test_arrays_replace_not_merge`, `test_empty_object_is_not_a_leaf`, `test_split_prunes_empty_parents`, `test_parse_path_quoted_key_with_at_sign`, `test_parse_path_rejects_unsupported_expr` (clear `Fail`), `test_non_ascii_survives_roundtrip`, `test_rejects_non_object_settings`, and `test_output_bytes_match_existing_dot_claude_settings` (load `dot-claude/settings.json`, dump, assert equal bytes; if it differs, the test records the jq-versus-Python formatting decision in the plan before moving on).
- [ ] **Step 2: Run** `python -m pytest tests/py/test_overlay.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** with `json`; `parse_path` is a small tokenizer, not `eval`.
- [ ] **Step 4: Run** same. Expected: PASS.
- [ ] **Step 5: Commit** `feat: port settings overlay with path parser`.

### Task 6: `safety`

**Files:** Create `claude_sync/safety.py`, `tests/py/test_safety.py`. Spec: `tests/safety.bats`; spec "Safety and publish".

**Interfaces:**
- Produces: `assert_no_denylisted_files(root: Path) -> None` and `assert_public_tree_clean(root: Path) -> None`, both raising `Fail` naming the offending path; denylist and patterns copied from `lib/safety.sh`, matched at any depth.

- [ ] **Step 1: Write failing tests** ported from `safety.bats` plus `test_match_at_depth_three` and `test_machines_settings_json_blocked_anywhere`.
- [ ] **Step 2: Run** `python -m pytest tests/py/test_safety.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** with `Path.rglob`/`os.walk`; do not follow symlinks.
- [ ] **Step 4: Run** same. Expected: PASS.
- [ ] **Step 5: Commit** `feat: port safety checks`.

### Task 7: `skilllinks`

**Files:** Create `claude_sync/skilllinks.py`, `tests/py/test_skilllinks.py`. Spec: `tests/skills_symlink.bats`; spec "Skill links".

**Interfaces:**
- Produces: `wire_skill_links(claude_home: Path, agents_home: Path) -> None`; `remove_skill_links(claude_home, agents_home) -> None` (used by `install --repair`); internal `make_link(target: Path, link: Path)` (symlink on POSIX, directory junction on Windows) and `read_link(link: Path) -> Path | None`.
  > **Settled (2026-11-11):** `wire_skill_links` keeps its required `tracked` and `disabled` arguments: `wire_skill_links(claude_home, agents_home, tracked, disabled) -> None`. The owner kept the four-argument form.

- [ ] **Step 1: Write failing tests** ported from `skills_symlink.bats` plus `test_real_directory_is_never_replaced`, `test_dangling_link_under_agents_skills_removed`, `test_junction_created_on_windows` (skipif not Windows).
- [ ] **Step 2: Run** `python -m pytest tests/py/test_skilllinks.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** using `os.symlink` and, on Windows, `_winapi.CreateJunction`; `os.path.isjunction` to detect.
- [ ] **Step 4: Run** same. Expected: PASS.
- [ ] **Step 5: Commit** `feat: port skill-link wiring with windows junctions`.

### Task 8: `profile`

**Files:** Create `claude_sync/profile.py`, `tests/py/test_profile.py`. Spec: `tests/profile.bats` (619 lines, largest); spec "Profile `CLAUDE.md` block".

**Interfaces:**
- Consumes: `syncfiles.mirror`, `machineconf.read_machine_profile`.
- Produces: `profile_valid_name(name) -> bool`; `profile_apply(repo, claude_home, profile)`; `profile_unapply(...)`; `profile_capture(...)`; `profile_has_local_edits(...) -> bool`; `profile_live_mismatches(...) -> list[str]`; `profile_dangling_hook_refs(...) -> list[str]`; `profile_describe(...) -> str`; `profile_excludes(profile) -> set[str]`. Marker constant `MARKER = b"<!-- claude-sync:profile-append -->"`.

- [ ] **Step 1: Port `profile.bats` in three commits' worth of tests** (names, apply/unapply, capture/mismatch) plus `test_stamp_written_via_tmp_then_rename`, `test_crash_after_unapply_reads_as_not_applied`, `test_marker_only_matches_whole_line`, `test_claude_md_crlf_preserved`, and `test_linux_only_entry_skipped_on_windows` (pins whatever rule Step 3 decides for Linux-only profile entries, per the spec's open item).
- [ ] **Step 2: Run** `python -m pytest tests/py/test_profile.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the signatures; decide and document in the module docstring how Linux-only entries (profile hooks that are `.sh`) behave on Windows; ask the owner if no safe default exists.
- [ ] **Step 4: Run** same. Expected: PASS.
- [ ] **Step 5: Commit** per group, `feat: port profile apply`, `feat: port profile capture and checks`.

### Task 9: `gitflow`

**Files:** Create `claude_sync/gitflow.py`, `tests/py/test_gitflow.py`. Specs: git-flow tests in `tests/sync_cli.bats`; bash functions listed in the spec's "Git flow" bullet.

**Interfaces:**
- Consumes: `util.run`, `lock.SyncLock`, `syncfiles`, `overlay`, `profile`.
- Produces: `default_branch(repo) -> str`; `sync_branch(hostname) -> str`; `require_git_version(min=(2,40))`; `materialise(repo, ref, ...)` (masks INT/TERM/HUP during `read-tree`, exits 130/143/129); `rebuild_sync_branch(...)`; `adopt_remote_sync_branch(...)`; `migrate_legacy_default(...)`; `record_applied_commit(...)`; `live_matches_commit(...) -> bool`; `do_capture_and_commit(...)`; `do_integrate(...)`; `do_apply(...)`.

- [ ] **Step 1: Write failing tests** ported from the matching `sync_cli.bats` cases, using the `git_repo` fixture, plus `test_ctrl_c_during_materialise_leaves_tree_consistent`, `test_old_git_message` (stub `git --version` through `PATH`), `test_no_origin_message`, `test_never_commits_on_default_branch`.
- [ ] **Step 2: Run** `python -m pytest tests/py/test_gitflow.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** function by function against the bash, one commit per bash function group (branch helpers; materialise; rebuild; adopt and migrate; capture and commit; apply). Keep every message from the bash that says what to do.
- [ ] **Step 4: Run** same. Expected: PASS.
- [ ] **Step 5: Commit** after each group, `feat: port <group>`.

### Task 10: `pr`

**Files:** Create `claude_sync/pr.py`, `tests/py/test_pr.py`. Spec: `tests/sync_pr.bats`.

**Interfaces:**
- Consumes: `util.run`, fake `gh` fixture.
- Produces: `gh_repo_slug(repo) -> str`; `gh_sync_pr_state(slug, branch) -> str`; `ensure_pr(...)`; `do_push(...)`. `CLAUDE_SYNC_GH` and `CLAUDE_SYNC_GH_REPO` honoured.

- [ ] **Step 1: Port `sync_pr.bats`** plus `test_gh_not_logged_in_message` and `test_gh_missing_message`.
- [ ] **Step 2: Run** `python -m pytest tests/py/test_pr.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** with the lease-guarded push (`--force-with-lease`) onto `sync/<hostname>` only.
- [ ] **Step 4: Run** same. Expected: PASS.
- [ ] **Step 5: Commit** `feat: port PR creation and push`.

### Task 11: `doctor`, `override` and `profile` subcommands, and `cli`

**Files:** Create `claude_sync/doctor.py`, `claude_sync/cli.py`, `tests/py/test_cli.py`, `tests/py/test_doctor.py`. Spec: remaining `tests/sync_cli.bats` cases.

**Interfaces:**
- Consumes: all earlier modules.
- Produces: `main(argv: list[str], env: Mapping[str, str]) -> int` with commands `push|pull|sync|bootstrap|doctor [remediate]|override add|remove|list [PATH]|profile list|show NAME|copy FROM [--force] [--yes]|use NAME`; usage text matches the bash `usage()` except the flock lines; skip-on-feature-branch prints "sync SKIPPED" and returns 0.

- [ ] **Step 1: Write failing tests** for every remaining `sync_cli.bats` case, plus `test_unknown_command_prints_usage_returns_1` and `test_skip_on_feature_branch_returns_0`.
- [ ] **Step 2: Run** `python -m pytest tests/py -v`. Expected: new tests FAIL.
- [ ] **Step 3: Implement** `cli.main` as a dispatcher only; logic stays in the modules.
- [ ] **Step 4: Run** `python -m pytest tests/py -v`. Expected: all PASS.
- [ ] **Step 5: Commit** `feat: python cli`.

### Task 12: `install` and `tools`

**Files:** Create `claude_sync/install.py`, `claude_sync/tools.py`, `tests/py/test_install.py`, `tests/py/test_tools.py`. Specs: `tests/npm_tools.bats`, `tests/gh_tools.bats`, `tests/python_tools.bats`.

**Interfaces:**
- Produces: `install.main(argv, env) -> int` (flags as in `install.sh`, `--repair` included, new `--project-path PATH`); `resolve_project_path(preferred: str | None, home: Path) -> str` (preferred if the directory exists, else the home directory); `tools.install_npm_tools(...)`, `tools.install_gh_tools(...)`, `tools.install_python_tools(...)` (pip via `sys.executable -m pip`).

- [ ] **Step 1: Port the three tools bats files and add** `test_project_path_falls_back_to_home_when_missing`, `test_project_path_preferred_when_exists`, `test_install_never_touches_real_home`, `test_repair_goes_through_pr`.
- [ ] **Step 2: Run** `python -m pytest tests/py/test_install.py tests/py/test_tools.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement.** Keep the pip package list (`mcp<2`, `aiosqlite`, `pyyaml`) from `python-tools.sh`; drop `--break-system-packages` only if the Linux behaviour tests allow it, else keep it on Linux.
- [ ] **Step 4: Run** same. Expected: PASS.
- [ ] **Step 5: Commit** `feat: port installer and tool installers`.

### Task 13: `publish`

**Files:** Create `claude_sync/publish.py`, `tests/py/test_publish.py`. Spec: `publish-public.sh` and the public-export cases in `tests/safety.bats`; `.github/workflows/publish-public.yml`.

**Interfaces:**
- Consumes: `safety`.
- Produces: `publish.main(argv, env) -> int` with the same flags and whitelist as the bash.

- [ ] **Step 1: Write failing tests** `test_export_contains_only_whitelist`, `test_export_aborts_on_denylisted_file`, `test_export_never_contains_personal_paths` (fixture tree with a `machines/` dir).
- [ ] **Step 2: Run** `python -m pytest tests/py/test_publish.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement,** then change the workflow to call `python -m claude_sync.publish`. If the Python port is clearly larger than the 118-line script, stop and ask the owner (the spec says to revisit).
- [ ] **Step 4: Run** same. Expected: PASS.
- [ ] **Step 5: Commit** `feat: port publish-public`.

### Task 14: Shims, CI, docs and the cutover gate

**Files:**
- Create: `claude-sync.cmd`, `install.cmd`
- Modify: `claude-sync.sh`, `install.sh` (become shims calling `python3 -m claude_sync` and `python3 -m claude_sync.install`; keep the old script as `legacy/` until phase 3), `.github/workflows/ci.yml`, `README.md`, `README.public.md`, `CLAUDE.md`, `docs/runbook.md`, `docs/risks.md`, `docs/doc-map.md`, `docs/superpowers/specs/2026-07-25-claude-config-sync-design.md` (superseded note)
- Test: `tests/py/test_shims.py`

**Interfaces:**
- Consumes: `cli.main`, `install.main`.

- [ ] **Step 1: Write failing tests** `test_sh_shim_runs_python_cli` (usage output matches), `test_cmd_shim_uses_py_launcher_first` (parse the `.cmd` text: `py -3`, then `python`), `test_legacy_script_still_present`.
- [ ] **Step 2: Run** `python -m pytest tests/py/test_shims.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement the shims,** update every doc named in `docs/doc-map.md` for "change sync flow, env vars, what is synced, profiles", drop the flock lines from usage and docs, and add rows to `docs/risks.md` for the lock, mirror copy and path parser, each with its test name.
- [ ] **Step 4: Run** `python -m pytest tests/py -v && bats tests && shellcheck install.sh claude-sync.sh npm-tools.sh gh-tools.sh lib/*.sh`. Expected: all pass; CI green on both OSes.
- [ ] **Step 5: Cutover gate (manual, owner decides).** On a test machine with a fake or throwaway HOME, run one `sync` with bash and one with Python from the same starting state and diff `~/.claude` and the pushed commit trees; they must match. Only then switch the Linux machines over.
- [ ] **Step 6: Run `docs-sync`, then commit** `feat: switch entry points to python` and open the PR with `gh pr create --repo <owner>/<repo>`.

---

## Self-review notes

- **Spec coverage:** goals, constraints, each module row, "Behaviour the port must reproduce" bullets (mirror, marker block, overlay, override paths, git flow, skill links, safety, shim), Windows specifics (project path fallback in Task 12, drive-letter encoding in Task 2, Linux-only profile entries in Task 8), testing and phasing are all covered. Hooks and bash removal are explicitly later phases.
- **Not decided here, flagged in the tasks:** the Windows memory-path encoding rule (Task 2 default given), jq-versus-Python JSON byte parity (Task 5), and the Linux-only profile entry rule (Task 8).
