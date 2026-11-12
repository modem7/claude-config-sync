# Runbook

Procedures for the sync tool. Paths assume the clone is at `~/claude-config`.

## Restore a machine on new infrastructure

First install the prerequisites: Python 3.12 or newer (the system one, not a venv), git 2.40 or newer, and the GitHub CLI (`gh`), logged in with `gh auth login`. `install.sh` prints a hint if `gh` is missing or not logged in; without it every sync pushes its branch, then fails.

Same hostname as before: nothing special. Clone the repo, run `install.sh`, and the machine's profile and settings override apply on the first sync.

```bash
git clone git@github.com:YOUR_USER/YOUR_REPO.git ~/claude-config
CLAUDE_SYNC_REPO_URL=git@github.com:YOUR_USER/YOUR_REPO.git ~/claude-config/install.sh
```

Different hostname, same behaviour as an existing machine (shared profile, edits go back to that profile):

```bash
~/claude-config/install.sh --profile host-a
```

`--project-path /absolute/path` sets the primary project path without the prompt. On Windows it is stored as `Z:\path`; a Git Bash `/z/path` is converted.

On Windows, clone with `-c core.autocrlf=false` and run the `.cmd` shim (it uses `py -3`, else `python`):

```bat
git clone -c core.autocrlf=false git@github.com:YOUR_USER/YOUR_REPO.git %USERPROFILE%\claude-config
set CLAUDE_SYNC_REPO_URL=git@github.com:YOUR_USER/YOUR_REPO.git
%USERPROFILE%\claude-config\install.cmd
```

or on a machine that is already set up:

```bash
~/claude-config/claude-sync.sh profile use host-a
```

## Clone a profile as an independent copy

The copy diverges from the original afterwards. It also copies the source's settings override.

```bash
~/claude-config/claude-sync.sh profile list
~/claude-config/claude-sync.sh profile show host-a
~/claude-config/claude-sync.sh profile copy host-a
```

`profile copy` lists the executable hooks it will copy and asks first (`--yes` skips). It refuses if this machine already has a profile or settings override (`--force` replaces them).

## Remove an exec bit

Sync never removes an exec bit: a `chmod -x` (or `update-index --chmod=-x`) on a machine is undone by its next sync, on purpose, so a bit lost by accident does not spread. To remove one on purpose, from any machine:

```bash
git -C <worktree> switch -c chore/drop-exec-bit origin/master
git -C <worktree> update-index --chmod=-x -- dot-claude/hooks/<name>
git -C <worktree> commit -m "chore: drop the exec bit of <name>"
```

Use a separate worktree or clone, not the live clone. Push the branch, open a PR and merge it. Each machine's next `sync` or `pull` then applies the mode (the live copy loses its bit on Linux and macOS) and does not add it back.

## Add a machine-only or replacement entry

Put the file under `machines/<profile>/agents/`, `hooks/` or `skills/`, or add `<entry>.disabled` to hide a shared one, then run `claude-sync.sh sync` on that machine. If you disable a hook that `settings.json` wires in, also remove the wiring with `override add`, or `doctor` will flag it.

## Share a machine's changes

`sync` puts this machine's changes on `sync/<hostname>` and opens a PR for it (it needs `gh`, logged in; `CLAUDE_SYNC_GH=<path>` picks the binary and `CLAUDE_SYNC_GH_REPO=owner/repo` the repo when origin's URL doesn't show it; `CLAUDE_SYNC_LOCK_WAIT=<seconds>` sets how long a second run waits for the lock; `CLAUDE_SYNC_FLOCK` is gone, see `docs/python-port.md`). Review and merge the PR; every machine picks it up on its next `sync` or `pull`. Later syncs from the same machine update the same PR. If `gh` is missing or not logged in, the branch is still pushed and the error prints the `gh pr create` command to run by hand.

To drop a machine's pending changes instead: close the PR, delete the branch on GitHub, then on that machine:

```bash
git -C ~/claude-config branch -D sync/<hostname>
git -C ~/claude-config reset -q --hard
~/claude-config/claude-sync.sh pull
```

## A sync seems stuck

```bash
~/claude-config/claude-sync.sh doctor
~/claude-config/claude-sync.sh doctor remediate
```

A real content conflict is reported, not guessed at: this machine's pending changes and a merged change edited the same lines. Either merge `origin/<default>` into `sync/<hostname>` in `~/claude-config`, fix the conflict and commit, then run `sync` again; or drop the pending changes as above. Other failures (fetch, push, `gh`, a git older than 2.40, a `git merge-tree` error) name the cause and the fix in their message, and the hooks log it to `~/.claude/claude-config-sync.log`.

"… is a hook script tracked without the exec bit": a hook was committed as `100644`, usually from Windows, so Linux and macOS get it non-executable. On Windows run `git -C ~/claude-config update-index --chmod=+x -- <path>`; on Linux or macOS run `chmod +x` on the file in `~/claude-config` and on its live copy under `~/.claude/hooks/`, if there is one. Then run `claude-sync.sh sync` and merge its PR.

"another claude-sync run is still working": another session's sync holds the lock. Wait and re-run. The lock frees itself when that run exits. `CLAUDE_SYNC_LOCK_WAIT=<seconds>` waits longer; it must be a number of seconds, 0 or more. Only the legacy bash without `flock` uses a `.git/claude-sync.lockpid` link: remove it once no `claude-sync.sh` is running.

"the last applied commit ... is no longer in": the stamp in `~/.claude/.claude-sync-applied-commit` points at a commit the clone lost (re-cloned or garbage-collected; it is pinned as `refs/claude-sync/applied` now). Either `pull` (discards local edits), or copy your edits aside, delete the stamp and run `sync`.

"warning: the project path … may not be the folder Claude Code uses": the memory folder name for `PRIMARY_PROJECT_PATH` in `machines/<hostname>.conf` is over 200 characters. Claude Code shortens such names and adds a hash, so the synced `memory/` may come from a folder it no longer writes. Point the conf at a shorter project path (or `install.sh --repair --project-path <shorter>`) and check the next sync PR's `memory/` diff.

"Unable to create '….lock': File exists" (for example `.git/index.lock` or `.git/refs/remotes/origin/<branch>.lock`): a run stopped while git was writing left git's own lock behind. Since a SIGTERM, SIGHUP or Ctrl-C now lets git finish first (`docs/risks.md` row 35), this is mostly after a power loss, a SIGKILL, the Windows hook timeout, or a run of the legacy bash. Once no `claude-sync` and no other git command is running in the clone, delete that `.lock` file and re-run.

"sync SKIPPED": the clone is on a branch other than its default branch, so nothing was captured. Switch it back (`git -C ~/claude-config switch <default>`), or develop in a separate worktree.

A merged PR whose branch GitHub kept is ignored: the machine remembers the tip it last pushed, and a fresh clone checks with `gh` for a merged or closed PR at that tip. The next change replaces that branch and opens a new PR.

## Reset a machine's sync state

```bash
~/claude-config/install.sh --repair
```

The machine's new `.conf` goes out through its sync PR; repair pushes nothing to the default branch. This keeps the machine's profile and settings override. Re-give the profile with `--profile <name>` if it followed another one. It refuses while this machine's sync PR is pending (the clone shows those changes as uncommitted), so merge or drop the PR and run `sync` first. Never `git stash` or `git reset --hard` the clone to get past it. A fresh clone picks up the machine's open `sync/<hostname>` from origin by itself.

On Windows, run `install.cmd --repair` from a prompt whose folder is outside the clone (for example `cd /d %USERPROFILE%` first): Windows cannot delete a folder a shell is using.

## Publish the public mirror by hand

Commit first; the script refuses on a dirty tree.

```bash
~/claude-config/publish-public.sh
```

The Python port, which the publish workflow runs, needs the public repo's URL set (it has no built-in default):

```bash
cd ~/claude-config && CLAUDE_CONFIG_PUBLIC_REPO_URL=git@github.com:<owner>/<repo>.git python3 -m claude_sync.publish
```

It aborts if `machines/`, `memory/`, `private/` or `weekly-review/` ended up in the generated tree. The Python port also aborts on a `.credentials.json` or `.claude.json` anywhere in it, skips links instead of copying them, and exports only files committed at `HEAD`. The bash script copies the working tree, ignored files included, so prefer the Python port.

Published files keep the mode they have at HEAD, also when it is run on Windows (`_keep_modes` in the Python; the bash twin does the same after its `git add -A`).

The whitelist (`PATHS` in `claude_sync/publish.py` and `publish-public.sh`) includes the `claude_sync/` package, `pytest.ini`, the `.cmd` shims and `legacy/`, so the mirror can run and test the Python. Keep the trigger paths in `.github/workflows/publish-public.yml` in step with it; `test_workflow_triggers_match_whitelist` checks.

## Run the old bash implementation

`claude-sync.sh` and `install.sh` run the Python port. To run the bash scripts kept in `legacy/` instead (Linux and macOS only, until phase 3):

```bash
CLAUDE_SYNC_IMPL=bash ~/claude-config/claude-sync.sh sync
```

To keep the hooks on the bash too, set `CLAUDE_SYNC_IMPL=bash` in the environment Claude Code runs them in (for example the shell profile that starts `claude`, or `env` in that machine's settings override). `bats tests` always runs the bash; `python -m pytest tests/py` runs the Python.

## Re-check out a Windows clone after a line-ending change

A Windows clone made with `core.autocrlf` on has CRLF in its working tree, and turning it off later does not rewrite files git thinks are up to date. With no sync PR pending and nothing uncommitted:

```bat
git -C %USERPROFILE%\claude-config config core.autocrlf false
git -C %USERPROFILE%\claude-config rm -r -q --cached .
git -C %USERPROFILE%\claude-config reset -q --hard
```

Or run `install.cmd --repair`, which clones afresh with `-c core.autocrlf=false`. Since `.gitattributes` unsets `text` for `dot-claude/`, `dot-agents/` and `memory/`, git no longer rewrites those files. Re-checking out the clone does not change the live copies: a file that is CRLF in `~/.claude` only because an old clone wrote it that way goes into the next sync's PR as a line-ending-only change, since capture runs first. Before that sync, convert such live files back to LF by hand, or drop the line-ending change from the PR (see `docs/python-port.md`, "Line endings").

## Cutover gate (owner, before merging the switch to Python)

The hooks call `~/claude-config/claude-sync.sh`, so once the PR that makes it a Python shim is merged, every machine runs the Python on its next sync. Do this first, by hand:

- [ ] On every machine, check `python3 --version` is 3.12 or newer, in the shell the hooks run in (on Windows, Git Bash), not only in your own. `command -v python3` is not enough: the Windows Store stub is found on `PATH` but exits 9009 without running anything. Debian 12 ships 3.11, Ubuntu 22.04 3.10 and stock macOS 3.9; on those, every hook sync fails after the merge until a newer Python is installed. Keep such a machine on the bash (next step) until it has one.
- [ ] On every machine, check the hostname (or `CLAUDE_SYNC_HOSTNAME`, if set) uses only letters, digits, `.`, `_` and `-`. The Python checks it on every command, the bash did not, so otherwise every hook sync fails with `Not a valid hostname` once the switch is merged.
- [ ] Before merging, set `CLAUDE_SYNC_IMPL=bash` on every machine that should stay on the bash for now (or all of them, to switch over one by one). Set it where the hooks get their environment. If you set it through `env` in a machine's settings override, that override must already be merged and applied on the machine, and Claude Code restarted, before the switch is merged.
- [ ] On a test machine, make two identical starting states, each with its own throwaway clone, origin and HOME (`HOME`, `CLAUDE_SYNC_CLAUDE_HOME` and `CLAUDE_SYNC_AGENTS_HOME` pointing at copies, the same `CLAUDE_SYNC_HOSTNAME`, a fake `CLAUDE_SYNC_GH`). Never use the real HOME.
- [ ] Run this gate on at least one machine that has a settings override (`machines/<hostname>.settings.json`) and one that has a profile (`machines/<profile>/`), not only a plain machine: those paths differ most between the two implementations.
- [ ] Run each clone's own `claude-sync.sh sync`: the first with `CLAUDE_SYNC_IMPL=bash`, the second without.
- [ ] Diff the two `~/.claude` (and `~/.agents`) trees, and the trees of the pushed `sync/<hostname>` commits (`git diff --stat <bash-tip> <python-tip>`, then `git diff` if anything shows). They must match, except `memory/` on a machine whose project path has a space, `.`, `_` or a drive letter: the Python uses Claude Code's folder name there and the bash does not (`docs/risks.md` row 28). On such a machine, check the `memory/` diff of its first Python sync PR before merging it; it can delete repo memory files. A Windows conf the bash wrote in the Git Bash form (`/z/...`) still works and need not be edited: the Python reads it as `Z:\...`, so it uses the `Z--...` folder Claude Code writes, not the bash's `-z-...`.
- [ ] Run `claude-sync.sh doctor` once on the Python side. A hook committed from Windows shows as "tracked without the exec bit"; fix each as the hint says (see "A sync seems stuck") and merge that PR first. The bash doctor does not check this.
- [ ] Check the Windows CI job is green, and walk the Windows checklist in `docs/python-port.md`.
- [ ] Only then merge. Switch machines over by removing `CLAUDE_SYNC_IMPL=bash` one at a time, and check each one's next sync in `~/.claude/claude-config-sync.log`.
- [ ] Switch a machine over only while no Claude Code session is running on it. The Python and the bash share the lock only when the bash uses util-linux `flock`; with its pid-link fallback, or Git Bash's `flock`, a bash hook and a Python hook can run at once.
- [ ] On Windows, after the first Python sync, delete once any real folders under `~/.claude/skills` that Git Bash's `ln -sfn` left as copies of `~/.agents/skills/<name>`. The Python only replaces links and junctions there, never a real folder, so those copies would stay stale.
- [ ] Know that on Windows the hook timeout kills Python outright, with no handler run. Recover as after a power loss: run `claude-sync.sh doctor`, and remove a stale git `.lock` file as in "A sync seems stuck".
