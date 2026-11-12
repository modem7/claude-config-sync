"""The installer: `python -m claude_sync.install [--repair|--reinstall]
[--profile NAME] [--project-path PATH]`.

Port of install.sh. It clones the sync repo (CLAUDE_SYNC_REPO_URL, else a
prompt on a terminal) into CLAUDE_SYNC_REPO_DIR (default ~/claude-config),
provisions the companion tools (claude_sync.tools), writes
machines/<hostname>.conf on a new machine, then hands over to the sync
tool: `bootstrap` for a new machine, `sync` when the conf already existed.
That first sync goes out on sync/<hostname> with its PR, like any other;
nothing is ever pushed to the default branch.

--profile NAME (or CLAUDE_SYNC_PROFILE) makes a new machine follow
machines/NAME/: PROFILE=NAME goes into the new conf. --project-path PATH
(else CLAUDE_SYNC_PROJECT_PATH, else a prompt that defaults to
~/project) is the project whose memory is synced. On Windows it is stored
in the native drive form (`Z:\\projects`; a Git Bash `/z/projects` is
converted), and a project path that does not exist falls back to the home
directory (resolve_project_path).
That fallback is Windows-only on purpose: elsewhere the path is stored as
given, as install.sh did, even when it does not exist yet.

--repair (alias --reinstall) resets this machine's own sync bookkeeping:
the local clone, its machines/<hostname>.conf (rewritten; the change goes
out through this machine's sync PR) and the skill links this tool made.
Live config (~/.claude, ~/.agents/skills, memory), profiles and settings
overrides are kept. It refuses while the clone holds a pending sync PR's
changes or commits that are not on origin.

Line endings: the clone is made with `git clone -c core.autocrlf=false`,
which stays in the clone's config, so git never adds CRLF to synced files
(Git for Windows usually sets autocrlf=true system-wide). A clone made
before this, by hand or by install.sh, keeps the CRLF it was checked out
with: on Windows, re-check it out once (`git -C <clone> config
core.autocrlf false`; then, only when `git -C <clone> status --porcelain`
prints nothing, i.e. its sync PR is merged and synced, since a pending PR's
changes sit unstaged in the working tree, remove its files and
`git checkout -- .`), or simply run `install --repair` once that PR is
merged, which checks this for you.
"""

from __future__ import annotations

import sys

if __name__ == "__main__" and sys.version_info < (3, 12):
    # Before the package imports: they use 3.12 APIs.
    print("claude-sync needs Python 3.12 or newer.", file=sys.stderr)
    sys.exit(1)

import os
import shutil
import socket
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from claude_sync import cli, machineconf, profile, skilllinks, tools
from claude_sync.gitflow import _git
from claude_sync.syncfiles import _remove
from claude_sync.util import Fail, check_real_dir, printable, redact, run

IS_WINDOWS = os.name == "nt"

USAGE = "Usage: install.sh [--repair|--reinstall] [--profile NAME] [--project-path PATH]"
_TOOL_STEPS = ("install_npm_tools", "install_gh_tools", "install_python_tools")


@dataclass
class _Args:
    repair: bool = False
    profile: str = ""
    project_path: str | None = None


def resolve_project_path(preferred: str | None, home: Path) -> str:
    """`preferred` when it is an existing directory, else the home directory."""
    if preferred and Path(preferred).is_dir():
        return preferred
    return str(home)


def main(argv: list[str], env: Mapping[str, str]) -> int:
    """Runs the installer; returns its exit code. Every failure prints what
    to do on stderr; no traceback."""
    try:
        return _main(list(argv), env)
    except Fail as err:
        print(err.message, file=sys.stderr)
        if err.hint:
            print(err.hint, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("install: stopped by Ctrl-C (exit 130). Re-run it to finish.", file=sys.stderr)
        return 130
    except SystemExit as exc:  # the SIGTERM / SIGHUP handlers, or a signal during apply
        if exc.code is None or isinstance(exc.code, int):
            if exc.code:
                print(f"install: stopped (exit {exc.code}). Re-run it to finish.", file=sys.stderr)
            return exc.code or 0
        print(f"install: {exc.code}", file=sys.stderr)
        return 1
    except OSError as err:
        print(f"install: {err}", file=sys.stderr)
        print("Check the path named above exists and is readable and writable, then re-run.", file=sys.stderr)
        return 1
    except Exception as err:  # noqa: BLE001 - the last line of defence: no traceback
        print(f"install: unexpected error: {type(err).__name__}: {err}", file=sys.stderr)
        print("This is a bug in claude-sync. Report it with the command you ran and this message.", file=sys.stderr)
        return 1


def _parse(argv: list[str], env: Mapping[str, str]) -> _Args:
    args = _Args(profile=env.get("CLAUDE_SYNC_PROFILE", ""))
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("--repair", "--reinstall"):
            args.repair = True
        elif arg in ("--profile", "--project-path"):
            if i + 1 >= len(argv):
                what = "a profile name" if arg == "--profile" else "a path"
                raise Fail(f"{arg} needs {what}.", hint=USAGE)
            i += 1
            if arg == "--profile":
                args.profile = argv[i]
            else:
                args.project_path = argv[i]
        elif arg.startswith("--profile="):
            args.profile = arg.removeprefix("--profile=")
        elif arg.startswith("--project-path="):
            args.project_path = arg.removeprefix("--project-path=")
        else:
            raise Fail(f"Unknown option: {arg}", hint=USAGE)
        i += 1
    if args.profile and not profile.profile_valid_name(args.profile):
        raise Fail(
            f"Not a valid profile name: {args.profile}",
            hint="Use only letters, digits, '.', '_' and '-' (not '.' or '..').",
        )
    return args


def _home(env: Mapping[str, str]) -> Path:
    keys = ("USERPROFILE", "HOME") if IS_WINDOWS else ("HOME",)
    for key in keys:
        if env.get(key):
            return Path(env[key])
    return Path.home()


def _ask(prompt: str) -> str:
    """One line from stdin; "" at end of input. The prompt shows only on a
    terminal, as bash `read -p` does."""
    if sys.stdin.isatty():
        print(prompt, end="", file=sys.stderr, flush=True)
    return sys.stdin.readline().rstrip("\r\n")


def _repo_url(env: Mapping[str, str]) -> str:
    if env.get("CLAUDE_SYNC_REPO_URL"):
        return env["CLAUDE_SYNC_REPO_URL"]
    if sys.stdin.isatty():
        url = _ask(
            "Git URL of your claude-config sync repo (create an empty one on GitHub/GitLab/etc first): "
        ).strip()
        if url:
            return url
        raise Fail("A repo URL is required. Re-run with CLAUDE_SYNC_REPO_URL=<url> set.")
    raise Fail(
        "CLAUDE_SYNC_REPO_URL is not set and no terminal is attached to prompt for it.",
        hint="Re-run with: CLAUDE_SYNC_REPO_URL=<your sync repo url> set in the environment.",
    )


def _clone(url: str, repo: Path) -> None:
    # `clone -c` writes the setting into the new clone's config before the
    # checkout, so it holds for every later git command too. `--` keeps a
    # URL that starts with "-" from being read as an option.
    result = run(["git", "clone", "-q", "-c", "core.autocrlf=false", "--", url, str(repo)])
    if result.returncode != 0:
        raise Fail(
            f"Could not clone the sync repo into {repo}.",
            hint=(redact(result.stderr).rstrip("\n") + "\n" if result.stderr.strip() else "")
            + "  Check CLAUDE_SYNC_REPO_URL and that you can reach it (for GitHub: 'gh auth login'"
            " or an SSH key), then re-run.",
        )


def _leave(repo: Path, home: Path) -> None:
    """Deleting the clone would pull the folder out from under a process
    whose working directory is inside it (and Windows cannot delete it)."""
    try:
        cwd = Path.cwd().resolve()
    except OSError:
        cwd = None
    if cwd is None or cwd.is_relative_to(repo.resolve()):
        os.chdir(home if home.is_dir() else Path(home.anchor or os.sep))


def _check_removable(repo: Path, home: Path, live: tuple[Path, ...]) -> None:
    if repo.is_symlink() or repo.is_junction():
        raise Fail(
            f"Refusing to repair: {repo} is a link.",
            hint="Point CLAUDE_SYNC_REPO_DIR at the clone itself, then re-run.",
        )
    real = repo.resolve()
    if real == home.resolve() or any(p.resolve().is_relative_to(real) for p in live):
        raise Fail(
            f"Refusing to repair: {repo} is your home folder or holds your live config.",
            hint="Point CLAUDE_SYNC_REPO_DIR at the claude-config clone, then re-run.",
        )
    if not (repo / ".git").exists():
        if repo.is_dir() and any(repo.iterdir()):
            raise Fail(
                f"Refusing to repair: {repo} is not a git clone, and it is not empty.",
                hint="Point CLAUDE_SYNC_REPO_DIR at the clone, or move this folder out of the way, then re-run.",
            )
        return
    status = _git(repo, "status", "--porcelain")
    if status.returncode != 0 or status.stdout.strip():
        raise Fail(
            f"Refusing to repair: {repo} has changes that are not on its default branch.",
            hint="  The usual cause is this machine's pending sync PR (sync/<hostname>): the clone holds its changes.\n"
            "  Merge that PR (or close it and delete the branch), run 'claude-sync.sh sync', then re-run this.\n"
            "  Do not use git stash or git reset --hard in the clone: that hides or loses those changes.",
        )
    upstream = _git(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if upstream.returncode == 0 and _git(repo, "log", "@{u}..HEAD", "--oneline").stdout.strip():
        raise Fail(
            f"Refusing to repair: {repo} has commits on its default branch that are not on origin.",
            hint="  Run 'claude-sync.sh sync' to move them onto sync/<hostname> and open a PR (never push them\n"
            "  to the default branch), then merge or close that PR and re-run this.",
        )


def _repair(url: str, repo: Path, host: str, home: Path, claude: Path, agents: Path) -> None:
    print(f"Repairing: resetting local sync state for {host}...")
    _leave(repo, home)
    if os.path.lexists(repo):
        _check_removable(repo, home, (claude, agents))
        _remove(repo)
    _clone(url, repo)
    # Only from the working tree: the new conf written later goes out with
    # this machine's sync PR, so nothing is pushed to the default branch.
    check_real_dir(repo / "machines")
    machineconf.machine_conf_path(repo, host).unlink(missing_ok=True)
    skilllinks.remove_skill_links(claude, agents)


def _run_tools() -> None:
    """Every step, every run; a failing one never stops the install."""
    for name in _TOOL_STEPS:
        try:
            getattr(tools, name)()
        except Exception as err:  # noqa: BLE001 - bash `step || true`
            print(f"{name.removeprefix('install_')} step failed: {err} — continuing.", file=sys.stderr)


def _gh_notes(env: Mapping[str, str], host: str) -> None:
    """Only a hint: without gh, syncs still push the branch, then fail loudly."""
    gh = env.get("CLAUDE_SYNC_GH") or "gh"
    has_sep = os.sep in gh or (os.altsep and os.altsep in gh)
    if not (Path(gh).is_file() if has_sep else shutil.which(gh)):
        print(
            "Note: the GitHub CLI (gh) is not installed. claude-sync.sh needs it to open its sync PRs;\n"
            f"  without it every sync pushes sync/{host} and then fails. Install gh, then run 'gh auth login'.",
            file=sys.stderr,
        )
    elif run([gh, "auth", "status"], env={"GH_PROMPT_DISABLED": "1"}).returncode != 0:
        print("Note: gh is not logged in, so claude-sync.sh can't open its sync PRs. Run 'gh auth login'.", file=sys.stderr)


def _check_project(value: str) -> None:
    """The path goes into one line of the conf: a line break would add lines."""
    if any(c in value for c in "\r\n\0"):
        raise Fail(
            "The project path has a line break or NUL character in it.",
            hint="Give the path on one line (--project-path or CLAUDE_SYNC_PROJECT_PATH), then re-run.",
        )


def _project_path(args: _Args, env: Mapping[str, str], home: Path) -> str:
    default = str(home / "project")
    chosen = args.project_path or env.get("CLAUDE_SYNC_PROJECT_PATH") or ""
    if not chosen:
        chosen = _ask(f"Primary Claude Code working directory on this machine [{default}]: ") or default
    _check_project(chosen)
    if IS_WINDOWS:
        # Stored in the native form; a Git Bash `/z/...` is converted first,
        # or is_dir() would look on the current drive.
        chosen = machineconf.windows_native_path(chosen)
        resolved = resolve_project_path(chosen, home)
        if resolved != chosen:
            print(
                f"The project path {printable(chosen)} does not exist;"
                f" using your home folder {printable(resolved)} instead."
            )
        chosen = resolved
    return chosen


def _main(argv: list[str], env: Mapping[str, str]) -> int:
    args = _parse(argv, env)
    url = _repo_url(env)
    home = _home(env)
    repo = Path(env.get("CLAUDE_SYNC_REPO_DIR") or home / "claude-config")
    host = env.get("CLAUDE_SYNC_HOSTNAME") or socket.gethostname()
    claude = Path(env.get("CLAUDE_SYNC_CLAUDE_HOME") or home / ".claude")
    agents = Path(env.get("CLAUDE_SYNC_AGENTS_HOME") or home / ".agents")
    if not profile.profile_valid_name(host):
        raise Fail(
            f"Not a valid hostname: {host}",
            hint="Set CLAUDE_SYNC_HOSTNAME to a name of letters, digits, '.', '_' and '-', then re-run.",
        )

    _check_project(args.project_path or env.get("CLAUDE_SYNC_PROJECT_PATH") or "")

    if args.repair:
        _repair(url, repo, host, home, claude, agents)
    if not (repo / ".git").exists():
        _clone(url, repo)

    _run_tools()
    _gh_notes(env, host)

    check_real_dir(repo / "machines")
    conf = machineconf.machine_conf_path(repo, host)
    existed = conf.is_file()
    if args.profile and existed:
        print(
            f"machines/{host}.conf already exists, so --profile is ignored."
            f" Use: claude-sync.sh profile use {args.profile}",
            file=sys.stderr,
        )
    elif args.profile and args.profile != host and not profile.profile_dir(repo, args.profile).is_dir():
        raise Fail(f"No profile called '{args.profile}': machines/{args.profile}/ does not exist in the repo.")

    if not existed:
        machineconf.write_machine_conf(repo, host, _project_path(args, env, home))
        if args.profile and args.profile != host:
            machineconf.write_machine_profile(repo, host, args.profile)

    child = {
        **env,
        "CLAUDE_SYNC_REPO_DIR": str(repo),
        "CLAUDE_SYNC_HOSTNAME": host,
        "CLAUDE_SYNC_CLAUDE_HOME": str(claude),
        "CLAUDE_SYNC_AGENTS_HOME": str(agents),
    }
    return cli.main(["sync" if existed else "bootstrap"], child)


if __name__ == "__main__":
    from claude_sync.__main__ import install_signal_handlers

    install_signal_handlers()
    sys.exit(main(sys.argv[1:], os.environ))
