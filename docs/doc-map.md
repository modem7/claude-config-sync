# Doc map

Which doc to update for each kind of change. Read it before opening a PR, then run the `docs-sync` agent on the branch to check nothing was missed.

## The docs

| Doc | Role |
|---|---|
| `README.md` | Full README for this repo: what the tool does, usage, synced paths, profiles, plugins. |
| `README.public.md` | README for the public template, published as its `README.md`. Shared sections stay identical to `README.md`; differences are only for private or personal content. |
| `CLAUDE.md` | Rules for Claude working in this repo. |
| `AGENTS.md` | Pointer to `CLAUDE.md` for other coding agents. |
| `CONTRIBUTING.md`, `.github/PULL_REQUEST_TEMPLATE.md` | Contributor process. |
| `docs/risks.md` | Failure modes, each with its guard and test. |
| `docs/runbook.md` | Step-by-step procedures. |
| `docs/agents.md` | Reference for the shared agents. |
| `docs/superpowers/` | Dated design specs and plans. A record: add a note if superseded, do not rewrite. |
| `dot-claude/CLAUDE.md` | Global instructions synced to every machine. |
| `machines/<profile>/NOTES.md` | Private notes for one machine profile. Never published. |
| `private/` | Repo-wide private notes. Never published. |
| `weekly-review/README.md` | Private weekly review job. Never published. |

## Change to docs

| When you | Update |
|---|---|
| Add, remove, rename or change a shared agent | `docs/agents.md` (row and note), the agents paragraph in `README.public.md`, the list in `dot-claude/CLAUDE.md`. |
| Add, remove or change a profile agent | The profile's `NOTES.md`. If it overrides a shared agent, diff the two. |
| Add or remove a skill | The skills paragraph in `README.public.md`, the profile `NOTES.md` for profile skills, and the whitelist in `publish-public.sh`. |
| Add or change a command, flag or env var | Usage text in the script, `install.sh` messages, the usage sections of both READMEs, `docs/runbook.md`, `docs/risks.md`, and the tests. |
| Change sync flow, branches, env vars or failure messages | `usage()` in `claude-sync.sh`, `install.sh` messages, both READMEs, `CLAUDE.md` "How it works" and Rules, `docs/runbook.md`, `docs/risks.md`, the tests, and a superseded note on the affected spec. |
| Change what is synced (`lib/sync-files.sh`) | The synced-paths table in both READMEs, `docs/risks.md`, `docs/runbook.md`. |
| Change profiles or overrides | The profile sections of both READMEs, `docs/runbook.md`, "How it works" in `CLAUDE.md`. |
| Change what is published | The "never published" notes in both READMEs, the publish section of `docs/runbook.md`, `CLAUDE.md` rules, `docs/risks.md`. |
| Find a new failure mode or add a guard | A row in `docs/risks.md` with the guard and the test name. |
| Change a global instruction | `dot-claude/CLAUDE.md` and the live `~/.claude/CLAUDE.md`, which must stay identical. |
| Change plugins or `settings.json` | The plugin list in `README.public.md`. |
| Add, move or remove a doc | This map, and "Read first" in `CLAUDE.md` if Claude should read it. |

## Twins that must stay in step

- `README.md` and `README.public.md`.
- `dot-claude/CLAUDE.md` and `~/.claude/CLAUDE.md`.
- A profile agent and the shared agent it overrides.
- Each agent file and its row in `docs/agents.md`.
