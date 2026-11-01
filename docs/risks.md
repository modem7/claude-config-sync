# Risks

Things that can go wrong in this sync tool, why, and what guards against each. Read this before changing capture, apply, profiles or the publish script. Each entry names the test that covers it.

| # | Risk | Cause | Guard | Test |
|---|---|---|---|---|
| 1 | Profile entry overwritten on restore | `sync` captures before it applies. A profile entry pulled from the repo but not applied yet looks identical to the shared file locally, so a naive copy-back would overwrite the profile with the shared version. | Copy back only when the live entry differs from the applied-state stamp (`.claude-sync-applied-profile`). No stamp record means "not applied yet", so the profile is left alone. | `tests/profile.bats` (capture-before-apply regression) |
| 2 | Profile content leaks into shared files | `rsync --delete` capture copies everything under `~/.claude/agents`, `hooks` and `~/.agents/skills` into the shared directories. | Names owned by the profile (entries, `.disabled` markers, stamp names) are excluded from the shared capture, and the shared copy is never deleted for them. `CLAUDE.md` is captured without the append block. | `tests/profile.bats`, `tests/sync_files.bats` |
| 3 | Capture refuses to run (deadlock) | `live_matches_commit` rebuilds what a clean apply would leave and diffs it with live. If it ignores the profile, an applied profile looks like a local edit and capture is refused. | It rebuilds the expected tree with the stamped profile applied. | `tests/profile.bats` |
| 4 | Shared file not restored after a profile entry is removed | rsync skips a file whose size and mtime match, so a shared file the same size as the old profile copy is not put back. | `profile_unapply` removes last time's applied entries before `sync_apply`. | `tests/profile.bats` |
| 5 | Dangling hook reference | A profile disables a hook script that `settings.json` still wires in, so the hook errors on every event. | `doctor` reports hook commands whose script is missing. Disable the wiring with a settings override. | `tests/profile.bats` |
| 6 | Hooks run unreviewed code | Hooks execute automatically. A shared hook change lands on every machine's next sync. A profile hook runs on its own machine only, but `profile copy` and `PROFILE=` move it to another machine. | `profile copy` lists executable hooks and asks before copying (`--yes` skips). Review hook changes like code. | `tests/profile.bats` |
| 7 | Profile does not apply after a rename | The profile is keyed by hostname. New infrastructure with a different hostname silently gets no profile. | `PROFILE=<name>` in the machine conf, set by `install.sh --profile <name>` or `profile use <name>`. | `tests/profile.bats`, `tests/machine_conf.bats` |
| 8 | Per-machine or personal content published | The public mirror is generated from a whitelist in `publish-public.sh`; a careless whitelist edit could add `machines/`, `memory/` or `private/`. | `assert_public_tree_clean` aborts the publish if any of them, or a nested `*.settings.json` under `machines/`, is in the generated tree. | `tests/safety.bats` |
| 9 | `install.sh --repair` loses the profile | Repair removes the machine's `.conf`. | Repair keeps `machines/<profile>/` and the settings override. The `PROFILE=` line must be given again with `--profile`. | `tests/profile.bats` |
| 10 | Stuck sync | Capture commits before the pull, so a later rebase conflict aborts before push and apply, and repeats. | `doctor` and `doctor remediate`. | `tests/sync_cli.bats` |
| 11 | Stale capture reverts merged work | Capture copies live files over the repo. If live is older than the clone's HEAD it commits stale files. | `check_capture_is_safe` skips or refuses capture. | `tests/sync_cli.bats` |
| 12 | Secrets committed | Account files are never copied, but a mistake could add one. | Denylist (`.credentials.json`, `.claude.json`), `.gitignore`, gitleaks in CI. | `tests/safety.bats` |

## Known gaps

- Entry names containing `*`, `?` or `[` are not escaped in the rsync exclude list. Profile entries should not use them.
- `install.sh --repair` on a machine that has synced before: the old applied-commit stamp makes bootstrap skip capture, so the new `.conf` (and its `PROFILE=` line) only reaches the remote on the next sync.
- `doctor` reports stale `.disabled` markers and dangling hook references but does not fix them, since that means editing the shared repo.
- Auto-memory mixes global and project-specific entries; see the design spec.
