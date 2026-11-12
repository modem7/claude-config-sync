# Python port

The sync tool and the installer are now Python (`claude_sync/`, standard library only, Python 3.12 or newer). This page lists how to run them, what changed for users, and where the Python behaves differently from the bash on purpose. The design is in `docs/superpowers/specs/2026-11-11-python-port-design.md`.

## Entry points

| Command | Runs |
|---|---|
| `claude-sync.sh` | `python3 -m claude_sync`, from the clone |
| `install.sh` | `python3 -m claude_sync.install`, from the clone |
| `claude-sync.cmd` (Windows) | `py -3 -m claude_sync`, else `python -m claude_sync` |
| `install.cmd` (Windows) | `py -3 -m claude_sync.install`, else `python -m claude_sync.install` |
| `python3 -m claude_sync.publish` | the public mirror export (the publish workflow runs it) |

- The `.sh` shims put the clone first on `PYTHONPATH` (keeping any existing value after it) and change to the clone folder before they start Python, so the package always imports from the clone. Give `--project-path` as an absolute path: a relative one is read from the clone folder.
- Without `python3` on `PATH`, the `.sh` shims print `claude-sync: needs python3 (3.12+); or set CLAUDE_SYNC_IMPL=bash` and exit 1. With a Python older than 3.12 the tool prints `claude-sync needs Python 3.12 or newer.` and exits 1.
- The `.cmd` shims use `PYTHONPATH` and leave the working folder alone, because Windows cannot delete a folder that a running shell is using, and `--repair` deletes the clone. For the same reason, run `install.cmd --repair` from a prompt outside the clone: the installer cannot move the prompt that started it.
- Every argument is passed through unchanged. The hooks still call `~/claude-config/claude-sync.sh`, so they run the Python with no change.

### Running the old bash

`CLAUDE_SYNC_IMPL=bash` makes both `.sh` shims run the old scripts, kept in `legacy/claude-sync.sh` and `legacy/install.sh` until phase 3 of the port. They still use `lib/*.sh`, `rsync`, `flock` and `jq`. The bats suite sets `CLAUDE_SYNC_IMPL=bash` in `tests/test_helper.bash`, so `bats tests` tests the bash, and `python -m pytest tests/py` tests the Python. There is no bash fallback on Windows.

## What changed for users

- **`CLAUDE_SYNC_FLOCK` is dropped.** The Python locks `.git/claude-sync.flock` itself (`fcntl.flock` on Linux and macOS, `msvcrt.locking` on Windows), so there is no `flock` binary to pick and no weaker pid-link lock. The file name is the same as the bash one, so a bash run and a Python run still keep each other out during the switch-over. Only the legacy bash still reads `CLAUDE_SYNC_FLOCK`.
- **`CLAUDE_SYNC_LOCK_WAIT` is checked.** A value that is not a finite number of seconds, 0 or more, fails with `CLAUDE_SYNC_LOCK_WAIT must be a number of seconds, not '<value>'.`. Blank or unset still means 10. It is read once at the start of every command, so a bad value also stops `doctor` and `profile list`, where the bash only read it when taking the lock.
- **The usage text** says `Needs git 2.40+. A second run waits CLAUDE_SYNC_LOCK_WAIT seconds (default 10) for the first.` in place of the two flock lines.
- **`install.sh --project-path PATH`** (or `--project-path=PATH`) sets the primary project path without the prompt. The usage line is `Usage: install.sh [--repair|--reinstall] [--profile NAME] [--project-path PATH]`.
- **Run the installer with the system Python**, not from a venv or `uv run`. pip runs as `sys.executable -I -m pip`, so it installs into whichever Python ran the installer. `-I` also leaves out the user site, so a pip that is only installed in the user site is not found, and the step prints a warning and carries on.
- **The installer verifies no downloads.** `npm install -g ccusage@latest` takes the latest version, unpinned. The pip packages (`mcp<2`, `aiosqlite`, `pyyaml`) are version ranges with no hashes. Both tools run from an empty temporary folder, so files in your current folder are not picked up.

## Deliberate differences from the bash

The bash is the baseline: anything not listed here behaves the same.

### claude-sync

- Messages the bash printed bare get the `claude-sync: ` prefix, for example `No git repo at …`, `Not a valid profile name: …`, `No profile called …`, `Aborted: nothing was copied.`, the project path check and the capture refusals. The wording is otherwise the same; some now have a hint line.
- Any `.git` counts as a clone, so a `git worktree` works. The bash wanted a `.git` folder.
- `override add` and `override remove` check the path before they capture or commit anything, so a bad path changes nothing. The bash failed after its capture commit.
- `doctor` reports an override that does not parse, or is not a JSON object, as an issue (`could not work out what settings.json should be: …`) and does not re-apply over it.
- `profile copy` follows the profile link rules: no link reaches the repo, and a folder link or special file in the source stops the copy with a message. The bash `cp -a` copied links as links. On Windows every hook file is listed for the confirmation, since there is no executable bit.
- A project path whose memory folder name is over 200 characters gets a warning from `sync` (`claude-sync: warning: the project path … may not be the folder Claude Code uses …`) and an issue from `doctor`. Claude Code shortens such names and adds a hash; claude-sync does not copy that rule. The bash said nothing.
- When a run is stopped (SIGTERM, SIGHUP, Ctrl-C) while git or another child runs, the child gets 5 seconds to finish, then is terminated, then killed, so git can remove its `.lock` files. The bash ran its trap only once the foreground git had finished.
- A run ended by a signal prints `claude-sync: stopped by Ctrl-C|SIGTERM|SIGHUP (exit N). Re-run the command; …`, in place of the bash `stopped with an error`.
- `doctor remediate` runs its nested sync in the same process, and a signal during it ends `doctor` with the same exit code.
- A signal during an apply is held until the applied record is written, then ends the run. The bash dropped a signal inside its hold window.
- Every git call passes `-c core.autocrlf=false`.
- Settings overrides: a nested empty object is a leaf, so `{"a": {}}` owns `a`. Only the root `{}` owns nothing. This follows the bash (jq) behaviour; the design spec's "empty objects are not leaves" is superseded.
- The memory folder is named the way Claude Code names it: every character of the project path other than `A-Z`, `a-z` and `0-9` becomes `-`. The bash only replaced `/`, so for a path with a space, `.`, `_` or a drive letter it synced a folder Claude Code never writes. The first Python sync on such a machine mirrors the right folder into `memory/` with delete; check that PR's `memory/` diff (`docs/runbook.md`, cutover gate). Claude Code also shortens a very long path and adds a hash; that is not handled, so keep the primary project path short.
- The settings split takes the shared base from the effective commit, not the clone's working tree, and parses the override, its applied record and the local `settings.json` before capture. If capture or the split stops, the clone's `dot-claude/settings.json` is put back as committed before the error leaves. The bash read the working tree and left the merged file there, so after a failed split its next run, or a `pull`, could push override values as shared.
- Capture refuses a repo-side folder (`dot-claude/`, `dot-agents/`, their `agents`, `hooks` and `skills`, `memory/`) that is a link, and a profile that is or holds a refused link, before writing. The bash `rsync` followed them.
- Every `claude-sync` command checks the hostname (`CLAUDE_SYNC_HOSTNAME`, else the system name) with the profile-name rules, as `install` does, and refuses a bad one with `Not a valid hostname: …`.
- `begin_sync` checks the git version before the `origin` remote.
- git's own error text (for example a failed fetch), and every failure message and hint `claude-sync` prints, are redacted before they are shown, so a credential in a remote URL never reaches the terminal or the hook log.
- Output that the terminal or log encoding cannot show is written as backslash escapes instead of ending the run with `UnicodeEncodeError`.
- The commit step keeps exec bits that Windows git (`core.filemode=false`) would drop. Between `git add -A` and `write-tree`, a path that is `100755` in the commit it builds on stays `100755`, and a new file under `dot-claude/hooks/` or `machines/<profile>/hooks/` whose name ends in `.sh` or whose first line starts with `#!` is set to `100755` (`git update-index --chmod=+x`). It only adds the bit, never removes it, on every OS: a hook that lost its bit on one machine (an editor rewrote it, a copy from Windows) keeps it. To remove a bit on purpose, change the mode on the default branch through a PR (`docs/runbook.md`, "Remove an exec bit"). This covers capture, `profile copy` and every other commit through `commit_worktree`, and the clone edits that picking up an open sync branch commits. Apply is unchanged: it checks out the committed mode. The bash took every mode from disk, so a hook captured or copied on Windows reached Linux non-executable.
- `doctor` reports a hook (`dot-claude/hooks/**`, `machines/*/hooks/**`) tracked as `100644` that has a `.sh` name or a `#!` line, with the fix: `git update-index --chmod=+x -- <path>`, or on Linux and macOS `chmod +x` on the clone and live copies, then `sync`. The paths in the hint are quoted for pasting and stripped of control characters; on Windows a path with `"`, `$`, `` ` ``, `%` or `!` gets no command line.
- On Windows, the git calls inside the apply window also run in their own process group (Ctrl-C off), since Windows has no signal mask for git to inherit.
- JSON is written with Python's encoder: a float such as `1e5` is written `100000.0` (jq: `1E+5`), and U+007F is written raw (jq: `\u007f`). No current file has either.

### install

- It runs the sync in the same process, and the tool steps from the package, not the new clone's `claude-sync.sh` and `*-tools.sh`.
- `--repair` refuses more. It does not delete a repo folder that is a link or junction, a non-empty folder that is not a clone, the home folder, or a folder that holds `~/.claude` or `~/.agents`. It also refuses when `git status` fails.
- The hostname is checked with the profile-name rules before it names `machines/<hostname>.conf`.
- The clone is made with `-c core.autocrlf=false`. A failed clone says `Could not clone the sync repo into DIR.` with git's error redacted. The URL is never printed.
- On Windows the gh and python steps only say what to install. pip runs without `--break-system-packages` there.
- On Windows the project path is stored in the native drive form (`Z:\projects`). A Git Bash form (`/z/projects`, `/c/Users/...`) given to the installer is converted before it is checked and stored, and the drive letter is upper-cased. A missing project path falls back to the home folder on Windows only. Elsewhere the path is stored as given, as before.
- An existing `machines/<hostname>.conf` written by the bash under Git Bash (`PRIMARY_PROJECT_PATH=/z/projects`) still works: on Windows `read_primary_project_path` reads it as `Z:\projects` and leaves the file as it is, so both forms resolve the same memory folder (`Z--projects`). The bash used `-z-projects` there (row 28 in `docs/risks.md`).

### publish

- It also refuses a `.credentials.json` or `.claude.json` anywhere in the public tree, skips links (with a warning) instead of copying them, and exports only files committed at `HEAD`, never ignored or untracked ones.
- Each published file keeps the mode it has at `HEAD`, set in the mirror's index, not read from disk. So publishing from Windows (no executable bit, `core.filemode=false`) keeps `100755` scripts executable. The bash took the mode from the copied file; `publish-public.sh` now sets HEAD's modes in its index too, so both agree.
- It has no built-in public repo URL: set `CLAUDE_CONFIG_PUBLIC_REPO_URL` (the workflow does). The commit message names the source repo from `GITHUB_REPOSITORY`, else `origin`, else the folder name.

## Line endings

`.gitattributes` has `* text=auto eol=lf` and `*.ps1`, `*.cmd` and `*.bat` with `eol=crlf`. Its last lines unset `text` for `dot-claude/**`, `dot-agents/**` and `memory/**`, so git stores and checks out the synced files byte for byte: a CRLF `CLAUDE.md` stays CRLF and an LF `.ps1` hook stays LF. They come last so they win over the per-extension rules above them. Other files in the repo are still normalised as before. This applies to the bash too, since it is git that does it. `tests/py/test_gitattributes.py` checks this against a copy of the real file.

- Not covered: profile files under `machines/<profile>/` (hooks, agents, `CLAUDE.append.md`) are still normalised by the per-extension rules.
- A machine whose live files are CRLF only because an old clone checked them out that way (Windows, `core.autocrlf` on) now captures them as CRLF. Before the next sync, convert those live files to LF by hand, or drop the line-ending change from the PR (`docs/runbook.md`, "Re-check out a Windows clone"); re-checking the clone out does not change the live copies.
- The Python passes `-c core.autocrlf=false` to git, so a global `core.autocrlf` adds nothing on top.
- On Windows, clone with `-c core.autocrlf=false` (the installer does). An existing Windows clone made with autocrlf on needs a one-time re-checkout, and its live CRLF files converted to LF by hand or the change dropped from the PR; see `docs/runbook.md`, "Re-check out a Windows clone".

## Windows CI checklist

The Windows job is the only place these run. Before the cutover, check it covers each, and add a test where it does not:

- The `.cmd` shims really run: the exit code comes through, arguments with spaces and quotes arrive unchanged, a clone path with spaces works, and `py -3` is used before `python` (with `python` only when `py` is missing).
- The lock: `msvcrt.locking` on a 0-byte `claude-sync.flock`, a killed holder frees it, and the handle is not inherited by a child (`git gc`).
- Junctions for `~/.claude/skills/<name>`: made, read, replaced and removed, never followed when deleting.
- A read-only file in the way is overwritten, and a read-only tree is deleted.
- Non-ASCII output redirected to a file or pipe in a legacy code page does not crash the run.
- `install.cmd --repair` run from a prompt outside the clone.
- The memory folder for a project path with spaces (`C:\Users\John Smith\project` gives `C--Users-John-Smith-project`).
- Uppercase and non-ASCII hostnames: an uppercase one is kept as is, a non-ASCII one is refused with `Not a valid hostname`.

## Open questions

- The Windows CI job is the only place the Windows branches run (lock, links and junctions, read-only removal, signals, `.cmd` shims).
