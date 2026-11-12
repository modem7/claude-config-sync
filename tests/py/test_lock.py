"""Tests for claude_sync.lock, the port of acquire_lock / release_lock."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from claude_sync.lock import SyncLock, lock_wait_from_env
from claude_sync.util import Fail

PKG_ROOT = Path(__file__).resolve().parents[2]

# Takes the lock; in "child" mode starts a long-lived child (printing its pid),
# says "locked", then sleeps until killed (or, in "child" mode, exits at once).
HOLDER = r"""
import os, subprocess, sys, time
from pathlib import Path
from claude_sync.lock import SyncLock
path, mode = Path(sys.argv[1]), sys.argv[2]
with SyncLock(path, wait=5):
    if mode == "child":
        # close_fds=False: only a handle marked inheritable would pass on.
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"], close_fds=False
        )
        print(child.pid, flush=True)
    print("locked", flush=True)
    if mode == "child":
        os._exit(0)
    time.sleep(30)
"""


def start_holder(path: Path, mode: str = "hold") -> tuple[subprocess.Popen, list[str]]:
    env = {**os.environ, "PYTHONPATH": str(PKG_ROOT)}
    proc = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(path), mode],
        stdout=subprocess.PIPE,
        text=True,
        env=env,
    )
    lines = []
    while True:
        line = proc.stdout.readline()
        assert line, "holder exited before taking the lock"
        lines.append(line.strip())
        if line.strip() == "locked":
            return proc, lines


@pytest.fixture
def lock_path(tmp_path) -> Path:
    git_dir = tmp_path / "repo" / ".git"
    git_dir.mkdir(parents=True)
    return git_dir / "claude-sync.flock"


def test_lock_acquire_and_release(lock_path):
    with SyncLock(lock_path, wait=0.2):
        assert lock_path.is_file()
    # Released: a second acquire in the same process works at once.
    with SyncLock(lock_path, wait=0):
        pass


def test_second_holder_times_out_with_message(lock_path):
    proc, _ = start_holder(lock_path)
    try:
        started = time.monotonic()
        with pytest.raises(Fail) as info:
            with SyncLock(lock_path, wait=0.2):
                pytest.fail("took a lock another process holds")
        assert time.monotonic() - started >= 0.2
        repo_dir = lock_path.parent.parent
        assert info.value.message == (
            f"another claude-sync run is still working on {repo_dir} (waited 0.2s for its lock)."
        )
        assert info.value.hint == (
            "  Wait for it to finish and re-run. The lock frees itself when that run exits."
        )
    finally:
        proc.kill()
        proc.wait()


def test_message_names_the_given_repo_dir(lock_path, tmp_path):
    proc, _ = start_holder(lock_path)
    try:
        with pytest.raises(Fail) as info:
            with SyncLock(lock_path, wait=0, repo_dir=tmp_path / "elsewhere"):
                pass
        assert f"working on {tmp_path / 'elsewhere'} (waited 0s" in info.value.message
    finally:
        proc.kill()
        proc.wait()


def test_lock_released_when_holder_killed(lock_path):
    proc, _ = start_holder(lock_path)
    proc.kill()  # SIGKILL on POSIX, TerminateProcess on Windows
    proc.wait()
    with SyncLock(lock_path, wait=2):
        pass


def test_lock_handle_not_inherited_by_child(lock_path):
    proc, lines = start_holder(lock_path, mode="child")
    child_pid = int(lines[0])
    try:
        proc.wait(timeout=10)
        # The holder is gone but its child still runs; the lock must be free.
        with SyncLock(lock_path, wait=2):
            pass
    finally:
        try:
            os.kill(child_pid, signal.SIGTERM)
        except OSError:
            pass


@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows has no real flock: an MSYS/Cygwin flock.exe is emulated "
    "and does not exclude msvcrt.locking",
)
@pytest.mark.skipif(shutil.which("flock") is None, reason="flock is not installed")
def test_bash_flock_excludes_python(lock_path):
    proc = subprocess.Popen(
        ["flock", str(lock_path), "-c", "echo locked; sleep 2"],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout.readline().strip() == "locked"
        with pytest.raises(Fail):
            with SyncLock(lock_path, wait=0.2):
                pass
        # Python waits for bash's flock and gets the lock once it is let go.
        started = time.monotonic()
        with SyncLock(lock_path, wait=10):
            pass
        assert time.monotonic() - started > 0.5
        proc.wait(timeout=5)
    finally:
        proc.kill()
        proc.wait()


def test_lock_wait_from_env():
    assert lock_wait_from_env({}) == 10
    assert lock_wait_from_env({"CLAUDE_SYNC_LOCK_WAIT": ""}) == 10
    assert lock_wait_from_env({"CLAUDE_SYNC_LOCK_WAIT": "3"}) == 3
    assert lock_wait_from_env({"CLAUDE_SYNC_LOCK_WAIT": "0.5"}) == 0.5
    assert lock_wait_from_env({"CLAUDE_SYNC_LOCK_WAIT": "0"}) == 0


@pytest.mark.parametrize("value", ["soon", "-1", "nan", "inf"])
def test_lock_wait_from_env_rejects_bad_values(value):
    with pytest.raises(Fail) as info:
        lock_wait_from_env({"CLAUDE_SYNC_LOCK_WAIT": value})
    assert "CLAUDE_SYNC_LOCK_WAIT" in info.value.message
