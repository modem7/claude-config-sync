"""The command line: `python -m claude_sync [COMMAND ...]`.

Port of main() and usage() in claude-sync.sh. `main` only works out the
run's settings, picks the command and turns how it ended into an exit
code; the work is in gitflow, pr and doctor.

    push | pull | sync | bootstrap | doctor [remediate]
    override add|remove|list [PATH]
    profile list | show NAME | copy FROM [--force] [--yes] | use NAME
    tier [show | basic | full [PROJECT_PATH]]

No command means `sync`. Settings come from `env` (CLAUDE_SYNC_REPO_DIR,
_CLAUDE_HOME, _AGENTS_HOME, _HOSTNAME, _LOCK_WAIT). pr.py does not see
`env`: it reads CLAUDE_SYNC_GH and CLAUDE_SYNC_GH_REPO from os.environ at
call time, and util.redact (used by pr and gitflow) reads GH_TOKEN,
GITHUB_TOKEN, GH_ENTERPRISE_TOKEN and GITHUB_ENTERPRISE_TOKEN the same way. `python -m claude_sync` passes os.environ,
so the two agree; an in-process caller must put those in os.environ.
"""

from __future__ import annotations

import socket
import sys
from collections.abc import Mapping
from contextlib import ExitStack
from pathlib import Path

from claude_sync import doctor, gitflow, pr, profile
from claude_sync.gitflow import SyncCtx
from claude_sync.lock import lock_wait_from_env
from claude_sync.util import Fail, UsageError, redact, run

# bash usage(), except that its two flock lines became one: there is no
# CLAUDE_SYNC_FLOCK and no weaker fallback lock any more.
USAGE = """\
Usage: claude-sync.sh [push|pull|sync|bootstrap|doctor [remediate]|override add|remove|list [JQ_PATH]
                       |profile list|show NAME|copy FROM [--force] [--yes]|use NAME
                       |tier [show|basic|full [PROJECT_PATH]]]
  sync       capture local changes onto sync/<hostname>, merge the default branch in, apply, push, open/update its PR
  push       capture onto sync/<hostname>, push it and open/update its PR (no apply)
  pull       bring in the default branch (keeping this machine's unmerged changes) and apply
  Nothing is ever pushed to the default branch; changes reach other machines when the PR is merged.
  The PR step needs gh, logged in. CLAUDE_SYNC_GH=<path> picks the gh binary;
  CLAUDE_SYNC_GH_REPO=owner/repo sets the repo when origin's URL doesn't show it.
  Needs git 2.40+. A second run waits CLAUDE_SYNC_LOCK_WAIT seconds (default 10) for the first.
  e.g. claude-sync.sh override add '.enabledPlugins["name@marketplace"]'
       claude-sync.sh profile use other-host   # follow machines/other-host/ (agents, hooks, skills, CLAUDE.md)
       claude-sync.sh tier basic               # stop syncing memory here (TIER= in machines/<hostname>.conf)
  tier: basic syncs CLAUDE.md, settings.json, agents, hooks and skills; full (the default) adds memory.
  'tier full' needs the primary project path once (PROJECT_PATH) if the conf has none.
"""

# Commands that capture, commit or push: only on the default branch.
_BRANCH_BOUND = ("push", "sync", "bootstrap", "override", "profile", "tier")
_SIGNAL_EXITS = {129: "SIGHUP", 130: "Ctrl-C", 143: "SIGTERM"}


class _Reported(SystemExit):
    """A nested run's signal exit that run already reported."""


def main(argv: list[str], env: Mapping[str, str]) -> int:
    """Runs one command; returns its exit code. Never raises (a signal's
    SystemExit becomes its code). Every failure prints what to do on stderr."""
    try:
        return _main(list(argv), env)
    except UsageError:
        sys.stderr.write(USAGE)
        return 1
    except Fail as err:
        # Redacted once here: a hint often carries git's own stderr.
        print(f"claude-sync: {redact(err.message)}", file=sys.stderr)
        if err.hint:
            print(redact(err.hint), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return _stopped(130)
    except _Reported as exc:
        return int(exc.code)
    except SystemExit as exc:
        # materialise / do_apply end a signalled run this way, and the
        # SIGTERM / SIGHUP handlers of __main__ do too.
        if exc.code is None or isinstance(exc.code, int):
            return _stopped(exc.code) if exc.code else 0
        print(f"claude-sync: {exc.code}", file=sys.stderr)
        return 1
    except OSError as err:
        print(f"claude-sync: {err}", file=sys.stderr)
        print("Check the path named above exists and is readable and writable, then re-run.", file=sys.stderr)
        return 1
    except Exception as err:  # noqa: BLE001 - the last line of defence: no traceback
        print(f"claude-sync: unexpected error: {type(err).__name__}: {err}", file=sys.stderr)
        print(
            "This is a bug in claude-sync. Nothing more was changed. Report it with the command you"
            " ran and this message, then use 'claude-sync.sh doctor' to check the clone.",
            file=sys.stderr,
        )
        return 1


def _stopped(code: int) -> int:
    """bash on_exit: a run ended by a signal says so on stderr."""
    by = _SIGNAL_EXITS.get(code)
    what = f"stopped by {by}" if by else "stopped"
    print(
        f"claude-sync: {what} (exit {code}). Re-run the command; 'claude-sync.sh doctor'"
        " checks the clone if anything looks wrong.",
        file=sys.stderr,
    )
    return code


def repo_dir(env: Mapping[str, str]) -> Path:
    """CLAUDE_SYNC_REPO_DIR, else the clone this package lives in (bash:
    the folder claude-sync.sh is in)."""
    return Path(env.get("CLAUDE_SYNC_REPO_DIR") or Path(__file__).resolve().parent.parent)


def make_ctx(repo: Path, env: Mapping[str, str]) -> SyncCtx:
    """The run's SyncCtx, with its PR state answered by GitHub."""
    claude = env.get("CLAUDE_SYNC_CLAUDE_HOME") or str(Path.home() / ".claude")
    agents = env.get("CLAUDE_SYNC_AGENTS_HOME") or str(Path.home() / ".agents")
    host = env.get("CLAUDE_SYNC_HOSTNAME") or socket.gethostname()
    # The same rule install uses: the name ends up in machines/<host>.* paths.
    if not profile.profile_valid_name(host):
        raise Fail(
            f"Not a valid hostname: {host}",
            hint="Set CLAUDE_SYNC_HOSTNAME to a name of letters, digits, '.', '_' and '-', then re-run.",
        )
    ctx = SyncCtx(
        repo=repo,
        claude_home=Path(claude),
        agents_home=Path(agents),
        hostname=host,
        lock_wait=lock_wait_from_env(env),
    )
    pr.bind_pr_state(ctx)
    return ctx


def _skip_message(repo: Path) -> str:
    head = run(["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
    return (
        "claude-sync: sync SKIPPED, nothing was captured, pushed or applied.\n"
        f"  Why: claude-config is on branch '{head}', not its default branch"
        f" '{gitflow.default_branch(repo)}',\n"
        "  and syncing there would commit local config onto that branch."
        " Switch back to the default branch to sync."
    )


def _main(argv: list[str], env: Mapping[str, str]) -> int:
    repo = repo_dir(env)
    # A worktree's .git is a file, so any .git counts (bash: -d).
    if not (repo / ".git").exists():
        raise Fail(f"No git repo at {repo}. Run install.sh first.")
    cmd, args = (argv[0], argv[1:]) if argv else ("sync", [])
    sub = args[0] if args else ""

    # The capture and PR flow only runs with the clone on its default
    # branch. Any other branch means this clone is being used to develop
    # the sync tool itself, so skip rather than mix local config into that
    # work. profile list/show and tier show only read, so they work on any branch.
    read_only = (cmd == "profile" and (sub or "list") in ("list", "show")) or (
        cmd == "tier" and (sub or "show") == "show"
    )
    if cmd in _BRANCH_BOUND and not read_only and not gitflow.on_default_branch(repo):
        print(_skip_message(repo), file=sys.stderr)
        return 0

    ctx = make_ctx(repo, env)
    with ExitStack() as stack:
        if cmd == "push":
            gitflow.begin_sync(ctx, stack)
            gitflow.do_capture_and_commit(ctx)
            gitflow.do_squash(ctx)
            pr.do_push(ctx)
        elif cmd == "pull":
            if gitflow.on_default_branch(repo) and gitflow.has_origin(repo):
                gitflow.begin_sync(ctx, stack)
                gitflow.do_integrate(ctx)
            else:
                gitflow.integrate_current_branch(ctx)
            gitflow.do_apply(ctx)
        elif cmd == "sync":
            gitflow.begin_sync(ctx, stack)
            gitflow.do_capture_and_commit(ctx)
            doctor.finish_sync(ctx)
        elif cmd == "bootstrap":
            gitflow.begin_sync(ctx, stack)
            gitflow.do_integrate(ctx)
            gitflow.do_apply_merge(ctx)
            gitflow.do_capture_and_commit(ctx)
            gitflow.do_squash(ctx)
            pr.do_push(ctx)
        elif cmd == "override":
            _override(ctx, stack, args)
        elif cmd == "profile":
            _profile(ctx, stack, args)
        elif cmd == "tier":
            _tier(ctx, stack, args)
        elif cmd == "doctor":
            return doctor.do_doctor(
                ctx, stack, remediate=sub == "remediate", run_sync=lambda: _nested_sync(env)
            )
        else:
            raise UsageError()
    return 0


def _nested_sync(env: Mapping[str, str]) -> bool:
    """doctor remediate's retry: a whole `sync` run of its own (the bash
    runs claude-sync.sh sync), which prints its own failure. A signal that
    ended it ends the doctor too."""
    rc = main(["sync"], env)
    if rc in _SIGNAL_EXITS:
        raise _Reported(rc)
    return rc == 0


def _override(ctx: SyncCtx, stack: ExitStack, args: list[str]) -> None:
    action = args[0] if args else "list"
    expr = args[1] if len(args) > 1 else ""
    if action == "list":
        doctor.override_list(ctx)
    elif action == "add":
        doctor.override_add(ctx, stack, expr)
    elif action == "remove":
        doctor.override_remove(ctx, stack, expr)
    else:
        raise UsageError()


def _profile(ctx: SyncCtx, stack: ExitStack, args: list[str]) -> None:
    action = args[0] if args else "list"
    name = args[1] if len(args) > 1 else ""
    if action == "list":
        doctor.profile_list(ctx)
    elif action == "show":
        doctor.profile_show(ctx, name)
    elif action == "use":
        doctor.profile_use(ctx, stack, name)
    elif action == "copy":
        doctor.profile_copy(ctx, stack, args[1:])
    else:
        raise UsageError()


def _tier(ctx: SyncCtx, stack: ExitStack, args: list[str]) -> None:
    action = args[0] if args else "show"
    if action == "show" and len(args) <= 1:
        doctor.tier_show(ctx)
    elif action == "basic" and len(args) == 1:
        doctor.tier_use(ctx, stack, action)
    elif action == "full" and len(args) <= 2:
        doctor.tier_use(ctx, stack, action, args[1] if len(args) > 1 else None)
    else:
        raise UsageError()
