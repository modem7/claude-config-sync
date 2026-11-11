# Working in this repo

This repo syncs Claude Code's global config (`CLAUDE.md`, `settings.json`, agents, hooks, skills, memory) between machines. It is AI safe: guidance files for Claude are welcome here. Keep this file short; read the linked docs when a task needs them.

## How it works

- `claude-sync.sh sync` = capture local files into the clone and commit onto `sync/<hostname>` (one commit on top of the default branch), rebuild that onto origin's default branch, apply it back to the machine, then force-push (with a lease) only that branch and open its PR with `gh`. It never commits or pushes to the default branch and never auto-merges. The clone stays on the default branch with the pending state in its working tree (not staged). Copy-based, never symlinks. Apply mirrors with `rsync --delete`. Every failure prints what to do on stderr and exits non-zero. Runs are serialised by `flock` on `.git/claude-sync.flock` (a pid symlink lock when `flock` is missing). Needs git 2.40+ and `gh`. `install.sh --repair` also goes through the PR.
- Shared files live in `dot-claude/` and `dot-agents/skills/`. Per-machine differences live only under `machines/`:
  - `machines/<hostname>.conf` (project path, optional `PROFILE=`)
  - `machines/<hostname>.settings.json` (settings override)
  - `machines/<profile>/` (agents, hooks, skills, `CLAUDE.append.md`, `NOTES.md`), see `lib/profile.sh`
- `lib/` holds the logic, `tests/` the bats tests, `publish-public.sh` the public mirror export.

## Rules

- Open a PR. Never push to the default branch. `claude-sync.sh` skips syncing (it prints "sync SKIPPED" and exits 0) while this clone is on a feature branch, which is expected.
- Develop in a separate `git worktree` cut from `origin/<default>`, not in the live clone: while a sync PR is pending, the live clone's working tree carries that pending state.
- Do not edit `memory/` or `machines/` by hand unless the task is about them. Sync rewrites them.
- Code, tests and docs outside `machines/`, `memory/` and `private/` must stay generic: placeholder hostnames (`host-a`, `host-b`), no personal paths, names or servers. They are published to the public template.
- Agents in `dot-claude/agents/` are published, so they must be generic: no personal hosts, URLs, repos or paths. An agent tied to one setup goes in `machines/<profile>/agents/`. A profile agent with the same name replaces the shared one on that machine, so keep the two in step when you change either.
- Shell scripts: `shellcheck install.sh claude-sync.sh npm-tools.sh gh-tools.sh lib/*.sh` must pass.
- Run `bats tests` before opening a PR. Tests use a fake HOME through the `CLAUDE_SYNC_*` environment variables. Never run `claude-sync.sh` or `install.sh` against the real HOME while testing.
- Follow `docs/doc-map.md`: when you change something, update every doc it lists for that kind of change. Run the `docs-sync` agent on the branch before opening a PR.
- Check `git status`, the branch and `origin` before starting.

## Read first

- `docs/risks.md`: what can go wrong (data loss, leaks, deadlocks) and the guard and test for each. Read before touching capture, apply, profiles or the publish script.
- `docs/runbook.md`: restoring a machine, cloning a profile, recovering a stuck sync, publishing by hand.
- `docs/doc-map.md`: which doc to update for each kind of change, and the twins that must stay in step.
- `docs/agents.md`: the shared agents, their models and rules.
- `docs/superpowers/specs/2026-07-25-claude-config-sync-design.md`: the design.
- If they exist (private repo only): `private/` for repo-wide private notes, and `machines/<profile>/NOTES.md` for the profile you are working on. These never reach the public mirror.
