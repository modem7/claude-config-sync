"""The command line: `python -m claude_sync [COMMAND ...]`.

Port of main() and usage() in claude-sync.sh. `main` only works out the
run's settings, picks the command and turns how it ended into an exit
code; the work is in gitflow, pr and doctor.

    push | pull | sync | bootstrap | doctor [remediate | install-plugins [--yes]]
    override add|remove|list [PATH]
    profile list | show NAME | copy FROM [--force] [--yes] | use NAME
    tier [show | basic | full [PROJECT_PATH]]
    status --line [--json]

No command means `sync`. Settings come from `env` (CLAUDE_SYNC_REPO_DIR,
_CLAUDE_HOME, _AGENTS_HOME, _HOSTNAME, _LOCK_WAIT, _BACKUPS, _PR_DAYS). pr.py does not see
`env`: it reads CLAUDE_SYNC_GH and CLAUDE_SYNC_GH_REPO from os.environ at
call time, and util.redact (used by pr and gitflow) reads GH_TOKEN,
GITHUB_TOKEN, GH_ENTERPRISE_TOKEN and GITHUB_ENTERPRISE_TOKEN the same way. `python -m claude_sync` passes os.environ,
so the two agree; an in-process caller must put those in os.environ.
"""

from __future__ import annotations

import json
import os
import socket
import sys
from collections.abc import Mapping
from contextlib import ExitStack
from pathlib import Path

from claude_sync import doctor, gitflow, machinecmd, machineconf, pr, profile, status, syncfiles, tools
from claude_sync.gitflow import SyncCtx
from claude_sync.lock import lock_wait_from_env
from claude_sync.util import Fail, UsageError, is_remote, redact, run

# bash usage(), except that its two flock lines became one: there is no
# CLAUDE_SYNC_FLOCK and no weaker fallback lock any more.
USAGE = """\
Usage: claude-sync.sh [push|pull|remote [--plugins] [--tools]|sync|bootstrap|doctor [remediate|install-plugins [--yes]]
                       |override add|remove|list [JQ_PATH]
                       |profile list|show NAME|copy FROM [--force] [--yes]|use NAME
                       |tier [show|basic|full [PROJECT_PATH]]|machine NAME ...|status --line [--json]]
  sync       capture local changes onto sync/<hostname>, merge the default branch in, apply, push, open/update its PR
  push       capture onto sync/<hostname>, push it and open/update its PR (no apply)
  pull       bring in the default branch (keeping this machine's unmerged changes) and apply
  remote     for a throwaway cloud session: apply the checkout as it is (no fetch, no push) as machine
             'cloud' (CLAUDE_SYNC_HOSTNAME to rename), basic tier, creating machines/<hostname>.conf if missing
             --plugins then runs 'doctor install-plugins --yes'; --tools installs gh and ccusage. Both opt-in.
             remote needs a cloud session (CLAUDE_CODE_REMOTE=true, or CLAUDE_SYNC_REMOTE=1) and is refused on a machine you own.
             There sync and pull run as remote, and push, bootstrap, override, profile, tier, machine (except 'machine NAME show')
             and the installer are refused. CLAUDE_SYNC_REMOTE=0 turns this off.
  Nothing is ever pushed to the default branch; changes reach other machines when the PR is merged.
  The PR step needs gh, logged in. CLAUDE_SYNC_GH=<path> picks the gh binary;
  CLAUDE_SYNC_GH_REPO=owner/repo sets the PR's repo when origin's URL doesn't show it (it must match
  the repo origin's push URL names, which is the one checked to be private before each push).
  Needs git 2.40+. A second run waits CLAUDE_SYNC_LOCK_WAIT seconds (default 10) for the first.
  Before apply overwrites or deletes a local file that differs, it copies it to
  ~/.claude/.claude-sync-backups/<UTC time>/. CLAUDE_SYNC_BACKUPS=N keeps the newest N (default 10; 0: none).
  e.g. claude-sync.sh override add '.enabledPlugins["name@marketplace"]'
       claude-sync.sh profile use other-host   # follow machines/other-host/ (agents, hooks, skills, CLAUDE.md)
       claude-sync.sh tier basic               # stop syncing memory here (TIER= in machines/<hostname>.conf)
  tier: basic syncs CLAUDE.md, settings.json, agents, hooks and skills; full (the default) adds memory.
  'tier full' needs the primary project path once (PROJECT_PATH) if the conf has none.
  machine NAME show                       show another machine's tier, profile, plugins and settings overrides
  machine NAME tier basic|full [PATH]     set its tier        machine NAME profile use PROFILE
  machine NAME plugin enable|disable|inherit PLUGIN@MARKET     choose its plugins (inherit: follow shared)
  machine NAME override set JQ_PATH JSON | unset JQ_PATH       any other settings key, for that machine only
  machine NAME plugins full|off           full: sync, pull and remote install the plugins its settings.json enables
             Edits the repo only, through this machine's sync branch and PR. NAME can be a machine that is off,
             or 'cloud' (what 'remote' runs as; an environment can set CLAUDE_SYNC_HOSTNAME to be another).
  status --line  one line when this machine needs attention (last hook sync failed, a doctor
             problem, its PR waiting over CLAUDE_SYNC_PR_DAYS days (default 3; 0: off), a tier change),
             else nothing. Local only (no fetch, no gh, no lock); always exits 0. The SessionStart hook runs it.
             --json prints the line as one hook JSON object (systemMessage for you, additionalContext for Claude).
  doctor install-plugins  run the claude plugin commands doctor lists (marketplaces first), after a
             y/N prompt; --yes skips it, no terminal and no --yes is refused. Stops at the first failure.
             Plain doctor never installs anything. PLUGINS=full in machines/<hostname>.conf runs it with --yes
             after each sync, pull and remote (so also after doctor remediate's sync); a failure there only warns.
"""

# Commands that capture, commit or push: only on the default branch.
_BRANCH_BOUND = ("push", "sync", "bootstrap", "override", "profile", "tier", "machine")
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
        backups=syncfiles.backups_from_env(env),
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
    if is_remote(env) and not env.get("CLAUDE_SYNC_HOSTNAME"):
        # A container's hostname is random: every command there is the one machine "cloud".
        env = {**env, "CLAUDE_SYNC_HOSTNAME": REMOTE_HOSTNAME}
    if argv and argv[0] == "status":
        return _status(argv[1:], env)
    repo = repo_dir(env)
    # A worktree's .git is a file, so any .git counts (bash: -d).
    if not (repo / ".git").exists():
        raise Fail(f"No git repo at {repo}. Run install.sh first.")
    cmd, args = (argv[0], argv[1:]) if argv else ("sync", [])
    offline = False  # `remote`: apply the checkout as cloned, never fetch
    if is_remote(env) and cmd in ("sync", "pull"):
        cmd, args = "remote", []  # a cloud session never captures or pushes
    extras: list[str] = []  # `remote --plugins --tools`: opt-in installs after the apply
    if cmd == "remote":
        if any(a not in ("--plugins", "--tools") for a in args):
            raise UsageError()
        if not is_remote(env):
            raise Fail(
                "'remote' is for a throwaway cloud session, and this looks like a machine you own:"
                " it would replace this machine's config with the 'cloud' machine's.",
                hint="Use 'sync' or 'pull' here. In a cloud setup script, set CLAUDE_SYNC_REMOTE=1"
                " on the command (the README script does).",
            )
        extras, args = args, []
        cmd, offline = "pull", True
        env = _remote_env(repo, env)
    sub = args[0] if args else ""

    # The capture and PR flow only runs with the clone on its default
    # branch. Any other branch means this clone is being used to develop
    # the sync tool itself, so skip rather than mix local config into that
    # work. profile list/show and tier show only read, so they work on any branch.
    read_only = (cmd == "profile" and (sub or "list") in ("list", "show")) or (
        cmd == "tier" and (sub or "show") == "show"
    )
    read_only = read_only or (cmd == "machine" and machinecmd.read_only(args))
    if is_remote(env) and cmd in _BRANCH_BOUND and not read_only:
        raise Fail(
            f"'{cmd}' would push a branch from a throwaway cloud session, so it is refused.",
            hint="Run it on a machine you own. To override (testing only), set CLAUDE_SYNC_REMOTE=0.",
        )
    if cmd in _BRANCH_BOUND and not read_only and not gitflow.on_default_branch(repo):
        print(_skip_message(repo), file=sys.stderr)
        return 0

    ctx = make_ctx(repo, env)
    # The plugin installs run after the `with` has released the sync lock,
    # so it is not held while `claude plugin install` fetches. Each is armed
    # only once the apply is done ("auto": PLUGINS=full, never fails the run).
    plugins = {"auto": False, "explicit": False}

    def arm_auto() -> None:
        plugins["auto"] = True

    try:
        with ExitStack() as stack:
            if cmd == "push":
                gitflow.begin_sync(ctx, stack)
                gitflow.do_capture_and_commit(ctx)
                gitflow.do_squash(ctx)
                pr.do_push(ctx)
            elif cmd == "pull":
                if offline:
                    pass  # `remote`: no fetch and no rebase, whatever the branch or upstream
                elif gitflow.on_default_branch(repo) and gitflow.has_origin(repo):
                    gitflow.begin_sync(ctx, stack)
                    gitflow.do_integrate(ctx)
                else:
                    gitflow.integrate_current_branch(ctx)
                gitflow.do_apply(ctx)
                if "--tools" in extras:  # gh and ccusage; the pip packages only serve agent-memory
                    tools.install_gh_tools()
                    tools.install_npm_tools()
                if "--plugins" in extras:  # the config is already applied if this fails
                    plugins["explicit"] = True
                else:
                    arm_auto()
            elif cmd == "sync":
                gitflow.begin_sync(ctx, stack)
                gitflow.do_capture_and_commit(ctx)
                doctor.finish_sync(ctx, applied=arm_auto)
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
            elif cmd == "machine":
                machinecmd.machine(ctx, stack, args)
            elif cmd == "doctor" and sub == "install-plugins":
                return doctor.install_plugins(ctx, args[1:])
            elif cmd == "doctor":
                return doctor.do_doctor(
                    ctx,
                    stack,
                    remediate=sub == "remediate",
                    run_sync=lambda: _nested_sync(env),
                    offline=is_remote(env),
                )
            else:
                raise UsageError()
    except Fail:
        # A failed push or PR still leaves the config applied: install, then
        # fail with the original error and exit code.
        if plugins["auto"]:
            doctor.auto_install_plugins(ctx)
        raise
    if plugins["explicit"]:
        return doctor.install_plugins(ctx, ["--yes"])
    if plugins["auto"]:
        doctor.auto_install_plugins(ctx)
    return 0


REMOTE_HOSTNAME = "cloud"


def _remote_env(repo: Path, env: Mapping[str, str]) -> Mapping[str, str]:
    """`remote`: a throwaway cloud container has a random hostname and no
    machines/<hostname>.conf, so every one is the same machine ("cloud") on
    the basic tier. The conf is written only when missing and is not
    committed: `remote` is a pull and never captures or pushes."""
    env = {**env, "CLAUDE_SYNC_HOSTNAME": env.get("CLAUDE_SYNC_HOSTNAME") or REMOTE_HOSTNAME}
    host = env["CLAUDE_SYNC_HOSTNAME"]
    if profile.profile_valid_name(host):  # make_ctx reports an invalid one
        conf = machineconf.machine_conf_path(repo, host)
        if not os.path.lexists(conf):  # a link, even a dangling one, is never written through
            conf.parent.mkdir(parents=True, exist_ok=True)
            conf.write_bytes(b"TIER=basic\n")
            _exclude_locally(repo, f"machines/{host}.conf")
    return env


def _exclude_locally(repo: Path, rel: str) -> None:
    """Keep a file `remote` wrote out of this clone's `git add -A`, so it is
    never committed by a later sync or lands in a developer's PR. Best effort."""
    try:
        exclude = repo / ".git" / "info" / "exclude"
        if exclude.parent.is_dir():
            text = exclude.read_text() if exclude.exists() else ""
            if f"/{rel}" not in text.splitlines():
                exclude.write_text(text + ("" if not text or text.endswith("\n") else "\n") + f"/{rel}\n")
    except OSError:
        pass


def _status(args: list[str], env: Mapping[str, str]) -> int:
    """status --line [--json]: read-only, on any branch, never fails the
    hook. A problem becomes the line itself rather than a stderr message.
    --json wraps the line as a SessionStart hook object, so the user sees
    it (systemMessage) as well as Claude (additionalContext); plain hook
    stdout reaches Claude only. Healthy prints nothing either way."""
    if args not in (["--line"], ["--line", "--json"]):
        raise UsageError()
    repo = repo_dir(env)
    if not (repo / ".git").exists():
        line = status.one_line([f"no git repo at {repo}. Run install.sh first."])
    else:
        try:
            ctx = make_ctx(repo, env)
        except Fail as err:
            line = status.one_line([f"the status check failed: {err.message}"])
        else:
            try:
                line = status.status_line(ctx, env)
            except Exception as err:  # noqa: BLE001 - the hook must still get its one line
                line = status.one_line(
                    [f"the status check hit an unexpected error ({type(err).__name__}). Run 'claude-sync.sh doctor'."]
                )
    if line and "--json" in args:
        print(hook_json(line))
    elif line:
        print(line)
    return 0


def hook_json(line: str) -> str:
    """The line as one SessionStart hook JSON object, ASCII only."""
    return json.dumps(
        {
            "systemMessage": line,
            "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": line},
        }
    )


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
