"""Reads and writes the per-machine config file (machines/<hostname>.conf).

The file maps a hostname to the local project
whose memory dir is synced, and optionally to the machine profile it follows.
Files are handled as bytes so lines this code does not know survive intact.

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

IS_WINDOWS = os.name == "nt"

_PROJECT_KEY = b"PRIMARY_PROJECT_PATH="
_PROFILE_KEY = b"PROFILE="

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


def write_machine_profile(repo: Path, hostname: str, profile: str) -> None:
    """Set the PROFILE line, or remove it when `profile` is empty. Other lines stay."""
    conf = machine_conf_path(repo, hostname)
    data = conf.read_bytes() if conf.is_file() else b""
    kept = [ln for ln in data.splitlines(keepends=True) if not ln.startswith(_PROFILE_KEY)]
    out = b"".join(kept)
    if profile:
        if out and not out.endswith(b"\n"):
            out += b"\n"
        out += _PROFILE_KEY + profile.encode("utf-8") + b"\n"
    conf.parent.mkdir(parents=True, exist_ok=True)
    conf.write_bytes(out)
