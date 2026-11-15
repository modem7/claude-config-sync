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
kept) and written atomically, keeping other keys, indent and line endings.
MCP fields are never printed; integrations.json is never touched.

Exit codes: 0 done or already so, 1 refused or failed, 2 bad arguments.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

KEEP_BACKUPS = 10
STAMP = re.compile(r"^\d{8}T\d{12}Z(-\d+)?$")
REMINDER = "Start a new Claude Code session to load the change."


class Refused(Exception):
    """A one-line reason to stop with exit 1."""


class JsonFile:
    """A JSON object file plus the layout needed to write it back alike."""

    def __init__(self, path: Path, label: str):
        self.path, self.label = path, label
        try:
            with open(path, encoding="utf-8", newline="") as f:
                self.text = f.read()
            self.mtime = path.stat().st_mtime_ns
        except FileNotFoundError:
            raise Refused(f"{label} not found") from None
        except (OSError, UnicodeDecodeError) as exc:
            raise Refused(f"cannot read {label}: {exc.__class__.__name__}") from None
        try:
            self.data = json.loads(self.text)
        except ValueError:
            raise Refused(f"{label} is not valid JSON, left unchanged") from None
        if not isinstance(self.data, dict):
            raise Refused(f"{label} is not a JSON object, left unchanged")

    def indent(self) -> str | int:
        m = re.search(r"\n([ \t]+)\S", self.text)
        if not m:
            return 2
        if m.group(1).startswith("\t"):
            return "\t"
        return 4 if len(m.group(1)) == 4 else 2

    def render(self) -> str:
        out = json.dumps(self.data, indent=self.indent(), ensure_ascii=False)
        if self.text.endswith("\n"):
            out += "\n"
        if "\r\n" in self.text:
            out = out.replace("\n", "\r\n")
        return out

    def save(self, backups: Path) -> None:
        backup(self.path, backups)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.name}.",
                                   suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
                f.write(self.render())
            shutil.copymode(self.path, tmp)
            if self.path.stat().st_mtime_ns != self.mtime:
                raise Refused(f"{self.label} changed while editing, try again")
            os.replace(tmp, self.path)
        except OSError as exc:
            raise Refused(f"cannot write {self.label}: {exc.__class__.__name__}") from None
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


def backup(path: Path, root: Path) -> None:
    """Copy path into a new UTC-stamped dir under root; keep the newest few."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    root.mkdir(parents=True, exist_ok=True)
    target, n = root / stamp, 0
    while True:
        try:
            target.mkdir()
            break
        except FileExistsError:
            n += 1
            target = root / f"{stamp}-{n}"
    shutil.copy2(path, target / path.name)
    dirs = sorted(p.name for p in root.iterdir() if p.is_dir() and STAMP.match(p.name))
    for old in dirs[:-KEEP_BACKUPS]:
        shutil.rmtree(root / old, ignore_errors=True)


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
    f = JsonFile(home / ".claude.json", "~/.claude.json")
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
