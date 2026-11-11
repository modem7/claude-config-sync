# Runbook

Procedures for the sync tool. Paths assume the clone is at `~/claude-config`.

## Restore a machine on new infrastructure

First install the prerequisites: git 2.40 or newer, and the GitHub CLI (`gh`), logged in with `gh auth login`. `install.sh` prints a hint if `gh` is missing or not logged in; without it every sync pushes its branch, then fails.

Same hostname as before: nothing special. Clone the repo, run `install.sh`, and the machine's profile and settings override apply on the first sync.

```bash
git clone git@github.com:YOUR_USER/YOUR_REPO.git ~/claude-config
CLAUDE_SYNC_REPO_URL=git@github.com:YOUR_USER/YOUR_REPO.git ~/claude-config/install.sh
```

Different hostname, same behaviour as an existing machine (shared profile, edits go back to that profile):

```bash
~/claude-config/install.sh --profile host-a
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

## Add a machine-only or replacement entry

Put the file under `machines/<profile>/agents/`, `hooks/` or `skills/`, or add `<entry>.disabled` to hide a shared one, then run `claude-sync.sh sync` on that machine. If you disable a hook that `settings.json` wires in, also remove the wiring with `override add`, or `doctor` will flag it.

## Share a machine's changes

`sync` puts this machine's changes on `sync/<hostname>` and opens a PR for it (it needs `gh`, logged in; `CLAUDE_SYNC_GH=<path>` picks the binary and `CLAUDE_SYNC_GH_REPO=owner/repo` the repo when origin's URL doesn't show it; `CLAUDE_SYNC_FLOCK=<path>` and `CLAUDE_SYNC_LOCK_WAIT=<seconds>` set the lock binary and wait). Review and merge the PR; every machine picks it up on its next `sync` or `pull`. Later syncs from the same machine update the same PR. If `gh` is missing or not logged in, the branch is still pushed and the error prints the `gh pr create` command to run by hand.

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

"another claude-sync run is still working": another session's sync holds the lock. Wait and re-run. With `flock` the lock frees itself when that run exits; without it, remove the `.git/claude-sync.lockpid` link the message names once no `claude-sync.sh` is running. `CLAUDE_SYNC_LOCK_WAIT=<seconds>` waits longer.

"the last applied commit ... is no longer in": the stamp in `~/.claude/.claude-sync-applied-commit` points at a commit the clone lost (re-cloned or garbage-collected; it is pinned as `refs/claude-sync/applied` now). Either `pull` (discards local edits), or copy your edits aside, delete the stamp and run `sync`.

"sync SKIPPED": the clone is on a branch other than its default branch, so nothing was captured. Switch it back (`git -C ~/claude-config switch <default>`), or develop in a separate worktree.

A merged PR whose branch GitHub kept is ignored: the machine remembers the tip it last pushed, and a fresh clone checks with `gh` for a merged or closed PR at that tip. The next change replaces that branch and opens a new PR.

## Reset a machine's sync state

```bash
~/claude-config/install.sh --repair
```

The machine's new `.conf` goes out through its sync PR; repair pushes nothing to the default branch. This keeps the machine's profile and settings override. Re-give the profile with `--profile <name>` if it followed another one. It refuses while this machine's sync PR is pending (the clone shows those changes as uncommitted), so merge or drop the PR and run `sync` first. Never `git stash` or `git reset --hard` the clone to get past it. A fresh clone picks up the machine's open `sync/<hostname>` from origin by itself.

## Publish the public mirror by hand

Commit first; the script refuses on a dirty tree.

```bash
~/claude-config/publish-public.sh
```

It aborts if `machines/`, `memory/`, `private/` or `weekly-review/` ended up in the generated tree.
