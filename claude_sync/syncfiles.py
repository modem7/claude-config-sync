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
- Apply can pass a Backup: each live file it is about to overwrite or delete
  is copied there first (see Backup).
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import sys
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from claude_sync.util import Fail, check_real_dir

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


# ── backups of what apply replaces ───────────────────────────────────────

# The folder under the live Claude home that holds one folder per apply.
# Nothing else reads it: capture copies only named files and folders.
BACKUP_DIR = ".claude-sync-backups"
DEFAULT_BACKUPS = 10
# Run folder names: UTC time with microseconds, so names are unique and
# sort by time. Prune touches only names of this shape.
_RUN_NAME = re.compile(r"^\d{8}T\d{6}\.\d{6}Z$")


def backups_from_env(env: Mapping[str, str]) -> int:
    """How many backup folders to keep: CLAUDE_SYNC_BACKUPS, default 10.
    0 turns backups off."""
    raw = env.get("CLAUDE_SYNC_BACKUPS", "").strip()
    if not raw:
        return DEFAULT_BACKUPS
    if not raw.isascii() or not raw.isdigit():
        raise Fail(
            f"CLAUDE_SYNC_BACKUPS must be a whole number, not '{raw}'.",
            hint="  Set it to 0 (no backups) or more (for example 10), or unset it for the default.",
        )
    return int(raw)


def _check_backup_root(root: Path) -> None:
    """Fail unless root is missing or a real folder (never a link)."""
    if os.path.lexists(root) and (
        root.is_symlink() or root.is_junction() or not root.is_dir()
    ):
        raise Fail(
            f"{root} is a link or not a folder, so apply cannot keep backups there.",
            hint=f"  Move it out of the way (it must be a real folder or absent), or set"
            " CLAUDE_SYNC_BACKUPS=0, then re-run. Nothing was changed.",
        )
    check_real_dir(root)


def _private_dir(path: Path) -> None:
    """mkdir with mode 0700 on POSIX, also when the folder exists."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "posix":
        os.chmod(path, 0o700)


class Backup:
    """Keeps a copy of each live file an apply overwrites or deletes.

    Files go to <root>/<UTC time>/<path>, where <path> is relative to the
    first matching home in `homes` with its prefix in front (the Claude home
    has prefix "", ~/.agents has ".agents/"). The run folder is made only
    when the first file is saved. Links are never followed: a link is not
    saved (removing it loses no data) and a folder is walked without
    following any link in it. Each path is saved once, so the first
    (original) bytes win.

    `finish` runs after the whole apply: a saved file whose live path now
    holds the same bytes again (a settings override or profile layered back
    on top) is dropped, so an unchanged apply leaves no folder.
    """

    def __init__(self, root: Path, homes: Sequence[tuple[Path, str]]) -> None:
        self.root = Path(root)
        self.homes = [(Path(h), prefix) for h, prefix in homes]
        self.run: Path | None = None
        self.saved: dict[str, Path] = {}

    def check(self) -> None:
        """Fail before apply changes anything if the root is a link."""
        _check_backup_root(self.root)

    def _rel(self, path: Path) -> str | None:
        for home, prefix in self.homes:
            try:
                rel = Path(path).relative_to(home)
            except ValueError:
                continue
            return prefix + rel.as_posix()
        return None

    def _run_dir(self) -> Path:
        if self.run is None:
            _check_backup_root(self.root)
            _private_dir(self.root)
            while True:
                name = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
                run = self.root / name
                try:
                    run.mkdir(mode=0o700)
                except FileExistsError:
                    continue
                break
            if os.name == "posix":
                os.chmod(run, 0o700)
            self.run = run
        return self.run

    def _save_file(self, path: Path) -> None:
        if _is_tmp(path.name):
            return
        rel = self._rel(path)
        if rel is None:
            _warn(f"not backed up (outside the synced homes): {path}")
            return
        if rel in self.saved:
            return
        run = self._run_dir()
        target = run / rel
        parent = target.parent
        missing = []
        while parent != run and not parent.exists():
            missing.append(parent)
            parent = parent.parent
        for folder in reversed(missing):
            _private_dir(folder)
        shutil.copy2(path, target, follow_symlinks=False)
        self.saved[rel] = Path(path)

    def _save_tree(self, folder: Path) -> None:
        with os.scandir(folder) as it:
            entries = sorted(it, key=lambda e: e.name)
        for entry in entries:
            if entry.is_symlink() or entry.is_junction():
                continue
            if entry.is_dir(follow_symlinks=False):
                self._save_tree(Path(entry.path))
            elif entry.is_file(follow_symlinks=False):
                self._save_file(Path(entry.path))

    def save(self, path: Path) -> None:
        """Copy path (a file, or every file under a folder) before it goes."""
        path = Path(path)
        if not os.path.lexists(path) or path.is_symlink() or path.is_junction():
            return
        st = os.lstat(path)
        if stat.S_ISDIR(st.st_mode):
            self._save_tree(path)
        elif stat.S_ISREG(st.st_mode):
            self._save_file(path)

    def finish(self) -> Path | None:
        """Drop copies whose live file holds the same bytes again, then empty
        folders. Returns the run folder if anything is left, else None."""
        if self.run is None:
            return None
        run = self.run
        for rel, live in self.saved.items():
            copy = run / rel
            if (
                os.path.lexists(live)
                and not live.is_symlink()
                and live.is_file()
                and os.lstat(live).st_size == os.lstat(copy).st_size
                and _same_bytes(live, copy)
            ):
                _remove(copy)
        for dirpath, _dirs, _files in os.walk(run, topdown=False):
            try:
                os.rmdir(dirpath)
            except OSError:
                pass  # not empty
        if not os.path.lexists(run):
            self.run = None
            return None
        kept = sum(len(files) for _d, _s, files in os.walk(run))
        print(
            f"claude-sync: apply replaced or removed {kept} local file(s) that differed"
            f" from the repo; the old copies are in {run}",
            file=sys.stderr,
        )
        return run


def backup_runs(root: Path) -> list[Path]:
    """The run folders under root, oldest first. Links are not counted."""
    root = Path(root)
    if not root.is_dir() or root.is_symlink() or root.is_junction():
        return []
    with os.scandir(root) as it:
        names = [
            e.name
            for e in it
            if _RUN_NAME.match(e.name)
            and e.is_dir(follow_symlinks=False)
            and not e.is_junction()
        ]
    return [root / n for n in sorted(names)]


def newest_backup(root: Path) -> Path | None:
    runs = backup_runs(root)
    return runs[-1] if runs else None


def prune_backups(root: Path, keep: int) -> None:
    """Remove all but the newest `keep` run folders. keep=0 (backups off)
    leaves existing ones alone. A failure is a warning, never an error."""
    if keep <= 0:
        return
    try:
        runs = backup_runs(root)
        for old in runs[:-keep]:
            try:
                _remove(old)
            except OSError as err:
                _warn(f"could not remove old backup {old}: {err}")
        left = len(backup_runs(root))
    except OSError as err:
        _warn(f"could not prune backups in {root}: {err}")
        return
    if left > keep:
        _warn(
            f"{left} backup folders in {root}, more than CLAUDE_SYNC_BACKUPS={keep}."
            " Remove the oldest by hand to free the space."
        )


# ── copying ──────────────────────────────────────────────────────────────


def _copy_file(
    src: Path, dst: Path, *, delete: bool = True, backup: Backup | None = None
) -> None:
    """Copy src's bytes, mtime and mode to dst, atomically, unless they match.

    With delete=False a non-empty folder in the way is kept (with a warning)
    instead of removed, as rsync -a without --delete does. With a backup,
    whatever dst held is saved there before it is replaced.
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
            if backup is not None:
                backup.save(dst)  # links are skipped, a folder's files kept
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
        elif backup is not None:
            backup.save(dst)
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
    backup: Backup | None = None,
) -> None:
    """mirror() plus names excluded at every depth (rsync's unanchored pattern).

    A top-level dst that is a link to a folder is followed, as rsync does for
    `dst/`; inside the tree a link or file in the way of a folder is replaced.
    With a backup, what is replaced or deleted in dst is saved there first.
    """
    if (
        nested
        and os.path.lexists(dst)
        and (dst.is_symlink() or dst.is_junction() or not dst.is_dir())
    ):
        if backup is not None:
            backup.save(dst)
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
                _copy_file(source, target, delete=delete, backup=backup)
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
                backup=backup,
            )
        elif entry.is_file(follow_symlinks=False):
            _copy_file(source, target, delete=delete, backup=backup)
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
        if backup is not None:
            backup.save(dst / name)
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
    memory_dir: Path | None,
    memory_delete: bool = True,
) -> None:
    """Copy live config into the repo (bash sync_capture).

    CLAUDE.md, settings.json and keybindings.json are copied when present
    (never deleted). agents, hooks, rules, commands, skills and memory are
    mirrored with delete, `.gitkeep` excluded at any depth. `profile_excludes`
    maps a kind ("agents", "hooks", "skills") to top-level names that are
    neither copied nor deleted on the repo side; rules, commands and memory
    never take excludes. A missing live folder or file is skipped, so it
    never empties the repo copy.

    memory_dir=None leaves the repo's memory/ alone entirely (the basic
    tier): not checked, created, copied into or deleted from.
    memory_delete=False copies memory in without deleting (adopting memory
    after a switch from basic to full).
    """
    claude_home, agents_home, repo = map(Path, (claude_home, agents_home, repo))
    repo_dirs = tuple(d for d in _REPO_DIRS if memory_dir is not None or d != "memory")
    # The repo side is never followed: a linked folder would have capture
    # write (and delete) outside the clone. Checked before mkdir, which
    # accepts a link to a folder without a word.
    for d in _REPO_TOPS + repo_dirs:
        check_real_dir(repo / d)
    for d in repo_dirs:
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
    if memory_dir is not None and Path(memory_dir).is_dir():
        _mirror(
            Path(memory_dir),
            repo / "memory",
            delete=memory_delete,
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
    memory_dir: Path | None,
    memory_mirror: bool | None = None,
    backup: Backup | None = None,
) -> None:
    """Copy repo config out to the live paths (bash sync_apply).

    With a backup, every live file this overwrites with other bytes or
    deletes is copied into it first (the caller runs backup.finish).

    mirror=True makes agents, hooks, rules, commands, skills and memory an
    exact copy of the repo, deleting local-only entries (steady state).
    keybindings.json is copied like CLAUDE.md, never merged or deleted.
    mirror=False merges without deleting (a machine's first-ever sync). Nothing is excluded,
    `.gitkeep` included, as in bash.

    memory_dir=None leaves the local memory folder alone entirely (the
    basic tier): not created, copied into or deleted from. memory_mirror
    overrides `mirror` for memory only (None: same as `mirror`), so memory
    can be merged in while the rest is mirrored.

    `profile_excludes` (same shape as for sync_capture) defaults to none, and
    the CLI passes none, matching bash: profile_unapply removes owned entries
    so that this call restores the shared copy. Passing owned names here
    would stop that restore.
    """
    repo, claude_home, agents_home = map(Path, (repo, claude_home, agents_home))
    for _kind, live, _repo_dir in _pairs(claude_home, agents_home, repo):
        live.mkdir(parents=True, exist_ok=True)
    if memory_dir is not None:
        memory_dir = Path(memory_dir)
        memory_dir.mkdir(parents=True, exist_ok=True)
    for live, in_repo in _FILES:
        if (repo / in_repo).is_file():
            _copy_file(repo / in_repo, claude_home / live, backup=backup)
        _sweep_tmp(claude_home, f".{live}.")
    for kind, live, in_repo in _pairs(claude_home, agents_home, repo):
        if in_repo.is_dir():
            _mirror(
                in_repo,
                live,
                delete=mirror,
                exclude=_kind_excludes(profile_excludes, kind),
                exclude_anywhere=frozenset(),
                backup=backup,
            )
    if memory_dir is not None and (repo / "memory").is_dir():
        _mirror(
            repo / "memory",
            memory_dir,
            delete=mirror if memory_mirror is None else memory_mirror,
            exclude=frozenset(),
            exclude_anywhere=frozenset(),
            backup=backup,
        )


def differing_files(src: Path, dst: Path) -> list[str]:
    """Paths (relative, `/`-separated, sorted) of regular files under src
    whose counterpart under dst is also a regular file but holds other
    bytes: what a non-deleting copy of src over dst would overwrite. Links
    are not followed on either side."""
    src, dst = Path(src), Path(dst)
    found: list[str] = []
    if not src.is_dir() or not dst.is_dir():
        return found
    for root, dirs, files in os.walk(src):
        rel_root = Path(root).relative_to(src)
        dirs[:] = [d for d in dirs if not (Path(root) / d).is_symlink()]
        for name in files:
            a, b = Path(root) / name, dst / rel_root / name
            if a.is_symlink() or b.is_symlink() or not b.is_file() or _is_tmp(name):
                continue
            if a.stat().st_size != b.stat().st_size or not _same_bytes(a, b):
                found.append((rel_root / name).as_posix())
    return sorted(found)
