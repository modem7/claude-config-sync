"""Discovery links from <claude_home>/skills/<name> to <agents_home>/skills/<name>.

Claude Code discovers skills under ~/.claude/skills, but only ~/.agents/skills
is tracked. Python twin of lib/skills-symlink.sh. As there, the tracked set
is the skill folders in the repo's dot-agents/skills plus the machine
profile's skills/, minus the names the profile disables (tracked_skills);
a folder that is only in <agents_home>/skills gets no link. Links are symlinks on POSIX and directory
junctions on Windows. A real directory is never replaced or removed.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path

_DISABLED = ".disabled"


def _is_link(path: Path) -> bool:
    if os.path.islink(path):
        return True
    return os.name == "nt" and os.path.isjunction(path)


def read_link(link: Path) -> Path | None:
    """Return where a symlink or junction points, or None if `link` is neither."""
    if not _is_link(link):
        return None
    try:
        target = os.readlink(link)
    except OSError:
        return None
    if os.name == "nt":
        # Junctions read back with the extended-length prefix.
        for prefix in ("\\\\?\\UNC\\", "\\\\?\\", "\\??\\"):
            if target.startswith(prefix):
                target = ("\\\\" if "UNC" in prefix else "") + target[len(prefix):]
                break
    return Path(target)


def _remove_link(link: Path) -> None:
    if os.name == "nt" and not os.path.islink(link):
        os.rmdir(link)  # a junction is removed like an empty directory
    else:
        os.unlink(link)


def make_link(target: Path, link: Path) -> None:
    """Create `link` pointing at `target`, replacing an existing link (never a real path)."""
    if _is_link(link):
        _remove_link(link)
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link)


def _points_into(target: Path | None, root: Path) -> bool:
    if target is None:
        return False
    return os.path.normcase(str(target)).startswith(os.path.normcase(str(root)) + os.sep)


def tracked_skills(repo: Path, profile_dir: Path | None = None) -> tuple[list[str], list[str]]:
    """(tracked, disabled) skill names, as bash wire_skill_symlinks works them
    out: the folders in <repo>/dot-agents/skills and <profile_dir>/skills,
    minus the names the profile disables with a <name>.disabled marker."""
    roots = [Path(repo) / "dot-agents" / "skills"]
    disabled: set[str] = set()
    if profile_dir is not None:
        pskills = Path(profile_dir) / "skills"
        roots.append(pskills)
        if pskills.is_dir():
            disabled = {
                e.name.removesuffix(_DISABLED) for e in pskills.iterdir() if e.name.endswith(_DISABLED)
            }
    names: set[str] = set()
    for root in roots:
        if root.is_dir():
            names.update(e.name for e in root.iterdir() if e.is_dir() and not e.name.endswith(_DISABLED))
    return sorted(names - disabled), sorted(disabled)


def wire_skill_links(
    claude_home: Path,
    agents_home: Path,
    tracked: Iterable[str],
    disabled: Iterable[str],
) -> None:
    """Link <claude_home>/skills/<name> to <agents_home>/skills/<name> for
    each tracked name, remove the links of `disabled` names, then remove
    dangling links into <agents_home>/skills. Both lists come from
    tracked_skills. Run it after sync apply."""
    claude_skills = Path(claude_home) / "skills"
    agents_skills = Path(agents_home) / "skills"
    claude_skills.mkdir(parents=True, exist_ok=True)

    off = set(disabled)
    for name in sorted(set(tracked) - off):
        link = claude_skills / name
        target = agents_skills / name
        if _is_link(link):
            if read_link(link) == target and link.exists():
                continue
            make_link(target, link)
        elif not os.path.lexists(link):
            make_link(target, link)

    for name in sorted(off):
        link = claude_skills / name
        if _is_link(link) and read_link(link) == agents_skills / name:
            _remove_link(link)

    for link in sorted(claude_skills.iterdir()):
        if not _is_link(link) or link.exists():
            continue
        if _points_into(read_link(link), agents_skills):
            _remove_link(link)


def remove_skill_links(claude_home: Path, agents_home: Path) -> None:
    """Remove every link under <claude_home>/skills that points into <agents_home>/skills."""
    claude_skills = Path(claude_home) / "skills"
    agents_skills = Path(agents_home) / "skills"
    if not claude_skills.is_dir():
        return
    for link in sorted(claude_skills.iterdir()):
        if _is_link(link) and _points_into(read_link(link), agents_skills):
            _remove_link(link)
