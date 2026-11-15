#!/usr/bin/env python3
"""Report switched-off integrations against the integrations.json registry.

Usage:
    python3 scripts/list_disabled.py [--json] [--repo PATH] [--registry PATH]
    py -3 scripts\\list_disabled.py ...            (Windows)

Reads, never writes:
  ~/.claude/settings.json                   enabledPlugins
  ~/.claude/plugins/installed_plugins.json  installed plugins
  ~/.claude.json                            mcpServers, disabledMcpServers
  ~/.claude/skills, skills-disabled         skills on and off
  ~/.claude/agents, agents-disabled         agents on and off
  REPO/machines/*/{skills,agents}/*.disabled  hidden by a machine profile
  <project>/.claude/...                     per-project plugins, skills, agents
REPO defaults to ~/claude-config. The registry defaults to
REPO/integrations.json; if missing or malformed, everything is unclassified.

The registry is shared by every machine. Registry `projects` match live
projects by basename (case-insensitive, / or \\), so one entry covers
/home/x/project/docs and C:\\Users\\x\\project\\docs. Optional `machines`
lists hostnames or profile names (CLAUDE_SYNC_HOSTNAME, else the hostname;
CLAUDE_SYNC_PROFILE, else PROFILE= in REPO/machines/<host>.conf). Anything
not installed or defined here is "not present here", never a mismatch.

Sections: Off until asked, Enabled per project, Uninstalled, Unclassified
(found off, hidden or not enabled but not in the registry), Mismatches.

MCP server commands, args, env, URLs and headers are never printed.
Exit codes: 0 always, 2 on bad arguments.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

Item = dict[str, Any]
KINDS = ("plugin", "mcp", "skill", "agent")
BUCKETS = ("off_until_asked", "enabled_per_project", "uninstalled")
TITLES = {
    "off_until_asked": "Off until asked",
    "enabled_per_project": "Enabled per project",
    "uninstalled": "Uninstalled",
}
ABSENT = "not present here"


def warn(msg: str) -> None:
    print(f"warning: {msg}", file=sys.stderr)


def load_json(path: Path, quiet: bool = False) -> dict[str, Any] | None:
    """Parsed JSON object, or None if missing or malformed (warns unless quiet)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if not quiet:
            warn(f"{path} not found")
        return None
    except (OSError, ValueError) as exc:
        if not quiet:
            warn(f"cannot read {path}: {exc}")
        return None
    if not isinstance(data, dict):
        if not quiet:
            warn(f"{path} is not a JSON object")
        return None
    return data


def obj(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def stem(name: str) -> str:
    return name.removesuffix(".md")


def base(path: str) -> str:
    """Last path part, case-folded, for / and \\ paths alike."""
    return re.split(r"[\\/]", path.rstrip("\\/"))[-1].casefold()


def this_machine(repo: Path | None) -> set[str]:
    host = os.environ.get("CLAUDE_SYNC_HOSTNAME") or socket.gethostname()
    names = {host.casefold()}
    profile = os.environ.get("CLAUDE_SYNC_PROFILE", "")
    if not profile and repo is not None:
        try:
            text = (repo / "machines" / f"{host}.conf").read_text(encoding="utf-8")
        except OSError:
            text = ""
        for line in text.splitlines():
            if line.startswith("PROFILE="):
                profile = line.split("=", 1)[1].strip().strip("\"'")
    if profile:
        names.add(profile.casefold())
    return names


def names_in(where: Path, dirs: bool) -> set[str]:
    if not where.is_dir():
        return set()
    return {p.name if dirs else stem(p.name) for p in where.iterdir()
            if (p.is_dir() if dirs else p.is_file())}


def description(skill_dir: Path) -> str:
    """description: from SKILL.md frontmatter, cut to 100 characters."""
    try:
        lines = (skill_dir / "SKILL.md").read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return ""
    if not lines or lines[0].strip() != "---":
        return ""
    body = lines[1:]
    for n, line in enumerate(body):
        if line.strip() == "---":
            break
        if line.startswith("description:"):
            text = line.split(":", 1)[1].strip()
            if text[:1] in (">", "|"):  # YAML block scalar: join indented lines
                rest = []
                for more in body[n + 1:]:
                    if more.strip() and not more[:1].isspace():
                        break
                    rest.append(more.strip())
                text = " ".join(r for r in rest if r)
            text = text.strip("\"'")
            return text if len(text) <= 100 else text[:97] + "..."
    return ""


@dataclass
class Live:
    """What is on and off on this machine. Holds no MCP field except type."""
    home: Path
    enabled: dict[str, Any] = field(default_factory=dict)
    installed: set[str] = field(default_factory=set)
    mcp: dict[str, dict[str, Any]] = field(default_factory=dict)  # path -> defined, disabled
    user_mcp: set[str] = field(default_factory=set)
    on: dict[str, set[str]] = field(default_factory=dict)
    off: dict[str, set[str]] = field(default_factory=dict)
    hidden: dict[str, dict[str, list[tuple[str, str]]]] = field(default_factory=dict)

    def match(self, wanted: str) -> list[str]:
        """Live project paths for a registry project: the same path if one is
        a live key, else every live path with the same basename."""
        def full(p: str) -> str:
            return p.replace("\\", "/").rstrip("/").casefold()
        exact = [p for p in self.mcp if full(p) == full(wanted)]
        return exact or [p for p in self.mcp if base(p) == base(wanted)]

    def where(self, wanted: str) -> list[str]:
        """Live keys for a registry project, else the path itself if it exists here."""
        return self.match(wanted) or ([wanted] if Path(wanted).is_dir() else [])

    def project_has(self, project: str, kind: str, name: str) -> bool:
        where = Path(project) / ".claude"
        if kind == "plugin":
            return any(obj(obj(load_json(where / f, quiet=True)).get("enabledPlugins")).get(name)
                       is True for f in ("settings.json", "settings.local.json"))
        if kind == "skill":
            return (where / "skills" / name).is_dir()
        return (where / "agents" / f"{name}.md").is_file()


def gather(home: Path, repo: Path | None) -> Live:
    live = Live(home)
    claude = home / ".claude"
    live.enabled = obj(obj(load_json(claude / "settings.json")).get("enabledPlugins"))
    live.installed = set(obj(obj(load_json(
        claude / "plugins" / "installed_plugins.json")).get("plugins")))
    data = obj(load_json(home / ".claude.json"))
    live.user_mcp = set(obj(data.get("mcpServers")))
    for path, proj in sorted(obj(data.get("projects")).items()):
        proj = obj(proj)
        # Keep only name -> type: every other field may hold secrets.
        defined = {n: (s.get("type") if isinstance(s.get("type"), str) else "stdio")
                   for n, s in obj(proj.get("mcpServers")).items() if isinstance(s, dict)}
        listed = proj.get("disabledMcpServers")
        listed = [n for n in listed if isinstance(n, str)] if isinstance(listed, list) else []
        live.mcp[path] = {"defined": defined, "disabled": listed}
    for kind, sub, dirs in (("skill", "skills", True), ("agent", "agents", False)):
        live.on[kind] = names_in(claude / sub, dirs)
        live.off[kind] = names_in(claude / f"{sub}-disabled", dirs)
        live.hidden[kind] = {}
        if repo is not None:
            for marker in sorted((repo / "machines").glob(f"*/{sub}/*.disabled")):
                name = stem(marker.name[: -len(".disabled")])
                how = f"delete {marker.relative_to(repo).as_posix()} in {repo}"
                live.hidden[kind].setdefault(name, []).append((marker.parent.parent.name, how))
    return live


def hint(kind: str, name: str, project: str | None = None) -> str:
    if kind == "plugin":
        return f'set enabledPlugins["{name}"] to true in ~/.claude/settings.json'
    if kind == "mcp":
        return f'remove "{name}" from disabledMcpServers for {project} in ~/.claude.json'
    sub = "skills" if kind == "skill" else "agents"
    suffix = "" if kind == "skill" else ".md"
    return f"move ~/.claude/{sub}-disabled/{name}{suffix} back to ~/.claude/{sub}/"


def check(project: str | None, state: str, ok: bool, enable: str | None = None) -> Item:
    return {"project": project, "state": state, "ok": ok, "enable": enable}


def evaluate_mcp(live: Live, bucket: str, name: str, projects: list[str]) -> list[Item]:
    defined_in = [p for p, m in live.mcp.items() if name in m["defined"]]
    enabled_in = [p for p in defined_in if name not in live.mcp[p]["disabled"]]
    if bucket == "uninstalled":
        out = [check(p, "still defined", False) for p in defined_in]
        if name in live.user_mcp:
            out.append(check(None, "still defined at user scope", False))
        return out or [check(None, ABSENT, True)]
    out: list[Item] = []
    wanted = {base(p) for p in projects}
    targets = list(dict.fromkeys(p for want in projects for p in live.match(want))) \
        if projects else defined_in
    out += [check(want, ABSENT, True) for want in projects if not live.match(want)]
    for p in targets:
        on = p in enabled_in
        if p not in defined_in:
            out.append(check(p, ABSENT, True))
        elif bucket == "off_until_asked":
            out.append(check(p, "enabled" if on else "disabled as expected", not on,
                             hint("mcp", name, p)))
        else:
            out.append(check(p, "enabled" if on else "disabled", on,
                             None if on else hint("mcp", name, p)))
    if bucket == "enabled_per_project":
        out += [check(p, "enabled, not listed", False) for p in enabled_in
                if base(p) not in wanted]
    return out or [check(None, ABSENT, True)]


def evaluate(live: Live, bucket: str, kind: str, name: str, projects: list[str]) -> list[Item]:
    """Live state of one registry entry, one check per project where it applies."""
    if kind == "mcp":
        return evaluate_mcp(live, bucket, name, projects)
    if kind == "plugin":
        value = live.enabled.get(name)
        on, present = value is True, name in live.installed or name in live.enabled
    else:
        value = None
        on, present = name in live.on[kind], name in live.on[kind] or name in live.off[kind]
        present = present or name in live.hidden[kind]
    if bucket == "uninstalled":
        if on:
            return [check(None, "still enabled", False)]
        return [check(None, "still installed" if present else ABSENT, not present)]
    if bucket == "enabled_per_project":
        out: list[Item] = []
        seen: set[str] = set()
        for want in projects:
            here = [p for p in live.where(want) if p not in seen]
            seen.update(here)
            for p in here:
                has = live.project_has(p, kind, name)
                out.append(check(p, "enabled" if has else "not enabled", has))
            if not live.where(want):
                out.append(check(want, ABSENT, True))
        if on:
            out.append(check(None, "enabled globally", False))
        return out or [check(None, "no projects listed", False)]
    if on:
        return [check(None, "enabled", False)]
    if value is False:
        return [check(None, "disabled as expected", True, hint(kind, name))]
    if kind == "plugin" and name in live.installed:
        return [check(None, "installed, not enabled", True, hint(kind, name))]
    if kind != "plugin" and name in live.off[kind]:
        return [check(None, "disabled as expected", True, hint(kind, name))]
    if kind != "plugin" and name in live.hidden[kind]:
        prof, how = live.hidden[kind][name][0]
        return [check(None, f"hidden by profile {prof}", True, how)]
    return [check(None, ABSENT, True)]


def load_registry(path: Path) -> dict[str, list[Item]]:
    reg: dict[str, list[Item]] = {b: [] for b in BUCKETS}
    data = load_json(path)
    if data is None:
        warn("no usable registry, everything is unclassified")
        return reg
    for bucket in BUCKETS:
        entries = data.get(bucket, [])
        for e in entries if isinstance(entries, list) else []:
            if not (isinstance(e, dict) and e.get("kind") in KINDS
                    and isinstance(e.get("name"), str)):
                warn(f"skipping bad {bucket} entry in {path}")
                continue
            lists = {}
            for key in ("projects", "machines"):
                v = e.get(key)
                lists[key] = [x for x in v if isinstance(x, str)] if isinstance(v, list) else []
            reg[bucket].append({
                "kind": e["kind"],
                "name": stem(e["name"]) if e["kind"] == "agent" else e["name"],
                "note": e["note"] if isinstance(e.get("note"), str) else "",
                **lists,
            })
    return reg


def unclassified(live: Live, covered: set[tuple[str, str, str]]) -> list[Item]:
    def free(kind: str, name: str, project: str = "*") -> bool:
        if kind != "mcp":  # plugin and skill switches are global, whatever projects the entry lists
            return not any(k == kind and n == name for k, n, _ in covered)
        return not {(kind, name, "*"), (kind, name, base(project))} & covered

    out: list[Item] = []
    for name, value in sorted(live.enabled.items()):
        if value is False and free("plugin", name):
            out.append({"kind": "plugin", "name": name, "state": "disabled",
                        "enable": hint("plugin", name)})
    for name in sorted(live.installed - set(live.enabled)):
        if free("plugin", name):
            out.append({"kind": "plugin", "name": name, "state": "installed, not enabled",
                        "enable": hint("plugin", name)})
    servers: dict[str, Item] = {}
    undefined: dict[str, list[str]] = {}
    for path, m in live.mcp.items():
        for name in m["disabled"]:
            if not free("mcp", name, path):
                continue
            if name not in m["defined"]:
                undefined.setdefault(name, []).append(path)
                continue
            s = servers.setdefault(name, {"kind": "mcp", "name": name, "types": [],
                                          "projects": [], "enable": []})
            s["projects"].append(path)
            s["enable"].append(hint("mcp", name, path))
            if m["defined"][name] not in s["types"]:
                s["types"].append(m["defined"][name])
    out += [servers[n] for n in sorted(servers)]
    out += [{"kind": "mcp", "name": n, "state": "listed but not defined", "projects": p}
            for n, p in sorted(undefined.items())]
    for kind in ("skill", "agent"):
        sub = "skills" if kind == "skill" else "agents"
        for name in sorted(live.off[kind]):
            if free(kind, name):
                item = {"kind": kind, "name": name, "state": "disabled", "enable": hint(kind, name)}
                if kind == "skill":
                    item["description"] = description(live.home / ".claude" / f"{sub}-disabled" / name)
                out.append(item)
        for name, marks in sorted(live.hidden[kind].items()):
            if free(kind, name):
                out += [{"kind": kind, "name": name, "state": f"hidden by profile {prof}",
                         "enable": how} for prof, how in marks]
    for item in out:
        item["bucket"] = "unclassified"
    return out


def build(live: Live, reg: dict[str, list[Item]], machine: set[str]) -> dict[str, Any]:
    report: dict[str, Any] = {}
    mismatches: list[Item] = []
    covered: set[tuple[str, str, str]] = set()
    for bucket in BUCKETS:
        report[bucket] = []
        for e in reg[bucket]:
            covered |= {(e["kind"], e["name"], base(p) if p != "*" else p)
                        for p in e["projects"] or ["*"]}
            if e["machines"] and not {m.casefold() for m in e["machines"]} & machine:
                checks = [check(None, "not for this machine", True)]
            else:
                checks = evaluate(live, bucket, e["kind"], e["name"], e["projects"])
            report[bucket].append({"bucket": bucket, **e, "checks": checks})
            mismatches += [{"bucket": bucket, "kind": e["kind"], "name": e["name"],
                            "project": c["project"], "state": c["state"]}
                           for c in checks if not c["ok"]]
    report["unclassified"] = unclassified(live, covered)
    report["mismatches"] = mismatches
    return report


def render(report: dict[str, Any]) -> str:
    lines: list[str] = []
    for bucket in BUCKETS:
        lines.append(f"{TITLES[bucket]}:")
        for e in report[bucket]:
            lines.append(f"  {e['kind']} {e['name']}" + (f" - {e['note']}" if e["note"] else ""))
            for c in e["checks"]:
                where = f"{c['project']}: " if c["project"] else ""
                flag = "" if c["ok"] else "MISMATCH: "
                lines.append(f"    {where}{flag}{c['state']}")
                if c["enable"]:
                    lines.append(f"      enable: {c['enable']}")
        if not report[bucket]:
            lines.append("  none")
        lines.append("")
    lines.append("Unclassified (decide which bucket):")
    for i in report["unclassified"]:
        if "types" in i:
            n = len(i["projects"])
            lines.append(f"  mcp {i['name']} [{', '.join(i['types'])}] ({n} project{'s' * (n != 1)})")
            for proj, how in zip(i["projects"], i["enable"]):
                lines += [f"    {proj}", f"      enable: {how}"]
        elif "projects" in i:
            lines.append(f"  mcp {i['name']} ({i['state']}): {', '.join(i['projects'])}")
        else:
            desc = f" - {i['description']}" if i.get("description") else ""
            lines += [f"  {i['kind']} {i['name']} ({i['state']}){desc}", f"    enable: {i['enable']}"]
    if not report["unclassified"]:
        lines.append("  none")
    lines += ["", "Mismatches:"]
    for m in report["mismatches"]:
        where = f" in {m['project']}" if m["project"] else ""
        lines.append(f"  {TITLES[m['bucket']]}: {m['kind']} {m['name']}{where}: {m['state']}")
    if not report["mismatches"]:
        lines.append("  none")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Report disabled integrations against the registry.")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--repo", type=Path, help="claude-config checkout (default ~/claude-config)")
    ap.add_argument("--registry", type=Path, help="registry file (default REPO/integrations.json)")
    args = ap.parse_args(argv)
    home = Path.home()
    repo: Path | None = args.repo or home / "claude-config"
    registry = args.registry or repo / "integrations.json"
    if not repo.is_dir():
        if args.repo:
            warn(f"repo {repo} not found, skipping profile markers")
        repo = None
    report = build(gather(home, repo), load_registry(registry), this_machine(repo))
    text = json.dumps(report, indent=2, ensure_ascii=False) + "\n" if args.json else render(report)
    out = sys.stdout
    if hasattr(out, "reconfigure"):
        out.reconfigure(encoding="utf-8")  # Windows consoles default to a code page
    out.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
