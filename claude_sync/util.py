"""Shared helpers: the Fail error, redact(), and run(), the only way to start a child process."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path


_TOKEN_RES = (
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{16,}|github_pat_[A-Za-z0-9_]{16,})"),
    re.compile(r"(?i)\b(token|authorization)(\s*[:=]\s*)(?:bearer\s+)?\S+"),
    re.compile(r"(?<=://)[^/@\s]+@"),  # user:secret@ in a URL
)


def redact(text: str) -> str:
    """`text` with anything that looks like a token or URL credential
    removed. Reads GH_TOKEN, GITHUB_TOKEN, GH_ENTERPRISE_TOKEN and
    GITHUB_ENTERPRISE_TOKEN from os.environ at call time."""
    for var in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"):
        secret = os.environ.get(var, "")
        if len(secret) >= 4:
            text = text.replace(secret, "***")
    text = _TOKEN_RES[0].sub("***", text)
    text = _TOKEN_RES[1].sub(lambda m: f"{m.group(1)}{m.group(2)}***", text)
    return _TOKEN_RES[2].sub("", text)


def printable(text: str) -> str:
    """text without control characters (ESC, BEL, line breaks...), so it
    can't move the cursor or hide part of a line when printed."""
    return "".join(ch for ch in text if ch.isprintable())


class Fail(Exception):
    """An expected failure: `message` says what went wrong, `hint` what to do next.

    The entry point prints both to stderr and exits non-zero, with no traceback.
    """

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self) -> str:
        return self.message


def check_real_dir(path: Path) -> Path:
    """Raise Fail if `path` is a link or junction, or resolves anywhere but
    its own place in its parent (so a link further down cannot redirect it).
    A missing folder is fine, and so is a link above the parent (a repo under
    a linked /tmp still works). Used for machines/ before anything in it is
    read or written, and for the repo folders capture copies into."""
    path = Path(path)
    if os.path.lexists(path) and (
        path.is_symlink()
        or path.is_junction()
        or path.resolve() != path.parent.resolve() / path.name
    ):
        raise Fail(
            f"{path} is a link or leads outside {path.parent}.",
            hint=f"{path.name}/ must be a real folder in {path.parent}, not a link."
            " Replace the link with the folder's contents, then try again.",
        )
    return path


class UsageError(Exception):
    """The command line is wrong (a missing or unknown argument): the CLI
    prints its usage text to stderr and exits 1, as bash usage() does."""


# Seconds a child gets to finish once this run is stopped, then after terminate.
STOP_GRACE = 5.0
KILL_GRACE = 2.0


def _stop_child(proc: subprocess.Popen) -> None:
    """This run was stopped while `proc` ran (SIGTERM or SIGHUP raise
    SystemExit, Ctrl-C KeyboardInterrupt). subprocess.run would SIGKILL the
    child at once, so a git mid-write left its `.lock` files behind. Give it
    STOP_GRACE seconds to finish (git also removes its locks when it gets
    the signal itself), then terminate it, then kill it. A second stop
    during the wait kills it at once. The child's own children are not
    waited for."""
    try:
        for step in (None, proc.terminate):
            if step is not None:
                step()
            try:
                proc.communicate(timeout=STOP_GRACE if step is None else KILL_GRACE)
                return
            except subprocess.TimeoutExpired:
                continue
    except BaseException:
        pass
    try:
        proc.kill()
    except OSError:
        pass
    # Not communicate(): a grandchild (ssh, a credential helper) can hold
    # the pipes open long after the child is gone.
    proc.wait()
    for pipe in (proc.stdout, proc.stderr):
        if pipe is not None:
            pipe.close()


def run(
    args: list[str],
    *,
    cwd: Path | None = None,
    check: bool = False,
    env: dict | None = None,
    new_process_group: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a program and capture its output as UTF-8 text.

    - `args` is a list and never goes through a shell; a str is refused.
    - A program path ending in `.py` is started with the current Python, so
      a script can stand in for a binary on Windows too.
    - `env` is merged over `os.environ` (it does not replace it).
    - stdin is empty, so a prompt from git or gh fails instead of hanging.
    - A missing program gives returncode 127 and a "not found" stderr.
    - With `check=True`, a non-zero exit raises Fail, with stderr as the hint.
    - If this run is stopped while the child runs, the child gets time to
      finish before it is terminated or killed (_stop_child).
    - `new_process_group=True` starts the child in its own process group, so
      a Ctrl-C or a signal sent to this run's group (a terminal, `timeout`)
      does not reach it (Windows: CREATE_NEW_PROCESS_GROUP, which also turns
      Ctrl-C off in the child).
    """
    if isinstance(args, (str, bytes)):
        raise TypeError("run() takes a list of arguments, not a string")
    argv = [os.fspath(a) for a in args]
    if not argv:
        raise ValueError("run() needs at least a program name")
    if argv[0].lower().endswith(".py"):
        argv = [sys.executable, *argv]
    if cwd is not None and not Path(cwd).is_dir():
        # Checked first: Windows and Linux raise different errors for this.
        raise Fail(
            f"folder not found: {cwd}",
            hint=f"{Path(argv[0]).name} was to run there; check the path exists.",
        )
    full_env = None
    if env is not None:
        full_env = {**os.environ, **{k: os.fspath(v) for k, v in env.items()}}
    group: dict = {}
    if new_process_group:
        if os.name == "nt":
            group["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            group["process_group"] = 0
    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            env=full_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            close_fds=True,
            shell=False,
            **group,
        )
    except FileNotFoundError:
        result = subprocess.CompletedProcess(argv, 127, "", f"{argv[0]}: command not found\n")
    else:
        try:
            stdout, stderr = proc.communicate()
        except BaseException:
            _stop_child(proc)
            raise
        result = subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)
    if check and result.returncode != 0:
        name = Path(argv[0]).name
        if result.returncode == 127 and not result.stdout and "not found" in result.stderr:
            raise Fail(
                f"{name} not found",
                hint=f"Install {name} or put it on PATH, then try again.",
            )
        raise Fail(
            f"{name} failed with exit code {result.returncode}: {' '.join(argv[1:])}",
            hint=result.stderr.strip(),
        )
    return result
