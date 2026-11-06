# claude-config-sync

A sync tool for Claude Code's global configuration — `CLAUDE.md`,
`settings.json`, personal skills, hooks, and "global" auto-memory — across
every machine you run `claude` on.

This repo is the *tool* (`install.sh`, `claude-sync.sh`, hooks, lib, tests)
plus a starting-point `dot-claude/CLAUDE.md` and `dot-claude/settings.json`
you're expected to edit to taste. It is **not** meant to be synced from
directly — see "Get your own copy" below.

## Why

Claude Code stores its config under `~/.claude/` and personal skills under
`~/.agents/`. This tool keeps a git repo as the single source of truth those
directories sync from, via `install.sh` (first run on a new machine) and
`claude-sync.sh` (every run after).

## Get your own copy

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
~/claude-config/claude-sync.sh sync   # capture local changes, merge with remote, apply back — the default
~/claude-config/claude-sync.sh push   # capture + commit + push only
~/claude-config/claude-sync.sh pull   # pull + apply only
~/claude-config/claude-sync.sh doctor            # check for a stuck sync (report only)
~/claude-config/claude-sync.sh doctor remediate  # check, and fix what's safely fixable
~/claude-config/claude-sync.sh override list     # this machine's settings overrides (see "Per-machine overrides")
~/claude-config/claude-sync.sh profile list      # this machine's profile (see "Per-machine profiles")
```

`push`/`sync`/`bootstrap` skip (without committing anything) if this clone
isn't currently on its default branch — e.g. if you're using this same
clone to develop the sync tool itself on a feature branch, the
`SessionStart`/`SessionEnd` hooks that call `sync` every session won't
commit your local config onto it.

If a machine's sync state itself is broken — wrong project path, a corrupted
clone, or you just want to start that machine over — `doctor` won't help
(it fixes specific known failure modes without discarding anything; it has
no notion of "the setup itself is wrong"). Use repair instead:

```bash
~/claude-config/install.sh --repair   # alias: --reinstall
```

This resets *this machine's own sync bookkeeping* — deletes the local
`~/claude-config` clone, removes this machine's entry from
`machines/<hostname>.conf` (from the shared repo too, not just locally, so
it doesn't linger for other machines), and removes the skill-discovery
symlinks this tool created — then runs through the normal first-time flow
again (re-clone, re-prompt for the repo URL and project path, bootstrap). It
does **not** touch your real Claude Code config (`~/.claude/CLAUDE.md`,
`settings.json`, `~/.claude/agents/`, `~/.claude/hooks/`,
`~/.agents/skills/`, memory) — only the tool's own state gets reset; your
actual config gets re-adopted fresh by the bootstrap that follows. Refuses
to run if the local clone has uncommitted or untracked changes, so it can't
silently discard something you meant to keep.

Machine profiles (`machines/<profile>/`) and settings overrides
(`machines/<name>.settings.json`) are shared repo content, so repair keeps
them. It only removes the `.conf`, so if this machine followed another
profile (a `PROFILE=` line), give it again: `install.sh --repair --profile
<name>`.

**Note:** `install.sh` has no built-in repo URL fallback — `--repair` needs
one to re-clone from, same as a first-time install. Run it from a terminal
so it can prompt you, or set `CLAUDE_SYNC_REPO_URL=<your repo>` beforehand
(e.g. for non-interactive/scripted use). Without either, it exits before
touching anything rather than repairing.

If a machine's `enabledPlugins`/settings never seem to catch up with what's
on your default branch, it's likely a stuck sync: `sync`'s capture step
commits the local `settings.json` *before* pulling, so if that pull then
conflicts with a change already pushed, it aborts before ever pushing or
applying — and repeats on every future sync until fixed. `doctor` detects
this (a stuck rebase, a diverged branch, or local `settings.json` merely
being stale) and `doctor remediate` retries automatically. A genuine content
conflict (both sides edited the same key) is reported for manual resolution
rather than guessed at — nothing gets silently discarded.

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
~/claude-config/claude-sync.sh override add '.enabledPlugins["dm@neural-initiative"]'   # make machine-only; other machines drop it on their next sync
~/claude-config/claude-sync.sh override remove '.enabledPlugins["dm@neural-initiative"]'  # follow the shared settings again
```

Editing the file by hand works too; the next `sync` picks it up. Override
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
copy` commit and push like `override add`. `doctor` reports a `.disabled`
marker that hides nothing, live files that differ from the shared config
plus the profile, and hook commands in `settings.json` that point at a
script that doesn't exist (e.g. a hook you disabled that is still wired up).

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
- `i-have-adhd` — ADHD-friendly task/focus workflow

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
are provisioned imperatively instead, via `npm-tools.sh` (declared in
`NPM_TOOLS` at the top of that file). Edit that list for your own tools; it
ships with one example:

- [`ccusage`](https://www.npmjs.com/package/ccusage) — Claude Code usage/cost
  analysis from local session logs.

`install.sh` runs `npm-tools.sh` on every invocation, not just first-time
setup — `npm install -g <pkg>@latest` installs if missing, upgrades if
outdated, and no-ops if already current, so re-running `install.sh` on an
already-set-up machine doubles as an update check. If `npm` isn't available
on a machine, this step is skipped with a message rather than failing the
rest of the install.

## Python dependencies for the agent-memory plugin

The agent-memory plugin arrives via claude.ai plugin sync, so its
dependencies can't be carried here either. `install.sh` runs
`python-tools.sh` on every invocation, which:

- installs `python-is-python3` via apt if there's no `python` command (the
  plugin's MCP server and hooks run `python`, and Ubuntu only ships
  `python3`);
- runs `pip install --break-system-packages 'mcp<2' aiosqlite pyyaml` (the
  plugin's server uses the mcp 1.x API, which mcp 2.x removed).

Either step is skipped with a message if apt-get or python3 is missing.

## Memory scoping

Claude Code keys its auto-memory storage off the exact working directory
path (`~/.claude/projects/<encoded-cwd>/memory/`), which differs machine to
machine. Each machine records its primary project path in
`machines/<hostname>.conf`, and that's the memory folder treated as
"global" for sync purposes — see the design spec's "Known limitation" for
what happens if project-specific memory entries start accumulating there.

## Design

See `docs/superpowers/specs/2026-07-25-claude-config-sync-design.md` for
the full design rationale, and `docs/superpowers/plans/2026-07-25-claude-config-sync.md`
for the implementation plan.

## Getting updates to this tool

This repo is a published mirror of the tool code (`install.sh`,
`claude-sync.sh`, hooks, `lib/`, `tests/`) from its maintainer's private
sync repo, with their personal `memory/` and `machines/` left out. It isn't
pushed to directly. To pick up updates: `git pull` this repo's default
branch into your own clone, then re-run `install.sh`/`claude-sync.sh sync`
to apply.
