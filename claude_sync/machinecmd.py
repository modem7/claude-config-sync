"""`machine NAME ...`: show or change another machine's config from this one.

A machine you cannot run commands on (a throwaway cloud session, a machine
that is off) still has a config in the repo: machines/NAME.conf (tier,
profile, PLUGINS=) and machines/NAME.settings.json (settings override, which is how
its plugins are chosen). This edits those files, through this machine's sync
branch and PR like `override` and `tier` do. It reads and writes the repo
only; NAME's own ~/.claude is never touched."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from contextlib import ExitStack

from claude_sync import doctor, gitflow, machineconf, overlay, profile
from claude_sync.gitflow import SyncCtx
from claude_sync.util import Fail, UsageError

_PLUGIN_ID = re.compile(r"[A-Za-z0-9._-]+@[A-Za-z0-9._-]+")


def _no_constant(name: str) -> object:
    raise ValueError(f"{name} is not valid JSON")


def read_only(args: list[str]) -> bool:
    return len(args) >= 2 and args[1] == "show" and len(args) == 2


def machine(ctx: SyncCtx, stack: ExitStack, args: list[str]) -> None:
    if len(args) < 2:
        raise UsageError()
    name, action, rest = args[0], args[1], args[2:]
    if not profile.profile_valid_name(name):
        raise Fail(
            f"Not a valid machine name: {name}",
            hint="  Use only letters, digits, '.', '_' and '-' (not '.' or '..').",
        )
    if action == "show" and not rest:
        _show(ctx, name)
        return
    if action in ("tier", "profile", "plugin", "plugins", "override"):
        _need_known(ctx, name)
    if action == "tier" and rest and rest[0] in ("basic", "full") and len(rest) <= 2:
        project = rest[1] if len(rest) == 2 else None
        _change(ctx, stack, f"machine: {name} uses sync tier {rest[0]}", lambda: _tier(ctx, name, rest[0], project))
        print(f"{name} is set to sync tier '{rest[0]}'{_when(ctx, name)}.")
    elif action == "profile" and len(rest) == 2 and rest[0] == "use":
        _change(ctx, stack, f"machine: {name} uses profile {rest[1]}", lambda: _profile(ctx, name, rest[1]))
        print(f"{name} now follows profile '{rest[1]}'{_when(ctx, name)}.")
    elif action == "plugins" and len(rest) == 1 and rest[0] in ("full", "off"):
        _plugins(ctx, stack, name, rest[0] == "full")
    elif action == "plugin" and len(rest) == 2 and rest[0] in ("enable", "disable", "inherit"):
        _plugin(ctx, stack, name, rest[0], rest[1])
    elif action == "override" and len(rest) >= 2 and rest[0] in ("set", "unset"):
        _override(ctx, stack, name, rest)
    else:
        raise UsageError()


def _is_cloud(name: str) -> bool:
    return name == "cloud" or name.startswith("cloud-")


def _need_known(ctx: SyncCtx, name: str) -> None:
    """A typo in NAME must not create a machine. Only cloud machines (a
    throwaway session has no conf until `remote` makes one) may be new."""
    known = (
        _is_cloud(name)
        or machineconf.machine_conf_path(ctx.repo, name).is_file()
        or profile.profile_dir(ctx.repo, name).is_dir()  # a shared profile, edited on purpose
    )
    if not known:
        raise Fail(
            f"No machine or profile called '{name}' (no machines/{name}.conf or machines/{name}/), so this looks like a typo.",
            hint="  Check the name with 'ls machines/'. A cloud machine is named 'cloud' or 'cloud-...'.",
        )


def _new_conf_is_basic(ctx: SyncCtx, name: str) -> None:
    """A conf made here for a cloud machine starts on the basic tier, as `remote`
    would make it: with no TIER line it would read as full, which needs a project path."""
    if not machineconf.machine_conf_path(ctx.repo, name).is_file():
        machineconf.write_machine_tier(ctx.repo, name, machineconf.TIER_BASIC)


def _change(ctx: SyncCtx, stack: ExitStack, message: str, edit: Callable[[], None]) -> None:
    """The shared flow: this machine's own pending edits go out first, then the
    one change is committed onto this machine's sync branch and its PR."""
    gitflow.begin_sync(ctx, stack)
    gitflow.do_capture_and_commit(ctx)
    edit()
    gitflow.commit_worktree(ctx, message)
    doctor.finish_sync(ctx)


def _when(ctx: SyncCtx, name: str) -> str:
    return " (applied on this machine now; other machines get it once the PR is merged)" if name == ctx.hostname else (
        f" (it takes effect on {name} once the PR is merged)"
    )


def _own_profile(ctx: SyncCtx, name: str) -> str:
    """The profile NAME's override file belongs to. A profile shared with other
    machines is never edited by a `machine NAME` command: that would change them too."""
    prof = profile.profile_resolve_name(ctx.repo, name)
    if prof != name:
        raise Fail(
            f"{name} follows the shared profile '{prof}', so its settings override is machines/{prof}.settings.json,"
            " which every machine on that profile uses.",
            hint=f"  To change that profile for all of them, run: claude-sync.sh machine {prof} ...\n"
            f"  To change only {name}, first run: claude-sync.sh machine {name} profile use {name}.",
        )
    return prof


def _show(ctx: SyncCtx, name: str) -> None:
    repo = ctx.repo
    conf = machineconf.machine_conf_path(repo, name)
    prof = profile.profile_resolve_name(repo, name)
    tier = machineconf.read_machine_tier(repo, name)
    print(f"{name}: " + (f"machines/{name}.conf exists." if conf.is_file() else f"no machines/{name}.conf yet."))
    print(f"  tier: {tier}" + ("" if conf.is_file() else " (the default; `remote` makes a missing conf basic)"))
    print(f"  profile: {prof}" + (" (its own)" if prof == name else " (shared: changes to its overrides reach every machine on it)"))
    try:
        auto = machineconf.read_machine_plugins(repo, name)
    except Fail as err:
        print(f"  plugins: {err.message}")
    else:
        print(
            "  plugins: full (sync, pull and remote install what its settings.json enables)"
            if auto else "  plugins: off (the default; install them with 'doctor install-plugins')"
        )
    print("  settings overrides:")
    for line in overlay.overlay_list(repo, prof):
        print(f"    {line}")


def _tier(ctx: SyncCtx, name: str, tier: str, project: str | None) -> None:
    existing = machineconf.read_primary_project_path(ctx.repo, name)
    if project is not None:
        if not project or any(c in project for c in "\r\n\0"):
            raise Fail("The project path is empty or has a line break or NUL character in it.")
        if existing is not None and existing != project:
            raise Fail(
                f"machines/{name}.conf already names the project {existing}; changing the memory folder"
                " of a full machine would mirror another folder over memory/.",
                hint="  Edit the conf by hand if you really mean it.",
            )
    if tier == machineconf.TIER_FULL and existing is None and project is None:
        raise Fail(
            f"The full tier syncs a project's memory, and machines/{name}.conf names no project path.",
            hint=f"  Give it once: claude-sync.sh machine {name} tier full /absolute/path/of/the/project",
        )
    if project is not None and existing is None:
        machineconf.write_primary_project_path(ctx.repo, name, project)
    machineconf.write_machine_tier(ctx.repo, name, tier)


def _profile(ctx: SyncCtx, name: str, prof: str) -> None:
    if not profile.profile_valid_name(prof):
        raise Fail(f"Not a valid profile name: {prof}", hint="  Use only letters, digits, '.', '_' and '-'.")
    if prof != name and not profile.profile_dir(ctx.repo, prof).is_dir():
        raise Fail(
            f"No profile called '{prof}': machines/{prof}/ does not exist.",
            hint="  'claude-sync.sh profile show NAME' describes a profile; pick one that exists.",
        )
    _new_conf_is_basic(ctx, name)
    machineconf.write_machine_profile(ctx.repo, name, "" if prof == name else prof)


def _plugins(ctx: SyncCtx, stack: ExitStack, name: str, on: bool) -> None:
    """PLUGINS=full or off in NAME's conf. A new cloud conf starts basic, as `remote` makes it."""
    value = "full" if on else "off"
    conf = machineconf.machine_conf_path(ctx.repo, name)
    try:
        current = machineconf.read_machine_plugins(ctx.repo, name) if conf.is_file() else False
    except Fail:
        current = None  # a bad value: rewrite it
    if current is on:
        print(f"{name} already has plugins {value}. Nothing was changed.")
        return

    def edit() -> None:
        _new_conf_is_basic(ctx, name)
        machineconf.write_machine_plugins(ctx.repo, name, on)

    _change(ctx, stack, f"machine: {name} plugins {value}", edit)
    what = "now installs the plugins its settings.json enables after each sync, pull and remote" if on else (
        "no longer installs plugins by itself"
    )
    print(f"{name} {what}{_when(ctx, name)}.")


def _plugin(ctx: SyncCtx, stack: ExitStack, name: str, action: str, plugin_id: str) -> None:
    if not _PLUGIN_ID.fullmatch(plugin_id):
        raise Fail(
            f"Not a plugin id: {plugin_id}",
            hint="  Use plugin@marketplace, e.g. code-review@claude-plugins-official.",
        )
    path = ("enabledPlugins", plugin_id)

    prof = _own_profile(ctx, name)

    def edit() -> None:
        if action == "inherit":
            overlay.overlay_remove(ctx.repo, prof, f'.enabledPlugins["{plugin_id}"]')
        else:
            overlay.overlay_set(ctx.repo, prof, path, action == "enable")

    if action == "inherit":
        if f'.enabledPlugins["{plugin_id}"]' not in "\n".join(overlay.overlay_list(ctx.repo, prof)):
            print(f"{name} does not override {plugin_id}; it already follows the shared settings. Nothing was changed.")
            return
    _change(ctx, stack, f"machine: {name} {action}s {plugin_id}", edit)
    verb = {"enable": "enables", "disable": "disables", "inherit": "follows the shared setting for"}[action]
    print(f"{name} now {verb} {plugin_id}{_when(ctx, name)}.")


def _override(ctx: SyncCtx, stack: ExitStack, name: str, rest: list[str]) -> None:
    action, expr = rest[0], rest[1]
    path = overlay.parse_path(expr)
    if action == "set":
        if len(rest) != 3:
            raise UsageError()
        try:
            value = json.loads(rest[2], parse_constant=_no_constant)
        except (ValueError, RecursionError) as err:
            raise Fail(f"The value is not JSON: {err}", hint="  Quote strings: '\"opus\"'; booleans are true and false.") from None

    prof = _own_profile(ctx, name)

    def edit() -> None:
        if action == "set":
            overlay.overlay_set(ctx.repo, prof, path, value)
        else:
            overlay.overlay_remove(ctx.repo, prof, expr)

    if action == "unset" and len(rest) != 2:
        raise UsageError()
    _change(ctx, stack, f"machine: {name} {action} {expr}", edit)
    print(f"{name}: {expr} {'set' if action == 'set' else 'unset'} in its settings override{_when(ctx, name)}.")
