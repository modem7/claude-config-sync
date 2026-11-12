"""Test helpers ported from tests/test_helper.bash (merge_sync_pr,
merge_and_converge, effective_show, refute). Plain functions, imported by
test modules as `from helpers import ...`; every git call here runs under
the fake HOME the calling test set up (fake_home / git_repo fixtures).
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from pathlib import Path

from claude_sync.util import run


def git(*args: str | Path, check: bool = True):
    """git with a fixed identity, failing the test on error unless check=False."""
    argv = ["git", "-c", "user.email=gh@test", "-c", "user.name=gh", *map(str, args)]
    result = run(argv)
    if check and result.returncode != 0:
        raise AssertionError(f"{' '.join(argv)} failed:\n{result.stderr}")
    return result


def refute(args: list[str | Path]) -> None:
    """Asserts a command fails (bash `refute cmd`)."""
    result = run([str(a) for a in args])
    assert result.returncode != 0, f"expected failure: {' '.join(map(str, args))}\n{result.stdout}"


def has_ref(repo: Path, ref: str) -> bool:
    return run(["git", "-C", str(repo), "show-ref", "-q", "--verify", ref]).returncode == 0


def rev(repo: Path, name: str) -> str:
    return git("-C", repo, "rev-parse", name).stdout.strip()


def merge_sync_pr(
    origin: Path,
    host: str,
    mode: str = "squash",
    keep: bool = False,
    gh_state: Path | None = None,
) -> None:
    """Simulates merging machine `host`'s sync PR on GitHub: squash (default)
    or a merge commit into origin's master, then deletes the branch (unless
    `keep`) and marks its PR closed in `gh_state` (the fake gh's state dir,
    which also records the merged tip)."""
    work = Path(tempfile.mkdtemp(prefix="merge.", dir=Path(origin).parent))
    clone = work / "c"
    git("clone", "-q", origin, clone)
    git("-C", clone, "fetch", "-q", "origin", f"sync/{host}")
    if gh_state is not None:
        tip = rev(origin, f"sync/{host}")
        with open(gh_state / f"merged-sync_{host}", "a", encoding="utf-8", newline="\n") as f:
            f.write(tip + "\n")
    if mode == "merge":
        git("-C", clone, "merge", "-q", "--no-ff", "-m", f"Merge sync/{host}", "FETCH_HEAD")
    else:
        git("-C", clone, "merge", "-q", "--squash", "FETCH_HEAD")
        git("-C", clone, "commit", "-q", "-m", f"sync: {host} (squashed)")
    git("-C", clone, "push", "-q", "origin", "master")
    if not keep:
        git("-C", origin, "update-ref", "-d", f"refs/heads/sync/{host}")
    if gh_state is not None:
        (gh_state / f"open-sync_{host}").unlink(missing_ok=True)


def merge_and_converge(origin: Path, host: str, runner: Callable[[str], tuple[int, str]]) -> None:
    """After a repo-changing command on `host` succeeded: if it pushed a sync
    branch, merge that PR and let the machine sync once more. `runner(cmd)`
    runs a command as that machine and returns (exit code, output)."""
    if not has_ref(origin, f"refs/heads/sync/{host}"):
        return
    merge_sync_pr(origin, host)
    rc, out = runner("sync")
    if rc != 0:
        raise AssertionError(f"converging sync for {host} failed:\n{out}")


def effective_show(repo: Path, host: str, path: str) -> str:
    """What a file holds on machine `repo`'s sync branch, else its default branch."""
    ref = f"sync/{host}"
    if not has_ref(repo, f"refs/heads/{ref}"):
        ref = "HEAD"
    return git("-C", repo, "show", f"{ref}:{path}").stdout
