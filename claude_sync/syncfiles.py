"""Copies tracked Claude Code config between the repo working tree and the
real local paths: the Python port of lib/sync-files.sh.

Copy-based (not symlinked) so a tool writing a file via write-temp-then-rename
can never silently break the sync. `mirror` stands in for `rsync -a
[--delete]`:

- Exclude names match top-level entries only (rsync's anchored `/name`), and
  an excluded name is never copied, overwritten or deleted on the receiver.
- Files are written to a temp name in the destination folder, then moved into
  place with os.replace, so an interrupted copy never leaves a partial file.
- A file is skipped only when its bytes already match (size first, then
  content). rsync's size+mtime skip can leave a stale file; this cannot.
  Matching bytes with a different mtime or mode get those fixed in place.
- mtime and mode (the exec bit on POSIX) are kept. Bytes are copied as-is;
  line endings are never touched.
- Symlinks in the source: a link to a regular file is copied as that file's
  bytes. Links to folders, dangling links and other special files are skipped
  with a warning, and the receiver's entry of the same name is left alone.
  On the receiving side, links and junctions are removed, never followed.
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path

from claude_sync.util import check_real_dir

GITKEEP = ".gitkeep"
# Suffix of our own in-flight temp files. Leftovers (a killed run) are never
# copied and are always removed from a receiving folder.
TMP_SUFFIX = ".claude-sync-tmp"
_CHUNK = 1 << 16

# (kind, folder under the live home, folder in the repo). agents, hooks and
# skills are the keys of profile_excludes. rules and commands are shared only
# (no profile kind), so they never take excludes; nor does memory.
_CLAUDE_DIRS = (
    ("agents", "agents", "dot-claude/agents"),
    ("hooks", "hooks", "dot-claude/hooks"),
    ("rules", "rules", "dot-claude/rules"),
    ("commands", "commands", "dot-claude/commands"),
)
_AGENTS_DIRS = (("skills", "skills", "dot-agents/skills"),)
_FILES = (
    ("CLAUDE.md", "dot-claude/CLAUDE.md"),
    ("settings.json", "dot-claude/settings.json"),
    ("keybindings.json", "dot-claude/keybindings.json"),
)
# Repo folders capture always creates. gitflow.live_matches_commit uses this
# list too, so the two can never drift apart.
_REPO_DIRS = (
    "dot-claude/agents",
    "dot-claude/hooks",
    "dot-claude/rules",
    "dot-claude/commands",
    "dot-agents/skills",
    "memory",
)
_REPO_TOPS = ("dot-claude", "dot-agents")


def _warn(message: str) -> None:
    print(f"claude-sync: warning: {message}", file=sys.stderr)


def _make_writable(path: str | os.PathLike) -> None:
    try:
        mode = os.lstat(path).st_mode
        os.chmod(
            path,
            stat.S_IMODE(mode)
            | stat.S_IWRITE
            | stat.S_IREAD
            | (stat.S_IEXEC if stat.S_ISDIR(mode) else 0),
        )
    except OSError:
        pass


def _rmtree_onexc(func, path, _exc) -> None:
    # Read-only files (Windows) or folders (POSIX) block removal: unlock and retry.
    _make_writable(path)
    parent = os.path.dirname(path)
    if parent:
        _make_writable(parent)
    func(path)


def _remove(path: Path) -> None:
    """Remove a file, link, junction or folder without following links."""
    if not os.path.lexists(path):
        return
    if path.is_junction():
        os.rmdir(path)  # removes the junction itself, not its target
        return
    st = os.lstat(path)
    if stat.S_ISDIR(st.st_mode):
        shutil.rmtree(path, onexc=_rmtree_onexc)
        return
    try:
        os.unlink(path)
    except PermissionError:
        _make_writable(path)
        os.unlink(path)


def _same_bytes(a: Path, b: Path) -> bool:
    with open(a, "rb") as fa, open(b, "rb") as fb:
        while True:
            ca = fa.read(_CHUNK)
            if ca != fb.read(_CHUNK):
                return False
            if not ca:
                return True


def _is_tmp(name: str) -> bool:
    return name.endswith(TMP_SUFFIX)


def _sweep_tmp(folder: Path, prefix: str = "") -> None:
    """Remove leftover temp files (from a killed run) directly in folder."""
    try:
        with os.scandir(folder) as it:
            names = [
                e.name for e in it if _is_tmp(e.name) and e.name.startswith(prefix)
            ]
    except FileNotFoundError:
        return
    for name in names:
        _remove(folder / name)


def _copy_file(src: Path, dst: Path, *, delete: bool = True) -> None:
    """Copy src's bytes, mtime and mode to dst, atomically, unless they match.

    With delete=False a non-empty folder in the way is kept (with a warning)
    instead of removed, as rsync -a without --delete does.
    """
    sst = os.stat(src)  # follows a source link to its file
    if os.path.lexists(dst):
        dlst = os.lstat(dst)
        if (
            not delete
            and stat.S_ISDIR(dlst.st_mode)
            and not dst.is_junction()
            and any(dst.iterdir())
        ):
            _warn(f"kept non-empty folder where the source has a file: {dst}")
            return
        if (
            stat.S_ISLNK(dlst.st_mode)
            or stat.S_ISDIR(dlst.st_mode)
            or dst.is_junction()
        ):
            _remove(dst)
        elif (
            stat.S_ISREG(dlst.st_mode)
            and dlst.st_size == sst.st_size
            and _same_bytes(src, dst)
        ):
            if stat.S_IMODE(dlst.st_mode) != stat.S_IMODE(sst.st_mode):
                os.chmod(dst, stat.S_IMODE(sst.st_mode))
            if dlst.st_mtime_ns != sst.st_mtime_ns:
                os.utime(dst, ns=(sst.st_atime_ns, sst.st_mtime_ns))
            return
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{dst.name}.", suffix=TMP_SUFFIX, dir=dst.parent
    )
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        shutil.copy2(src, tmp)
        # os.replace cannot overwrite a read-only file on Windows.
        if os.name == "nt" and os.path.lexists(dst):
            _make_writable(dst)
        os.replace(tmp, dst)
    except BaseException:
        try:
            _remove(tmp)
        except OSError:
            pass
        raise


def _mirror(
    src: Path,
    dst: Path,
    *,
    delete: bool,
    exclude: frozenset[str],
    exclude_anywhere: frozenset[str],
    nested: bool = False,
) -> None:
    """mirror() plus names excluded at every depth (rsync's unanchored pattern).

    A top-level dst that is a link to a folder is followed, as rsync does for
    `dst/`; inside the tree a link or file in the way of a folder is replaced.
    """
    if (
        nested
        and os.path.lexists(dst)
        and (dst.is_symlink() or dst.is_junction() or not dst.is_dir())
    ):
        _remove(dst)
    dst.mkdir(parents=True, exist_ok=True)
    _sweep_tmp(dst)

    with os.scandir(src) as it:
        entries = sorted(it, key=lambda e: e.name)
    seen = {e.name for e in entries}
    for entry in entries:
        name = entry.name
        if name in exclude or name in exclude_anywhere or _is_tmp(name):
            continue
        source, target = Path(entry.path), dst / name
        if entry.is_symlink() or entry.is_junction():
            if not entry.is_junction() and entry.is_file():  # a link to a file
                _copy_file(source, target, delete=delete)
            else:
                _warn(f"skipped link to a folder or missing target: {source}")
            continue
        if entry.is_dir(follow_symlinks=False):
            _mirror(
                source,
                target,
                delete=delete,
                exclude=frozenset(),
                exclude_anywhere=exclude_anywhere,
                nested=True,
            )
        elif entry.is_file(follow_symlinks=False):
            _copy_file(source, target, delete=delete)
        else:
            _warn(f"skipped special file: {source}")

    if not delete:
        return
    with os.scandir(dst) as it:
        stale = [
            e.name
            for e in it
            if e.name not in seen
            and e.name not in exclude
            and e.name not in exclude_anywhere
        ]
    for name in stale:
        _remove(dst / name)


def mirror(
    src: Path, dst: Path, *, delete: bool, exclude: set[str] = frozenset()
) -> None:
    """Copy the tree under src into dst, like `rsync -a [--delete] src/ dst/`.

    `exclude` holds top-level entry names: they are neither copied nor
    deleted, and the receiver's copy is left as it is. With delete=True,
    entries in dst that src lacks are removed. A dst that is itself a link
    to a folder is followed (the caller trusts its own top-level paths).
    """
    _mirror(
        Path(src),
        Path(dst),
        delete=delete,
        exclude=frozenset(exclude),
        exclude_anywhere=frozenset(),
    )


def _kind_excludes(
    profile_excludes: Mapping[str, Iterable[str]] | None, kind: str
) -> frozenset[str]:
    if not profile_excludes:
        return frozenset()
    # Bash wrote rsync patterns as "/name"; accept either form.
    return frozenset(n.removeprefix("/") for n in profile_excludes.get(kind, ()))


def _pairs(claude_home: Path, agents_home: Path, repo: Path):
    for kind, live, in_repo in _CLAUDE_DIRS:
        yield kind, claude_home / live, repo / in_repo
    for kind, live, in_repo in _AGENTS_DIRS:
        yield kind, agents_home / live, repo / in_repo


def sync_capture(
    claude_home: Path,
    agents_home: Path,
    repo: Path,
    profile_excludes: Mapping[str, Iterable[str]] | None = None,
    *,
    memory_dir: Path,
) -> None:
    """Copy live config into the repo (bash sync_capture).

    CLAUDE.md, settings.json and keybindings.json are copied when present
    (never deleted). agents, hooks, rules, commands, skills and memory are
    mirrored with delete, `.gitkeep` excluded at any depth. `profile_excludes`
    maps a kind ("agents", "hooks", "skills") to top-level names that are
    neither copied nor deleted on the repo side; rules, commands and memory
    never take excludes. A missing live folder or file is skipped, so it
    never empties the repo copy.
    """
    claude_home, agents_home, repo, memory_dir = map(
        Path, (claude_home, agents_home, repo, memory_dir)
    )
    # The repo side is never followed: a linked folder would have capture
    # write (and delete) outside the clone. Checked before mkdir, which
    # accepts a link to a folder without a word.
    for d in _REPO_TOPS + _REPO_DIRS:
        check_real_dir(repo / d)
    for d in _REPO_DIRS:
        (repo / d).mkdir(parents=True, exist_ok=True)
    for live, in_repo in _FILES:
        if (claude_home / live).is_file():
            _copy_file(claude_home / live, repo / in_repo)
        _sweep_tmp((repo / in_repo).parent, f".{Path(in_repo).name}.")
    keep = frozenset({GITKEEP})
    for kind, live, in_repo in _pairs(claude_home, agents_home, repo):
        if live.is_dir():
            _mirror(
                live,
                in_repo,
                delete=True,
                exclude=_kind_excludes(profile_excludes, kind),
                exclude_anywhere=keep,
            )
    if memory_dir.is_dir():
        _mirror(
            memory_dir,
            repo / "memory",
            delete=True,
            exclude=frozenset(),
            exclude_anywhere=keep,
        )


def sync_apply(
    repo: Path,
    claude_home: Path,
    agents_home: Path,
    *,
    mirror: bool,
    profile_excludes: Mapping[str, Iterable[str]] | None = None,
    memory_dir: Path,
) -> None:
    """Copy repo config out to the live paths (bash sync_apply).

    mirror=True makes agents, hooks, rules, commands, skills and memory an
    exact copy of the repo, deleting local-only entries (steady state).
    keybindings.json is copied like CLAUDE.md, never merged or deleted.
    mirror=False merges without deleting (a machine's first-ever sync). Nothing is excluded,
    `.gitkeep` included, as in bash.

    `profile_excludes` (same shape as for sync_capture) defaults to none, and
    the CLI passes none, matching bash: profile_unapply removes owned entries
    so that this call restores the shared copy. Passing owned names here
    would stop that restore.
    """
    repo, claude_home, agents_home, memory_dir = map(
        Path, (repo, claude_home, agents_home, memory_dir)
    )
    for _kind, live, _repo_dir in _pairs(claude_home, agents_home, repo):
        live.mkdir(parents=True, exist_ok=True)
    memory_dir.mkdir(parents=True, exist_ok=True)
    for live, in_repo in _FILES:
        if (repo / in_repo).is_file():
            _copy_file(repo / in_repo, claude_home / live)
        _sweep_tmp(claude_home, f".{live}.")
    for kind, live, in_repo in _pairs(claude_home, agents_home, repo):
        if in_repo.is_dir():
            _mirror(
                in_repo,
                live,
                delete=mirror,
                exclude=_kind_excludes(profile_excludes, kind),
                exclude_anywhere=frozenset(),
            )
    if (repo / "memory").is_dir():
        _mirror(
            repo / "memory",
            memory_dir,
            delete=mirror,
            exclude=frozenset(),
            exclude_anywhere=frozenset(),
        )
