# claude-config-sync

A sync tool for Claude Code's global configuration — `CLAUDE.md`,
`settings.json`, personal skills, hooks, and "global" auto-memory — across
every machine you run `claude` on.

This repo is the *tool* (`install.sh`, `claude-sync.sh`, the `claude_sync/`
Python package they run, hooks, lib, tests) plus a starting-point
`dot-claude/CLAUDE.md` and `dot-claude/settings.json` you're expected to
edit to taste. It is **not** meant to be synced from
directly — see "Get your own copy" below.

## Why

Claude Code stores its config under `~/.claude/` and personal skills under
`~/.agents/`. This tool keeps a git repo as the single source of truth those
directories sync from, via `install.sh` (first run on a new machine) and
`claude-sync.sh` (every run after).

## Get your own copy

You need Python 3.12 or newer, git 2.40 or newer and the GitHub CLI
(`gh`), logged in with `gh auth login`: every machine's changes go out as a
PR that `gh` opens. On Windows, clone with `-c core.autocrlf=false` and run
`install.cmd` and `claude-sync.cmd` in place of the `.sh` scripts.

This tool syncs your personal config, so you need your own private repo —
don't point it at this one. On GitHub: click **Use this template** (or fork
it and later delete the shared history), make it **private**, then:

```bash
git clone git@github.com:YOUR_USER/YOUR_REPO.git ~/claude-config
CLAUDE_SYNC_REPO_URL=git@github.com:YOUR_USER/YOUR_REPO.git ~/claude-config/install.sh
```

`install.sh` will also prompt for the repo URL interactively if you don't
set `CLAUDE_SYNC_REPO_URL` and a terminal is attached. It then asks for the
primary working directory you'll run `claude` from on this machine (used to
locate its Claude Code memory folder — see "Memory scoping" below),
defaulting to `~/project`, and runs a one-time **bootstrap**: it adopts
whatever's already in your repo first, then contributes anything unique to
this machine (nothing local-only gets deleted, and nothing already-shared
gets clobbered by this machine's own defaults). Every run after that is a
normal `sync` — see "Everyday use" below.

On a second machine, once your own repo has content, just run `install.sh`
again with the same `CLAUDE_SYNC_REPO_URL` — no need to re-set the working
directory unless it differs.

## Everyday use

```bash
~/claude-config/claude-sync.sh sync   # capture local changes onto sync/<hostname>, bring in the default branch, apply, push and open/update its PR — the default
~/claude-config/claude-sync.sh push   # capture onto sync/<hostname>, push it and open/update its PR (no apply)
~/claude-config/claude-sync.sh pull   # bring in the default branch (keeping this machine's unmerged changes) and apply
~/claude-config/claude-sync.sh doctor            # check for a stuck sync (report only)
~/claude-config/claude-sync.sh doctor remediate  # check, and fix what's safely fixable
~/claude-config/claude-sync.sh override list     # this machine's settings overrides (see "Per-machine overrides")
~/claude-config/claude-sync.sh profile list      # this machine's profile (see "Per-machine profiles")
```

`claude-sync.sh` and `install.sh` are shims that run the Python package in
`claude_sync/` (`python3 -m claude_sync` and `python3 -m
claude_sync.install`) from the clone, passing every argument through. Run
the installer with the system Python, not a venv: its pip step installs into
whichever Python runs it. `install.sh --project-path /absolute/path` skips
the project path prompt. `CLAUDE_SYNC_IMPL=bash` runs the old bash scripts,
kept in `legacy/` until the port is finished. What differs from the bash,
and why, is in `docs/python-port.md`.

`push`/`sync`/`bootstrap` skip (without committing anything) if this clone
isn't currently on its default branch — e.g. if you're using this same
clone to develop the sync tool itself on a feature branch, the
`SessionStart`/`SessionEnd` hooks that call `sync` every session won't
commit your local config onto it. They print `sync SKIPPED` and the reason
on stderr and exit 0, so the hooks don't report a failure.

If a machine's sync state itself is broken — wrong project path, a corrupted
clone, or you just want to start that machine over — `doctor` won't help
(it fixes specific known failure modes without discarding anything; it has
no notion of "the setup itself is wrong"). Use repair instead:

```bash
~/claude-config/install.sh --repair   # alias: --reinstall
```

This resets *this machine's own sync bookkeeping* — deletes the local
`~/claude-config` clone, rewrites this machine's
`machines/<hostname>.conf` from scratch (the new file reaches the shared
repo through this machine's sync PR like any other change; nothing is
pushed to the default branch), and removes the skill-discovery
symlinks this tool created — then runs through the normal first-time flow
again (re-clone, re-prompt for the repo URL and project path, bootstrap). It
does **not** touch your real Claude Code config (`~/.claude/CLAUDE.md`,
`settings.json`, `~/.claude/agents/`, `~/.claude/hooks/`,
`~/.agents/skills/`, memory) — only the tool's own state gets reset; your
actual config gets re-adopted fresh by the bootstrap that follows. Refuses
to run while the local clone has changes that aren't on its default branch,
so it can't silently discard something you meant to keep. The usual cause is
this machine's pending sync PR, whose changes the clone holds: merge (or
drop) that PR and `sync` once first. Don't `git stash` or `git reset --hard`
the clone to get past it. Commits left on the default branch by an older
`claude-sync.sh` are refused too: `sync` moves them onto `sync/<hostname>`
as a PR.

Machine profiles (`machines/<profile>/`) and settings overrides
(`machines/<name>.settings.json`) are shared repo content, so repair keeps
them. It only rewrites the `.conf`, so if this machine followed another
profile (a `PROFILE=` line), give it again: `install.sh --repair --profile
<name>`.

**Note:** `install.sh` has no built-in repo URL fallback — `--repair` needs
one to re-clone from, same as a first-time install. Run it from a terminal
so it can prompt you, or set `CLAUDE_SYNC_REPO_URL=<your repo>` beforehand
(e.g. for non-interactive/scripted use). Without either, it exits before
touching anything rather than repairing.

If a machine's `enabledPlugins`/settings never seem to catch up with what's
on your default branch, first check its sync PR is merged: until then, other
machines don't have its changes. If its own sync keeps failing with "a real
conflict", its pending changes and a merged change edited the same lines:
the message says how to resolve or drop them. `doctor` also detects a stuck
rebase, a clone left with commits on the default branch by an older
`claude-sync.sh` (the next sync moves them onto `sync/<hostname>` as a
PR), or local `settings.json` merely being stale, and `doctor remediate`
fixes what it safely can. A genuine content conflict is reported for manual
resolution rather than guessed at — nothing gets silently discarded.

## How changes reach other machines

Nothing is ever pushed to the default branch. Each machine has one branch,
`sync/<hostname>`, holding everything it has captured that isn't merged
yet, as a single commit on top of the default branch. Every `sync`
rebuilds that commit, force-pushes the branch (with a lease, and only that
branch) and opens a PR for it with `gh` if none is open. Later syncs from
the same machine update the same PR. Nothing auto-merges: once you merge
the PR, every other machine picks the change up on its next `sync` or
`pull`, and the machine that made it drops its branch on its next sync.

While a PR is pending, the clone stays on the default branch, but its
working tree shows this machine's pending state, so `git status` in the
clone lists the pending changes as unstaged or untracked. That's expected;
nothing is staged, so a commit made by hand doesn't pick them up. Apply, `doctor`, profiles and overrides all use that state, so a
machine keeps its own unmerged edits; and its own open PR never makes a
sync refuse to capture. Hand edits in the clone are committed to the sync
branch on the next run. Don't `git stash`, `git reset --hard` or
`git commit` in the clone while a PR is pending, or the next sync may
push a PR that drops files (such as a new `machines/<hostname>.conf`).

`sync`, `push`, `bootstrap`, `override add|remove` and `profile use|copy`
need the GitHub CLI, logged in (`gh auth login`). The owner/repo comes from
the `origin` URL; set `CLAUDE_SYNC_GH_REPO=owner/repo` if it can't be
worked out, and `CLAUDE_SYNC_GH=<path>` to use a `gh` that isn't on
`PATH`. Without `gh`, the branch is still pushed and the live config
still applied, but the command exits non-zero and prints the
`gh pr create` command to run by hand. That repeats on every sync until a
PR is open; if you open it by hand, the message also prints the
`git config claude-sync.prVerified <sha>` command that silences it.
`install.sh` prints a hint when `gh` is missing or not logged in.

It needs git 2.40 or newer and refuses clearly on an older one. Two runs at
once (one session's `SessionEnd` and another's `SessionStart`) are kept
apart by an OS lock on a file in the clone's `.git`, which frees itself when
a run dies. A run waits `CLAUDE_SYNC_LOCK_WAIT` seconds (default 10; it must
be a number of seconds, 0 or more), then fails. `CLAUDE_SYNC_FLOCK` is gone:
the Python takes the lock itself, on Linux, macOS and Windows alike.

If GitHub keeps a merged PR's branch, the machine ignores that stale
branch: it remembers the tip it last pushed, and a fresh clone asks `gh`
whether a merged or closed PR has exactly that tip. The next push replaces
the stale branch and opens a new PR.

Every failure (a refusal, a git or gh error, a rejected push, a conflict
with a merged change) prints what failed and what to do on stderr and
exits non-zero. The `SessionStart`/`SessionEnd` hooks log that output to
`~/.claude/claude-config-sync.log` and show "claude-config sync failed"
without breaking the session.

To drop a machine's pending changes instead of merging them: close the
PR, delete its `sync/<hostname>` branch on GitHub, then on that machine run
`git -C ~/claude-config branch -D sync/<hostname> && git -C ~/claude-config reset -q --hard && ~/claude-config/claude-sync.sh pull`.

## What's synced

| Repo path | Local path |
|---|---|
| `dot-claude/CLAUDE.md` | `~/.claude/CLAUDE.md` |
| `dot-claude/settings.json` | `~/.claude/settings.json` |
| `dot-claude/agents/` | `~/.claude/agents/` |
| `dot-claude/hooks/` | `~/.claude/hooks/` |
| `dot-agents/skills/` | `~/.agents/skills/` |
| `memory/` | this machine's primary-project memory folder |

Hook scripts run automatically — they're wired up via `settings.json`
(already synced), so a merged change to `dot-claude/hooks/` takes effect on
every machine's next `sync`/`pull`. Same trust model `settings.json` and
plugin installs already imply; review hook-script changes accordingly.

**Never synced:** `.credentials.json`, `~/.claude.json`, plugin caches, or
any session/task/security state — see
`docs/superpowers/specs/2026-07-25-claude-config-sync-design.md` for the
full list and rationale.

**Agents:** the subagents in `dot-claude/agents/`, what each is for, which
model it runs on and the rules they follow are in `docs/agents.md`. Agents
tied to one machine live in `machines/<profile>/agents/` and are never
published.

## Per-machine overrides

`dot-claude/settings.json` is shared by every machine. To keep a setting on
just one machine (a plugin only one box uses, a different theme, a hook that
only makes sense there), put it in `machines/<hostname>.settings.json`:

```json
{
  "enabledPlugins": { "dm@neural-initiative": true },
  "extraKnownMarketplaces": {
    "neural-initiative": { "source": { "source": "github", "repo": "neuralinitiative/claude-dnd-skill" } }
  }
}
```

On that machine, `~/.claude/settings.json` becomes the shared file with the
override deep-merged on top: objects merge key by key, arrays and plain
values replace, and `null` removes a key. Other machines never see it.

Every key the override file mentions belongs to that machine. If you change
one locally (e.g. toggle that plugin with `/plugin`), the change is written
back into the override file, not the shared file. Anything else you change
is shared as before.

The easy way to manage overrides is with jq-style paths, run on the machine
in question:

```bash
~/claude-config/claude-sync.sh override list
~/claude-config/claude-sync.sh override add '.enabledPlugins["dm@neural-initiative"]'   # make machine-only; other machines drop it once the PR is merged
~/claude-config/claude-sync.sh override remove '.enabledPlugins["dm@neural-initiative"]'  # follow the shared settings again
```

Editing the file by hand in the clone works too; the next `sync` picks it
up, including while this machine's sync PR is pending (the clone's working
tree shows the pending version). Override
files live in `machines/`, so they're never published to the public mirror.

## Per-machine profiles

Overrides cover `settings.json`. A *profile* does the same for agents,
hooks, skills and `CLAUDE.md`: one machine can replace, add or hide entries
without changing the shared files.

```
machines/<profile>/agents/<name>.md         replaces or adds ~/.claude/agents/<name>.md
machines/<profile>/hooks/<name>.sh          replaces or adds ~/.claude/hooks/<name>.sh (keep it executable)
machines/<profile>/skills/<name>/           replaces or adds ~/.agents/skills/<name>/ (the whole directory)
machines/<profile>/<kind>/<name>.disabled   hides the shared <name> on this machine (an empty file)
machines/<profile>/CLAUDE.append.md         appended to ~/.claude/CLAUDE.md
```

The profile name is the hostname, unless `machines/<hostname>.conf` has a
`PROFILE=<name>` line: then the machine follows that profile instead, and
uses `machines/<name>.settings.json` as its settings override. An entry the
shared config doesn't have is simply machine-only.

On apply, the shared files go into place first and the profile is layered
on top: entries are copied over, disabled ones are removed, and
`CLAUDE.append.md` is added after a `<!-- claude-sync:profile-append -->`
line. On capture, everything the profile mentions stays out of the shared
copy, and the append block is stripped from `CLAUDE.md`. If you edit a
profile entry locally after it was applied, the edit goes back into the
profile, not the shared copy. A profile entry that hasn't been applied yet
(just pulled, or on a freshly restored machine) is never overwritten by
whatever happens to be on disk. Take an entry out of the profile and the
machine goes back to the shared version on its next sync.

```bash
~/claude-config/claude-sync.sh profile list              # this machine's profile and everything in it
~/claude-config/claude-sync.sh profile show other-host   # another profile
~/claude-config/claude-sync.sh profile use other-host    # follow machines/other-host/ (sets PROFILE= in this machine's conf)
~/claude-config/claude-sync.sh profile copy other-host   # snapshot it, and its settings override, into this machine's own profile
```

`profile copy` lists the executable hook scripts it will copy and asks
first (`--yes` skips the question). It refuses to replace a profile this
machine already has unless you add `--force`. `profile use` and `profile
copy` go out through the sync PR like `override add`. `doctor` reports a `.disabled`
marker that hides nothing, live files that differ from the shared config
plus the profile, hook commands in `settings.json` that point at a
script that doesn't exist (e.g. a hook you disabled that is still wired up),
and hook scripts committed without the exec bit. Sync sets that bit on new
hooks itself, so a hook added on Windows still runs on Linux.

**Restoring on new infrastructure:** a rebuilt machine with the same
hostname just works, since its conf and profile are in the repo. With a
different hostname, run `install.sh --profile <name>` (or set
`CLAUDE_SYNC_PROFILE=<name>`) to follow the old profile, or `profile use
<name>` later. For an independent copy that can drift from the original,
use `profile copy <name>` instead.

Profiles live in `machines/`, so they're never published to the public
mirror.

## My plugins and skills (opinionated — take or leave)

`dot-claude/settings.json`'s `enabledPlugins` and `dot-agents/skills/` ship
with what I personally use day to day. This is one person's picks, not a
recommended baseline — prune, replace, or ignore it entirely.

Plugins (all from `claude-plugins-official` unless noted):

- `frontend-design` — production-grade UI aesthetics
- `code-review` — multi-agent PR review with confidence scoring
- `commit-commands` — git commit/push/PR workflows
- `security-guidance` — warnings when editing sensitive files
- `context7` — live, version-specific library docs
- `docker` — image/container/Compose workflows
- `superpowers` (`superpowers-marketplace`) — brainstorm → plan → implement (TDD)
- `deployment-engineer` (`awesome-claude-code-plugins`) — CI/CD, Docker, cloud/K8s deploys
- `crowdsec` (`crowdsecurity`) — CrowdSec install/config/ops
- `andrej-karpathy-skills` (`karpathy-skills`) — behavioral guidelines against overcomplication
- `i-have-adhd` (`i-have-adhd`) — ADHD-friendly task/focus workflow
- `claude-code-wakatime` (`wakatime`) — coding-time tracking with WakaTime

Skills in `dot-agents/skills/` (`find-skills`,
`usage-audit`) — a few example personal skills as a starting point; prune or
replace with your own.

Agents in `dot-claude/agents/` — example subagents, each on the cheapest model
that does the job. Haiku for mechanical checks: `search` (repo lookups),
`audit` (diff review and test runs) and `pr-shepherd` (branch to PR, then CI
state), and `config-sync-checker` (flags files a mirror apply would delete).
Sonnet where judgement is needed: `ci-debugger`, `renovate-triage`,
`shell-reviewer`, `docs-sync` (checks a repo's docs agree with each other and
the code) and `research` (sourced web and GET-only API lookups). Opus for
`script-writer`. They are read-only unless stated. `ci-debugger` and
`pr-shepherd` read `WOODPECKER_SERVER` and `WOODPECKER_TOKEN` from your
environment. Anything tied to your own setup belongs in a machine profile
under `machines/<profile>/agents/`, which is never published. See
`docs/agents.md` for the full reference.

For a broader look at what's out there beyond my picks, browse
[claudedirectory.org](https://www.claudedirectory.org) and the official
Claude Code plugin/skill listings.

## Companion CLI tools (npm, not a Claude Code plugin)

Some tools support a Claude Code workflow without being installed through the
plugin marketplace, so `settings.json`'s `enabledPlugins` has no way to
represent them, and `claude-sync.sh` has no way to carry them via git. These
are provisioned imperatively instead, by the installer (declared in
`NPM_TOOLS` in `claude_sync/tools.py`, and at the top of `npm-tools.sh` for
the legacy bash installer). Edit that list for your own tools; it
ships with one example:

- [`ccusage`](https://www.npmjs.com/package/ccusage) — Claude Code usage/cost
  analysis from local session logs.

`install.sh` runs this npm step on every invocation, not just first-time
setup — `npm install -g <pkg>@latest` (unpinned, so always the latest
release) installs if missing, upgrades if
outdated, and no-ops if already current, so re-running `install.sh` on an
already-set-up machine doubles as an update check. If `npm` isn't available
on a machine, this step is skipped with a message rather than failing the
rest of the install.

## Python dependencies for the agent-memory plugin

The agent-memory plugin arrives via claude.ai plugin sync, so its
dependencies can't be carried here either. `install.sh` runs its Python
step (`python-tools.sh` in the legacy bash installer) on every invocation,
which:

- installs `python-is-python3` via apt if there's no `python` command (the
  plugin's MCP server and hooks run `python`, and Ubuntu only ships
  `python3`);
- runs `pip install --break-system-packages 'mcp<2' aiosqlite pyyaml` (the
  plugin's server uses the mcp 1.x API, which mcp 2.x removed) with the
  Python that runs the installer, as `python -I -m pip`. These are version
  ranges, not hash-checked. On Windows the flag is left out, and if pip
  refuses the install it retries once with `--user`; the
  `python-is-python3` step only says what to do.

Either step is skipped with a message if apt-get or python3 is missing.

## Memory scoping

Claude Code keys its auto-memory storage off the exact working directory
path (`~/.claude/projects/<encoded-cwd>/memory/`), which differs machine to
machine. Each machine records its primary project path in
`machines/<hostname>.conf`, and that's the memory folder treated as
"global" for sync purposes — see the design spec's "Known limitation" for
what happens if project-specific memory entries start accumulating there.

On Windows the project path is stored in its native form (`Z:\projects`); an older `machines/<hostname>.conf` with the Git Bash form (`/z/projects`) is still read and converted. If the encoded memory folder name is over 200 characters, Claude Code shortens it with a hash that claude-sync does not reproduce, so sync warns and `doctor` reports an issue. Synced files under `dot-claude/`, `dot-agents/` and `memory/` keep their exact bytes (`.gitattributes` unsets `text` there).

## Design

See `docs/superpowers/specs/2026-07-25-claude-config-sync-design.md` for
the full design rationale, and `docs/superpowers/plans/2026-07-25-claude-config-sync.md`
for the implementation plan.

## Getting updates to this tool

This repo is a published mirror of the tool code (`install.sh`,
`claude-sync.sh`, `claude_sync/`, hooks, `lib/`, `tests/`) from its maintainer's private
sync repo, with personal and private directories left out. It isn't
pushed to directly. To pick up updates: `git pull` this repo's default
branch into your own clone, then re-run `install.sh`/`claude-sync.sh sync`
to apply.
