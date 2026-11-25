# Runbook

Procedures for the sync tool. Paths assume the clone is at `~/claude-config`.

## One-command setup

`get.sh` (Linux, macOS) and `get.ps1` (Windows) do the prerequisites, `gh` login, clone and install in one go. Fetch them from the public mirror, because they run before `gh` exists:

```bash
curl -fsSL https://raw.githubusercontent.com/modem7/claude-config-sync/main/get.sh | bash -s -- OWNER/REPO
```

```powershell
$env:CLAUDE_SYNC_REPO = 'OWNER/REPO'; irm https://raw.githubusercontent.com/modem7/claude-config-sync/main/get.ps1 | iex
```

Flags: `--yes`, `--dry-run`, `--dir`, `--project-path`, `--profile`, `--tier basic|full`, `--skip-python-deps` (PowerShell: `-Repo -Dir -ProjectPath -Profile -Tier -Yes -DryRun -SkipPythonDeps`). `--tier basic` runs only the `gh` step, and the script then skips its own pip check. `--skip-python-deps` only changes the `full` tier: it drops the python step while npm and `gh` still run. Environment: `CLAUDE_SYNC_REPO`, `CLAUDE_SYNC_DIR`. `--yes` does not cover the installer's own project-path question, so pass `--project-path` for a hands-off run. The script never overwrites an existing clone: it uses it as it is, and stops if the folder is not a git clone. A failure says what to do; fix it and re-run, the steps are safe to repeat. `--skip-python-deps` leaves out only the agent-memory plugin's pip packages and the `python` alias; use it when pip is unavailable. `get.ps1` has not been run on real Windows yet (`docs/risks.md`, row 42). The rest of this page is the manual route.

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

`--tier basic|full` (or `CLAUDE_SYNC_TIER`) picks the sync tier; see "Choose or switch a sync tier" below. `--project-path /absolute/path` sets the primary project path without the prompt. On Windows it is stored as `Z:\path`; a Git Bash `/z/path` is converted. If the folder does not exist, the installer asks whether to create it (terminal only); otherwise it falls back to the home folder.

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

Plugin code (`~/.claude/plugins/cache`) is never synced, only `settings.json`, so plugins can show as enabled and not be installed. Run `claude-sync.sh doctor` after the first sync and see "Install plugins that settings.json enables" below.

## Install plugins that settings.json enables

`claude-sync.sh doctor` compares `enabledPlugins` and `extraKnownMarketplaces` (or its alias `additionalMarketplaces`) in the live `~/.claude/settings.json` with `~/.claude/plugins/installed_plugins.json` and `known_marketplaces.json`. For each marketplace declared but not known, and each plugin enabled but not installed, it prints the command to run, marketplaces first:

```bash
claude plugin marketplace add example-org/example-marketplace
claude plugin install example-plugin@example-marketplace
```

- It never runs them. Read each one before you paste it: a plugin runs code on this machine.
- It prints nothing when everything matches, and never changes the exit code (`doctor remediate` does not install either).
- The live file already has this machine's override applied, so a plugin an override turns off is not listed. To keep a plugin off one machine, set it to `false` in `~/.claude/settings.json` there and run `claude-sync.sh override add '.enabledPlugins["example-plugin@example-marketplace"]'`.
- `claude-plugins-official` is added by Claude Code itself on the first interactive session; doctor gives `claude plugin marketplace add anthropics/claude-plugins-official` when it is missing.
- A marketplace whose source the CLI would misread (a `git` URL without `.git` on a host other than GitHub or GitLab, an inline `settings` source) gets a note instead of a command. Claude Code clones declared marketplaces itself after a session starts.
- Plugins enabled as `<name>@synced` come from the claude.ai account and are not checked.
- If either plugin file is not JSON or not in a shape this tool knows (Claude Code can change it between versions), doctor prints one "Plugin check skipped" line and goes on.
- Inside a session, `/plugin marketplace add <source>` and `/plugin install <name>@<marketplace>` do the same.

To have the tool run them for you, opt in with `install-plugins`:

```bash
~/claude-config/claude-sync.sh doctor install-plugins        # shows the commands, asks y/N
~/claude-config/claude-sync.sh doctor install-plugins --yes  # no question; read the list first
```

- It works out the same list as `doctor`, prints the exact commands and the `claude` it found on `PATH`, then asks. Anything but `y` or `yes` stops with nothing run.
- Without a terminal (a hook, a pipe) it refuses unless you pass `--yes`.
- It runs every `marketplace add` first, then every `install`, one at a time, with no shell. At the first failure it stops, lists what succeeded and what was not run, and exits non-zero. Fix the cause and run it again; what is installed by then is no longer listed.
- It skips what `doctor` only notes (a marketplace with no add command, a plugin whose marketplace is not declared), `@synced` plugins, `claudeai-` marketplaces, and any name it could not run exactly as shown (control characters, a leading `-`, or characters the Windows shell would read). Each skip is listed; skips do not change the exit code.
- If `claude` is not on `PATH` it says so and exits non-zero. If a plugin file is in an unknown shape it installs nothing and exits non-zero.
- It does not take the sync lock and does not change the clone. Start a new Claude Code session afterwards to load the plugins.

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

`sync` puts this machine's changes on `sync/<hostname>` and opens a PR for it (it needs `gh`, logged in; `CLAUDE_SYNC_GH=<path>` picks the binary and `CLAUDE_SYNC_GH_REPO=owner/repo` the repo when origin's URL doesn't show it; `CLAUDE_SYNC_LOCK_WAIT=<seconds>` sets how long a second run waits for the lock; `CLAUDE_SYNC_BACKUPS=<n>` how many apply backups are kept, see "Restore a file from an apply backup"; `CLAUDE_SYNC_FLOCK` is gone, see `docs/python-port.md`). Review and merge the PR; every machine picks it up on its next `sync` or `pull`. Later syncs from the same machine update the same PR. If `gh` is missing or not logged in, the branch is still pushed and the error prints the `gh pr create` command to run by hand.

To drop a machine's pending changes instead: close the PR, delete the branch on GitHub, then on that machine:

```bash
git -C ~/claude-config branch -D sync/<hostname>
git -C ~/claude-config reset -q --hard
~/claude-config/claude-sync.sh pull
```

## First sync after rules, commands and keybindings joined the sync

`~/.claude/rules/`, `~/.claude/commands/` and `~/.claude/keybindings.json` are synced, shared by every machine. A machine that has its own copies from before proposes them in its next sync PR. Check that PR: once merged, every machine mirrors the repo's `rules/` and `commands/`, so files only one machine had are removed on the others at their next apply.

A machine without them is fine: capture skips what is missing and the repo copy is kept.

If a sync says "refusing to capture" because the clone moved while this machine had its own rules or commands, copy `~/.claude/rules/`, `~/.claude/commands/` and `~/.claude/keybindings.json` somewhere safe first, as `pull` discards them. Run `pull`, copy the files back, then run `sync` to propose them.

## A sync seems stuck

```bash
~/claude-config/claude-sync.sh doctor
~/claude-config/claude-sync.sh doctor remediate
```

A real content conflict is reported, not guessed at: this machine's pending changes and a merged change edited the same lines. Either merge `origin/<default>` into `sync/<hostname>` in `~/claude-config`, fix the conflict and commit, then run `sync` again; or drop the pending changes as above. Other failures (fetch, push, `gh`, a git older than 2.40, a `git merge-tree` error) name the cause and the fix in their message, and the hooks log it to `~/.claude/claude-config-sync.log`.

"… is a hook script tracked without the exec bit": a hook was committed as `100644`, usually from Windows, so Linux and macOS get it non-executable. On Windows run `git -C ~/claude-config update-index --chmod=+x -- <path>`; on Linux or macOS run `chmod +x` on the file in `~/claude-config` and on its live copy under `~/.claude/hooks/`, if there is one. Then run `claude-sync.sh sync` and merge its PR.

"another claude-sync run is still working": another session's sync holds the lock. Wait and re-run. The lock frees itself when that run exits. `CLAUDE_SYNC_LOCK_WAIT=<seconds>` waits longer; it must be a number of seconds, 0 or more.

"the last applied commit ... is no longer in": the stamp in `~/.claude/.claude-sync-applied-commit` points at a commit the clone lost (re-cloned or garbage-collected; it is pinned as `refs/claude-sync/applied` now). Either `pull` (discards local edits), or copy your edits aside, delete the stamp and run `sync`.

"warning: the project path … may not be the folder Claude Code uses": the memory folder name for `PRIMARY_PROJECT_PATH` in `machines/<hostname>.conf` is over 200 characters. Claude Code shortens such names and adds a hash, so the synced `memory/` may come from a folder it no longer writes. Point the conf at a shorter project path (or `install.sh --repair --project-path <shorter>`) and check the next sync PR's `memory/` diff.

"Unable to create '….lock': File exists" (for example `.git/index.lock` or `.git/refs/remotes/origin/<branch>.lock`): a run stopped while git was writing left git's own lock behind. Since a SIGTERM, SIGHUP or Ctrl-C now lets git finish first (`docs/risks.md` row 35), this is mostly after a power loss, a SIGKILL, or the Windows hook timeout. Once no `claude-sync` and no other git command is running in the clone, delete that `.lock` file and re-run.

"… is a link or not a folder, so apply cannot keep backups there": `~/.claude/.claude-sync-backups` is a link, a junction or a file. Nothing was changed. Move it out of the way so the next apply can make a real folder (or set `CLAUDE_SYNC_BACKUPS=0` to make no backups), then re-run.

"warning: N backup folders in …, more than CLAUDE_SYNC_BACKUPS=…": pruning could not remove an old backup folder (usually a file in use on Windows). Close what holds it and delete the oldest folders by hand; the apply itself succeeded.

"sync SKIPPED": the clone is on a branch other than its default branch, so nothing was captured. Switch it back (`git -C ~/claude-config switch <default>`), or develop in a separate worktree.

A merged PR whose branch GitHub kept is ignored: the machine remembers the tip it last pushed, and a fresh clone checks with `gh` for a merged or closed PR at that tip. The next change replaces that branch and opens a new PR.

## A session starts with a `claude-config:` line

The third `SessionStart` hook runs `claude-sync.sh status --line --json`, which prints something only when this machine needs attention; a healthy machine prints nothing. Plain `SessionStart` stdout is added to the session's context for Claude only, so the hook prints one JSON object instead: `systemMessage` shows the line to you and `hookSpecificOutput.additionalContext` gives the same line to Claude. This was checked against the Claude Code hooks docs (https://code.claude.com/docs/en/hooks). Run `claude-sync.sh status --line` by hand for the plain line. It never fetches, never calls `gh`, never takes the lock and always exits 0, so it is safe beside a running sync. A session's hooks run side by side, so the line describes the state the previous run left, not this session's own sync (whose result shows as "Synced config loaded." or "claude-config sync failed").

What the line can say, most urgent first (with "N more" when there is more than one):

- "the last claude-config sync (SessionEnd) failed with exit N": read the end of `~/.claude/claude-config-sync.log`. Each hook run ends its entry with `claude-sync-hook: <event> exit=<code> <UTC time>`; exit 124 means the 20 s `timeout` stopped it. Fix the cause and run `claude-sync.sh sync`. The line goes when a later hook sync ends with `exit=0`. A sync you run by hand writes no result line.
- "'claude-sync.sh doctor' reports a problem: …": the offline part of `doctor` (a stuck rebase, a hook that `settings.json` runs but that does not exist, a hook tracked without the exec bit, a too-long memory folder name). Run `doctor` for the details and the fix; see "A sync seems stuck". Drift between live files and the repo is left to `doctor`, since it can show mid-sync.
- "this machine's changes on sync/<hostname> have waited over N days for their PR": merge or close the PR on GitHub, then run `sync`. The age is that of the local branch (its oldest reflog entry), so rebuilding it on a newer default branch does not reset it. `CLAUDE_SYNC_PR_DAYS=<days>` sets N (default 3, a whole number; 0 turns this check off). If the PR is already merged, the next sync deletes the branch and the line goes.
- "the sync tier changed to basic" or "… to full": the `TIER=` line changed and the next sync switches (see "Choose or switch a sync tier"). After a switch to full, check the `memory/` diff of the next sync PR.
- "the status check failed: …" or "no git repo at …": the check itself could not run (for example a bad `CLAUDE_SYNC_PR_DAYS`). Fix what it names.

To turn the line off on one machine, delete that hook from the live `~/.claude/settings.json`, then run `claude-sync.sh override add '.hooks.SessionStart'`. That keeps this machine's whole `SessionStart` list as it is now, so later shared changes to that list no longer reach this machine.

## Restore a file from an apply backup

Before `sync`, `pull`, `bootstrap` or `doctor remediate` overwrites or deletes a local file whose bytes differ from what the apply ends up with, it copies the old file to `~/.claude/.claude-sync-backups/<UTC time>/` and prints `the old copies are in <folder>`. Paths inside a folder follow `~/.claude` (`CLAUDE.md`, `agents/x.md`, `projects/<encoded-path>/memory/x.md`); files from `~/.agents` go under `.agents/` (`.agents/skills/<name>/SKILL.md`). Folder names are UTC times (`20260101T120000.123456Z`), so the last one listed is the newest; `doctor` names it when it finds drift. Links are not backed up (removing a link loses no data).

The newest 10 folders are kept, pruned after each successful apply. `CLAUDE_SYNC_BACKUPS=N` keeps N; `0` makes no new ones and leaves existing ones alone. A bad value (not a whole number) stops the command before anything changes. The folder is local only, mode 0700 on Linux and macOS, and is never captured, committed or published, but it can hold copies of secrets that were in synced files: delete it when you no longer need it.

Copy a file back, then run `sync` if you want the edit shared (the next capture proposes it in this machine's PR):

```bash
ls ~/.claude/.claude-sync-backups/
cp -p ~/.claude/.claude-sync-backups/<time>/agents/x.md ~/.claude/agents/x.md
```

PowerShell:

```powershell
Get-ChildItem "$HOME\.claude\.claude-sync-backups" | Sort-Object Name
Copy-Item "$HOME\.claude\.claude-sync-backups\<time>\agents\x.md" "$HOME\.claude\agents\x.md"
```

Command Prompt:

```bat
dir /b /o:n "%USERPROFILE%\.claude\.claude-sync-backups"
copy /y "%USERPROFILE%\.claude\.claude-sync-backups\<time>\agents\x.md" "%USERPROFILE%\.claude\agents\x.md"
```

A file a profile owns (`machines/<profile>/...`) is put back by the next apply unless the edit goes into the profile: restore it, then run `sync`, which writes it back into the profile. For `settings.json`, the backup is the merged file (shared plus this machine's override); copying it back and syncing splits the override out as usual.

## Use the config in Claude Code on the web

Cloud sessions start from an empty container, so the environment's **Setup script** runs `claude-sync.sh remote` (apply the checkout as cloned, as machine `cloud` on the basic tier, with no fetch or push) instead of the installer. The script to paste, and a field-by-field guide to the **Edit environment** screen (Setup script, Network access, Environment variables, Save changes), are in the README, "Claude Code on the web (remote sessions)". Do not run `install.sh` there: it pushes a new `sync/<hostname>` branch per container.

Choose what the cloud machine gets from any machine you own, with `claude-sync.sh machine cloud ...` (README, "Give the cloud machine its own config"). Each change is a PR; merge it, and the next new cloud session has it. Check one with `claude-sync.sh machine cloud show`.

- Nothing was applied (`~/.claude` is empty): the session did not include the sync repo. The setup script only sees repos the session cloned and cannot fetch a private one, so add the sync repo to the session when you start it, or list it under the environment's **Repositories** (the Default environment has no such list).
- "Unknown command" or the usage text: the checkout is older than the `remote` command. Merge the latest tool into the sync repo, then start a new session.
- A plugin is missing in the session: `remote` installs plugins only with `--plugins`, and `machine cloud plugin disable` removes one on purpose (`machine cloud show` lists the overrides). If it failed, the message names the step: the `claude` CLI not on `PATH`, or the marketplace host blocked by the environment's network (set Network access to Full, or allow it). The config was applied either way.
- The session will not start: the script exited non-zero. Keep the `exit 0` at its end, and `|| echo` after `remote`.
- A `sync/<container name>` branch appeared in the repo: something pushed from a container, so `CLAUDE_CODE_REMOTE` was not seen (or `CLAUDE_SYNC_REMOTE=0` was set). Set `CLAUDE_SYNC_REMOTE=1` in the environment's variables, then delete the stray branch.
- `install`, `push`, `override`, `profile`, `tier` or `machine` says "throwaway cloud session": that is the guard working. Run them on a machine you own, or set `CLAUDE_SYNC_REMOTE=0` to override.
- `doctor` says "Cloud session: not checking the upstream": expected. It never fetches in a cloud session; run `doctor` on a machine you own to check the sync state.
- Config merged in a PR is missing: `remote` applies the checkout as cloned and never fetches, so it appears from the next new session, not a running one.
- `machine cloud plugin ...` says the machine follows a shared profile: the override file belongs to that profile, so a change would reach every machine on it. Run `machine PROFILE plugin ...` to do that on purpose, or `machine cloud profile use cloud` to give the cloud machine its own.
- `machine NAME ...` says it looks like a typo: only `cloud`, `cloud-...`, an existing `machines/NAME.conf` or an existing `machines/NAME/` profile are accepted.
- `remote` says it is for a throwaway cloud session: you ran it on a machine you own. Use `sync` or `pull` there; the setup script sets `CLAUDE_SYNC_REMOTE=1` itself.
- `machine cloud tier full` says the conf names no project path: memory is keyed to a project folder, so give it once, `machine cloud tier full /absolute/path`.

## Choose or switch a sync tier

A machine's tier is `TIER=` in `machines/<hostname>.conf` (no line means `full`). `basic` syncs `CLAUDE.md`, `settings.json`, agents, hooks and skills, with profiles and overrides as usual; `full` adds the primary project's memory folder, and the installer adds the Python packages and `ccusage`. `gh` is installed on both.

- New machine: `install.sh --tier basic` (or `CLAUDE_SYNC_TIER=basic`). No project path is asked for; one given with `--project-path` is still stored. An existing `.conf` wins: the flag is ignored with a hint.
- Show it: `claude-sync.sh tier` (works on any branch).
- Full to basic: `claude-sync.sh tier basic`. It captures first under `full`, so pending memory edits still go out, then the PR changes only the `.conf`. From then on `memory/` in the repo and the local memory folder are left exactly as they are, in both directions. Installed pip packages and `ccusage` stay installed; nothing is uninstalled. A local record, `~/.claude/.claude-sync-applied-tier`, notes that memory was left alone; do not delete it while on `basic`.
- Basic to full: `claude-sync.sh tier full`, or `claude-sync.sh tier full /absolute/project/path` when the `.conf` has no project path (the command refuses, changing nothing, without one; it never changes an existing path). The same happens if the `TIER=` change arrives from another machine or by hand. The first full-tier runs adopt memory as `bootstrap` does: the repo's memory is merged into the local folder without deleting (the repo's copy wins a same-named file; the local version is copied to `~/.claude/.claude-sync-memory-backup/<time>-*/` first, and the run says so), and the next capture adds local-only memory files without deleting shared ones (it merges the repo's memory in first only if no apply has done so yet, so memory edits made after the switch are kept). So the `tier full` PR carries only the `.conf`; the local memory files appear in the next sync's PR. After that capture the local record is removed and memory mirrors (with delete) again, as on every full machine. Check that PR's `memory/` diff.
- `doctor` prints the tier. On `basic` it warns (exit 0) when `settings.json` enables an agent-memory plugin, since its Python packages are not installed; on `full` it reports a too-long memory folder name as before.

## Reset a machine's sync state

```bash
~/claude-config/install.sh --repair
```

The machine's new `.conf` goes out through its sync PR; repair pushes nothing to the default branch. This keeps the machine's profile and settings override. Re-give the profile with `--profile <name>` if it followed another one. Re-give the tier with `--tier basic` if it was basic (the new `.conf` is full otherwise; that is safe, the next syncs adopt memory without deleting, see "Choose or switch a sync tier"). It refuses while this machine's sync PR is pending (the clone shows those changes as uncommitted), so merge or drop the PR and run `sync` first. Never `git stash` or `git reset --hard` the clone to get past it. A fresh clone picks up the machine's open `sync/<hostname>` from origin by itself.

On Windows, run `install.cmd --repair` from a prompt whose folder is outside the clone (for example `cd /d %USERPROFILE%` first): Windows cannot delete a folder a shell is using.

## Publish the public mirror by hand

Commit first; the tool refuses on a dirty tree.

```bash
cd ~/claude-config && python3 -m claude_sync.publish
```

The publish workflow runs the same command. It needs the public repo's URL set (it has no built-in default):

```bash
cd ~/claude-config && CLAUDE_CONFIG_PUBLIC_REPO_URL=git@github.com:<owner>/<repo>.git python3 -m claude_sync.publish
```

It aborts if `machines/`, `memory/`, `private/` or `weekly-review/` ended up in the generated tree. It also aborts on a `.credentials.json` or `.claude.json` anywhere in it, skips links instead of copying them, and exports only files committed at `HEAD`.

Published files keep the mode they have at HEAD, also when it is run on Windows (`_keep_modes` in `claude_sync/publish.py`).

The whitelist (`PATHS` in `claude_sync/publish.py`) includes the `claude_sync/` package, `pytest.ini` and the `.cmd` shims, so the mirror can run and test the Python. It leaves out `dot-claude/rules/`, `dot-claude/commands/` and `dot-claude/keybindings.json`: they sync between machines but may be personal, so the template never ships them (`test_synced_rules_commands_and_keybindings_are_not_published`). Keep the trigger paths in `.github/workflows/publish-public.yml` in step with it; `test_workflow_triggers_match_whitelist` checks.

## Re-check out a Windows clone after a line-ending change

A Windows clone made with `core.autocrlf` on has CRLF in its working tree, and turning it off later does not rewrite files git thinks are up to date. With no sync PR pending and nothing uncommitted:

```bat
git -C %USERPROFILE%\claude-config config core.autocrlf false
git -C %USERPROFILE%\claude-config rm -r -q --cached .
git -C %USERPROFILE%\claude-config reset -q --hard
```

Or run `install.cmd --repair`, which clones afresh with `-c core.autocrlf=false`. Since `.gitattributes` unsets `text` for `dot-claude/`, `dot-agents/` and `memory/`, git no longer rewrites those files. Re-checking out the clone does not change the live copies: a file that is CRLF in `~/.claude` only because an old clone wrote it that way goes into the next sync's PR as a line-ending-only change, since capture runs first. Before that sync, convert such live files back to LF by hand, or drop the line-ending change from the PR (see `docs/python-port.md`, "Line endings").

## Add an MCP server

Keep the tool surface small, because every tool definition costs tokens each session. Pin the 2 or 3 tools used every session and make the rest discoverable through a search tool, as cognee's MCP does. Default the server to off and enable it on request (see the on-request notes in `memory/`).

## MCP servers (opt-in sync)

`claude-sync.sh doctor` reads `~/.claude.json` (next to `~/.claude`) and lists each MCP server: its name, scope (`user`, or the project folder's name), transport (the `type` field: `stdio` when unset, `http`, `sse`, `ws` or `sdk`, else `unknown`) and its bucket in `integrations.json` (`unclassified` if none; "listed for other projects" if the entry names other projects). Project matching is the same as `scripts/list_disabled.py`: the exact path first, else the same folder name. An entry with a `machines` list that does not include this host (or its profile), matched as in `scripts/list_disabled.py`, is per-machine and shows as not syncing.

- It warns `WARNING: MCP server <name> (<scope>): literal credential in <env KEY | header KEY | arg #N | url | command>`. `arg #N` counts from 1. It names the place only, never the value.
- Allowed: an empty value, or only `${VAR}` / `${VAR:-default}` references (`Bearer ${VAR}` too). Flagged: a literal under a secret-like name (token, key, secret, password, auth, bearer, credential, cookie), a value shaped like a known token, a long random-looking string, `--token value` style args, and a url with a password or a secret-named query parameter.
- It warns if `~/.claude.json` can be read by group or others (not on Windows); `chmod 600 ~/.claude.json` fixes it.
- It ends with one summary line. Warnings are not issues and never change the exit code. If the file is missing or not valid JSON it prints one "MCP check skipped" line.
- `doctor` writes nothing. Literal credentials stay in `~/.claude.json` on this machine; to move one out, put it in an environment variable and use `${VAR}`.

**Turning sync on.** Add `MCP_SYNC=on` to `machines/<hostname>.conf` (remove the line to turn it off). Full tier only: with `TIER=basic`, or without the line, sync never opens `~/.claude.json`. Do not turn it on for a machine you have not looked at with `doctor` first.

- Capture reads only the top-level `mcpServers` of `~/.claude.json` (never `projects.*` or any other key) and merges the servers that pass the checks below into `mcp/servers.json` in the repo (live wins a name clash; nothing is removed from the repo file). The file is private: `mcp/` is in `PRIVATE_TOP_LEVEL`, so the public mirror refuses it.
- Apply merges the repo's servers into the live `mcpServers`. Every live server stays: refused ones, per-machine ones and ones the repo lacks. If a live entry of the same name would itself be refused (a literal token, a Windows path), it is kept as it is. Only `mcpServers` changes; every other key keeps its value and order.
- Skipped, with one line `mcp: skipped <server>: <reason>` (the place of a credential such as `env KEY` or `arg #2`, never its value; sync does not fail): a literal credential in env, headers, args, url or command; a registry entry in `off_until_asked` or `uninstalled` (matched by name, whatever its `projects`, because `integration.py` cannot switch user-scope servers); a registry entry whose `machines` list lacks this host or profile; a `command` that is not a bare program name (a path, a `~` form, `.cmd`, `.exe`, `.bat`, `.ps1`, `.com`; they are flagged, never rewritten, on every OS).
- The write goes through `claude_sync/safejson.py`: it re-checks size and mtime just before the swap (Claude Code rewrites this file; the step retries three times, then skips with one line), backs the file up to `~/.claude/.claude-sync-mcp-backups/` (newest 10 kept), keeps mode 0600, and does nothing when the merge equals the live file.
- Deletes do not propagate (there is no base snapshot to tell a removed server from a new one). To remove a server everywhere, remove it from `~/.claude.json` on each machine and from `mcp/servers.json` in the repo.
- A moved clone together with a local edit to `mcpServers` makes capture refuse, like any other local edit; run `pull` after saving your edit elsewhere.

## Audit what a repo costs in tokens (repomix)

Use this when a repo's size matters, for example to find what an agent would read if it packed the repo. Nothing is installed: `npx` fetches a pinned version each time.

```bash
cd ~/some-repo
npx repomix@1.19.0 --token-count-tree --ignore '.git/**,private/**' -o "$(mktemp -d)/pack.xml"
```

- The tree lists tokens per file and folder (`o200k_base`, so an estimate, not a Claude count). Read the tree; the pack file is a by-product and can be deleted.
- Write the output to a scratch directory, never into the repo.
- Most of a repo is never loaded into a session. Only the global `CLAUDE.md`, the memory index and skill and agent descriptions are, so for session cost use the harness-context measurement in modem7/claude-config#86 and not this tree.
- The security check may flag test fixtures that hold fake tokens. Open the flagged files before trusting or ignoring them.

## Convert Office files and PDFs to Markdown (markitdown)

`markitdown` (MIT, Microsoft) converts docx, pptx, xlsx, EPUB, HTML, CSV, JSON and PDF to Markdown on this machine. Run it through `uvx` as a one-off, with no MCP server, so it costs no tokens per session:

```bash
uvx --from 'markitdown[pdf]' markitdown report.pdf > report.md
uvx --from 'markitdown[docx,pptx,xlsx]' markitdown deck.pptx > deck.md
```

- For a PDF alone, `pdftotext -layout` is as good. markitdown output is 15 to 18% smaller and keeps headings and ticked items, but it does not normalise ligatures (`oﬃcial`, `puriﬁcation`). Clean those up before searching or diffing, and do not feed its output to a diff of the source text.
- Prefer markitdown for Office formats, where it replaces a separate tool per format.
- Leave the Azure Document Intelligence, LLM caption and hosted transcription extras off. They send the file off the machine.

## List issues that are ready to work on

GitHub tracks "blocked by" links natively, so no label or convention is needed. In the issue sidebar, add the blocker under Relationships. Then list the open issues with no open blocker:

```bash
gh api 'repos/OWNER/REPO/issues?state=open&per_page=100' \
  --jq '.[] | select(.pull_request|not) | select(.issue_dependencies_summary.blocked_by==0) | "#\(.number)\t\(.title)"'
```

The API returns pull requests in the same list, so the filter drops them. `blocked_by` is the blocker count in the issue summary; check on a real blocked issue that it drops to 0 once the blocker closes. Past 100 open issues, add `--paginate`. This is the one idea taken from beads; the rest of beads was skipped (see modem7/claude-config#86).

## Check a smaller `.gitattributes` is safe

Before replacing a repo's `.gitattributes` with a shorter one, prove it changes nothing real:

```bash
python3 scripts/gitattributes_check.py ~/some-repo /path/to/new-gitattributes
```

It clones the repo to a temp directory, compares `git check-attr` (text, eol, diff, merge, binary) for every tracked file before and after, then runs `git add --renormalize .` in the clone. Your repo is not touched. Exit 0 means no real attribute change and no renormalize change; 1 means there is one (listed); 2 is a usage or git error. A `text` rule going from forced to `auto` is reported as a soft change and does not fail the check.

Carry each repo's own deliberate lines (for example `dot-claude/** -text`) into the new file, or the check fails. A repo that already has unnormalised files fails too, even if the swap is not the cause. Open the PR only on a pass, and never push to the default branch.

## List what is installed but switched off

Plugins, MCP servers, skills and agents that are not simply always on are recorded in `integrations.json` (repo root) in three buckets: **off until asked** (installed, disabled, enabled when you ask), **enabled per project**, and **uninstalled** (removed on purpose). Before anything is disabled, hidden or removed, Claude asks which bucket it belongs in and records it there. To see the state:

```bash
python3 ~/claude-config/scripts/list_disabled.py          # text, one section per bucket, with enable hints
python3 ~/claude-config/scripts/list_disabled.py --json   # same, as JSON
```

The report compares the registry with the live state and flags mismatches (for example an "off until asked" plugin that is enabled). Anything found disabled, hidden or installed-but-not-enabled that is not in the registry is listed as "Unclassified": decide its bucket and add it to `integrations.json`. It reads `~/.claude/settings.json`, `~/.claude/plugins/installed_plugins.json`, `~/.claude.json` (MCP server names, project paths and transport only; never commands, arguments, environment or URLs), `~/.claude/skills-disabled/`, `~/.claude/agents-disabled/` and the profile `.disabled` markers under `machines/`. It writes nothing. A disabled skill is moved to `~/.claude/skills-disabled/<name>`; move it back to enable it. `scripts/integration.py`, which switches one on or off, keeps `~/.claude.json` at mode 0600 and writes through the shared `claude_sync/safejson.py`.

## Checks for each machine

The hooks call `~/claude-config/claude-sync.sh`, a shim that runs the Python. On every machine, check:

- [ ] On every machine, check `python3 --version` is 3.12 or newer, in the shell the hooks run in (on Windows, Git Bash), not only in your own. `command -v python3` is not enough: the Windows Store stub is found on `PATH` but exits 9009 without running anything. Debian 12 ships 3.11, Ubuntu 22.04 3.10 and stock macOS 3.9; on those, every hook sync fails until a newer Python is installed.
- [ ] On every machine, check the hostname (or `CLAUDE_SYNC_HOSTNAME`, if set) uses only letters, digits, `.`, `_` and `-`. The Python checks it on every command, so otherwise every hook sync fails with `Not a valid hostname`.
- [ ] Run `claude-sync.sh doctor` once. A hook committed from Windows shows as "tracked without the exec bit"; fix each as the hint says (see "A sync seems stuck") and merge that PR first.
- [ ] Check the Windows CI job is green, and walk the Windows checklist in `docs/python-port.md`, then run `docs/windows-manual-test.md`.
- [ ] On Windows, after the first Python sync, delete once any real folders under `~/.claude/skills` that Git Bash's `ln -sfn` left as copies of `~/.agents/skills/<name>`. The Python only replaces links and junctions there, never a real folder, so those copies would stay stale.
- [ ] Know that on Windows the hook timeout kills Python outright, with no handler run. Recover as after a power loss: run `claude-sync.sh doctor`, and remove a stale git `.lock` file as in "A sync seems stuck".
