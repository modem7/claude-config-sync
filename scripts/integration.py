#!/usr/bin/env python3
"""Switch one plugin, MCP server or skill on or off on this machine.

Usage:
    python3 scripts/integration.py enable|disable KIND NAME [--project PATH] [--dry-run]
    py -3 scripts\\integration.py ...                       (Windows)

plugin: enabledPlugins[NAME] in ~/.claude/settings.json.
mcp: NAME in disabledMcpServers for one project in ~/.claude.json (--project,
  else the nearest cwd ancestor that is a key). NAME must already be defined
  in that project. skill: move between ~/.claude/skills and
  skills-disabled; refuses if the target exists.
JSON files are backed up to ~/.claude/.integration-backups/<UTC>/ (newest 10
kept) and written atomically, keeping other keys, indent and line endings;
~/.claude.json is kept at mode 0600. Shares claude_sync/safejson.py.
MCP fields are never printed; integrations.json is never touched.

Exit codes: 0 done or already so, 1 refused or failed, 2 bad arguments.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Any

# Run as a script, sys.path[0] is scripts/; add the repo root for claude_sync.
_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from claude_sync import safejson

REMINDER = "Start a new Claude Code session to load the change."


class Refused(Exception):
    """A one-line reason to stop with exit 1."""


class JsonFile:
    """A JSON object file plus the layout needed to write it back alike.

    secret=True keeps the file at mode 0600 (skipped on Windows);
    otherwise its current mode is kept.
    """

    def __init__(self, path: Path, label: str, secret: bool = False):
        self.path, self.label, self.secret = path, label, secret
        try:
            self.data, self.meta = safejson.read_json_with_meta(path, label)
        except safejson.SafeJsonError as exc:
            raise Refused(str(exc)) from None
        if not isinstance(self.data, dict):
            raise Refused(f"{label} is not a JSON object, left unchanged")

    def save(self, backups: Path) -> None:
        mode = safejson.SECRET_MODE if self.secret else None
        try:
            safejson.write_json_atomic(self.path, self.data, self.meta,
                                       backup_dir=backups, mode=mode)
        except safejson.SafeJsonError as exc:
            raise Refused(str(exc)) from None


def norm(path: str) -> str:
    """Comparable form of a project path: / separators, no trailing one."""
    p = path.replace("\\", "/")
    p = p.rstrip("/") or "/"
    return p.casefold() if os.name == "nt" else p


def absolute(path: str) -> str:
    """Leave / , \\ and drive-letter paths as given; resolve the rest."""
    if re.match(r"^([A-Za-z]:)?[\\/]", path):
        return path
    return os.path.abspath(path)


def ancestors(path: str) -> list[str]:
    """path and each parent, nearest first, in norm() form."""
    parts = norm(path).split("/")
    out = ["/".join(parts[:i]) or "/" for i in range(len(parts), 0, -1)]
    return list(dict.fromkeys(out))


def find_project(projects: dict[str, Any], wanted: str | None) -> str:
    """The ~/.claude.json key for --project, or for the cwd and its parents."""
    if wanted is not None:
        wanted = absolute(wanted)
        if wanted in projects:
            return wanted
        hits = [k for k in projects if norm(k) == norm(wanted)]
        return hits[0] if hits else wanted
    cwd = os.getcwd()
    keys = {norm(k): k for k in projects}
    for cand in ancestors(cwd):
        if cand in keys:
            return keys[cand]
    return cwd


def do_plugin(on: bool, name: str, claude: Path, dry: bool) -> str:
    f = JsonFile(claude / "settings.json", "~/.claude/settings.json")
    plugins = f.data.get("enabledPlugins")
    if plugins is None:
        plugins = f.data["enabledPlugins"] = {}
    if not isinstance(plugins, dict):
        raise Refused("enabledPlugins in ~/.claude/settings.json is not an object")
    verb = "enable" if on else "disable"
    if plugins.get(name) is on:
        return f"plugin {name} is already {verb}d."
    if dry:
        return f"Dry run: would {verb} plugin {name} in ~/.claude/settings.json."
    plugins[name] = on
    f.save(claude / ".integration-backups")
    return f"{verb.capitalize()}d plugin {name} in ~/.claude/settings.json."


def do_mcp(on: bool, name: str, home: Path, project: str | None, dry: bool) -> str:
    f = JsonFile(home / ".claude.json", "~/.claude.json", secret=True)
    projects = f.data.get("projects")
    projects = projects if isinstance(projects, dict) else {}
    key = find_project(projects, project)
    entry = projects.get(key)

    def defined(where: Any) -> bool:
        return isinstance(where, dict) and isinstance(where.get("mcpServers"), dict) \
            and name in where["mcpServers"]

    if not isinstance(entry, dict) or not defined(entry):
        raise Refused(f"no MCP server {name} defined for {key} in ~/.claude.json, nothing changed")
    listed = entry.get("disabledMcpServers")
    if listed is None:
        listed = []
    if not isinstance(listed, list):
        raise Refused(f"disabledMcpServers for {key} is not a list, nothing changed")
    verb = "enable" if on else "disable"
    if (name not in listed) is on:
        return f"MCP server {name} is already {verb}d for {key}."
    if dry:
        return f"Dry run: would {verb} MCP server {name} for {key} in ~/.claude.json."
    entry["disabledMcpServers"] = [n for n in listed if n != name] if on else listed + [name]
    f.save(home / ".claude" / ".integration-backups")
    return f"{verb.capitalize()}d MCP server {name} for {key} in ~/.claude.json."


def do_skill(on: bool, name: str, claude: Path, dry: bool) -> str:
    if not name or name in (".", "..") or re.search(r"[\\/]", name):
        raise Refused(f"bad skill name {name!r}")
    on_dir, off_dir = claude / "skills", claude / "skills-disabled"
    src, dst = (off_dir / name, on_dir / name) if on else (on_dir / name, off_dir / name)
    verb = "enable" if on else "disable"
    if not os.path.lexists(src):
        if os.path.lexists(dst):
            return f"skill {name} is already {verb}d."
        raise Refused(f"skill {name} not found in ~/.claude/skills or skills-disabled")
    if os.path.lexists(dst):
        raise Refused(f"skill {name} is in both ~/.claude/skills and skills-disabled, "
                      "nothing moved")
    to = f"~/.claude/{dst.parent.name}/"
    if dry:
        return f"Dry run: would {verb} skill {name} (move to {to})."
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.rename(src, dst)
    except OSError as exc:
        raise Refused(f"cannot move skill {name}: {exc.__class__.__name__}") from None
    return f"{verb.capitalize()}d skill {name} (moved to {to})."


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Switch a plugin, MCP server or skill on or off.")
    ap.add_argument("action", choices=("enable", "disable"))
    ap.add_argument("kind", choices=("plugin", "mcp", "skill"))
    ap.add_argument("name")
    ap.add_argument("--project", help="project path for mcp (default: from the cwd)")
    ap.add_argument("--dry-run", action="store_true", help="say what would change, write nothing")
    args = ap.parse_args(argv)
    if args.project is not None and args.kind != "mcp":
        ap.error("--project only applies to mcp")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to a code page
    home = Path.home()
    on = args.action == "enable"
    try:
        if args.kind == "plugin":
            msg = do_plugin(on, args.name, home / ".claude", args.dry_run)
        elif args.kind == "mcp":
            msg = do_mcp(on, args.name, home, args.project, args.dry_run)
        else:
            msg = do_skill(on, args.name, home / ".claude", args.dry_run)
    except Refused as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(msg)
    print(REMINDER)
    return 0


if __name__ == "__main__":
    sys.exit(main())
