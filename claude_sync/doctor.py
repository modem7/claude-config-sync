"""`claude-sync doctor [remediate | install-plugins [--yes]]`: diagnose (and
repair) a stuck clone, or install the plugins settings.json enables.

Port of do_doctor and doctor_profile in claude-sync.sh. A claude-sync.sh
from before sync branches committed captures on the default branch; if a
rebase onto upstream then conflicted, those commits stayed local and every
later sync repeated the failure. Today's sync moves such commits onto
sync/<hostname> instead, so remediation is a normal sync, which fails
loudly if they really conflict.

Issues go to stdout as "ISSUE: ..." lines; do_doctor returns 1 when it
printed any (even if remediate fixed them), else 0.

Also here (they cannot live in overlay.py or profile.py, which gitflow
imports): the repo-changing `override`, `profile` and `tier` subcommands,
ports of do_override, do_profile and do_profile_copy, and finish_sync, the tail
they share with `sync`. Each takes the run's ExitStack, which holds the
lock begin_sync takes until the run ends.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, field

from claude_sync import gitflow, machineconf, overlay, pr, profile, syncfiles
from claude_sync.gitflow import SyncCtx
from claude_sync.memorypath import long_name_warning
from claude_sync.syncfiles import _remove
from claude_sync.util import Fail, UsageError, printable, run


class _Report:
    def __init__(self) -> None:
        self.issues = 0
        self.drift = False  # live config differs from the repo

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
    has_conf = machineconf.machine_conf_path(ctx.repo, ctx.hostname).is_file()
    tier = gitflow.current_tier(ctx) if has_conf else machineconf.DEFAULT_TIER
    if has_conf:
        print(_tier_line(ctx, tier))
    if tier == machineconf.TIER_BASIC:
        # A basic machine needs no project path: its memory is not synced.
        _warn_memory_plugins(ctx)
        _check_settings(ctx, report, remediate)
        doctor_profile(ctx, report, remediate)
    elif project is not None:
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

    if report.drift:
        _note_newest_backup(ctx)

    _check_hook_modes(ctx, report)

    # Report only, never an ISSUE: remediate could not fix it, since doctor
    # never installs anything (only the separate install-plugins does).
    for line in plugin_report(ctx):
        print(line)

    if not report.issues:
        print("No issues found - sync is healthy.")
    if gitflow.has_sync_branch(ctx):
        sb = gitflow.sync_branch(ctx.hostname)
        print(f"Note: {sb} holds this machine's changes that are not merged yet; its PR shares them once merged.")
    return report.issues


def _note_newest_backup(ctx: SyncCtx) -> None:
    """After drift: where the last apply put the local files it replaced,
    in case the drift is that apply discarding a local edit."""
    newest = syncfiles.newest_backup(gitflow.backups_root(ctx))
    if newest is None:
        print("Note: no backups of replaced local files exist (CLAUDE_SYNC_BACKUPS).")
        return
    print(
        f"Note: the newest backup of local files an apply replaced or removed is {printable(str(newest))}"
        " (see docs/runbook.md, 'Restore a file from an apply backup')."
    )


def _tier_line(ctx: SyncCtx, tier: str) -> str:
    if tier == machineconf.TIER_BASIC:
        return (
            f"Sync tier: basic (TIER= in machines/{ctx.hostname}.conf): memory, the python"
            " packages and the npm tools are not synced or installed here."
        )
    line = "Sync tier: full (memory synced)."
    if gitflow.memory_mode(ctx) == gitflow.MEMORY_ADOPT:
        line += (
            " Memory is being adopted after a switch from basic: nothing is deleted from"
            " memory until the next capture has taken in the local memory files."
        )
    return line


# A plugin whose name holds this needs the full tier's pip packages.
_MEMORY_PLUGIN = "agent-memory"


def _warn_memory_plugins(ctx: SyncCtx) -> None:
    """A warning, not an ISSUE (doctor still exits 0): on the basic tier the
    pip packages an agent-memory plugin needs are not installed. Reads the
    live settings.json (shared plus overrides), else the shared one."""
    live = ctx.claude_home / "settings.json"
    path = live if live.is_file() else ctx.repo / "dot-claude" / "settings.json"
    try:
        data = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return
    plugins = data.get("enabledPlugins") if isinstance(data, dict) else None
    if not isinstance(plugins, dict):
        return
    for name in sorted(k for k, v in plugins.items() if v is True and _MEMORY_PLUGIN in k):
        print(
            f"WARNING: settings.json enables {_printable(name)}, which needs the python packages"
            " that only the full tier installs (mcp<2, aiosqlite, pyyaml). Install them by hand,"
            " turn the plugin off on this machine with a settings override, or run"
            " 'claude-sync.sh tier full'."
        )


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


# ── plugins enabled in settings.json but not on this machine ─────────────
# settings.json is synced, the plugin code (~/.claude/plugins/cache) is not.
# So a new machine can have plugins enabled that were never installed. This
# lists the commands to run; doctor never runs them (supply chain). Only
# `doctor install-plugins`, an explicit opt-in that asks first, does.

# Claude Code adds this marketplace itself on the first interactive session,
# so settings.json usually does not declare it.
_OFFICIAL_MARKETPLACE = "claude-plugins-official"
_OFFICIAL_SOURCE = "anthropics/claude-plugins-official"
# installed_plugins.json format versions whose shape this check knows.
_KNOWN_INSTALL_VERSIONS = (1, 2)


class _Shape(Exception):
    """A plugin file this check cannot read or does not recognise."""


def _load_json(path) -> object | None:
    """The parsed file, None when it does not exist. _Shape when it can't
    be read or is not JSON."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as err:
        raise _Shape(f"{path} could not be read ({err.strerror or err})") from err
    try:
        return json.loads(raw)
    except ValueError as err:
        raise _Shape(f"{path} is not valid JSON") from err


def _installed_ids(path) -> set[str]:
    """Plugin ids with an install record. Only the keys of "plugins" are
    used, so both the dict (v1) and list (v2) record forms work."""
    data = _load_json(path)
    if data is None:
        return set()
    if not isinstance(data, dict) or not isinstance(data.get("plugins"), dict):
        raise _Shape(f"{path} has no \"plugins\" object")
    version = data.get("version", 1)
    if version not in _KNOWN_INSTALL_VERSIONS or isinstance(version, bool):
        raise _Shape(f"{path} is format version {repr(version)}, which this check does not know")
    return {k for k in data["plugins"] if isinstance(k, str)}


def _known_marketplaces(path) -> set[str]:
    data = _load_json(path)
    if data is None:
        return set()
    if not isinstance(data, dict):
        raise _Shape(f"{path} is not a JSON object")
    return {k for k in data if isinstance(k, str)}


def _declared_marketplaces(settings: dict) -> dict[str, object]:
    """extraKnownMarketplaces, plus its alias additionalMarketplaces."""
    declared: dict[str, object] = {}
    for key in ("additionalMarketplaces", "extraKnownMarketplaces"):
        value = settings.get(key)
        if isinstance(value, dict):
            declared.update((k, v) for k, v in value.items() if isinstance(k, str))
    # claude.ai marketplaces are recorded elsewhere (known_marketplaces_claudeai.json).
    return {k: v for k, v in declared.items() if not k.startswith("claudeai-")}


def _marketplace_source(entry: object) -> str | None:
    """The `claude plugin marketplace add` argument for a declared entry, or
    None when the CLI can't be given it (it would read it as another type)."""
    source = entry.get("source") if isinstance(entry, dict) else None
    if not isinstance(source, dict):
        return None
    kind = source.get("source")
    if kind == "github":
        repo, ref = source.get("repo"), source.get("ref")
        if not isinstance(repo, str) or not repo:
            return None
        return f"{repo}#{ref}" if isinstance(ref, str) and ref else repo
    if kind == "git":
        url, ref = source.get("url"), source.get("ref")
        if not isinstance(url, str) or not url:
            return None
        # The CLI reads any other https URL as a hosted marketplace.json.
        bare = url.split("#", 1)[0].rstrip("/")
        hosted_repo = bare.startswith(("https://github.com/", "https://gitlab.com/"))
        if not (bare.endswith(".git") or "/_git/" in bare or hosted_repo or "://" not in bare):
            return None
        return f"{url}#{ref}" if isinstance(ref, str) and ref and "#" not in url else url
    if kind == "url":
        url = source.get("url")
        # A .git URL would be cloned, not fetched as a marketplace.json.
        if not isinstance(url, str) or not url or url.split("#", 1)[0].endswith(".git"):
            return None
        return url
    if kind in ("directory", "file"):
        path = source.get("path")
        return path if isinstance(path, str) and path else None
    return None


@dataclass
class _PluginPlan:
    add: dict[str, str] = field(default_factory=dict)  # marketplace name -> its add argument
    missing: list[str] = field(default_factory=list)  # plugin ids enabled, not installed
    notes: list[str] = field(default_factory=list)
    known: set[str] = field(default_factory=set)  # marketplaces this machine has


def _plugin_plan(ctx: SyncCtx) -> _PluginPlan | str | None:
    """What this machine lacks: None without a usable live settings.json, a
    reason (str) when a plugin file can't be read. Never runs anything."""
    try:
        settings = json.loads((ctx.claude_home / "settings.json").read_bytes())
    except (OSError, ValueError):
        return None  # missing or broken: other checks cover settings.json
    if not isinstance(settings, dict):
        return None
    enabled = settings.get("enabledPlugins")
    enabled = enabled if isinstance(enabled, dict) else {}
    declared = _declared_marketplaces(settings)

    plugins_dir = ctx.claude_home / "plugins"
    try:
        installed = _installed_ids(plugins_dir / "installed_plugins.json")
        known = _known_marketplaces(plugins_dir / "known_marketplaces.json")
    except _Shape as err:
        return _printable(str(err))

    plan = _PluginPlan(known=known)
    add, notes, missing = plan.add, plan.notes, plan.missing
    for name in sorted(declared):
        if name in known:
            continue
        source = _marketplace_source(declared[name])
        if source is None:
            notes.append(
                f"marketplace {_printable(name)}: no add command can be given for its source;"
                " Claude Code adds it from settings.json after the next interactive session starts."
            )
        else:
            add[name] = source

    for plugin_id in sorted(k for k, v in enabled.items() if v is True and isinstance(k, str)):
        _, at, market = plugin_id.rpartition("@")
        # "@synced" plugins come from the claude.ai account, not a marketplace.
        if not at or not market or market == "synced" or plugin_id in installed:
            continue
        missing.append(plugin_id)
        if market in known or market in declared:
            continue
        if market == _OFFICIAL_MARKETPLACE:
            add[market] = _OFFICIAL_SOURCE
        else:
            notes.append(
                f"{_printable(plugin_id)}: its marketplace {_printable(market)} is not declared in"
                " settings.json; add that marketplace first."
            )
    return plan


def plugin_report(ctx: SyncCtx) -> list[str]:
    """Lines saying which plugins the live settings.json enables, and which
    marketplaces it declares, that this machine does not have, each with
    the command to run. Empty when everything matches or there is no live
    settings.json. Reads the live file, so a plugin an override turns off is
    not reported. Never runs anything."""
    plan = _plugin_plan(ctx)
    if isinstance(plan, str):
        return [f"Plugin check skipped: {plan}."]
    if plan is None or not (plan.add or plan.missing or plan.notes):
        return []
    add, missing, notes = plan.add, plan.missing, plan.notes
    lines = [
        "Plugins: settings.json enables plugins or declares marketplaces this machine does not have."
        " doctor never installs anything; review and run these yourself"
        " (or run 'claude-sync.sh doctor install-plugins', which asks first):"
    ]
    for name in sorted(add):
        arg = _paste_arg(add[name])
        if arg is None:
            lines.append(
                f"  (add marketplace {_printable(name)} with /plugin marketplace add:"
                " its source can't be pasted safely here)"
            )
        else:
            lines.append(f"  claude plugin marketplace add {arg}")
    for plugin_id in missing:
        arg = _paste_arg(plugin_id)
        if arg is None:
            lines.append(
                f"  (install {_printable(plugin_id)} with /plugin install:"
                " its name can't be pasted safely here)"
            )
        else:
            lines.append(f"  claude plugin install {arg}")
    lines.extend(f"  Note: {note}" for note in notes)
    lines.append(
        "  Inside a session use /plugin marketplace add and /plugin install instead. To keep a plugin"
        " off this machine only, set it to false in ~/.claude/settings.json and run"
        " 'claude-sync.sh override add' on its .enabledPlugins entry."
    )
    return lines


# ── doctor install-plugins: the opt-in that runs those commands ──────────
# Plain doctor and doctor remediate never get here. Each command is shown,
# then run only after a yes (or --yes), with a list argv and no shell.

_YES = ("y", "Y", "yes", "YES")
# cmd.exe reads these even in an argument Python passes to a .cmd file.
_CMD_UNSAFE = set("&|<>^()")


def _install_args(args: list[str]) -> bool:
    """`[--yes]`; anything else is a usage error. Says whether --yes was given."""
    if args not in ([], ["--yes"]):
        raise UsageError()
    return bool(args)


def _unsafe_arg(text: str, via_cmd: bool) -> str | None:
    """Why text can't be run exactly as shown, or None when it can."""
    if _printable(text) != text:
        return "it holds control characters"
    if text.startswith("-"):
        return "it starts with '-', so the CLI would read it as an option"
    if _paste_arg(text) is None or (via_cmd and _CMD_UNSAFE & set(text)):
        return "it holds characters this platform's shell would read"
    return None


def install_plugins(
    ctx: SyncCtx,
    args: list[str],
    *,
    ask: Callable[[str], str] | None = None,
    interactive: bool | None = None,
) -> int:
    """Runs the `claude plugin` commands plugin_report lists: marketplaces
    first, then plugins, stopping at the first failure. Shows them and asks
    first; `--yes` in `args` skips the question, and without a terminal
    (`interactive`, default: stdin is one) it refuses unless --yes. Returns
    0 when every command ran; a failure raises Fail. Entries the report
    only notes, and names that could not be run exactly as shown, are
    listed and not run. Never touches the clone, so takes no lock."""
    yes = _install_args(args)
    plan = _plugin_plan(ctx)
    if isinstance(plan, str):
        raise Fail(
            f"Plugin check skipped: {plan}. Nothing was installed.",
            hint="  Fix or move the file named above (Claude Code may have changed its format), then re-run.",
        )
    if plan is None or not (plan.add or plan.missing):
        print("Plugins: nothing to install; settings.json enables nothing this machine lacks.")
        for note in plan.notes if plan else []:
            print(f"  Note: {note}")
        return 0

    claude = shutil.which("claude")
    if claude is None:
        raise Fail(
            "the claude CLI is not on PATH, so nothing was installed.",
            hint="  Install Claude Code or add it to PATH, then re-run. Inside a session"
            " /plugin marketplace add and /plugin install do the same.",
        )
    via_cmd = os.name == "nt" and claude.lower().endswith((".cmd", ".bat"))

    steps: list[tuple[str, list[str]]] = []  # (line shown, argv after the CLI)
    not_run: list[str] = list(plan.notes)
    available = set(plan.known)
    for name in sorted(plan.add):
        source = plan.add[name]
        why = _unsafe_arg(source, via_cmd)
        if why:
            not_run.append(f"marketplace {_printable(name)}: {why}; add it with /plugin marketplace add.")
            continue
        available.add(name)
        steps.append((f"claude plugin marketplace add {_paste_arg(source)}", ["plugin", "marketplace", "add", source]))
    for plugin_id in plan.missing:
        market = plugin_id.rpartition("@")[2]
        if market not in available:
            shown = _printable(plugin_id)
            if not any(note.startswith(f"{shown}: ") for note in plan.notes):  # else that note says why
                not_run.append(
                    f"{shown}: its marketplace {_printable(market)} is not on this machine and is not added"
                    " here, so it is not installed."
                )
            continue
        why = _unsafe_arg(plugin_id, via_cmd)
        if why:
            not_run.append(f"{_printable(plugin_id)}: {why}; install it with /plugin install.")
            continue
        steps.append((f"claude plugin install {_paste_arg(plugin_id)}", ["plugin", "install", plugin_id]))

    if not_run:
        print("Not run here (do these by hand, or leave them to Claude Code):")
        for line in not_run:
            print(f"  {line}")
    if not steps:
        print("Plugins: nothing this command can install.")
        return 0

    print(
        f"These commands will run, in this order, with {_printable(claude)}."
        " Each fetches and installs third-party code from the source named:"
    )
    for line, _ in steps:
        print(f"  {line}")
    sys.stdout.flush()
    if not yes:
        if not (sys.stdin.isatty() if interactive is None else interactive):
            raise Fail(
                "Refused: no terminal to ask on and no --yes, so nothing was installed.",
                hint="  Read the commands above, then re-run in a terminal or with --yes.",
            )
        if (ask or _ask)("Run them? [y/N] ") not in _YES:
            raise Fail("Aborted: nothing was installed.")

    done: list[str] = []
    for i, (line, tail) in enumerate(steps):
        print(f"Running: {line}", flush=True)
        result = run([claude, *tail])
        for text in (result.stdout, result.stderr):
            for out in text.splitlines():
                print(f"    {_printable(out)}")
        if result.returncode != 0:
            rest = [ln for ln, _ in steps[i + 1:]]
            summary = [f"  Succeeded: {ln}" for ln in done] + [f"  Not run: {ln}" for ln in rest]
            summary.append("  Fix the cause shown above, then re-run 'claude-sync.sh doctor install-plugins'.")
            raise Fail(f"{line} failed (exit {result.returncode}); stopped there.", hint="\n".join(summary))
        done.append(line)
        print(f"  ok: {line}")
    print(f"Plugins: ran {len(done)} command(s). Start a new Claude Code session to load them.")
    return 0


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
    report.drift = True
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
    if isinstance(report, _Report):
        report.drift = True
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


# ── tier [show|basic|full [PROJECT_PATH]] ────────────────────────────────


def _require_conf(ctx: SyncCtx) -> None:
    conf = machineconf.machine_conf_path(ctx.repo, ctx.hostname)
    if not conf.is_file():
        raise Fail(f"No machine config at {conf}. Run install.sh first.")


def tier_show(ctx: SyncCtx) -> None:
    _require_conf(ctx)
    print(_tier_line(ctx, gitflow.current_tier(ctx)))
    print("  basic: CLAUDE.md, settings.json, agents, hooks and skills (with profiles and overrides).")
    print("  full:  basic plus the primary project's memory folder, the python packages and npm tools.")


def _tier_project(ctx: SyncCtx, project: str | None) -> str | None:
    """The project path `tier full PROJECT_PATH` may add. None when none is
    given. Refused when it has a line break, or when the conf already names
    a different one: changing the memory folder of a full machine would
    mirror another folder over memory/."""
    if project is None:
        return None
    if not project or any(c in project for c in "\r\n\0"):
        raise Fail(
            "The project path is empty or has a line break or NUL character in it.",
            hint="  Give the path on one line: claude-sync.sh tier full /absolute/path",
        )
    project = machineconf.project_path_for_os(project)
    current = machineconf.read_primary_project_path(ctx.repo, ctx.hostname)
    if current is not None and current != project:
        raise Fail(
            f"machines/{ctx.hostname}.conf already names the project path {_printable(current)}.",
            hint="  Run 'claude-sync.sh tier full' without a path to keep it. This command does not"
            " change a project path.",
        )
    return None if current is not None else project


def tier_use(ctx: SyncCtx, stack: ExitStack, tier: str, project: str | None = None) -> None:
    """Makes this machine sync at `tier`. Nothing is deleted on either side:
    basic stops touching memory, and the first full-tier sync after basic
    adopts memory without deleting (gitflow.memory_mode)."""
    machineconf.check_tier(tier)
    _require_conf(ctx)
    host = ctx.hostname
    new_project = _tier_project(ctx, project)
    has_project = machineconf.read_primary_project_path(ctx.repo, host) is not None
    if tier == machineconf.TIER_FULL and not has_project and new_project is None:
        raise Fail(
            f"The full tier syncs the primary project's memory, and machines/{host}.conf"
            " names no project path.",
            hint="  Give it once: claude-sync.sh tier full /absolute/path/of/your/project\n"
            "  (the folder you usually run claude from). Nothing was changed.",
        )
    if tier == gitflow.current_tier(ctx) and new_project is None:
        print(f"{host} already uses sync tier '{tier}'. Nothing was changed.")
        return
    gitflow.begin_sync(ctx, stack)
    # Capture under the old tier first, so pending edits (memory too, when
    # leaving full) go out before anything changes.
    gitflow.do_capture_and_commit(ctx)
    if new_project is not None:
        machineconf.write_primary_project_path(ctx.repo, host, new_project)
    machineconf.write_machine_tier(ctx.repo, host, tier)
    gitflow.commit_worktree(ctx, f"tier: {host} uses sync tier {tier}")
    finish_sync(ctx)
    if tier == machineconf.TIER_BASIC:
        print(
            f"{host} now uses sync tier 'basic': memory is no longer synced here, and nothing was"
            " deleted. Installed python packages and npm tools stay installed."
        )
    else:
        print(
            f"{host} now uses sync tier 'full': memory is merged in without deleting anything;"
            " the next sync adds this machine's own memory files to its PR."
        )


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
