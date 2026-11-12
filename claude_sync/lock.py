"""The lock that stops two runs from rewriting the clone at the same time.

The port of acquire_lock / release_lock in claude-sync.sh. It uses the same
file, `.git/claude-sync.flock`, so a bash run (flock) and a Python run
exclude each other on Linux. The OS drops the lock when the holding process
dies, so a crash or kill never leaves a stale lock behind.

The handle is opened non-inheritable, so a child (git's background gc, say)
that outlives this run never keeps the lock; bash closes fd 9 for the same
reason.
"""

from __future__ import annotations

import math
import os
import time
from collections.abc import Mapping
from pathlib import Path

from claude_sync.util import Fail

DEFAULT_WAIT = 10.0
POLL_SECONDS = 0.05

# The OS-specific part is these two functions: _try_lock(fd) -> bool takes
# the lock without blocking; _unlock(fd) lets it go.
if os.name == "nt":
    import msvcrt

    def _try_lock(fd: int) -> bool:
        # Lock one byte at offset 0; it may lie past the end of the file.
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock(fd: int) -> bool:
        # flock(2), the same call util-linux flock makes, so the two exclude.
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


def lock_wait_from_env(env: Mapping[str, str]) -> float:
    """Seconds to wait for the lock: CLAUDE_SYNC_LOCK_WAIT, default 10."""
    raw = env.get("CLAUDE_SYNC_LOCK_WAIT", "").strip()
    if not raw:
        return DEFAULT_WAIT
    try:
        wait = float(raw)
    except ValueError:
        wait = -1.0
    if not math.isfinite(wait) or wait < 0:
        raise Fail(
            f"CLAUDE_SYNC_LOCK_WAIT must be a number of seconds, not '{raw}'.",
            hint="  Set it to 0 or more (for example 10), or unset it for the default.",
        )
    return wait


def _format_seconds(wait: float) -> str:
    # 10.0 -> "10", 0.5 -> "0.5", matching what the user set.
    return f"{wait:g}"


class SyncLock:
    """Hold the sync lock for the body of a `with` block.

    `path` is the lock file (normally `<git dir>/claude-sync.flock`). Waits
    up to `wait` seconds, then raises Fail. `repo_dir` is the clone named in
    that message; it defaults to the folder above the git dir.
    """

    def __init__(self, path: Path, wait: float, *, repo_dir: Path | None = None) -> None:
        self.path = Path(path)
        self.wait = wait
        self.repo_dir = Path(repo_dir) if repo_dir is not None else self.path.parent.parent
        self._fd: int | None = None

    def __enter__(self) -> SyncLock:
        try:
            # os.open handles are non-inheritable (PEP 446); said again for clarity.
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644)
            os.set_inheritable(fd, False)
        except OSError as err:
            raise Fail(
                f"could not open the lock file {self.path}.",
                hint=f"  {err.strerror or err}",
            ) from err
        deadline = time.monotonic() + self.wait
        while not _try_lock(fd):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                os.close(fd)
                raise Fail(
                    f"another claude-sync run is still working on {self.repo_dir} "
                    f"(waited {_format_seconds(self.wait)}s for its lock).",
                    hint="  Wait for it to finish and re-run. "
                    "The lock frees itself when that run exits.",
                )
            time.sleep(min(POLL_SECONDS, remaining))
        self._fd = fd
        return self

    def __exit__(self, *exc: object) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            _unlock(fd)
        finally:
            os.close(fd)
