"""`claude-sync doctor [remediate]`: diagnose (and repair) a stuck clone.

Port of do_doctor and doctor_profile in claude-sync.sh. A claude-sync.sh
from before sync branches committed captures on the default branch; if a
rebase onto upstream then conflicted, those commits stayed local and every
later sync repeated the failure. Today's sync moves such commits onto
sync/<hostname> instead, so remediation is a normal sync, which fails
loudly if they really conflict.

Issues go to stdout as "ISSUE: ..." lines; do_doctor returns 1 when it
printed any (even if remediate fixed them), else 0.

Also here (they cannot live in overlay.py or profile.py, which gitflow
imports): the repo-changing `override` and `profile` subcommands, ports
of do_override, do_profile and do_profile_copy, and finish_sync, the tail
they share with `sync`. Each takes the run's ExitStack, which holds the
lock begin_sync takes until the run ends.
"""

from __future__ import annotations

import os
import shlex
import shutil
import sys
from collections.abc import Callable
from contextlib import ExitStack

from claude_sync import gitflow, machineconf, overlay, pr, profile
from claude_sync.gitflow import SyncCtx
from claude_sync.memorypath import long_name_warning
from claude_sync.syncfiles import _remove
from claude_sync.util import Fail, UsageError, printable


class _Report:
    def __init__(self) -> None:
        self.issues = 0

    def __call__(self, text: str) -> None:
        self.issues = 1
        print(f"ISSUE: {text}")


def _read_only(what: str, func, *args):
    """profile.py's read-only checks raise OSError as is (T8): say what
    could not be read instead of a traceback."""
    try:
        return func(*args)
    except OSError as err:
        raise Fail(
            f"could not {what}: {err}",
            hint="  Check the path named above is readable, then re-run 'claude-sync.sh doctor'.",
        ) from err


def do_doctor(
    ctx: SyncCtx,
    stack: ExitStack,
    *,
    remediate: bool = False,
    run_sync: Callable[[], bool] | None = None,
) -> int:
    """Prints what is wrong with this clone and its live config; with
    `remediate`, also repairs what it safely can. `run_sync` runs a full
    'sync' as its own run (the bash calls claude-sync.sh sync) and says
    whether it succeeded; remediate needs it.

    Remediation changes the clone and live config, so it holds the lock in
    `stack`, except around the nested sync, which takes the lock itself
    (SyncLock is not re-entrant)."""
    if remediate and run_sync is None:
        raise ValueError("doctor remediate needs run_sync")
    report = _Report()
    if remediate:
        gitflow.acquire_lock(ctx, stack)

    if gitflow.git_path(ctx, "rebase-merge").is_dir() or gitflow.git_path(ctx, "rebase-apply").is_dir():
        report("repo is mid-rebase (a previous sync's conflict was never resolved).")
        if remediate:
            gitflow._git(ctx, "rebase", "--abort")
            print("  -> aborted the stuck rebase.")

    _check_upstream(ctx, stack, report, remediate, run_sync)

    project = machineconf.read_primary_project_path(ctx.repo, ctx.hostname)
    if project is not None:
        warning = long_name_warning(project)
        if warning:
            report(warning[0].upper() + warning[1:])
        _check_settings(ctx, report, remediate)
        doctor_profile(ctx, report, remediate)

    # A hook command pointing at a script that isn't there (e.g. a hook a
    # profile disabled but settings.json still wires up) fails on every use.
    for missing in profile.profile_dangling_hook_refs(ctx.claude_home, ctx.claude_home / "settings.json"):
        report(
            f"settings.json runs {missing}, which does not exist. Remove that hook from"
            " settings.json (or 'override add' it away on this machine), or restore the script."
        )

    _check_hook_modes(ctx, report)

    if not report.issues:
        print("No issues found - sync is healthy.")
    if gitflow.has_sync_branch(ctx):
        sb = gitflow.sync_branch(ctx.hostname)
        print(f"Note: {sb} holds this machine's changes that are not merged yet; its PR shares them once merged.")
    return report.issues


def _check_upstream(ctx: SyncCtx, stack: ExitStack, report: _Report, remediate: bool, run_sync) -> None:
    if not gitflow._ok(ctx, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"):
        return
    fetched = gitflow._git(ctx, "fetch", "-q")
    if fetched.returncode != 0:
        gitflow._show_stderr(fetched)
        raise gitflow._fetch_failed()
    ahead = int(gitflow._out(ctx, "rev-list", "--count", "@{u}..HEAD"))
    behind = int(gitflow._out(ctx, "rev-list", "--count", "HEAD..@{u}"))
    if ahead <= 0:
        return
    if behind > 0:
        report(
            f"local branch is {ahead} ahead / {behind} behind its upstream - a prior sync likely"
            " failed to push/apply."
        )
    else:
        report(
            f"local branch is {ahead} commit(s) ahead of its upstream - claude-sync never commits"
            f" there, so the next sync moves them onto {gitflow.sync_branch(ctx.hostname)} as a PR."
        )
    if not remediate:
        return
    print("  -> retrying a full sync now that a stuck rebase (if any) is cleared...")
    sys.stdout.flush()
    gitflow.release_lock(ctx)
    if run_sync():
        print("  -> sync succeeded.")
    else:
        print(
            "  -> still failing: a real conflict exists between local and remote settings, not just staleness.",
            file=sys.stderr,
        )
        print(
            f"     Resolve manually in {ctx.repo} (git status), then re-run 'claude-sync.sh sync'.",
            file=sys.stderr,
        )
    gitflow.acquire_lock(ctx, stack)


_printable = printable


# Special inside double quotes in cmd or PowerShell.
_WINDOWS_UNSAFE = set('"$`%!')


def _paste_arg(text: str) -> str | None:
    """text as one shell argument that is safe to copy and paste, or None
    when this platform's shells can't be given it safely. POSIX: shlex
    quoting. Windows: double quotes, which cmd, PowerShell and Git Bash
    all read literally once the characters above are ruled out."""
    text = _printable(text)
    if os.name != "nt":
        return shlex.quote(text)
    if _WINDOWS_UNSAFE & set(text):
        return None
    return f'"{text}"'


def _check_hook_modes(ctx: SyncCtx, report: _Report) -> None:
    """A hook script committed as 100644 (as Windows git does) lands
    non-executable on Linux and macOS. Reported, not fixed: the fix is a
    change to the shared repo, which goes out through a sync."""
    for rel, mode in gitflow.tree_modes(ctx, gitflow.effective_commit(ctx)).items():
        if mode != "100644" or not gitflow.is_hook_path(rel):
            continue
        if not gitflow.looks_executable(rel, ctx.repo / rel):
            continue
        repo_arg, rel_arg = _paste_arg(str(ctx.repo)), _paste_arg(rel)
        if repo_arg is None or rel_arg is None:
            fix = "Fix: set its exec bit in the clone's index with git update-index --chmod=+x"
        else:
            fix = f"Fix: git -C {repo_arg} update-index --chmod=+x -- {rel_arg}"
        report(
            f"{_printable(rel)} is a hook script tracked without the exec bit (mode 100644), so Linux"
            f" and macOS get it non-executable. {fix}"
            " (on Linux or macOS: chmod +x it in the clone and its live copy, if any, instead),"
            " then run 'claude-sync.sh sync'."
        )


def _check_settings(ctx: SyncCtx, report: _Report, remediate: bool) -> None:
    live = ctx.claude_home / "settings.json"
    shared = ctx.repo / "dot-claude" / "settings.json"
    if not (live.is_file() and shared.is_file()):
        return
    try:
        expected = gitflow.expected_local_settings(ctx)
    except Fail as err:
        # bash's diff against a failed overlay_merge counts as "differs";
        # here the reason is shown, and nothing is re-applied over it.
        report(f"could not work out what settings.json should be: {err.message} {err.hint}".rstrip())
        return
    if _read_only("read the live settings.json", live.read_bytes) == expected:
        return
    report(
        "local ~/.claude/settings.json differs from the repo's dot-claude/settings.json"
        " (plus this machine's overrides)."
    )
    if remediate:
        gitflow.do_apply(ctx)
        print("  -> re-applied the repo's settings.json to local.")


def doctor_profile(ctx: SyncCtx, report: Callable[[str], None], remediate: bool) -> None:
    """Profile checks, only when a profile or its stamp exists. Stale
    markers are reported, not fixed: that would mean editing the shared
    repo. A live mismatch is re-applied, unless that would discard a local
    edit to an owned entry that a sync would write back into the profile."""
    name = gitflow.current_profile(ctx)
    pdir = profile.profile_dir(ctx.repo, name)
    if not profile.profile_in_use(ctx.repo, ctx.claude_home, name):
        return
    for kind in profile.KINDS:
        for entry in profile.profile_disabled(pdir, kind):
            if (profile.profile_shared_dir(ctx.repo, kind) / entry).exists():
                continue
            report(
                f"machines/{name}/{kind}/{entry}.disabled hides nothing: there is no shared {kind}"
                f" entry called {entry}. Delete the marker."
            )

    mismatches = _read_only(
        f"compare the live config with profile '{name}'",
        profile.profile_live_mismatches, ctx.repo, ctx.claude_home, ctx.agents_home, name,
    )  # fmt: skip
    if not mismatches:
        return
    report(
        f"live config differs from the shared config plus profile '{name}' for: {', '.join(mismatches)}."
    )
    if not remediate:
        return
    edited = _read_only(
        f"check profile '{name}' for local edits",
        profile.profile_has_local_edits, ctx.repo, ctx.claude_home, ctx.agents_home, name,
    )  # fmt: skip
    if edited:
        print(
            f"  -> not re-applied: an entry of profile '{name}' was edited locally. Run"
            " 'claude-sync.sh sync' to keep the edit in the profile."
        )
        return
    gitflow.do_apply(ctx)
    print(f"  -> re-applied the shared config and profile '{name}' to local.")


# ── the shared tail ──────────────────────────────────────────────────────


def finish_sync(ctx: SyncCtx) -> None:
    """The tail every repo-changing command shares once its own commits are
    on the sync branch: integrate, apply, then push and open the PR.
    Applying before pushing means a failed push or PR never leaves the live
    config behind the clone."""
    gitflow.do_integrate(ctx)
    gitflow.do_apply(ctx)
    pr.do_push(ctx)


# ── override add|remove|list [PATH] ──────────────────────────────────────
# The override file follows the profile name, so a machine following
# another profile shares that profile's settings override.


def override_list(ctx: SyncCtx) -> None:
    for line in overlay.overlay_list(ctx.repo, gitflow.current_profile(ctx)):
        print(line)


def _need(value: str) -> str:
    if not value:
        raise UsageError()
    return value


def override_add(ctx: SyncCtx, stack: ExitStack, expr: str) -> None:
    """Makes the key at `expr` specific to this machine (profile)."""
    overlay.parse_path(_need(expr))  # a bad path fails before anything changes
    gitflow.begin_sync(ctx, stack)
    # Capture first so any other pending local edits are committed as
    # shared, then move this one key out of the shared file.
    gitflow.do_capture_and_commit(ctx)
    overlay.overlay_add(
        ctx.repo, gitflow.current_profile(ctx), ctx.claude_home / "settings.json", expr
    )
    gitflow.commit_worktree(ctx, f"override: {ctx.hostname} keeps {expr} to itself")
    finish_sync(ctx)
    sb = gitflow.sync_branch(ctx.hostname)
    print(f"{expr} is now specific to {ctx.hostname}; other machines drop it once {sb}'s PR is merged.")


def override_remove(ctx: SyncCtx, stack: ExitStack, expr: str) -> None:
    """Hands the key at `expr` back to the shared settings."""
    overlay.parse_path(_need(expr))
    gitflow.begin_sync(ctx, stack)
    # Capture while the override still owns the key, so its local value
    # isn't mistaken for a new shared value, then drop it and re-apply.
    gitflow.do_capture_and_commit(ctx)
    overlay.overlay_remove(ctx.repo, gitflow.current_profile(ctx), expr)
    gitflow.commit_worktree(ctx, f"override: {ctx.hostname} stops overriding {expr}")
    finish_sync(ctx)
    print(f"{expr} on {ctx.hostname} now follows the shared settings.")


# ── profile list|show NAME|copy FROM [--force] [--yes]|use NAME ──────────


def _valid(name: str) -> str:
    if not profile.profile_valid_name(_need(name)):
        raise Fail(
            f"Not a valid profile name: {name}",
            hint="  Use only letters, digits, '.', '_' and '-' (not '.' or '..').",
        )
    return name


def profile_list(ctx: SyncCtx) -> None:
    name = gitflow.current_profile(ctx)
    if machineconf.read_machine_profile(ctx.repo, ctx.hostname) is not None:
        print(f"{ctx.hostname} follows profile '{name}' (PROFILE= in machines/{ctx.hostname}.conf):")
    else:
        print(f"{ctx.hostname} uses its own profile '{name}':")
    print(profile.profile_describe(ctx.repo, name), end="")


def profile_show(ctx: SyncCtx, name: str) -> None:
    _valid(name)
    print(f"Profile '{name}':")
    print(profile.profile_describe(ctx.repo, name), end="")


def profile_use(ctx: SyncCtx, stack: ExitStack, name: str) -> None:
    """Makes this machine follow profile `name` (its own: the hostname)."""
    _valid(name)
    own = ctx.hostname
    # Going back to the machine's own profile needs no directory.
    if name != own and not profile.profile_dir(ctx.repo, name).is_dir():
        raise Fail(
            f"No profile called '{name}': machines/{name}/ does not exist.",
            hint="  'claude-sync.sh profile show NAME' describes a profile; pick one that exists.",
        )
    gitflow.begin_sync(ctx, stack)
    # Capture under the old profile first, so pending edits land in it.
    gitflow.do_capture_and_commit(ctx)
    machineconf.write_machine_profile(ctx.repo, own, "" if name == own else name)
    gitflow.commit_worktree(ctx, f"profile: {own} uses profile {name}")
    finish_sync(ctx)
    print(f"{own} now follows profile '{name}'; edits to its entries go to machines/{name}/.")


def _ask(prompt: str) -> str:
    """One line from stdin, read at call time; "" at end of input. The
    prompt shows only on a terminal, as bash `read -p` does."""
    if sys.stdin.isatty():
        print(prompt, end="", file=sys.stderr, flush=True)
    return sys.stdin.readline().rstrip("\r\n")


def _copy_args(args: list[str]) -> tuple[str, bool, bool]:
    source, force, yes = "", False, False
    for arg in args:
        if arg == "--force":
            force = True
        elif arg == "--yes":
            yes = True
        elif arg.startswith("-") or source:
            raise UsageError()
        else:
            source = arg
    return _need(source), force, yes


def profile_copy(
    ctx: SyncCtx, stack: ExitStack, args: list[str], ask: Callable[[str], str] | None = None
) -> None:
    """Snapshots profile FROM (machines/<from>/ and its settings override)
    into this machine's own, machines/<hostname>/, and makes this machine
    follow it. Hook scripts run automatically, so they are shown first and
    copied only on a yes (or --yes). `args` is FROM [--force] [--yes]."""
    source, force, yes = _copy_args(args)
    _valid(source)
    own = ctx.hostname
    if source == own:
        raise Fail(f"'{source}' is already this machine's own profile.")
    src = profile.profile_dir(ctx.repo, source)
    dest = profile.profile_dir(ctx.repo, own)
    src_ov = overlay.overlay_path(ctx.repo, source)
    own_ov = overlay.overlay_path(ctx.repo, own)
    if not src.is_dir() and not src_ov.is_file():
        raise Fail(
            f"No profile called '{source}': neither machines/{source}/ nor"
            f" machines/{source}.settings.json exists."
        )
    if (dest.exists() or dest.is_symlink() or own_ov.is_file()) and not force:
        raise Fail(
            f"{own} already has its own profile (machines/{own}/ or machines/{own}.settings.json)."
            " Re-run with --force to replace it."
        )
    # Refused before begin_sync, so a refusal leaves no capture commit.
    not_copied = Fail(
        f"machines/{source}/ was not copied: it holds a link or special file (see the warning above).",
        hint=f"  Replace it with a real file in machines/{source}/, then re-run.",
    )
    if src.is_dir() and not profile._copyable(src, ctx.repo):
        raise not_copied

    hooks = profile.profile_hook_scripts(src)
    if hooks:
        print("These executable hook scripts will be copied, and run by Claude Code on this machine:")
        for hook in hooks:
            print(f"  {hook.relative_to(ctx.repo).as_posix()}")
        sys.stdout.flush()
        if not yes and (ask or _ask)("Copy them? [y/N] ") not in ("y", "Y", "yes", "YES"):
            raise Fail("Aborted: nothing was copied.")

    gitflow.begin_sync(ctx, stack)
    gitflow.do_capture_and_commit(ctx)
    # Checked again before anything is replaced: either may have changed
    # since, and a refusal must leave machines/<own>/ as it was.
    overlay.check_override_path(own_ov)
    overlay.check_override_path(src_ov)
    if src.is_dir():
        if not profile._copy_entry(src, dest, ctx.repo):  # checked again: it may have changed
            raise not_copied
    else:
        _remove(dest)
    own_ov.unlink(missing_ok=True)
    if src_ov.is_file():
        shutil.copyfile(src_ov, own_ov, follow_symlinks=False)
    # The copy only takes effect if this machine follows its own profile.
    machineconf.write_machine_profile(ctx.repo, own, "")
    gitflow.commit_worktree(ctx, f"profile: {own} copies profile {source}")
    finish_sync(ctx)
    print(f"{own} now has its own copy of profile '{source}' in machines/{own}/; edit it freely.")
