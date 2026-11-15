"""Read a JSON file and write it back safely, in the layout it had.

Used for files Claude Code itself rewrites (~/.claude.json and
~/.claude/settings.json). write_json_atomic():

- does nothing (returns False) if the new data renders to the same bytes;
- backs the file up to <backup_dir>/<UTC stamp>/<name>, keeping the newest few;
- writes a temp file in the same folder, sets its mode, then os.replace()s it;
- refuses if the file's mtime or size changed since it was read;
- removes the temp file on any failure.

Error messages name the file's label and an exception class only, never
its contents or values. Standard library only.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

KEEP_BACKUPS = 10
STAMP = re.compile(r"^\d{8}T\d{12}Z(-\d+)?$")
SECRET_MODE = 0o600


class SafeJsonError(Exception):
    """A one-line reason, safe to print: no file contents or values."""


class NotFound(SafeJsonError):
    pass


class InvalidJson(SafeJsonError):
    pass


class Changed(SafeJsonError):
    """The file changed on disk after it was read (Claude Code rewrote it)."""


@dataclass(frozen=True)
class JsonMeta:
    """What is needed to write the file back alike and spot a rewrite."""

    label: str
    raw: bytes
    indent: str | int
    trailing_newline: bool
    crlf: bool
    mtime_ns: int
    size: int


def detect_indent(text: str) -> str | int:
    """Tab, 4 or 2 (the default, also for a compact file)."""
    m = re.search(r"\n([ \t]+)\S", text)
    if not m:
        return 2
    if m.group(1).startswith("\t"):
        return "\t"
    return 4 if len(m.group(1)) == 4 else 2


def read_json_with_meta(path: Path, label: str | None = None) -> tuple[Any, JsonMeta]:
    """Parse path; return the data and its layout and stat."""
    label = label or path.name
    try:
        with open(path, "rb") as f:
            raw = f.read()
            st = os.fstat(f.fileno())
        text = raw.decode("utf-8")
    except FileNotFoundError:
        raise NotFound(f"{label} not found") from None
    except (OSError, UnicodeDecodeError) as exc:
        raise SafeJsonError(f"cannot read {label}: {exc.__class__.__name__}") from None
    try:
        data = json.loads(text)
    except ValueError:
        raise InvalidJson(f"{label} is not valid JSON, left unchanged") from None
    meta = JsonMeta(label=label, raw=raw, indent=detect_indent(text),
                    trailing_newline=text.endswith("\n"), crlf="\r\n" in text,
                    mtime_ns=st.st_mtime_ns, size=st.st_size)
    return data, meta


def render(data: Any, meta: JsonMeta) -> bytes:
    """data as Claude Code writes it, in meta's layout."""
    out = json.dumps(data, indent=meta.indent, ensure_ascii=False)
    if meta.trailing_newline:
        out += "\n"
    if meta.crlf:
        out = out.replace("\n", "\r\n")
    return out.encode("utf-8")


def backup(path: Path, root: Path, keep: int = KEEP_BACKUPS, mode: int | None = None) -> Path:
    """Copy path into a new UTC-stamped dir under root; keep the newest few."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    root.mkdir(parents=True, exist_ok=True)
    target, n = root / stamp, 0
    while True:
        try:
            target.mkdir()
            break
        except FileExistsError:
            n += 1
            target = root / f"{stamp}-{n}"
    copy = target / path.name
    shutil.copy2(path, copy)
    if mode is not None and os.name != "nt":
        os.chmod(copy, mode)
    dirs = sorted(p.name for p in root.iterdir() if p.is_dir() and STAMP.match(p.name))
    for old in dirs[:-keep] if keep > 0 else dirs:
        if old != target.name:
            shutil.rmtree(root / old, ignore_errors=True)
    return copy


def _unchanged(path: Path, meta: JsonMeta) -> bool:
    st = path.stat()
    return st.st_mtime_ns == meta.mtime_ns and st.st_size == meta.size


def write_json_atomic(path: Path, data: Any, meta: JsonMeta, *, backup_dir: Path,
                      keep: int = KEEP_BACKUPS, mode: int | None = SECRET_MODE) -> bool:
    """Write data to path in meta's layout. True if written, False if the
    bytes would not change. mode=None copies the file's current mode."""
    label = meta.label
    changed = Changed(f"{label} changed while editing, try again")
    tmp: str | None = None
    try:
        if not _unchanged(path, meta):
            raise changed
        new = render(data, meta)
        if new == meta.raw:
            return False
        backup(path, backup_dir, keep, mode)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        with os.fdopen(fd, "wb") as f:
            f.write(new)
        if mode is None:
            shutil.copymode(path, tmp)
        elif os.name != "nt":
            os.chmod(tmp, mode)
        if not _unchanged(path, meta):
            raise changed
        os.replace(tmp, path)
        tmp = None
        return True
    except FileNotFoundError:
        raise changed from None
    except SafeJsonError:
        raise
    except (OSError, ValueError) as exc:
        raise SafeJsonError(f"cannot write {label}: {exc.__class__.__name__}") from None
    finally:
        if tmp is not None and os.path.exists(tmp):
            os.unlink(tmp)
