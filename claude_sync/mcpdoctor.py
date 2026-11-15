"""The read-only MCP server check `claude-sync doctor` prints (issue 75).

Reads ~/.claude.json (found next to the Claude home, so a test's fake home
is used) and the repo's integrations.json. Writes nothing and runs nothing.
Lists each MCP server with its scope, transport and registry bucket, and
warns about literal credentials and a group- or world-readable file.

Never prints a server's command, args, env values, url or headers: a
warning names only where the credential is (env KEY, header KEY, arg #N
counted from 1, or url). Nothing here is an ISSUE and nothing changes the
exit code. Each user-scope server also gets a sync status from the same
mcpsync.classify() that sync uses; they sync only with MCP_SYNC=on in
machines/<host>.conf on the full tier. Doctor itself only reports.
"""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlsplit

from claude_sync.util import printable

if TYPE_CHECKING:
    from claude_sync.gitflow import SyncCtx

BUCKETS = ("off_until_asked", "enabled_per_project", "uninstalled")
TRANSPORTS = ("stdio", "http", "sse", "ws", "sdk")

# Key names: split into words, then match whole words (or these endings),
# so NODE_TLS_REJECT_UNAUTHORIZED, AUTHOR and TOKENIZER_PATH are not hits.
_SECRET_WORDS = {
    "token", "tokens", "key", "keys", "secret", "secrets", "password", "passwords",
    "passwd", "auth", "authorization", "bearer", "credential", "credentials",
    "cookie", "cookies", "pat",
}
_SECRET_ENDINGS = (
    "token", "secret", "password", "passwd", "apikey", "accesskey", "privatekey",
    "credential", "credentials", "cookie",
)
# A key whose last word is one of these names where a secret is, not the secret.
_NOT_SECRET_LAST = {"file", "path", "dir", "url", "uri", "endpoint", "host", "name", "type", "mode", "id"}

_REF = r"\$\{[A-Za-z_][A-Za-z0-9_]*(?::-[^}]*)?\}"
_REFS_ONLY = re.compile(rf"(?:\s*{_REF}\s*)+")
_SCHEME = re.compile(r"(?i)^(?:bearer|basic|token)\s+")
_PREFIXES = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    r"sk-[A-Za-z0-9_\-]{16,}"
    r"|gh[po]_[A-Za-z0-9]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|xox[bpas]-[A-Za-z0-9\-]{10,}"
    r"|eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{4,}"
    r"|AKIA[0-9A-Z]{16}"
    r")"
)
_AUTH_LITERAL = re.compile(r"(?i)(?<![A-Za-z0-9])(?:bearer|basic)\s+(\S+)")
_RANDOM = re.compile(r"[A-Za-z0-9_\-+/=]{24,}")
_URL = re.compile(r"(?i)^[a-z][a-z0-9+.\-]*://")
_BOOL_OR_INT = re.compile(r"(?i)^(?:true|false|yes|no|on|off|none|null|-?\d+)$")
_ENV_ASSIGN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.DOTALL)
_OPTION = re.compile(r"^--?([A-Za-z][A-Za-z0-9_\-.]*)(?:=(.*))?$", re.DOTALL)
_HEADER = re.compile(r"^([A-Za-z][A-Za-z0-9_\-]*):\s*(.+)$", re.DOTALL)


def _words(name: str) -> list[str]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    return [w.casefold() for w in re.split(r"[^A-Za-z0-9]+", spaced) if w]


def secret_name(name: str) -> bool:
    """Whether an env, header, option or query name says it holds a secret."""
    words = _words(name)
    if not words or words[-1] in _NOT_SECRET_LAST:
        return False
    joined = "".join(words)
    return any(w in _SECRET_WORDS for w in words) or joined.endswith(_SECRET_ENDINGS)


def allowed(value: str) -> bool:
    """Empty, or only ${VAR} / ${VAR:-default} references (after an optional
    Bearer/Basic/Token scheme). Anything else is a literal."""
    rest = _SCHEME.sub("", value.strip(), count=1)
    return rest == "" or _REFS_ONLY.fullmatch(rest) is not None


def _path_like(value: str) -> bool:
    v = value.strip()
    return v.startswith(("/", "~", ".")) or "\\" in v or re.match(r"^[A-Za-z]:[\\/]", v) is not None


def _random_looking(value: str) -> bool:
    """24+ characters of token alphabet, with one long part mixing letters
    and digits (so hyphenated package names and UUIDs do not count)."""
    v = value.strip()
    if _path_like(v) or not _RANDOM.fullmatch(v):
        return False
    for part in re.split(r"[\-_./+=]+", v):
        if len(part) >= 16 and re.search(r"[A-Za-z]", part) and re.search(r"\d", part):
            return True
    return False


def _url_literal(value: str) -> bool:
    try:
        parts = urlsplit(value.strip())
        user, password = parts.username, parts.password
    except ValueError:
        return False
    if password is not None and not allowed(password):
        return True
    if user and password is None and not allowed(user) and _looks_secret(user):
        return True
    for name, val in parse_qsl(parts.query, keep_blank_values=True):
        if secret_name(name) and not allowed(val):
            return True
    return False


def _looks_secret(value: str) -> bool:
    """The value alone looks like a credential, whatever its key."""
    if allowed(value):
        return False
    if _PREFIXES.search(value):
        return True
    for literal in _AUTH_LITERAL.findall(value):
        if not allowed(literal):
            return True
    if _URL.match(value.strip()) and _url_literal(value):
        return True
    return _random_looking(value)


def literal_under(key: str, value: str) -> bool:
    """A value under a named key (env, header, option, NAME=value arg)."""
    if allowed(value):
        return False
    if secret_name(key) and not (_BOOL_OR_INT.match(value.strip()) or _path_like(value)):
        return True
    return _looks_secret(value)


def _header_literal(text: str) -> bool:
    """A header passed as an arg ("X-Api-Key: value", as mcp-remote's
    --header takes it) with a literal value. URLs and drive paths are not headers."""
    m = _HEADER.match(text)
    if not m or _URL.match(text) or re.match(r"^[A-Za-z]:[\\/]", text):
        return False
    return literal_under(m.group(1), m.group(2))


def scan_server(server: dict[str, Any], *, scan_command: bool = False) -> list[str]:
    """Where a server holds literal credentials: 'env KEY', 'header KEY',
    'arg #N' (from 1) or 'url', plus 'command' (first) when scan_command is
    set and the command itself looks like a credential. Never the value."""
    found: list[str] = []
    command = server.get("command")
    if scan_command and isinstance(command, str) and _looks_secret(command):
        found.append("command")
    for field, label in (("env", "env"), ("headers", "header")):
        table = server.get(field)
        if isinstance(table, dict):
            for key, value in table.items():
                if isinstance(key, str) and isinstance(value, str) and literal_under(key, value):
                    found.append(f"{label} {printable(key)}")
    args = server.get("args")
    if isinstance(args, list):
        flagged: set[int] = set()
        for i, arg in enumerate(args):
            if not isinstance(arg, str) or i in flagged:
                continue
            hit = False
            opt = _OPTION.match(arg)
            assign = _ENV_ASSIGN.match(arg)
            if opt and opt.group(2) is not None:
                hit = literal_under(opt.group(1), opt.group(2)) or _header_literal(opt.group(2))
            elif opt and secret_name(opt.group(1)) and i + 1 < len(args):
                nxt = args[i + 1]
                if isinstance(nxt, str) and not nxt.startswith("-") and literal_under(opt.group(1), nxt):
                    flagged.add(i + 1)
                    found.append(f"arg #{i + 2}")
                    continue
            elif assign:
                hit = literal_under(assign.group(1), assign.group(2))
            else:
                hit = _header_literal(arg) or _looks_secret(arg)
            if hit:
                flagged.add(i)
                found.append(f"arg #{i + 1}")
    url = server.get("url")
    if isinstance(url, str) and not allowed(url) and (_url_literal(url) or _PREFIXES.search(url)):
        found.append("url")
    return found


def _full(path: str) -> str:
    return path.replace("\\", "/").rstrip("/").casefold()


def _base(path: str) -> str:
    """Last path part, case-folded, for / and \\ paths alike (list_disabled.base)."""
    return re.split(r"[\\/]", path.rstrip("\\/"))[-1].casefold()


def match(wanted: str, live: list[str]) -> list[str]:
    """Live project paths for a registry project, as list_disabled's
    Live.match: the same path if one is live, else every same basename."""
    exact = [p for p in live if _full(p) == _full(wanted)]
    return exact or [p for p in live if _base(p) == _base(wanted)]


Registry = list[tuple[str, str, list[str], list[str]]]


def _strings(value: Any) -> list[str]:
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def load_registry(path: Path) -> Registry | None:
    """(bucket, name, projects, machines) for each mcp entry, or None if unusable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    out: Registry = []
    for bucket in BUCKETS:
        entries = data.get(bucket)
        for e in entries if isinstance(entries, list) else []:
            if isinstance(e, dict) and e.get("kind") == "mcp" and isinstance(e.get("name"), str):
                out.append((bucket, e["name"], _strings(e.get("projects")), _strings(e.get("machines"))))
    return out


def bucket_of(registry: Registry, name: str, project: str | None, live: list[str]) -> str:
    """The registry bucket for a server at user scope (project None) or in a
    live project. An entry with no projects covers every scope; one with
    projects covers the live paths `match` gives (and user scope only for
    uninstalled). A name listed only for other projects says so."""
    exact: list[str] = []
    other: list[str] = []
    for bucket, entry_name, projects, _machines in registry:
        if entry_name != name:
            continue
        if not projects or (project is None and bucket == "uninstalled"):
            hit = True
        elif project is None:
            hit = False
        else:
            hit = any(project in match(w, live) for w in projects)
        (exact if hit else other).append(bucket)
    if exact:
        return ", ".join(dict.fromkeys(exact))
    if other:
        return ", ".join(dict.fromkeys(other)) + " (listed for other projects)"
    return "unclassified"


def _transport(server: dict[str, Any]) -> str:
    kind = server.get("type")
    if kind is None:
        return "stdio"
    return kind if isinstance(kind, str) and kind in TRANSPORTS else "unknown"


def _mode_warning(path: Path) -> str | None:
    if os.name == "nt":
        return None
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return None
    if mode & 0o077:
        return f"WARNING: ~/.claude.json has mode {mode:04o}; 0600 is expected (owner read and write only)."
    return None


def _read(path: Path) -> dict[str, Any] | str:
    """The parsed file, or a fixed reason it was skipped (never its contents)."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return "~/.claude.json not found"
    except OSError:
        return "~/.claude.json is not readable"
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except UnicodeDecodeError:
        return "~/.claude.json is not valid UTF-8"
    except json.JSONDecodeError as err:
        return f"~/.claude.json is not valid JSON (line {err.lineno} column {err.colno})"
    except ValueError:
        return "~/.claude.json is not valid JSON"
    if not isinstance(data, dict):
        return "~/.claude.json is not a JSON object"
    return data


def _sync_state(ctx: SyncCtx) -> tuple[bool, str | None]:
    """(MCP sync on for this machine, why it is off when that needs saying)."""
    from claude_sync import gitflow, machineconf
    from claude_sync.util import Fail

    try:
        if gitflow.mcp_sync_enabled(ctx):
            return True, None
    except OSError:
        return False, "machine conf not readable"
    except Fail:
        try:
            machineconf.read_machine_mcp_sync(ctx.repo, ctx.hostname)
        except Fail:
            return False, "MCP_SYNC value invalid"
        return False, "TIER value invalid"
    try:
        if machineconf.read_machine_mcp_sync(ctx.repo, ctx.hostname):
            return False, "basic tier"
    except Fail:
        return False, "MCP_SYNC value invalid"
    return False, None


def _user_reasons(ctx: SyncCtx, user: dict[str, Any]) -> dict[str, str | None]:
    """Why each user-scope server is kept local (None: it syncs), from the
    same policy and classify() that sync uses."""
    from claude_sync import gitflow, mcpsync
    from claude_sync.util import Fail

    try:
        names = mcpsync.host_names(ctx.hostname, gitflow.current_profile(ctx))
    except (Fail, OSError):
        names = mcpsync.host_names(ctx.hostname, "")
    policy = mcpsync.load_policy(ctx.repo / "integrations.json", names)
    if policy is None:
        return dict.fromkeys(user, "integrations.json unusable")
    eligible, skipped = mcpsync.classify(user, policy)
    reasons: dict[str, str | None] = dict.fromkeys(eligible)
    reasons.update(skipped)
    return reasons


def _status(on: bool, reason: str | None) -> str:
    if on:
        return "sync: yes" if reason is None else f"sync: no ({reason})"
    return "sync: off" if reason is None else f"sync: off (would be skipped: {reason})"


def _report(ctx: SyncCtx) -> list[str]:
    path = ctx.claude_home.parent / ".claude.json"
    data = _read(path)
    mode_line = _mode_warning(path) if path.is_file() else None
    if isinstance(data, str):
        return [f"MCP check skipped: {data}."] + ([mode_line] if mode_line else [])

    registry = load_registry(ctx.repo / "integrations.json")
    projects = data.get("projects") if isinstance(data.get("projects"), dict) else {}
    live = [p for p in projects if isinstance(p, str)]
    servers: list[tuple[str, str | None, dict[str, Any]]] = []
    user = data.get("mcpServers")
    if isinstance(user, dict):
        servers += [(n, None, s) for n, s in sorted(user.items()) if isinstance(s, dict)]
    for proj_path in sorted(live):
        proj = projects[proj_path]
        table = proj.get("mcpServers") if isinstance(proj, dict) else None
        if isinstance(table, dict):
            servers += [(n, proj_path, s) for n, s in sorted(table.items()) if isinstance(s, dict)]

    on, note = _sync_state(ctx)
    reasons = _user_reasons(ctx, {n: s for n, p, s in servers if p is None})
    conf = f"machines/{printable(ctx.hostname)}.conf"
    headline = (
        "MCP servers in ~/.claude.json (doctor only reports and changes nothing;"
        f" user-scope servers sync only with MCP_SYNC=on in {conf}, full tier):"
    )
    lines = [headline]
    if registry is None:
        lines.append("  (no usable integrations.json in the repo, so every server is unclassified)")
    warnings: list[str] = []
    flagged = 0
    for name, proj_path, server in servers:
        label = re.split(r"[\\/]", proj_path.rstrip("\\/"))[-1] if proj_path else ""
        scope = "user" if proj_path is None else printable(label or proj_path)
        shown = printable(str(name))
        bucket = bucket_of(registry or [], name, proj_path, live)
        status = _status(on, reasons.get(name)) if proj_path is None else "sync: no (project scope)"
        lines.append(f"  {shown} ({scope}): {_transport(server)}, {bucket}; {status}")
        where = scan_server(server)
        if where:
            flagged += 1
            warnings += [f"WARNING: MCP server {shown} ({scope}): literal credential in {w}" for w in where]
    if not servers:
        lines.append("  none")
    lines += warnings
    if mode_line:
        lines.append(mode_line)
    n_user = sum(1 for _, p, _ in servers if p is None)
    n_ok = sum(1 for n, p, _s in servers if p is None and reasons.get(n) is None)
    state = "on" if on else "off"
    if note:
        state += f" ({note})"
    lines.append(
        f"MCP servers: {n_user} user, {len(servers) - n_user} local; {flagged} with literal"
        f" credentials (kept local, they will never sync); {n_ok} user-scope eligible to sync."
        f" MCP sync is {state} for this machine."
    )
    return lines


def mcp_report(ctx: SyncCtx) -> list[str]:
    """Lines for doctor. Never raises, never writes, never prints a value."""
    try:
        return _report(ctx)
    except Exception:  # noqa: BLE001 - a report must not stop doctor or show a traceback
        return ["MCP check skipped: internal error."]
