# Python port of the config sync tool — design

Status: approved by the owner on 2026-11-11, except items marked "my call". Nothing is implemented. This extends, and in phase 1 supersedes the implementation language of, `2026-07-25-claude-config-sync-design.md`. Behaviour stays the same; only the implementation moves.

## Intent

Make the sync tool run identically on Linux and Windows from one implementation. Today it is bash and depends on `rsync`, `flock`, `jq`, symlinks and a POSIX shell, so it cannot run on a plain Windows machine. A Windows machine should need only Python 3.12+, `git` and `gh` to capture, sync (push `sync/<hostname>` and open the PR) and apply.

## Goals

- One implementation, standard library only, with the same CLI and the same on-disk results on both platforms.
- Linux machines move to the Python version too, so there are not two code paths to drift.
- Windows needs no WSL, `rsync`, `flock`, `jq` or Git Bash for the sync tool.
- Existing behaviour is preserved: never commit or push to the default branch, one commit on `sync/<hostname>`, copy-based (no symlinks in the repo), every failure prints what to do and exits non-zero.
- A pytest suite runs on Linux and Windows and replaces the bats suite.

## Non-goals

- Changing the sync model, repo layout, profile format or settings overlay semantics.
- Supporting Python older than 3.12.
- A GUI or a service. It stays a CLI run by hand or by hooks.
- Third-party dependencies. If one seems necessary, that is a design change and needs approval.

## Anti-goals

- No behavioural improvements smuggled in with the port. A fix found on the way gets its own PR.
- No big-bang switch of Linux machines. The tool overwrites `~/.claude`; a regression there costs real data.
- No re-implementation that diverges from the bats tests, which are the spec.

## Constraints

- Repo rules in `CLAUDE.md` apply: PR only, develop in a worktree, generic code and docs (placeholder hostnames such as `host-a`), tests use a fake HOME through `CLAUDE_SYNC_*` variables and never touch the real HOME.
- Needs git 2.40+ and `gh`, as today.
- Env vars keep their names: `CLAUDE_SYNC_REPO_URL`, `_REPO_DIR`, `_HOSTNAME`, `_CLAUDE_HOME`, `_AGENTS_HOME`, `_PROFILE`, `_PROJECT_PATH`, `_GH`, `_LOCK_WAIT`. `CLAUDE_SYNC_FLOCK` becomes unnecessary and is dropped with a note in the docs. Child `git` processes must not inherit the lock handle (bash closes fd 9 for this; Python uses non-inheritable handles), or git's background gc can hold the lock.
- Paths are handled with `pathlib`; no hard-coded separators. Line endings in synced files are preserved byte for byte (read and write in binary).

## Decisions

| Decision | Why |
|---|---|
| Standard-library Python, 3.12+ | Removes `rsync`, `flock`, `jq` and bash from the sync path. |
| Linux moves to Python as well | One code path, one shared test suite. |
| Lock with `msvcrt.locking` on Windows and `fcntl.flock` on Linux, on the existing `.git/claude-sync.flock` (same name as bash, so a Python and a bash run exclude each other during the Linux cutover; default wait 10s) | The OS drops the lock if the process dies. Replaces `flock` and the pid-symlink fallback. |
| Port `install.sh`, `npm-tools.sh`, `gh-tools.sh`, `python-tools.sh` | Part of the user's "port everything". |
| Port `publish-public.sh` (my call) | It is short, shares the denylist logic with `lib/safety.sh`, and a workflow runs it. Porting avoids keeping bash only for this. Revisit if the port turns out larger than the script. |
| Hooks are ported in phase 2 | Removes `jq` from Windows entirely. Until then Windows still needs bash and `jq` for hooks only. |
| Replace `rsync -a --delete --exclude` with Python code (my call) | Only that one form is used. Needs: copy tree, delete files absent from source, honour the exclude list, skip unchanged files by size and mtime. |
| Skill-discovery links on Windows are directory junctions (my call) | No admin or Developer Mode needed; `os.path.isjunction` exists in 3.12+. Linux keeps symlinks. |
| Keep `claude-sync.sh` as a thin shim to the Python entry point, plus a `.cmd` shim on Windows (my call) | Hooks, docs and habit keep working. |
| Port module by module with the bats tests as the spec, then switch Linux over only when pytest matches bats (my call) | Limits regression risk. |

Items marked "my call" were not explicitly confirmed by the user and are open to change at review.

## Proposed structure

A package `claude_sync/` at the repo root, run as `python -m claude_sync`, with these modules mapped from the bash files:

| Bash | Python module | Notes |
|---|---|---|
| `claude-sync.sh` (git flow, branches, PR, doctor, override, profile subcommands) | `cli.py`, `gitflow.py`, `pr.py`, `doctor.py` | Calls `git` and `gh` through one `run()` helper that never uses a shell. |
| lock functions in `claude-sync.sh` | `lock.py` | Context manager. Wait time from `CLAUDE_SYNC_LOCK_WAIT`. |
| `lib/sync-files.sh` | `syncfiles.py` | Capture and apply, plus the mirror-copy helper that replaces rsync. |
| `lib/settings-overlay.sh` | `overlay.py` | Deep merge: objects merge key by key, arrays and scalars replace, `null` deletes the key. Split local settings into shared plus override by leaf path. Uses `json`. |
| `lib/profile.sh` | `profile.py` | Entries, apply, unapply, capture, mismatch checks, `CLAUDE.md` marker block. |
| `lib/machine-conf.sh`, `lib/memory-path.sh` | `machineconf.py`, `memorypath.py` | Small. |
| `lib/skills-symlink.sh` | `skilllinks.py` | Symlink on Linux, junction on Windows. |
| `lib/safety.sh`, `publish-public.sh` | `safety.py`, `publish.py` | Shared denylist. |
| `install.sh`, `npm-tools.sh`, `gh-tools.sh`, `python-tools.sh` | `install.py`, `tools.py` | The pip step moves to `pip install` from the running interpreter. |

The `weekly-review/` job is already Python and stays as is.

## Behaviour the port must reproduce

Found by reading the bash; each needs a pytest case.

- **Mirror copy.** Capture is `-a --delete` with `.gitkeep` excluded plus the profile exclude list. Apply is `-a` and deletes only when `mirror=true` (`mirror=false` is the first-ever sync). Exclude entries are anchored, top-level names only, and excluded names are never deleted on the receiving side. Copy via temp name then `os.replace`, keep mtime, skip when size and mtime match, keep the exec bit on Linux (hooks). Because rsync's size+mtime skip can leave a stale file, `profile_unapply` exists; the Python mirror must compare content or reproduce that step.
- **Profile `CLAUDE.md` block.** The marker `<!-- claude-sync:profile-append -->` is a whole line. Apply appends `\n<marker>\n` plus `CLAUDE.append.md`; strip cuts at the byte offset of the first exact-match line. Read and write binary. The stamp dir is written to `.tmp` then renamed, with the old record removed first, so a crash reads as "not applied".
- **Settings overlay.** Merge is `shared * override`, then delete leaf paths where override is `null`. Split takes five inputs (local, shared before, override before, and so on) and prunes empty parent objects. Leaf rules: the root is never a leaf, empty objects are not leaves, arrays are leaves. *(Superseded in phase 1: a nested empty object is a leaf, as in the bash jq `_leaf` helper, so `{"a": {}}` owns `a`; only the root `{}` owns nothing. See `docs/python-port.md`.)* JSON output is 2-space indent, `ensure_ascii=False`, trailing newline; key order is preserved, where jq may differ, so parity of bytes against existing files is decided in the plan.
- **Override paths.** `override add/remove/list` take jq path expressions (for example `.enabledPlugins["x@y"]`). Python has no jq, so the plan either writes a small path parser that accepts exactly that syntax, or changes the CLI syntax. Default: write the parser so existing usage and docs keep working.
- **Git flow.** `refs/claude-sync/applied`, `materialise` (masks INT/TERM/HUP during `read-tree`; exits 130/143/129), `rebuild_sync_branch`, `adopt_remote_sync_branch`, `migrate_legacy_default` are about 600 lines and are the largest estimate risk. They get their own module (`gitflow.py`) and their own plan tasks.
- **Skill links.** A link is replaced only if it is a symlink or absent. Dangling links whose targets are under `<agents_home>/skills` are removed. Junction reading and the `.disabled` marker need Windows care.
- **Safety and publish.** Denylisted names and the `*/machines/*` plus `*.settings.json` patterns are matched at any depth, so Python needs a recursive walk equivalent to `find`.
- **Shim.** `claude-sync.sh` finds Python via `python3`; the `.cmd` shim via `py -3`, then `python`.

## Windows specifics to settle in the plan

- On Windows the preferred `PRIMARY_PROJECT_PATH` (for example `/z/projects`) falls back to the user's home directory if that directory does not exist. Because this doc is published, the code must not hard-code any personal path: the installer takes the preferred path from a flag or `CLAUDE_SYNC_PROJECT_PATH`, and applies the home-directory fallback when it is missing. Still to settle: the stored format (`/z/projects` against `Z:\projects`) and how it is written to `machines/<host>.conf`. The chosen value lives only in the machine's conf.
  > **Settled (2026-11-11):** the owner chose the native form. On Windows the installer stores `Z:\projects` (a Git Bash `/z/projects` is converted), and an existing conf in the `/z/...` form is read as `Z:\...` without rewriting the file. See `docs/python-port.md`.
- The memory directory name encoding (`encode_project_path`) for drive-letter paths.
  > **Settled (2026-11-11):** Claude Code's rule, every character other than `A-Z`, `a-z` and `0-9` becomes `-` (`C:\Users\x` gives `C--Users-x`). See `docs/python-port.md`.
- The Windows machine follows the shared profile (confirmed). The plan must check which profile entries are Linux-only and how the profile handles them on Windows.
- Hook commands in `settings.json` currently call `"$HOME/.claude/hooks/<name>.sh"` and inline `jq`. Phase 2 needs a per-OS command form, likely through the machine settings override.
- `api-error-alert.sh` notification path on Windows.

## Testing

- Provide a pytest fixture equivalent to `test_helper.bash` (fake HOME, fake `gh` stub, `CLAUDE_SYNC_*` variables). `sync_cli.bats` alone has 56 tests.
- Port each bats file to pytest, one module at a time: `gh_tools`, `machine_conf`, `memory_path`, `npm_tools`, `plugin_suggest`, `profile`, `python_tools`, `safety`, `settings_overlay`, `settings_portability`, `skills_symlink`, `sync_cli`, `sync_files`, `sync_pr`, `weekly_review`.
- Add cases bash never needed: lock held by a dead process, CRLF content, paths with spaces, junction creation and removal, mirror copy deleting only what the source lacks.
- CI runs pytest on `ubuntu-latest` and `windows-latest`. bats and `shellcheck` stay in CI until the bash is removed.
- Tests never touch the real HOME.

## Phasing

1. Sync core: lock, file sync, settings overlay, profiles, machine conf, skill links, CLI with shims, installers, `publish-public`, pytest on both platforms. Linux switches over only after parity with bats and one real sync on a test machine.
2. Hooks: port to Python, remove `jq` and bash from Windows, update `settings.json` commands.
3. Remove the bash implementation and the bats suite, and update every doc in `docs/doc-map.md`.

Each phase is its own PR or series of PRs. Docs and a superseded note on the 2026-07-25 spec land with phase 1.

## Left to the builder

- Exact function signatures and internal layout inside the modules above.
- How to make the mirror copy robust against partial failure (write to a temp name, then replace).
- Error message wording, as long as each still says what to do.

## Open questions for the user

- Whether to port `publish-public.sh` (current lean: yes).
- Minimum Python version on Windows (3.12 assumed).
