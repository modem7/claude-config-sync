"""Per-machine profiles.

A profile lets one machine differ from the shared config in agents, hooks,
skills and CLAUDE.md without changing the shared files:

  machines/<profile>/agents/<entry>            replaces/adds ~/.claude/agents/<entry>
  machines/<profile>/hooks/<entry>             replaces/adds ~/.claude/hooks/<entry>
  machines/<profile>/skills/<entry>/           replaces/adds ~/.agents/skills/<entry>/
  machines/<profile>/<kind>/<entry>.disabled   hides the shared <entry> on this machine
  machines/<profile>/CLAUDE.append.md          appended to ~/.claude/CLAUDE.md

The profile name is PROFILE= in machines/<hostname>.conf, else the hostname.
An entry is a top-level name in the kind folder; a whole skill folder is
replaced, never merged.

- apply: after the shared files are mirrored into place, copy each profile
  entry over the live path, delete disabled entries, and append
  CLAUDE.append.md after a marker line. A snapshot of what was applied is
  kept in a local-only stamp, written to `<stamp>.tmp` and renamed into
  place after the old record is removed, so a crash reads as "not applied".
- capture: every name the profile or the stamp mentions is owned by this
  machine, so it never flows into the shared copy (and the shared copy is
  never deleted for it). A live owned entry is copied back into the profile
  only when it differs from the stamp's snapshot, i.e. it was edited locally
  since the last apply. No snapshot means "not applied yet", not "edited".

CLAUDE.md is handled as bytes. The marker is a whole line: the first line
whose bytes are exactly MARKER (a CRLF marker line does not match, as with
bash's `grep -x`). Apply appends `\\n<marker>\\n` plus CLAUDE.append.md;
strip cuts just before the newline in front of the marker, so the shared
text round-trips byte for byte whatever its line endings.

The stamp's layout is the same as bash's, so bash and Python runs can take
turns on one machine.

Linux-only entries on Windows (a ruling; bash never ran there): when
IS_WINDOWS is true, a profile hook entry whose name ends in `.sh` is skipped.
It is not copied into the live hooks folder (the shared copy, if any, stays),
and it is left out of the stamp, so capture never sees it as a local edit
and never writes the shared bytes back into the profile. It stays an owned
name, so it is never captured into the shared copy either. It is not a live
mismatch, and profile_dangling_hook_refs ignores every `.sh` reference on
Windows (it has no profile to ask). `.disabled` markers for `.sh` hooks
still delete as usual. The same profile on Linux applies them normally.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import json
import tempfile
from pathlib import Path

from claude_sync import machineconf, overlay
from claude_sync.syncfiles import TMP_SUFFIX, Backup, _copy_file, _remove, _warn, mirror, sync_capture
from claude_sync.util import Fail, check_real_dir

KINDS = ("agents", "hooks", "skills")
MARKER = b"<!-- claude-sync:profile-append -->"
APPEND_FILE = "CLAUDE.append.md"
STAMP_NAME = ".claude-sync-applied-profile"
NAME_FILE = ".profile-name"
DISABLED = ".disabled"
_VALID = re.compile(r"[A-Za-z0-9._-]+")
_HOOK_REF = re.compile(r"\.claude[/\\]hooks[/\\]([A-Za-z0-9._-]+)")
LINUX_ONLY_SUFFIX = ".sh"
# Read at call time, so tests can simulate Windows on Linux.
IS_WINDOWS = os.name == "nt"
_POSIX = os.name == "posix"


# ── paths and names ──────────────────────────────────────────────────────


def profile_stamp_dir(claude_home: Path) -> Path:
    """Local-only record of the last applied profile, laid out like
    machines/<profile>/ plus .profile-name. Never synced."""
    return Path(claude_home) / STAMP_NAME


def profile_valid_name(name: str) -> bool:
    return bool(_VALID.fullmatch(name)) and name not in (".", "..")


def _check_name(name: str) -> None:
    if not profile_valid_name(name):
        raise Fail(
            f"'{name}' is not a valid profile name.",
            hint="Use only letters, digits, '.', '_' and '-' (not '.' or '..'),"
            " and fix PROFILE= in machines/<hostname>.conf or CLAUDE_SYNC_PROFILE.",
        )


def profile_resolve_name(repo: Path, hostname: str) -> str:
    """This machine's profile name: PROFILE= in its conf, else the hostname.
    Raises Fail when the name could lead outside machines/."""
    name = machineconf.read_machine_profile(repo, hostname) or hostname
    _check_name(name)
    return name


def profile_dir(repo: Path, profile: str) -> Path:
    """machines/<profile>. Every name passes through here, so a name that
    is invalid, or a profile folder that is a link or resolves outside
    machines/, raises Fail."""
    _check_name(profile)
    machines = check_real_dir(Path(repo) / "machines")
    pdir = machines / profile
    if os.path.lexists(pdir) and (
        pdir.is_symlink()
        or pdir.is_junction()
        or not pdir.resolve().is_relative_to(machines.resolve())
    ):
        raise Fail(
            f"{pdir} is a link or leads outside {machines}.",
            hint="A profile folder must be a real folder in machines/, not a link."
            " Replace it with the folder's contents, then try again.",
        )
    return pdir


def profile_shared_dir(repo: Path, kind: str) -> Path:
    """Where the shared copy of `kind` lives in the repo."""
    return Path(repo) / {
        "agents": "dot-claude/agents",
        "hooks": "dot-claude/hooks",
        "skills": "dot-agents/skills",
    }[kind]


def profile_live_dir(claude_home: Path, agents_home: Path, kind: str) -> Path:
    """Where `kind` lives on this machine."""
    if kind == "skills":
        return Path(agents_home) / "skills"
    return Path(claude_home) / kind


def _names(folder: Path) -> list[str]:
    """Top-level names in folder (dangling links too), sorted, no .gitkeep."""
    try:
        with os.scandir(folder) as it:
            return sorted(e.name for e in it if e.name != ".gitkeep")
    except (FileNotFoundError, NotADirectoryError):
        return []


def profile_entries(pdir: Path, kind: str) -> list[str]:
    """Names pdir/kind replaces or adds."""
    return [n for n in _names(Path(pdir) / kind) if not n.endswith(DISABLED)]


def profile_disabled(pdir: Path, kind: str) -> list[str]:
    """Names pdir/kind hides."""
    return [
        n.removesuffix(DISABLED)
        for n in _names(Path(pdir) / kind)
        if n.endswith(DISABLED)
    ]


def profile_owned_names(
    repo: Path, claude_home: Path, profile: str, kind: str
) -> list[str]:
    """Entries and markers in the profile, plus everything the last apply
    touched (it may have been removed from the profile since). Sorted."""
    names: set[str] = set()
    for d in (profile_dir(repo, profile), profile_stamp_dir(claude_home)):
        names.update(profile_entries(d, kind))
        names.update(profile_disabled(d, kind))
    return sorted(names)


def profile_excludes(
    repo: Path, claude_home: Path, profile: str
) -> dict[str, set[str]]:
    """Owned names per kind, for sync_capture: never copied into the shared
    copy, and the shared copy is never deleted for them."""
    return {
        kind: set(profile_owned_names(repo, claude_home, profile, kind))
        for kind in KINDS
    }


def profile_in_use(repo: Path, claude_home: Path, profile: str) -> bool:
    """True when a profile or a stamp is in play."""
    return profile_dir(repo, profile).is_dir() or profile_stamp_dir(claude_home).is_dir()


# ── helpers ──────────────────────────────────────────────────────────────


def _skipped(kind: str, name: str) -> bool:
    """A Linux-only entry this machine does not apply (see the module doc)."""
    return IS_WINDOWS and kind == "hooks" and name.endswith(LINUX_ONLY_SUFFIX)


def _is_exec(path: Path) -> bool:
    return os.access(path, os.X_OK)


def _is_link(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def _same(a: Path, b: Path) -> bool:
    """Same content, and for plain files the same exec bit (bash _profile_same).
    Folders compare names and bytes only, like `diff -rq`. A folder link or
    junction on either side, at any depth, is never followed and counts as
    different. Callers check file links with _refusal first."""
    if _is_link(a) and a.is_dir() or _is_link(b) and b.is_dir():
        return False
    if a.is_dir() and b.is_dir():
        return _same_tree(a, b)
    if a.is_file() and b.is_file():
        if a.stat().st_size != b.stat().st_size or a.read_bytes() != b.read_bytes():
            return False
        return not _POSIX or _is_exec(a) == _is_exec(b)
    return False


def _same_tree(a: Path, b: Path) -> bool:
    names_a = sorted(os.listdir(a))
    if names_a != sorted(os.listdir(b)):
        return False
    for name in names_a:
        x, y = a / name, b / name
        if _is_link(x) and not x.is_file() or _is_link(y) and not y.is_file():
            return False
        if x.is_dir() and y.is_dir():
            if not _same_tree(x, y):
                return False
        elif x.is_file() and y.is_file():
            if x.read_bytes() != y.read_bytes():
                return False
        else:
            return False
    return True


def _refusal(src: Path, root: Path) -> str | None:
    """Why _copy_entry must not copy src, or None. The syncfiles link rule
    plus containment, applied to src and everything under it: a link to a
    regular file is allowed only when it resolves inside root; links to
    folders, junctions, dangling links and special files are refused. Folder
    links are never followed, so a cycle cannot recurse."""
    if _is_link(src):
        if src.is_junction() or not src.is_file():
            return f"link to a folder or missing target: {src}"
        if not src.resolve().is_relative_to(Path(root).resolve()):
            return f"link that leads outside {root}: {src}"
        return None
    if src.is_dir():
        with os.scandir(src) as it:
            names = sorted(e.name for e in it)
        for name in names:
            reason = _refusal(src / name, root)
            if reason:
                return reason
        return None
    if src.is_file():
        return None
    return f"special file: {src}"


def _copyable(src: Path, root: Path) -> bool:
    """False, with a warning, when _refusal refuses src or anything in it.
    The whole top-level entry is then left alone, never copied in part."""
    reason = _refusal(src, root)
    if reason:
        _warn(f"skipped {src.name}, which holds a {reason}")
        return False
    return True


def _copy_entry(src: Path, dst: Path, root: Path) -> bool:
    """cp -a for one entry, without following folder links. If _copyable
    refuses src (or anything in it), dst is left alone and False returned.
    Otherwise dst is removed and rebuilt: bytes, mode and mtime kept, an
    allowed file link copied as its bytes, never recreated as a link."""
    if not _copyable(src, root):
        return False
    _remove(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    _copy_tree(src, dst)
    return True


def _copy_tree(src: Path, dst: Path) -> None:
    if _is_link(src) or not src.is_dir():
        _copy_file(src, dst)
        return
    dst.mkdir()
    with os.scandir(src) as it:
        names = sorted(e.name for e in it)
    for name in names:
        _copy_tree(src / name, dst / name)
    st = src.stat()
    os.chmod(dst, stat.S_IMODE(st.st_mode))
    os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns))


_HINT_RETRY = "Check the path named above is writable and not in use, then run the sync again"


def _guard(what: str, hint: str, func, *args) -> None:
    """Run func, turning an OSError into a Fail that says what to do."""
    try:
        func(*args)
    except OSError as e:
        raise Fail(f"could not {what}: {e}", hint=f"{_HINT_RETRY}; {hint}") from e


def _write_bytes(path: Path, data: bytes) -> None:
    """Write through a temp file and os.replace, keeping the old mode."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=TMP_SUFFIX, dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        if path.exists():
            os.chmod(tmp, stat.S_IMODE(path.stat().st_mode))
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _rename(src: Path, dst: Path) -> None:
    """Moves the finished stamp into place (a seam for crash tests)."""
    os.rename(src, dst)


def _marker_offset(data: bytes) -> int | None:
    """Byte offset of the first line that is exactly MARKER, or None."""
    offset = 0
    for line in data.split(b"\n"):
        if line == MARKER:
            return offset
        offset += len(line) + 1
    return None


def profile_strip_claude_md(data: bytes) -> bytes:
    """CLAUDE.md without the append block: everything before the newline
    that precedes the marker line. The exact inverse of the append."""
    offset = _marker_offset(data)
    if offset is None:
        return data
    return data[: offset - 1] if offset > 0 else b""


def profile_append_block(data: bytes) -> bytes | None:
    """The append block's bytes (after the marker line), or None."""
    offset = _marker_offset(data)
    if offset is None:
        return None
    return data[offset + len(MARKER) + 1 :]


# ── apply / unapply ──────────────────────────────────────────────────────


def profile_unapply(claude_home: Path, agents_home: Path, backup: Backup | None = None) -> None:
    _guard(
        "remove the applied profile entries",
        "nothing in the repo was changed.",
        _unapply, claude_home, agents_home, backup,
    )


def _unapply(claude_home: Path, agents_home: Path, backup: Backup | None = None) -> None:
    """Before sync_apply: remove the live copies of the entries the last
    apply put in place, so sync_apply restores the shared version afresh.
    The stamp itself stays until the next profile_apply. With a backup, each
    entry is saved first (backup.finish drops it if apply puts it back)."""
    stamp = profile_stamp_dir(claude_home)
    if not stamp.is_dir():
        return
    for kind in KINDS:
        live = profile_live_dir(claude_home, agents_home, kind)
        for name in profile_entries(stamp, kind):
            if backup is not None:
                backup.save(live / name)
            _remove(live / name)


def _refuse_linked_profile(repo: Path, profile: str, outcome: str) -> None:
    """Raise Fail for a bad name, or when machines/<profile>/ is or holds a
    link that _refusal refuses: apply would copy through it, and capture
    would write back through it."""
    pdir = profile_dir(repo, profile)  # refuses a pdir that is itself a link
    if not pdir.is_dir():
        return
    try:
        reason = _refusal(pdir, repo)
    except OSError as e:
        reason = str(e)
    if reason:
        raise Fail(
            f"profile '{profile}' holds a {reason}",
            hint=f"Replace it with a real file or folder in machines/{profile}/,"
            f" then run the sync again. {outcome}",
        )


def profile_apply(repo: Path, claude_home: Path, agents_home: Path, profile: str) -> None:
    """Copy the profile over the live files (after sync_apply) and record it.
    Raises Fail for a bad name, a profile that holds a link, or a failed
    copy; the first two before anything on the machine changes."""
    _refuse_linked_profile(repo, profile, "Nothing was applied.")
    _guard(
        f"apply profile '{profile}'",
        "the next sync applies the profile again from scratch.",
        _apply, repo, claude_home, agents_home, profile,
    )


def _apply(repo: Path, claude_home: Path, agents_home: Path, profile: str) -> None:
    pdir = profile_dir(repo, profile)
    stamp = profile_stamp_dir(claude_home)
    stamp_tmp = stamp.with_name(stamp.name + ".tmp")
    claude_md = Path(claude_home) / "CLAUDE.md"

    # Drop the record first: if this apply dies half way, capture then sees
    # "not applied yet" and leaves the profile alone.
    _remove(stamp)
    _remove(stamp_tmp)

    # A block left from an earlier apply must never be appended twice.
    base = claude_md.read_bytes() if claude_md.is_file() else None
    if base is not None and _marker_offset(base) is not None:
        base = profile_strip_claude_md(base)
        _write_bytes(claude_md, base)

    if not pdir.is_dir():
        return

    for kind in KINDS:
        live = profile_live_dir(claude_home, agents_home, kind)
        for name in profile_entries(pdir, kind):
            if _skipped(kind, name):
                continue
            live.mkdir(parents=True, exist_ok=True)
            _copy_entry(pdir / kind / name, live / name, repo)
        # Markers go last, so a marker wins over an entry of the same name.
        for name in profile_disabled(pdir, kind):
            _remove(live / name)

    append = pdir / APPEND_FILE
    if append.is_file():
        _write_bytes(claude_md, (base or b"") + b"\n" + MARKER + b"\n" + append.read_bytes())

    _copy_entry(pdir, stamp_tmp, repo)
    for name in profile_entries(stamp_tmp, "hooks"):
        if _skipped("hooks", name):
            _remove(stamp_tmp / "hooks" / name)
    (stamp_tmp / NAME_FILE).write_bytes(profile.encode("utf-8") + b"\n")
    _rename(stamp_tmp, stamp)


# ── capture ──────────────────────────────────────────────────────────────


def profile_capture(
    repo: Path,
    claude_home: Path,
    agents_home: Path,
    memory_dir: Path | None,
    profile: str,
    update_stamp: bool = False,
    *,
    memory_delete: bool = True,
) -> None:
    """sync_capture without the owned names, CLAUDE.md without the append
    block, and local edits to owned entries written back into the profile.
    update_stamp=True (the real capture, not a dry run into a scratch tree)
    also records written-back entries as applied. memory_dir=None and
    memory_delete are passed on to sync_capture (the basic tier, and memory
    adoption). Raises Fail for a bad name, a profile that holds a link, or
    a failed copy; the first two before anything in the repo changes."""
    _refuse_linked_profile(repo, profile, "Nothing was captured.")
    _guard(
        "capture the live config",
        "the live files are untouched; check `git status` in the repo before committing.",
        _capture, repo, claude_home, agents_home, memory_dir, profile, update_stamp, memory_delete,
    )


def _capture(
    repo, claude_home, agents_home, memory_dir, profile, update_stamp, memory_delete=True
) -> None:
    excludes = profile_excludes(repo, claude_home, profile)
    sync_capture(
        claude_home, agents_home, repo, excludes,
        memory_dir=memory_dir, memory_delete=memory_delete,
    )
    claude_md = Path(claude_home) / "CLAUDE.md"
    if claude_md.is_file():
        data = claude_md.read_bytes()
        if _marker_offset(data) is not None:
            _write_bytes(Path(repo) / "dot-claude/CLAUDE.md", profile_strip_claude_md(data))

    _writeback(repo, claude_home, agents_home, profile, "write-stamp" if update_stamp else "write")


def _writeback(
    repo: Path, claude_home: Path, agents_home: Path, profile: str, mode: str
) -> bool:
    """Find owned entries (and the append block) edited locally since the
    last apply. mode "check" stops at the first; "write" copies each back
    into the profile; "write-stamp" also refreshes the stamp so the live copy
    counts as applied. True when there was at least one."""
    pdir = profile_dir(repo, profile)
    stamp = profile_stamp_dir(claude_home)
    if not (pdir.is_dir() and stamp.is_dir()):
        return False
    # A snapshot of some other profile is no record of this one.
    try:
        recorded = (stamp / NAME_FILE).read_bytes().rstrip(b"\n")
    except OSError:
        return False
    if recorded != profile.encode("utf-8"):
        return False

    found = False
    for kind in KINDS:
        live_dir = profile_live_dir(claude_home, agents_home, kind)
        home = Path(agents_home if kind == "skills" else claude_home)
        # Only names still in the profile: one removed from it since the
        # last apply reverts to the shared version on the next apply.
        for name in profile_entries(pdir, kind):
            snap, live = stamp / kind / name, live_dir / name
            if _skipped(kind, name) or not snap.exists() or not live.exists():
                continue
            if not _copyable(live, home) or _same(snap, live):
                continue
            found = True
            if mode == "check":
                return True
            _copy_entry(live, pdir / kind / name, home)
            if mode == "write-stamp":
                _copy_entry(live, snap, home)

    append, snap = pdir / APPEND_FILE, stamp / APPEND_FILE
    claude_md = Path(claude_home) / "CLAUDE.md"
    if append.is_file() and snap.is_file() and claude_md.is_file():
        block = profile_append_block(claude_md.read_bytes())
        if block is not None and block != snap.read_bytes():
            found = True
            if mode == "check":
                return True
            _write_bytes(append, block)
            if mode == "write-stamp":
                _write_bytes(snap, block)
    return found


def profile_has_local_edits(
    repo: Path, claude_home: Path, agents_home: Path, profile: str
) -> bool:
    """True when an owned entry or the append block was edited locally since
    the last apply, so a re-apply would discard it."""
    return _writeback(repo, claude_home, agents_home, profile, "check")


# ── checks ───────────────────────────────────────────────────────────────


def profile_live_mismatches(
    repo: Path, claude_home: Path, agents_home: Path, profile: str
) -> list[str]:
    """Each owned "<kind>/<name>" (and "CLAUDE.md append block") whose live
    state differs from what a clean apply of shared + profile gives. The
    clean apply runs in a scratch home; the real one is never touched."""
    out: list[str] = []
    with tempfile.TemporaryDirectory(prefix="claude-sync-profile-") as tmp_name:
        tmp = Path(tmp_name)
        t_claude, t_agents = tmp / "claude", tmp / "agents"
        for kind in KINDS:
            exp = profile_live_dir(t_claude, t_agents, kind)
            shared = profile_shared_dir(repo, kind)
            if shared.is_dir():
                mirror(shared, exp, delete=True)
            else:
                exp.mkdir(parents=True, exist_ok=True)
        shared_md = Path(repo) / "dot-claude/CLAUDE.md"
        if shared_md.is_file():
            shutil.copyfile(shared_md, t_claude / "CLAUDE.md")
        profile_apply(repo, t_claude, t_agents, profile)

        for kind in KINDS:
            exp = profile_live_dir(t_claude, t_agents, kind)
            live = profile_live_dir(claude_home, agents_home, kind)
            home = Path(agents_home if kind == "skills" else claude_home)
            for name in profile_owned_names(repo, claude_home, profile, kind):
                if _skipped(kind, name):
                    continue
                if os.path.lexists(live / name) and _refusal(live / name, home):
                    out.append(f"{kind}/{name}")  # never read through the link
                    continue
                if not (exp / name).exists() and not (live / name).exists():
                    continue
                if not _same(exp / name, live / name):
                    out.append(f"{kind}/{name}")

        def block(path: Path) -> bytes | None:
            return profile_append_block(path.read_bytes()) if path.is_file() else None

        a = block(t_claude / "CLAUDE.md")
        b = block(Path(claude_home) / "CLAUDE.md")
        if (a is not None or b is not None) and a != b:
            out.append("CLAUDE.md append block")
    return out


def _commands(node) -> list[str]:
    """jq: `.. | objects | select(has("command")) | .command | strings`."""
    found: list[str] = []
    if isinstance(node, dict):
        if isinstance(node.get("command"), str):
            found.append(node["command"])
        for value in node.values():
            found.extend(_commands(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_commands(value))
    return found


def profile_dangling_hook_refs(claude_home: Path, settings: Path) -> list[str]:
    """Each hook command target under claude_home/hooks that settings.json's
    "hooks" refers to but that does not exist (e.g. a hook disabled by a
    profile but still wired in settings). Any ".claude/hooks/<name>"
    reference counts, written with / or \\, after $HOME, ${HOME}, ~ or an
    absolute path. A missing or unreadable settings file gives nothing.
    On Windows `.sh` references are skipped (see the module doc)."""
    try:
        data = json.loads(Path(settings).read_bytes())
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    hooks = data.get("hooks")
    refs: set[str] = set()
    for command in _commands(hooks if hooks is not None else {}):
        refs.update(_HOOK_REF.findall(command))
    hooks_dir = Path(claude_home) / "hooks"
    return [
        str(hooks_dir / ref)
        for ref in sorted(refs)
        if not _skipped("hooks", ref) and not os.path.exists(hooks_dir / ref)
    ]


def profile_hook_scripts(pdir: Path) -> list[Path]:
    """The hook scripts Claude Code would run from pdir/hooks: regular
    files (not links) anywhere under it that are executable, sorted by
    path bytes (bash: find -type f -perm -u+x | LC_ALL=C sort). Windows
    has no executable bit, so there every regular file counts."""
    found: list[Path] = []
    for root, dirs, files in os.walk(Path(pdir) / "hooks"):
        for name in files:
            path = Path(root) / name
            if path.is_symlink() or not path.is_file():
                continue
            if IS_WINDOWS or _is_exec(path):
                found.append(path)
    return sorted(found, key=os.fsencode)


def profile_describe(repo: Path, profile: str) -> str:
    """Every entry, marker and append in the profile, one indented line each
    (newline-terminated), or a single "(nothing ...)" line."""
    pdir = profile_dir(repo, profile)
    lines: list[str] = []
    for kind in KINDS:
        for name in profile_entries(pdir, kind):
            if os.path.exists(profile_shared_dir(repo, kind) / name):
                lines.append(f"  {kind}/{name}  (replaces the shared one)")
            else:
                lines.append(f"  {kind}/{name}  (machine-only)")
        for name in profile_disabled(pdir, kind):
            lines.append(f"  {kind}/{name}  (disabled)")
    if (pdir / APPEND_FILE).is_file():
        lines.append(f"  {APPEND_FILE}  (appended to CLAUDE.md)")
    if overlay.overlay_path(repo, profile).is_file():
        lines.append(f"  {profile}.settings.json  (settings override; see 'override list')")
    if not lines:
        lines.append(f"  (nothing: no machines/{profile}/ entries and no settings override)")
    return "".join(line + "\n" for line in lines)
