"""Reads and writes the per-machine config file (machines/<hostname>.conf).

The file maps a hostname to the local project
whose memory dir is synced, optionally to the machine profile it follows
(PROFILE=), and optionally to its sync tier (TIER=basic|full; no line means
full). Files are handled as bytes so lines this code does not know survive
intact.

On Windows the project path is stored and shown in the native drive form
(`Z:\\projects`). A conf written by the bash under Git Bash holds `/z/projects`;
read_primary_project_path turns that into `Z:\\projects` in memory and leaves
the file as it is, so both forms give the same memory folder. The drive
letter is upper-cased (Windows reports the cwd that way). Elsewhere the path
is used as stored.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from claude_sync.util import Fail

IS_WINDOWS = os.name == "nt"

_PROJECT_KEY = b"PRIMARY_PROJECT_PATH="
_PROFILE_KEY = b"PROFILE="
_TIER_KEY = b"TIER="

# basic: CLAUDE.md, settings.json, agents, hooks, skills (and profiles and
# overrides). full: basic plus the primary project's memory folder and the
# python and npm companion tools. full is the default.
TIER_BASIC = "basic"
TIER_FULL = "full"
TIERS = (TIER_BASIC, TIER_FULL)
DEFAULT_TIER = TIER_FULL

# `/z` or `/z/...` (Git Bash), or `z:` / `z:\\...` / `z:/...` (native).
_DRIVE = re.compile(r"^(?:/([A-Za-z])|([A-Za-z]):)(?=[\\/]|$)")


def windows_native_path(path: str) -> str:
    """`/z/foo` (Git Bash) or `z:/foo` as `Z:\\foo`. Other paths (`/tmp`,
    `//server/share`, relative ones) are returned unchanged. Pure string
    work, so it behaves the same on any OS."""
    m = _DRIVE.match(path)
    if not m:
        return path
    rest = path[m.end():].replace("/", "\\")
    return f"{(m.group(1) or m.group(2)).upper()}:" + (rest or "\\")


def project_path_for_os(path: str) -> str:
    """The form this OS uses: native on Windows, as given elsewhere."""
    return windows_native_path(path) if IS_WINDOWS else path


def machine_conf_path(repo: Path, hostname: str) -> Path:
    return Path(repo) / "machines" / f"{hostname}.conf"


def write_machine_conf(repo: Path, hostname: str, project_path: str) -> None:
    """Create the conf with only the project path line (replaces any content)."""
    conf = machine_conf_path(repo, hostname)
    conf.parent.mkdir(parents=True, exist_ok=True)
    conf.write_bytes(_PROJECT_KEY + project_path.encode("utf-8") + b"\n")


def _value(repo: Path, hostname: str, key: bytes) -> str | None:
    conf = machine_conf_path(repo, hostname)
    if not conf.is_file():
        return None
    for line in conf.read_bytes().split(b"\n"):
        if line.startswith(key):
            return line[len(key):].rstrip(b"\r").decode("utf-8", errors="replace")
    return None


def read_primary_project_path(repo: Path, hostname: str) -> str | None:
    """The first PRIMARY_PROJECT_PATH value, or None when the conf or line is
    missing. On Windows a Git Bash form is returned in the native form."""
    value = _value(repo, hostname, _PROJECT_KEY)
    return None if value is None else project_path_for_os(value)


def read_machine_profile(repo: Path, hostname: str) -> str | None:
    """The PROFILE value, or None when there is no conf, no line or an empty value."""
    return _value(repo, hostname, _PROFILE_KEY) or None


def _set_line(repo: Path, hostname: str, key: bytes, value: str) -> None:
    """Set the `key` line to `value` (at the end), or remove it when `value`
    is empty. Every other line stays byte for byte."""
    conf = machine_conf_path(repo, hostname)
    data = conf.read_bytes() if conf.is_file() else b""
    kept = [ln for ln in data.splitlines(keepends=True) if not ln.startswith(key)]
    out = b"".join(kept)
    if value:
        if out and not out.endswith(b"\n"):
            out += b"\n"
        out += key + value.encode("utf-8") + b"\n"
    conf.parent.mkdir(parents=True, exist_ok=True)
    conf.write_bytes(out)


def write_machine_profile(repo: Path, hostname: str, profile: str) -> None:
    """Set the PROFILE line, or remove it when `profile` is empty. Other lines stay."""
    _set_line(repo, hostname, _PROFILE_KEY, profile)


def write_primary_project_path(repo: Path, hostname: str, project_path: str) -> None:
    """Set the PRIMARY_PROJECT_PATH line. Other lines stay."""
    _set_line(repo, hostname, _PROJECT_KEY, project_path)


def check_tier(value: str, where: str = "", hint: str = "") -> str:
    """`value` when it is a known tier, else Fail saying what is allowed."""
    if value in TIERS:
        return value
    shown = value.encode("unicode_escape").decode("ascii")
    raise Fail(
        f"Not a valid sync tier{where}: '{shown}'.",
        hint=hint or "  The tier is 'basic' or 'full' (the default).",
    )


def read_machine_tier(repo: Path, hostname: str) -> str:
    """The TIER value: 'full' when there is no conf, no line or an empty
    value. Fail for any other value than 'basic' or 'full'."""
    value = _value(repo, hostname, _TIER_KEY)
    value = (value or "").strip()
    if not value:
        return DEFAULT_TIER
    return check_tier(
        value,
        f" in machines/{hostname}.conf",
        f"  Set the TIER= line in {machine_conf_path(repo, hostname)} to 'basic' or 'full'"
        " (or remove it for full), then re-run.",
    )


def write_machine_tier(repo: Path, hostname: str, tier: str) -> None:
    """Set the TIER line. 'full' (the default) removes the line, so a full
    machine's conf looks as it did before tiers. Other lines stay."""
    check_tier(tier)
    _set_line(repo, hostname, _TIER_KEY, "" if tier == DEFAULT_TIER else tier)
