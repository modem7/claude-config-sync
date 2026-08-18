# Claude Code config sync — design

## Goal

Keep Claude Code setup (global instructions, settings, personal skills, and
"global-flavored" auto-memory) consistent across every machine the user runs
`claude` on, via a private GitHub repo that each machine can both contribute
to and pull updates from.

## Non-goals

- **claude.ai account-level settings** (personalization/custom preferences in
  the web/desktop app). These live server-side on Anthropic's account with no
  file or API access available to Claude Code — out of scope. If useful, the
  synced `CLAUDE.md` can carry equivalent behavioral guidance instead.
- Syncing plugin-managed or built-in skill directories
  (`~/.claude/skills/find-skills`, `~/.claude/skills/webapp-testing`) —
  these reinstall from marketplaces per `settings.json` and are not
  hand-authored content.
- Syncing `~/.claude.json` or `~/.claude/.credentials.json` — machine/account
  identity and auth secrets, never tracked.

## Repository

- Private GitHub repo: [github.com/modem7/claude-config](https://github.com/modem7/claude-config)
  — already exists (created via the user's normal new-repo flow: MIT
  `LICENSE`, placeholder `README.md`, default branch `master`).
- Cloned to `~/claude-config` on every machine (sibling to `~/.claude` and
  `~/.agents`, not nested under `/home/modem7/project` — this is personal
  dotfiles-style config, not a project).
- Bootstrapped from the user's own [`DefaultRepo`](https://github.com/modem7/DefaultRepo)
  template (`.editorconfig`, `.gitattributes`, `.gitignore`, `CONTRIBUTING.md`,
  `.github/settings.yml`, `CODEOWNERS`, `PULL_REQUEST_TEMPLATE.md`,
  `ISSUE_TEMPLATE/`, `autoassign.yml`) for consistency with the user's other
  repos, and from [`renovate-config`](https://github.com/modem7/renovate-config)
  for dependency updates. See "Repo bootstrap" below for specifics and
  deviations.

## Layout

```
claude-config/
├── README.md
├── LICENSE                        # already present, MIT, correct — no change needed
├── CONTRIBUTING.md
├── .editorconfig
├── .gitattributes
├── .gitignore
├── .wakatime-project               # "claude-config"
├── renovate.json
├── install.sh
├── claude-sync.sh
├── .github/
│   ├── settings.yml
│   ├── CODEOWNERS
│   ├── PULL_REQUEST_TEMPLATE.md
│   ├── ISSUE_TEMPLATE/
│   │   ├── bug_report.yml
│   │   ├── feature_request.yml
│   │   └── config.yml
│   └── workflows/
│       ├── autoassign.yml
│       └── ci.yml
├── tests/
│   └── *.bats                      # machine_conf, memory_path, safety, skills_symlink, sync_cli, sync_files
├── machines/
│   └── <hostname>.conf
├── dot-claude/
│   ├── CLAUDE.md
│   ├── settings.json
│   └── agents/
├── dot-agents/
│   └── skills/
└── memory/
    ├── MEMORY.md
    └── *.md
```

**Deliberately dropped from the DefaultRepo template:** `.github/FUNDING.yml`
(Sponsor button is meaningless on a private repo nobody else can see).

## File inventory

| Repo path | Local path | Notes |
|---|---|---|
| `dot-claude/CLAUDE.md` | `~/.claude/CLAUDE.md` | global instructions |
| `dot-claude/settings.json` | `~/.claude/settings.json` | verified free of secrets |
| `dot-claude/agents/` | `~/.claude/agents/` | currently empty; ready for future reusable subagent `.md` files |
| `dot-agents/skills/` | `~/.agents/skills/` | personal skill library (e.g. `release`) |
| `memory/*` | resolved per-machine memory dir (see below) | "global" auto-memory content |

**Never touched:** `.credentials.json`, `~/.claude.json`, plugin caches,
`sessions/`, `session-env/`, `shell-snapshots/`, `security/`, `tasks/`,
`teams/`, `remote/`, `~/.claude/skills/find-skills`,
`~/.claude/skills/webapp-testing`.

## Memory resolution

Claude Code stores auto-memory under
`~/.claude/projects/<encoded-cwd>/memory/`, where `<encoded-cwd>` is the
working directory path with `/` replaced by `-` (confirmed:
`/home/modem7/project` → `-home-modem7-project`). Since the working
directory differs per machine, a per-machine config file records which local
project maps to the synced "global" memory:

```
machines/<hostname>.conf:
PRIMARY_PROJECT_PATH=/home/modem7/project
```

`claude-sync.sh` reads this, derives the encoded folder name, and treats
that folder's `memory/` as the sync target.

**Known limitation:** the auto-memory system mixes globally-applicable
entries (feedback/reference-type, e.g. "no Co-Authored-By trailers") with
entries that could in principle be project-specific. Today's `MEMORY.md`
content is entirely global-flavored (explicitly marked "applies globally"),
so wholesale syncing is safe. If project-specific memory entries start
accumulating in the primary project's memory folder, they will also sync to
other machines as if global — this is an accepted tradeoff for now, not
solved by this design. Revisit if it becomes a problem.

## Repo bootstrap (from DefaultRepo / renovate-config)

Files copied verbatim from `DefaultRepo`: `.editorconfig`, `.gitattributes`,
`CONTRIBUTING.md`, `.github/CODEOWNERS`, `.github/PULL_REQUEST_TEMPLATE.md`,
`.github/ISSUE_TEMPLATE/*`, `.github/workflows/autoassign.yml`.

Files copied with adjustments:

- **`LICENSE`** — already present on the remote repo, MIT, copyright year
  2026 (correct at creation time). No change needed.
- **`.wakatime-project`** — set to `claude-config` (template default is the
  placeholder `ChangeMe`).
- **`.gitignore`** — DefaultRepo's version (OS/editor/secrets/build-artifact
  patterns, including a `.claude/` entry) plus explicit denylist entries for
  `.credentials.json` and `.claude.json`, which aren't covered by the
  generic secret patterns (`*.pem`, `*.key`, `.env*`, etc.). Using
  `dot-claude/` and `dot-agents/` (not `.claude/`/`.agents/`) as the
  in-repo directory names is deliberate — it avoids collision with
  DefaultRepo's blanket `.claude/` ignore rule, which would otherwise
  silently exclude the very content this repo exists to track.
- **`.github/settings.yml`** — adapted from the template:
  `description` set to describe this repo's purpose, `topics` changed to
  something like `claude-code, dotfiles, config-sync`, **`private: true`**
  (override — most of the user's repos are public, this one isn't),
  `default_branch: master` (unchanged, matches what already exists). Merge
  settings, label list, and security-alert settings carried over unchanged.
- **`renovate.json`** — uses the shared preset rather than duplicating
  settings:
  ```json
  {
    "$schema": "https://docs.renovatebot.com/renovate-schema.json",
    "extends": ["github>modem7/renovate-config:shared"]
  }
  ```
  This matches `renovate-config/default.json`'s pattern (the newer,
  DRY convention) rather than `DefaultRepo/renovate.json`'s standalone
  `config:recommended`. The shared preset's automerge rule for
  `matchManagers: [github-actions]` on minor/patch/digest updates will
  apply to `ci.yml`'s and `autoassign.yml`'s pinned action versions.
- **`README.md`** — replaces the current one-line placeholder with real
  documentation: purpose, repo layout, how `install.sh`/`claude-sync.sh`
  work, and the memory-scoping limitation called out above.

## Skill discovery wiring

Claude Code discovers skills under `~/.claude/skills/`, but only
`~/.agents/skills/` content is git-tracked (it's the clearly user-owned
library). `install.sh` recreates a symlink
`~/.claude/skills/<name>` → `~/.agents/skills/<name>` for each directory
under `dot-agents/skills/`, mirroring the pattern already in manual use for
the `release` skill.

## Sync mechanism (Approach B: copy-based push/pull)

Chosen over live symlinks because Claude Code (or other tools) may write
config files via write-temp-then-rename, which would silently replace a
symlink with a plain file and break the link without warning. Copying is
explicit and robust to that.

`claude-sync.sh` supports `push`, `pull`, and `sync` (default, does both in
a safe order):

1. **Capture**: copy each real file/dir into its repo location (per the file
   inventory table), using the machine's resolved memory dir.
2. **Commit**: `git add -A`; commit only if there's a diff, message includes
   hostname + timestamp.
3. **Integrate**: `git pull --rebase`. On conflict: abort the rebase
   automatically (`git rebase --abort`) and print a message telling the user
   to resolve manually — no automatic conflict resolution, to avoid
   corrupting `settings.json` or memory files.
4. **Push**: `git push`.
5. **Apply**: copy repo content back out to the real local paths, so this
   machine ends up with the fully merged state (including anything pulled
   from other machines), and re-run the skill-symlink wiring step.

`install.sh` (first run on a new machine): clones the existing
`github.com/modem7/claude-config` repo to `~/claude-config`, prompts for
`PRIMARY_PROJECT_PATH` if `machines/<hostname>.conf` doesn't exist, writes
it, then runs the same full Capture → Commit → Integrate → Push → Apply
sequence as `claude-sync.sh sync`. This matters even though the repo already
has bootstrap/template content: on this first real-content machine, Capture
picks up this machine's existing `CLAUDE.md`/`settings.json`/memory and
seeds the repo with it rather than the (still mostly-template) repo
silently overwriting local config with nothing. On subsequent machines,
Capture picks up whatever pre-existing local content they have, Integrate
merges in what's already in the repo, and Apply leaves the machine with the
reconciled result.

## CI & safety checks

`.gitignore`: DefaultRepo's standard patterns (OS/editor cruft, secrets,
build artifacts — see "Repo bootstrap" above), plus explicit denylist
entries for `.credentials.json` and `.claude.json` even though the script
itself never copies these in.

`.github/workflows/ci.yml`, on every push/PR:

1. **shellcheck** on `install.sh` and `claude-sync.sh`.
2. **`jq empty`** on `dot-claude/settings.json` — valid-JSON check.
3. **gitleaks** secret scan over the full diff — backstop against an
   AI-driven push accidentally committing a credential.
4. **bats-core** (`tests/*.bats`: `machine_conf`, `memory_path`, `safety`,
   `skills_symlink`, `sync_cli`, `sync_files`): runs `claude-sync.sh push`/`pull`
   against a fake `$HOME` under a temp directory (never the real one),
   asserting:
   - files land in the correct relative locations after `push`/`pull`
   - `machines/<hostname>.conf` round-trips correctly
   - none of the denylisted filenames ever appear in the repo tree after a
     push

## Error handling

- No `machines/<hostname>.conf` and not running `install.sh`: exit with a
  clear message pointing at `install.sh`.
- No `~/claude-config` clone yet: `claude-sync.sh` exits with a message to
  run `install.sh` first.
- Rebase conflict: abort automatically, exit non-zero, tell the user which
  files conflicted and to resolve manually before re-running.

## Out of scope for this design

- Automatic/scheduled syncing (e.g. cron, git hooks) — manual invocation
  only for now.
- Multi-user or team sharing of this repo — single-user, private.
- Encrypting repo contents at rest — repo is private and contains no
  secrets by design (enforced by the CI gitleaks gate).
- A Claude Code **plugin** wrapping `claude-sync.sh` in a slash command
  (e.g. `/claude-config:sync`). Considered and deferred: a plugin only runs
  once Claude Code is already up, so it can't bootstrap `settings.json`/
  `CLAUDE.md` onto a fresh machine — the repo+script remains the actual
  mechanism regardless. Worth revisiting as a convenience layer once the
  core sync is working.

## Addendum (2026-07-30): hook scripts added to sync

`settings.json` (already synced) references hook scripts under
`~/.claude/hooks/*.sh` by absolute path, but this design's original file
inventory never actually included that directory — an oversight from when
those hooks were added later (PR #6), not a deliberate exclusion. Net
effect: a fresh machine bootstrapped from this repo would get a
`settings.json` pointing at hook scripts that don't exist on disk.

Fixed by adding `dot-claude/hooks/` ↔ `~/.claude/hooks/` to
`sync_capture`/`sync_apply` in `lib/sync-files.sh`, same rsync-mirror
pattern already used for `dot-claude/agents/`. Unlike the other synced
paths, hook scripts execute automatically once merged and applied (that's
their entire purpose) — this is the same trust model `settings.json` and
plugin installs already imply, just worth being explicit about: a hook
script change lands and takes effect on every machine's next `sync`/`pull`,
not just this one.

Not extended in this pass: `.github/workflows/ci.yml`'s shellcheck step
still only covers `install.sh claude-sync.sh npm-tools.sh gh-tools.sh
lib/*.sh`, not `dot-claude/hooks/*.sh` — the existing hook scripts have a
couple of pre-existing style-level shellcheck findings (`SC2034` in
`api-error-alert.sh`, `SC2001` x2 in `context-monitor.sh`) that would need
fixing first to avoid breaking CI. Left as a follow-up rather than folded
into this change.
