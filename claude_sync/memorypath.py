"""Resolves the memory directory Claude Code uses for a working directory.

Port of lib/memory-path.sh. Claude Code names a project's folder after its
cwd with every character other than A-Z, a-z and 0-9 turned into a dash,
so separators, a drive colon, spaces, dots and underscores all become `-`.
The legacy bash only replaced `/`, so it differs for paths with any other
such character.

Claude Code also shortens a very long folder name and adds a hash. That
rule is not reproduced here, so long_name_warning flags a name over
LONG_NAME characters instead of guessing.
"""

from __future__ import annotations

import re
from pathlib import Path

from claude_sync.util import printable

_NOT_ALNUM = re.compile(r"[^A-Za-z0-9]")


def encode_project_path(path: str) -> str:
    """Replace each character other than A-Z, a-z and 0-9 with `-`."""
    return _NOT_ALNUM.sub("-", path)


def resolve_memory_dir(claude_home: Path, project_path: str) -> Path:
    return Path(claude_home) / "projects" / encode_project_path(project_path) / "memory"


LONG_NAME = 200


def long_name_warning(project_path: str) -> str | None:
    """A warning when the folder name for `project_path` is over LONG_NAME
    characters, else None."""
    name = encode_project_path(project_path)
    if len(name) <= LONG_NAME:
        return None
    return (
        f"the project path {printable(project_path)} gives a memory folder name of {len(name)} characters."
        " Claude Code shortens very long names and adds a hash, which claude-sync does not do,"
        " so the memory synced may not be the folder Claude Code uses."
        f" Use a project path whose name is at most {LONG_NAME} characters"
        " (PRIMARY_PROJECT_PATH in machines/<hostname>.conf)."
    )
