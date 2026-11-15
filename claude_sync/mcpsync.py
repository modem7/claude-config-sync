"""Opt-in sync of the user-scope MCP servers in ~/.claude.json (issue 75).

Only the top-level `mcpServers` object is read or written; projects.* and
every other key stay as they are. Servers travel through REPO_FILE in the
private repo. A server is kept local (never written to the repo or to a
machine) when the registry turns it off, when it is listed for other
machines only, when it holds a literal credential, or when its command is
a path or a Windows-only file name. A live server that is kept local is
never replaced by a repo entry of the same name.

Merges never delete: capture adds and updates repo entries, apply adds and
updates live ones. capture() and apply() never raise; each problem is one
say() line naming servers and keys only, never a command, arg, env,
header or url value. Standard library only.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claude_sync import mcpdoctor, safejson
from claude_sync.util import printable

REPO_FILE = "mcp/servers.json"
LIVE_LABEL = "~/.claude.json"
EXCLUDE_BUCKETS = ("off_until_asked", "uninstalled")
WINDOWS_SUFFIXES = (".cmd", ".exe", ".bat", ".ps1", ".com")
ATTEMPTS = 3
PER_MACHINE = "per-machine, not listed for this host"

Say = Callable[[str], None]


def default_say(line: str) -> None:
    print(f"claude-sync: {line}", file=sys.stderr)


def host_names(hostname: str, profile: str) -> set[str]:
    """Names this machine answers to, as list_disabled.this_machine."""
    names = {hostname.casefold()}
    if profile:
        names.add(profile.casefold())
    return names


@dataclass
class Policy:
    excluded: dict[str, str] = field(default_factory=dict)
    per_machine: set[str] = field(default_factory=set)

    def reason(self, name: str) -> str | None:
        if name in self.excluded:
            return self.excluded[name]
        if name in self.per_machine:
            return PER_MACHINE
        return None


def load_policy(registry_path: Path, names: set[str]) -> Policy | None:
    """Rules from integrations.json. Missing file: no rules. Unusable: None."""
    if not registry_path.exists():
        return Policy()
    registry = mcpdoctor.load_registry(registry_path)
    if registry is None:
        return None
    policy = Policy()
    for bucket, name, _projects, machines in registry:
        if bucket in EXCLUDE_BUCKETS:
            policy.excluded.setdefault(name, f"registry: {bucket}")
        if machines and not {m.casefold() for m in machines} & names:
            policy.per_machine.add(name)
    return policy


def bare_command(command: Any) -> bool:
    """A plain program name looked up on PATH (npx, uvx, node...)."""
    if not isinstance(command, str) or not command:
        return False
    if "/" in command or "\\" in command or ":" in command or command.startswith("~"):
        return False
    return not command.casefold().endswith(WINDOWS_SUFFIXES)


def classify(servers: dict[str, Any], policy: Policy) -> tuple[dict[str, Any], list[tuple[str, str]]]:
    """(eligible servers, sorted (name, reason) for the ones kept local)."""
    eligible: dict[str, Any] = {}
    skipped: list[tuple[str, str]] = []
    for name, server in servers.items():
        if not isinstance(server, dict):
            skipped.append((name, "not a JSON object"))
            continue
        why = policy.reason(name)
        if why:
            skipped.append((name, why))
            continue
        where = mcpdoctor.scan_server(server, scan_command=True)
        if where:
            skipped.append((name, "literal credential in " + ", ".join(where)))
            continue
        if "command" in server and not bare_command(server["command"]):
            skipped.append((name, "command is not a bare program name, kept local"))
            continue
        eligible[name] = server
    return eligible, sorted(skipped)


def merge_for_repo(repo_servers: dict[str, Any], eligible_live: dict[str, Any]) -> dict[str, Any]:
    """Repo plus live; live wins a clash; nothing is deleted."""
    return {**repo_servers, **eligible_live}


def merge_for_live(live_servers: dict[str, Any], eligible_repo: dict[str, Any]) -> dict[str, Any]:
    """Live in its order, repo values in place, new names appended sorted."""
    merged = dict(live_servers)
    for name, value in eligible_repo.items():
        if name in merged:
            merged[name] = value
    for name in sorted(n for n in eligible_repo if n not in live_servers):
        merged[name] = eligible_repo[name]
    return merged


def render_repo(servers: dict[str, Any]) -> bytes:
    text = json.dumps({"mcpServers": servers}, indent=2, sort_keys=True, ensure_ascii=False)
    return (text + "\n").encode("utf-8")


def _say_skipped(skipped: list[tuple[str, str]], say: Say) -> None:
    for name, why in skipped:
        say(f"mcp: skipped {printable(str(name))}: {why}")


class _Malformed(Exception):
    pass


def _read_repo(path: Path) -> tuple[dict[str, Any], bytes] | None:
    """(servers, raw bytes), None if missing. _Malformed if unusable."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        raise _Malformed("not readable") from None
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError:
        raise _Malformed("not valid JSON") from None
    if not isinstance(data, dict) or not isinstance(data.get("mcpServers"), dict):
        raise _Malformed("has no mcpServers object") from None
    return data["mcpServers"], raw


def _write_bytes(path: Path, data: bytes) -> None:
    """Temp file in the same dir, then os.replace; temp removed on failure."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".claude-sync-tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        tmp = ""
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)


def capture(claude_json: Path, repo: Path, policy: Policy, say: Say) -> bool:
    """Copy eligible live user-scope servers into REPO_FILE. True if written."""
    try:
        return _capture(claude_json, repo, policy, say)
    except Exception:  # noqa: BLE001 - must not abort sync or print a value
        say("mcp: capture skipped: internal error")
        return False


def _capture(claude_json: Path, repo: Path, policy: Policy, say: Say) -> bool:
    try:
        data, _meta = safejson.read_json_with_meta(claude_json, LIVE_LABEL)
    except safejson.NotFound:
        return False
    except safejson.SafeJsonError as exc:
        say(f"mcp: capture skipped: {exc}")
        return False
    live = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(live, dict):
        return False
    eligible, skipped = classify(live, policy)
    _say_skipped(skipped, say)
    target = repo / REPO_FILE
    try:
        found = _read_repo(target)
    except _Malformed as exc:
        say(f"mcp: capture skipped: {REPO_FILE} {exc}, left unchanged")
        return False
    if found is None:
        if not eligible:
            return False
        current, raw = {}, None
    else:
        current, raw = found
    new = render_repo(merge_for_repo(current, eligible))
    if new == raw:
        return False
    try:
        _write_bytes(target, new)
    except OSError as exc:
        say(f"mcp: capture failed: cannot write {REPO_FILE}: {exc.__class__.__name__}")
        return False
    return True


def apply(claude_json: Path, repo: Path, policy: Policy, backup_dir: Path, say: Say) -> bool:
    """Add or update eligible repo servers in the live file. True if written."""
    try:
        return _apply(claude_json, repo, policy, backup_dir, say)
    except Exception:  # noqa: BLE001 - must not abort sync or print a value
        say("mcp: apply skipped: internal error")
        return False


def _apply(claude_json: Path, repo: Path, policy: Policy, backup_dir: Path, say: Say) -> bool:
    try:
        found = _read_repo(repo / REPO_FILE)
    except _Malformed as exc:
        say(f"mcp: apply skipped: {REPO_FILE} {exc}")
        return False
    if found is None:
        return False
    eligible, skipped = classify(found[0], policy)
    _say_skipped(skipped, say)
    held: set[str] = set()
    for _attempt in range(ATTEMPTS):
        try:
            data, meta = safejson.read_json_with_meta(claude_json, LIVE_LABEL)
        except safejson.NotFound:
            say(f"mcp: apply skipped: {LIVE_LABEL} not found, not created")
            return False
        except safejson.SafeJsonError as exc:
            say(f"mcp: apply skipped: {exc}")
            return False
        if not isinstance(data, dict):
            say(f"mcp: apply skipped: {LIVE_LABEL} is not a JSON object, left unchanged")
            return False
        live = data.get("mcpServers", {})
        if not isinstance(live, dict):
            say(f"mcp: apply skipped: mcpServers in {LIVE_LABEL} is not an object, left unchanged")
            return False
        _live_ok, refused = classify(live, policy)
        clash = [(n, why) for n, why in refused if n in eligible]
        for name, why in clash:
            if name not in held:
                held.add(name)
                say(f"mcp: kept local {printable(str(name))}: live entry {why}")
        keep = {n for n, _ in clash}
        merged = merge_for_live(live, {n: v for n, v in eligible.items() if n not in keep})
        if merged == live:
            return False
        out = dict(data)
        out["mcpServers"] = merged
        try:
            return safejson.write_json_atomic(claude_json, out, meta, backup_dir=backup_dir)
        except safejson.Changed:
            continue
        except safejson.SafeJsonError as exc:
            say(f"mcp: apply failed: {exc}")
            return False
        except OSError as exc:
            say(f"mcp: apply failed: {exc.__class__.__name__}")
            return False
    say(f"mcp: apply skipped: {LIVE_LABEL} kept changing, try again")
    return False
