# Runbook

Procedures for the sync tool. Paths assume the clone is at `~/claude-config`.

## Restore a machine on new infrastructure

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

## A sync seems stuck

```bash
~/claude-config/claude-sync.sh doctor
~/claude-config/claude-sync.sh doctor remediate
```

A real content conflict is reported, not guessed at. Resolve it in `~/claude-config`, then run `sync` again.

## Reset a machine's sync state

```bash
~/claude-config/install.sh --repair
```

This keeps the machine's profile and settings override. Re-give the profile with `--profile <name>` if it followed another one.

## Publish the public mirror by hand

Commit first; the script refuses on a dirty tree.

```bash
~/claude-config/publish-public.sh
```

It aborts if `machines/`, `memory/` or `private/` ended up in the generated tree.
