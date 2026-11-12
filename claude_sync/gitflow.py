"""How changes leave this machine: the git flow of claude-sync.sh.

Nothing is ever committed or pushed to the default branch. Each machine
has one branch, sync/<hostname>, holding exactly one commit on top of the
default branch: everything this machine has captured that is not merged
yet. Each sync rebuilds that commit, force-pushes the branch (with a
lease) and makes sure a PR is open for it. Merging the PR shares it.

The clone stays on the default branch, but its index and working tree
hold the effective state: the sync branch's tree when one exists, else
the default branch's. A command that rewrites the working tree first
commits whatever is in it to the sync branch, so nothing can be lost.

The bash keeps its state in globals (REPO_DIR, CLAUDE_HOME, DEF,
SYNC_START, ...); here they live on a SyncCtx passed to every function.

Every git call goes through `_git`, which passes `-c core.autocrlf=false`
so a global autocrlf on Windows never turns LF into CRLF in the clone (and
from there in ~/.claude). Output is read as text: only ids, ref names and
paths are read back, never file content. File content leaves git through
read-tree / checkout-index, which write the bytes themselves.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import sys
import tempfile
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from claude_sync import machineconf, overlay, profile, safety, skilllinks, syncfiles
from claude_sync.lock import DEFAULT_WAIT, SyncLock
from claude_sync.memorypath import long_name_warning, resolve_memory_dir
from claude_sync.util import Fail, redact, run

MIN_GIT = (2, 40)

# What GitHub knows about head branch `branch` at tip `tip`: "open", "done"
# (a merged or closed PR had exactly that tip), "nopr" or "unknown".
PrState = Callable[[str, str], str]


def _unknown_pr_state(_branch: str, _tip: str) -> str:
    return "unknown"


@dataclass
class SyncCtx:
    """One run's paths and state (the bash globals)."""

    repo: Path
    claude_home: Path
    agents_home: Path
    hostname: str
    lock_wait: float = DEFAULT_WAIT
    # T10's gh_sync_pr_state bound to the repo slug; "unknown" until then.
    pr_state: PrState = _unknown_pr_state
    default: str = ""  # DEF: origin's default branch, set by begin_sync
    sync_start: str = ""  # SYNC_START: the pending state at the start of the run
    _lock: SyncLock | None = field(default=None, repr=False)
    _long_path_warned: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        self.repo = Path(self.repo)
        self.claude_home = Path(self.claude_home)
        self.agents_home = Path(self.agents_home)


# ── git helpers ───────────────────────────────────────────────────────────


def _git_argv(repo: Path, *args: str) -> list[str]:
    return ["git", "-c", "core.autocrlf=false", "-C", str(repo), *args]


def _git(ctx_or_repo, *args: str, env: dict | None = None, new_process_group: bool = False):
    repo = ctx_or_repo.repo if isinstance(ctx_or_repo, SyncCtx) else Path(ctx_or_repo)
    return run(_git_argv(repo, *args), env=env, new_process_group=new_process_group)


def _show_stderr(result) -> None:
    # The bash lets git's own error reach the terminal; "see the git error
    # above". Redacted, so a credential in a remote URL never shows.
    if result.stderr:
        sys.stderr.write(redact(result.stderr))


def _out(
    ctx: SyncCtx, *args: str, what: str | None = None, new_process_group: bool = False
) -> str:
    """stdout of a git call with the trailing newline removed; Fail on error."""
    result = _git(ctx, *args, new_process_group=new_process_group)
    if result.returncode != 0:
        raise Fail(
            what or f"git {args[0]} failed in {ctx.repo} (exit {result.returncode}).",
            hint=result.stderr.rstrip("\n"),
        )
    return result.stdout.rstrip("\n")


def _ok(ctx: SyncCtx, *args: str, new_process_group: bool = False) -> bool:
    return _git(ctx, *args, new_process_group=new_process_group).returncode == 0


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _indent_conflicts(out: str) -> str:
    """merge-tree output without its first line (the tree), blank lines
    dropped, each line indented four spaces (bash tail | sed)."""
    lines = out.split("\n")[1:]
    return "\n".join("    " + ln for ln in lines if ln)


def _hint(*lines: str) -> str:
    return "\n".join(ln for ln in lines if ln != "")


def _write_atomic(path: Path, data: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ── branch helpers ────────────────────────────────────────────────────────


def default_branch(repo: Path) -> str:
    """origin's default branch from refs/remotes/origin/HEAD, or "" when the
    clone has none (git init'd, never cloned)."""
    result = _git(repo, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    ref = result.stdout.strip() if result.returncode == 0 else ""
    return ref.removeprefix("origin/")


def on_default_branch(repo: Path) -> bool:
    """True when HEAD is on the default branch, or there is none to compare."""
    default = default_branch(repo)
    if not default:
        return True
    result = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    return result.returncode == 0 and result.stdout.strip() == default


def sync_branch(hostname: str) -> str:
    return f"sync/{hostname}"


def has_origin(repo: Path) -> bool:
    return _git(repo, "remote", "get-url", "origin").returncode == 0


def has_sync_branch(ctx: SyncCtx, *, new_process_group: bool = False) -> bool:
    return _ok(
        ctx, "show-ref", "-q", "--verify", f"refs/heads/{sync_branch(ctx.hostname)}",
        new_process_group=new_process_group,
    )


def effective_commit(ctx: SyncCtx, *, new_process_group: bool = False) -> str:
    """The sync branch tip when this machine has pending changes, else HEAD."""
    group = {"new_process_group": new_process_group}
    if has_sync_branch(ctx, **group):
        return _out(ctx, "rev-parse", f"refs/heads/{sync_branch(ctx.hostname)}", **group)
    return _out(ctx, "rev-parse", "HEAD", **group)


def git_path(ctx: SyncCtx, name: str) -> Path:
    """An absolute path inside the clone's git dir."""
    return Path(_out(ctx, "rev-parse", "--absolute-git-dir")) / name


def tree_of(ctx: SyncCtx, rev: str) -> str:
    return _out(ctx, "rev-parse", f"{rev}^{{tree}}")


def current_profile(ctx: SyncCtx) -> str:
    """PROFILE= in this machine's conf, else the hostname. Read afresh each
    time, since a pull or 'profile use' can change it."""
    return profile.profile_resolve_name(ctx.repo, ctx.hostname)


def require_project_path(ctx: SyncCtx) -> str:
    project = machineconf.read_primary_project_path(ctx.repo, ctx.hostname)
    if project is None:
        conf = machineconf.machine_conf_path(ctx.repo, ctx.hostname)
        raise Fail(f"No machine config at {conf}. Run install.sh first.")
    return project


def memory_dir_of(ctx: SyncCtx) -> Path:
    """The memory folder; warns once per run when its name is very long."""
    project = require_project_path(ctx)
    warning = long_name_warning(project)
    if warning and not ctx._long_path_warned:
        ctx._long_path_warned = True
        print(f"claude-sync: warning: {warning}", file=sys.stderr)
    return resolve_memory_dir(ctx.claude_home, project)


def parse_git_version(text: str) -> tuple[int, int] | None:
    m = re.search(r"([0-9]+)\.([0-9]+)", text)
    return (int(m.group(1)), int(m.group(2))) if m else None


def require_git_version(min: tuple[int, int] = MIN_GIT) -> None:
    """merge-tree --write-tree arrived in git 2.38 and --merge-base in 2.40."""
    result = run(["git", "--version"])
    v = result.stdout.strip() if result.returncode == 0 else ""
    found = parse_git_version(v)
    if found is None or found < tuple(min):
        raise Fail(
            f"git {min[0]}.{min[1]} or newer is needed (found: {v or 'no git'}). "
            "claude-sync relies on 'git merge-tree --write-tree --merge-base'.",
            hint="  Upgrade git and re-run. Nothing was changed.",
        )


def set_sync_branch(ctx: SyncCtx, new: str, old: str) -> None:
    """Points the sync branch at `new`, expecting it at `old` ("" = absent).

    bash feeds `create`/`update` to `update-ref --stdin`; run() has no
    stdin, so this passes the expected old value on the command line, where
    "" means "must not exist yet". Same check, same atomicity.
    """
    ref = f"refs/heads/{sync_branch(ctx.hostname)}"
    result = _git(ctx, "update-ref", "-m", "claude-sync", ref, new, old)
    if result.returncode != 0:
        _show_stderr(result)
        verb = "create" if not old else "update"
        raise Fail(f"could not {verb} {ref}. Another process may have changed it; re-run.")


def delete_sync_branch(ctx: SyncCtx) -> None:
    sb = sync_branch(ctx.hostname)
    if not has_sync_branch(ctx):
        return
    result = _git(ctx, "branch", "-q", "-D", sb)
    if result.returncode != 0:
        _show_stderr(result)
        raise Fail(f"could not delete the local branch {sb}.")


def merge_trees(ctx: SyncCtx, *args: str) -> tuple[bool, str]:
    """git merge-tree --write-tree --name-only ARGS: (clean, output), output
    being the tree then any conflicts. Any exit above 1 is git failing,
    raised as that rather than reported as a conflict."""
    result = _git(ctx, "merge-tree", "--write-tree", "--name-only", *args)
    if result.returncode > 1:
        _show_stderr(result)
        raise Fail(
            f"git merge-tree failed (exit {result.returncode}, see the git error above). "
            "This is a git error, not a merge conflict.",
            hint=f"  Nothing was pushed. Check 'git -C {ctx.repo} status' and re-run.",
        )
    return result.returncode == 0, result.stdout.rstrip("\n")


def _first_line(out: str) -> str:
    return out.split("\n", 1)[0]


def changes_contained(ctx: SyncCtx, a: str, b: str) -> bool:
    """True when merging `b` into `a` would change nothing."""
    result = _git(ctx, "merge-base", a, b)
    if result.returncode != 0:
        return False
    clean, out = merge_trees(ctx, f"--merge-base={result.stdout.strip()}", a, b)
    return clean and _first_line(out) == tree_of(ctx, a)


# ── materialise ──────────────────────────────────────────────────────────

_EXIT_FOR = {"SIGINT": 130, "SIGTERM": 143, "SIGHUP": 129}
# Windows has no signal mask for git to inherit, so git started inside a
# held window (do_apply) gets its own process group there instead (Ctrl-C
# off). Off on POSIX, where the blocked mask is inherited.
_HELD_GROUP = os.name == "nt"


def _held_signals() -> list[signal.Signals]:
    return [getattr(signal, n) for n in _EXIT_FOR if hasattr(signal, n)]


def materialise(ctx: SyncCtx) -> None:
    """Makes the working tree match the effective commit, and the index
    match HEAD, so nothing pending is staged. Only call it once everything
    in the working tree is in a commit (commit_worktree).

    INT, TERM and HUP are held off so a timeout or Ctrl-C can't leave the
    working tree half old, half new (bash: `trap '' INT TERM HUP`). bash's
    ignored disposition passes on to git. Here this process blocks the
    three signals meanwhile, and git inherits that blocked mask (Python
    does not reset it in the child). git also runs in its own process
    group, so a Ctrl-C or `timeout` aimed at this run's group is not even
    sent to it. One that arrived ends the run afterwards with 130 / 143 /
    129, as bash's restored traps do for signals after the window (bash
    drops those inside it).

    Windows has no signal mask: there git gets CREATE_NEW_PROCESS_GROUP
    (Ctrl-C off) and this process records Ctrl-C in a handler instead.
    """
    eff = effective_commit(ctx)
    rc = 0
    err = ""
    with _signals_held() as caught:
        for args in (("read-tree", "-u", "--reset", eff), ("reset", "-q")):
            result = _git(ctx, *args, new_process_group=True)
            if result.returncode != 0:
                rc, err = result.returncode, result.stderr
                break
    if rc != 0:
        if err:
            sys.stderr.write(err)
        raise Fail(
            f"could not update the working tree in {ctx.repo} to {eff}.",
            hint=f"  Nothing was lost: every change is committed on {sync_branch(ctx.hostname)} "
            f"or {ctx.default}. Check 'git -C {ctx.repo} status' and re-run.",
        )
    _exit_if_caught(caught)


def _exit_if_caught(caught: list[signal.Signals]) -> None:
    if caught:
        raise SystemExit(_EXIT_FOR[caught[0].name])


class _signals_held:
    """Context manager: holds INT/TERM/HUP off; `as` gives the list of
    signals that arrived meanwhile, filled in on exit."""

    def __init__(self) -> None:
        self.caught: list[signal.Signals] = []
        self._old_mask = None
        self._old_handlers: dict = {}

    def __enter__(self) -> list[signal.Signals]:
        sigs = _held_signals()
        if hasattr(signal, "pthread_sigmask"):
            self._old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, sigs)
            return self.caught
        # Windows: no mask. Record instead of raising KeyboardInterrupt mid-way.
        for sig in sigs:
            try:
                self._old_handlers[sig] = signal.signal(sig, lambda s, _f: self.caught.append(
                    signal.Signals(s)))
            except (ValueError, OSError):  # not the main thread, or not settable
                pass
        return self.caught

    def __exit__(self, *exc: object) -> None:
        if self._old_mask is not None:
            pending = signal.sigpending()
            for sig in _held_signals():
                if sig in pending:
                    signal.sigwait([sig])  # consume it while still blocked
                    self.caught.append(sig)
            signal.pthread_sigmask(signal.SIG_SETMASK, self._old_mask)
        for sig, handler in self._old_handlers.items():
            signal.signal(sig, handler)


# ── commit and rebuild ───────────────────────────────────────────────────


_HOOK_PATH = re.compile(r"(?:dot-claude/hooks|machines/[^/]+/hooks)/.+")


def is_hook_path(rel: str) -> bool:
    """dot-claude/hooks/** or machines/<profile>/hooks/**."""
    return _HOOK_PATH.fullmatch(rel) is not None


def looks_executable(rel: str, path: Path) -> bool:
    """A `.sh` name or a `#!` first line."""
    if rel.endswith(".sh"):
        return True
    try:
        with open(path, "rb") as fh:
            return fh.read(2) == b"#!"
    except OSError:
        return False


def _modes(ctx: SyncCtx, *args: str) -> dict[str, str]:
    """path -> mode from `git ls-tree -r -z REV` or `git ls-files -s -z`."""
    out = _out(ctx, *args)
    modes = {}
    for record in out.split("\0"):
        if record:
            meta, rel = record.split("\t", 1)
            modes[rel] = meta.split(" ", 1)[0]
    return modes


def tree_modes(ctx: SyncCtx, rev: str) -> dict[str, str]:
    return _modes(ctx, "ls-tree", "-r", "-z", rev)


def keep_exec_bits(ctx: SyncCtx, base: str) -> None:
    """Sets the exec bit in the index that `git add -A` just filled, before
    write-tree. Windows git (core.filemode=false) stages a file it has no
    index entry for as 100644, so a hook would reach Linux non-executable.
    Only adds the bit, never removes it:
    - a path at `base` as 100755 stays 100755;
    - a new hook (is_hook_path) that looks_executable gets 100755.
    Everything else keeps the mode git add gave it."""
    before = tree_modes(ctx, base)
    plus = [
        rel
        for rel, mode in _modes(ctx, "ls-files", "-s", "-z").items()
        if mode == "100644"
        and (
            before.get(rel) == "100755"
            or (rel not in before and is_hook_path(rel) and looks_executable(rel, ctx.repo / rel))
        )
    ]
    # Batches keep the command line short enough for Windows.
    for i in range(0, len(plus), 100):
        _out(
            ctx, "update-index", "--chmod=+x", "--", *plus[i:i + 100],
            what=f"could not set the exec bit in the index of {ctx.repo}.",
        )


def commit_worktree(ctx: SyncCtx, msg: str) -> None:
    """Commits everything in the working tree onto the sync branch (creating
    it off the default branch if needed), when it differs from the
    effective state. Never commits on the default branch. Exec bits are
    kept as keep_exec_bits says, against the effective commit."""
    try:
        safety.assert_no_denylisted_files(ctx.repo)
    except Fail as err:
        raise Fail(
            "refusing to commit: denylisted file present in repo tree.",
            hint=f"  {err.message}\n  Remove it from {ctx.repo} (it must never be synced),"
            " then re-run.",
        ) from None
    _out(ctx, "add", "-A", what=f"git add failed in {ctx.repo}.")
    eff = effective_commit(ctx)
    keep_exec_bits(ctx, eff)
    tree = _out(ctx, "write-tree", what=f"git write-tree failed in {ctx.repo}.")
    if tree == tree_of(ctx, eff):
        return
    new = _out(
        ctx, "commit-tree", tree, "-p", eff, "-m", msg,
        what=f"git commit-tree failed in {ctx.repo} (is git's user.name/user.email set?).",
    )
    old = eff if has_sync_branch(ctx) else ""
    set_sync_branch(ctx, new, old)


def _rebuild_conflict(ctx: SyncCtx, out: str) -> Fail:
    sb, default, repo = sync_branch(ctx.hostname), ctx.default, ctx.repo
    return Fail(
        f"this machine's pending changes on {sb} conflict with changes merged on {default} "
        "(a real conflict, not staleness).",
        hint=_hint(
            f"  Nothing was applied and {default} was not moved; the live config is untouched.",
            "  Conflicts:",
            _indent_conflicts(out),
            f"  Resolve manually: in {repo}, merge origin/{default} into {sb}, fix the conflict"
            " and commit, then re-run 'claude-sync.sh sync'.",
            f"  Or drop this machine's pending changes: git -C {repo} branch -D {sb}"
            f" && git -C {repo} reset -q --hard && claude-sync.sh pull",
            f"  If {sb}'s PR was already merged or closed, a stale copy was picked up from"
            " origin: drop it as above.",
        ),
    )


def _rebuild_body(ctx: SyncCtx, target: str, tip: str) -> str:
    log = _out(ctx, "log", "--format=%B", f"{target}..{tip}")
    lines = set()
    for line in log.split("\n"):
        if line.startswith(("sync: ", "claude-sync: ")) or line == "":
            continue
        lines.add(line if line.startswith("- ") else f"- {line}")
    return "\n".join(sorted(lines))


def rebuild_sync_branch(ctx: SyncCtx, target: str) -> None:
    """Rebuilds the sync branch as one commit on top of `target`. Two steps,
    so a pending change that was already merged (even squash-merged) drops
    out cleanly before this run's new commits go on top: first the pending
    state at the start of the run (ctx.sync_start), then everything
    committed since. Deletes the branch when nothing is left that `target`
    doesn't already have."""
    sb = sync_branch(ctx.hostname)
    if not has_sync_branch(ctx):
        return
    tip = _out(ctx, "rev-parse", f"refs/heads/{sb}")
    target = _out(ctx, "rev-parse", f"{target}^{{commit}}")
    found = _git(ctx, "merge-base", ctx.sync_start, target)
    if found.returncode != 0:
        raise Fail(
            f"{sb} and {ctx.default} share no history, so the sync branch can't be rebuilt.",
            hint=f"  Inspect {ctx.repo} by hand; to start over: git -C {ctx.repo} branch -D {sb}",
        )
    mb = found.stdout.strip()
    clean, out = merge_trees(ctx, f"--merge-base={mb}", target, ctx.sync_start)
    if not clean:
        raise _rebuild_conflict(ctx, out)
    tree1 = tree2 = _first_line(out)
    if tip != ctx.sync_start:
        s1 = _out(ctx, "commit-tree", tree1, "-p", target, "-m", "claude-sync: intermediate")
        clean, out = merge_trees(ctx, f"--merge-base={ctx.sync_start}", s1, tip)
        if not clean:
            raise _rebuild_conflict(ctx, out)
        tree2 = _first_line(out)

    if tree2 == tree_of(ctx, target):
        # Everything pending is merged: back to plain default branch.
        delete_sync_branch(ctx)
    elif (
        tree2 == tree_of(ctx, tip)
        and _out(ctx, "rev-list", "--count", f"{target}..{tip}") == "1"
        and _out(ctx, "rev-parse", f"{tip}^") == target
    ):
        pass  # already one commit with this content on this target
    else:
        body = _rebuild_body(ctx, target, tip)
        msg = f"sync: {ctx.hostname} {_now()}"
        if body:
            msg = f"{msg}\n\n{body}"
        new = _out(
            ctx, "commit-tree", tree2, "-p", target, "-m", msg,
            what=f"git commit-tree failed while rebuilding {sb}.",
        )
        set_sync_branch(ctx, new, tip)
    ctx.sync_start = effective_commit(ctx)


# ── adopt and migrate ────────────────────────────────────────────────────


def migrate_legacy_default(ctx: SyncCtx) -> None:
    """A default branch carrying commits that are not on origin was left by
    a claude-sync.sh from before sync branches existed. Those commits move
    onto the sync branch (and out as a PR); the default branch goes back to
    where it left origin. The working tree is untouched."""
    sb, default, repo = sync_branch(ctx.hostname), ctx.default, ctx.repo
    ahead = int(_out(ctx, "rev-list", "--count", f"refs/remotes/origin/{default}..HEAD"))
    if ahead <= 0:
        return
    if has_sync_branch(ctx):
        raise Fail(
            f"local {default} has {ahead} commit(s) that are not on origin, and {sb} also exists.",
            hint=_hint(
                f"  claude-sync never commits on {default}, so these came from elsewhere."
                f" See them with: git -C {repo} log origin/{default}..{default}",
                f"  Move them onto {sb} or drop them (git -C {repo} branch -f {default}"
                f" origin/{default} while on another branch), then re-run.",
            ),
        )
    head = _out(ctx, "rev-parse", "HEAD")
    mb = _out(
        ctx, "merge-base", "HEAD", f"refs/remotes/origin/{default}",
        what=f"local {default} shares no history with origin/{default}. Fix {repo} by hand.",
    )
    set_sync_branch(ctx, head, "")
    result = _git(
        ctx, "update-ref", "-m", f"claude-sync: move local commits to {sb}",
        f"refs/heads/{default}", mb, head,
    )
    if result.returncode != 0:
        _show_stderr(result)
        raise Fail(
            f"could not move {default} back to origin/{default} after copying its commits to {sb}."
        )
    print(f"claude-sync: moved {ahead} unpushed commit(s) from {default} onto {sb};"
          " they will go out as a PR.")


def _config_get(ctx: SyncCtx, key: str) -> str:
    result = _git(ctx, "config", "--get", key)
    return result.stdout.strip() if result.returncode == 0 else ""


def _config_set(ctx: SyncCtx, key: str, value: str) -> None:
    _out(ctx, "config", key, value)


def adopt_remote_sync_branch(ctx: SyncCtx) -> None:
    """A fresh clone has no local sync branch, but origin may still hold
    this machine's open one. Pick it up so its pending changes are neither
    dropped from the live config nor overwritten by the next push. Edits
    already in the working tree are merged on top.

    Not when it is the tip this machine last pushed: the local branch was
    then deleted because the PR was merged (or dropped by hand), and GitHub
    just kept the branch. Uses ctx.pr_state(branch, tip)."""
    sb, repo = sync_branch(ctx.hostname), ctx.repo
    if has_sync_branch(ctx):
        return
    result = _git(ctx, "rev-parse", "-q", "--verify", f"refs/remotes/origin/{sb}")
    if result.returncode != 0:
        return
    remote = result.stdout.strip()
    if remote == _config_get(ctx, "claude-sync.lastPushed"):
        return
    if changes_contained(ctx, f"refs/remotes/origin/{ctx.default}", remote):
        return
    # No record (a fresh clone): skip it only on proof that its PR at this
    # very tip was merged or closed. A branch pushed without a PR still
    # holds pending changes, so it is picked up.
    state = ctx.pr_state(sb, remote)
    if state == "done":
        _config_set(ctx, "claude-sync.lastPushed", remote)
        print(
            f"claude-sync: origin's {sb} belongs to a PR that was merged or closed, so it was not"
            " picked up; the next push replaces it.",
            file=sys.stderr,
        )
        return
    if state not in ("open", "nopr"):
        print(
            f"claude-sync: gh could not say whether {sb} still has an open PR, so origin's {sb}"
            " is picked up as if it had.",
            file=sys.stderr,
        )

    _out(ctx, "add", "-A", what=f"git add failed in {repo}.")
    head = _out(ctx, "rev-parse", "HEAD")
    keep_exec_bits(ctx, head)
    tree = _out(ctx, "write-tree", what=f"git write-tree failed in {repo}.")
    merged = tree_of(ctx, remote)
    if tree != tree_of(ctx, head):
        wt = _out(ctx, "commit-tree", tree, "-p", head, "-m", "claude-sync: clone edits")
        clean, out = merge_trees(ctx, f"--merge-base={head}", remote, wt)
        if not clean:
            raise Fail(
                f"origin has this machine's open {sb}, but it conflicts with uncommitted"
                f" changes in {repo}.",
                hint=_hint(
                    _indent_conflicts(out),
                    f"  Move those changes aside (git -C {repo} stash -u), re-run,"
                    " then reapply them.",
                ),
            )
        merged = _first_line(out)
    set_sync_branch(ctx, remote, "")
    ctx.sync_start = remote
    _config_set(ctx, "claude-sync.lastPushed", remote)
    if merged != tree_of(ctx, remote):
        new = _out(
            ctx, "commit-tree", merged, "-p", remote, "-m", f"sync: {ctx.hostname} clone edits"
        )
        set_sync_branch(ctx, new, remote)
    materialise(ctx)
    print(f"claude-sync: picked up this machine's open {sb} from origin.")


# ── applied-commit record ────────────────────────────────────────────────


def applied_commit_stamp(ctx: SyncCtx) -> Path:
    """Local-only record of the commit the live config last matched. In
    CLAUDE_HOME but outside everything sync_capture copies."""
    return ctx.claude_home / ".claude-sync-applied-commit"


def applied_override_stamp(ctx: SyncCtx) -> Path:
    """Local-only record of the override the last apply layered on."""
    return ctx.claude_home / ".claude-sync-applied-override.json"


def record_applied_commit(ctx: SyncCtx, *, new_process_group: bool = False) -> None:
    """The ref keeps the stamped commit from being garbage-collected once
    the sync branch is rebuilt or deleted. `new_process_group` is passed to
    every git call (do_apply sets it on Windows, see _HELD_GROUP)."""
    eff = effective_commit(ctx, new_process_group=new_process_group)
    result = _git(
        ctx, "update-ref", "-m", "claude-sync: applied", "refs/claude-sync/applied", eff,
        new_process_group=new_process_group,
    )
    if result.returncode != 0:
        _show_stderr(result)
        raise Fail(
            f"could not record the applied commit in {ctx.repo} (git update-ref failed)."
        )
    _write_atomic(applied_commit_stamp(ctx), f"{eff}\n".encode())


def _same_tree(a: Path, b: Path) -> bool:
    """diff -rq: same names, same file bytes, same link targets."""
    names = sorted(os.listdir(a))
    if names != sorted(os.listdir(b)):
        return False
    for name in names:
        x, y = a / name, b / name
        if x.is_symlink() or y.is_symlink():
            if not (x.is_symlink() and y.is_symlink()) or os.readlink(x) != os.readlink(y):
                return False
        elif x.is_dir() and y.is_dir():
            if not _same_tree(x, y):
                return False
        elif x.is_file() and y.is_file():
            if x.stat().st_size != y.stat().st_size or x.read_bytes() != y.read_bytes():
                return False
        else:
            return False
    return True


def _export_commit(ctx: SyncCtx, sha: str, dest: Path, scratch: Path) -> None:
    """Writes commit `sha`'s files under dest (bash: git archive | tar -x).
    A throwaway index keeps the clone's own index untouched, and git writes
    the bytes itself."""
    env = {"GIT_INDEX_FILE": str(scratch / "index")}
    for args in (
        ("read-tree", sha),
        ("checkout-index", "-a", "-f", f"--prefix={dest.as_posix()}/"),
    ):
        result = _git(ctx, *args, env=env)
        if result.returncode != 0:
            raise Fail(
                f"could not read commit {sha} in {ctx.repo} (git {args[0]} failed).",
                hint=result.stderr.rstrip("\n"),
            )


def live_matches_commit(ctx: SyncCtx, sha: str, memory_dir: Path) -> bool:
    """True when the live config is exactly what a clean apply of `sha`
    would have left, i.e. nothing was edited locally since. The capture
    into the scratch tree is the same one a real capture does."""
    with tempfile.TemporaryDirectory(prefix="claude-sync-") as tmp_name:
        tmp = Path(tmp_name)
        applied, live = tmp / "applied", tmp / "live"
        applied.mkdir()
        _export_commit(ctx, sha, applied, tmp)
        # The export drops empty directories, which sync_capture always creates.
        for d in ("dot-claude/agents", "dot-claude/hooks", "dot-agents/skills", "memory"):
            (applied / d).mkdir(parents=True, exist_ok=True)
        shutil.copytree(applied, live, symlinks=True)
        profile.profile_capture(
            live, ctx.claude_home, ctx.agents_home, memory_dir, current_profile(ctx)
        )
        stamp = applied_override_stamp(ctx)
        shared = applied / "dot-claude" / "settings.json"
        if stamp.is_file() and shared.is_file():
            try:
                merged = overlay.merge_files(shared, stamp)
            except Fail:
                pass  # bash: overlay_merge ... && mv; a failed merge is left out
            else:
                shared.write_bytes(merged)
        return _same_tree(applied, live)


def check_capture_is_safe(ctx: SyncCtx, memory_dir: Path) -> bool:
    """True to capture, False to skip capture (live is stale but unmodified,
    so integrate and apply bring it up to date). Raises Fail when live has
    local edits AND the clone moved, which needs a human.

    Capture copies live over the clone, so it must never run when the live
    files are older than the clone's effective state: it would commit the
    stale files over newer work. Trees are compared, not commit ids,
    because each sync rebuilds the sync branch's commit.
    """
    stamp = applied_commit_stamp(ctx)
    if not stamp.is_file():
        return True
    sha = stamp.read_text(encoding="utf-8", errors="replace").rstrip("\n")
    eff = effective_commit(ctx)
    if sha == eff:
        return True
    if not _ok(ctx, "cat-file", "-e", f"{sha}^{{commit}}"):
        raise Fail(
            f"refusing to capture. The last applied commit {sha} (recorded in {stamp}) "
            f"is no longer in {ctx.repo}.",
            hint=_hint(
                "  Usually the clone was re-cloned or garbage-collected; claude-sync now pins it,"
                " so this should not recur.",
                "  Without it, local edits can't be told apart from stale files,"
                " so nothing was captured or changed.",
                "  Either run 'claude-sync.sh pull' (discards local edits, applies the repo),",
                f"  or copy your edits somewhere safe, delete {stamp}, and re-run"
                " 'claude-sync.sh sync' (captures the live config as it is).",
            ),
        )
    if tree_of(ctx, sha) == tree_of(ctx, eff):
        return True
    if live_matches_commit(ctx, sha, memory_dir):
        print(
            "claude-sync: the clone moved on since the last apply and the live config is "
            "unmodified; skipping capture so stale files are not committed.",
            file=sys.stderr,
        )
        return False
    raise Fail(
        "refusing to capture. The clone's HEAD moved since the last apply and the live "
        "config also has local edits, so capturing could overwrite newer repo changes.",
        hint="  Review the differences, then run 'claude-sync.sh pull' (discards local edits, "
        "applies the repo) or copy your edits somewhere safe first.",
    )


# ── begin, capture, integrate ────────────────────────────────────────────


def _fetch_failed() -> Fail:
    return Fail(
        "git fetch failed - could not reach the remote (network, SSH or auth problem). "
        "Nothing was changed locally; fix connectivity and re-run."
    )


def acquire_lock(ctx: SyncCtx, stack: ExitStack) -> None:
    """Takes the lock (once per run; a second call while held does nothing,
    like bash's acquire_lock). It is released when `stack` closes, or by
    release_lock."""
    if ctx._lock is not None:
        return
    lock_file = Path(_out(ctx, "rev-parse", "--absolute-git-dir")) / "claude-sync.flock"
    lock = SyncLock(lock_file, ctx.lock_wait, repo_dir=ctx.repo)
    stack.enter_context(lock)
    ctx._lock = lock
    stack.callback(setattr, ctx, "_lock", None)


def release_lock(ctx: SyncCtx) -> None:
    """Lets the lock go early (doctor remediate around its nested sync)."""
    lock, ctx._lock = ctx._lock, None
    if lock is not None:
        lock.__exit__(None, None, None)


def begin_sync(ctx: SyncCtx, stack: ExitStack) -> None:
    """Common start of every command that changes the repo: lock, fetch,
    and get the sync branch into a known state. Sets ctx.default and
    ctx.sync_start. The lock is held until `stack` closes."""
    sb, repo = sync_branch(ctx.hostname), ctx.repo
    require_git_version()
    if not has_origin(repo):
        raise Fail(
            f"the clone at {repo} has no 'origin' remote, so there is nowhere to push.",
            hint=f"  Add it (git -C {repo} remote add origin <url>) and re-run.",
        )
    acquire_lock(ctx, stack)
    if (
        git_path(ctx, "rebase-merge").is_dir()
        or git_path(ctx, "rebase-apply").is_dir()
        or git_path(ctx, "MERGE_HEAD").is_file()
    ):
        raise Fail(
            f"{repo} is in the middle of a rebase or merge.",
            hint="  Run 'claude-sync.sh doctor remediate', or finish/abort it by hand"
            f" (git -C {repo} status), then re-run.",
        )
    # Fetch separately so an unreachable remote isn't misreported as a conflict.
    fetched = _git(ctx, "fetch", "-q", "--prune", "origin")
    if fetched.returncode != 0:
        _show_stderr(fetched)
        raise _fetch_failed()
    ctx.default = default_branch(repo)
    if not ctx.default:
        _git(ctx, "remote", "set-head", "origin", "--auto")
        ctx.default = default_branch(repo)
    if not ctx.default:
        raise Fail(
            "can't tell origin's default branch (is the remote repository empty?).",
            hint=_hint(
                "  claude-sync only pushes sync/<hostname> branches and opens PRs against the"
                " default branch,",
                "  so the default branch must exist first: push an initial commit to it by hand,"
                " then re-run.",
            ),
        )
    if not _ok(ctx, "show-ref", "-q", "--verify", f"refs/remotes/origin/{ctx.default}"):
        raise Fail(f"origin/{ctx.default} does not exist after fetching. Check the remote, then re-run.")
    if not _ok(ctx, "check-ref-format", "--branch", sb) or sb == ctx.default:
        raise Fail(
            f"'{sb}' can't be used as this machine's sync branch name.",
            hint="  Set CLAUDE_SYNC_HOSTNAME to a simple name (letters, digits, '-', '.')"
            " and re-run.",
        )
    migrate_legacy_default(ctx)
    adopt_remote_sync_branch(ctx)
    if not ctx.sync_start:
        ctx.sync_start = effective_commit(ctx)


_SHARED_SETTINGS = "dot-claude/settings.json"


def _export_shared_settings(ctx: SyncCtx, sha: str, dest: Path, scratch: Path) -> bool:
    """Writes commit `sha`'s shared settings.json to dest, or `{}` when the
    commit has none; True when it has one. A throwaway index keeps the
    clone's own untouched."""
    if not _ok(ctx, "cat-file", "-e", f"{sha}:{_SHARED_SETTINGS}"):
        dest.write_bytes(b"{}\n")
        return False
    out = scratch / "shared-export"
    env = {"GIT_INDEX_FILE": str(scratch / "shared-index")}
    for args in (
        ("read-tree", sha),
        ("checkout-index", "-f", f"--prefix={out.as_posix()}/", "--", _SHARED_SETTINGS),
    ):
        result = _git(ctx, *args, env=env)
        if result.returncode != 0:
            raise Fail(
                f"could not read {_SHARED_SETTINGS} from commit {sha} in {ctx.repo}"
                f" (git {args[0]} failed).",
                hint=result.stderr.rstrip("\n"),
            )
    os.replace(out / _SHARED_SETTINGS, dest)
    return True


def _restore_shared_settings(shared: Path, committed: Path | None) -> None:
    """Puts the clone's shared settings.json back as committed (`committed`
    is its copy, None when the commit has none). Best effort: it runs while
    another error is on its way out."""
    try:
        if committed is None:
            if shared.is_file() and not shared.is_symlink():
                shared.unlink()
        else:
            _write_atomic(shared, committed.read_bytes())
    except OSError as err:
        print(
            f"claude-sync: could not restore {shared} ({err}); before the next pull or sync,"
            f" run: git -C {shared.parent.parent} checkout -- {_SHARED_SETTINGS}",
            file=sys.stderr,
        )


def do_capture_and_commit(ctx: SyncCtx) -> None:
    """Captures the live config into the clone and commits it onto the sync
    branch, then records it as applied. Skips (returns) when live is stale
    but unmodified; raises Fail when that is not safe to tell."""
    memory_dir = memory_dir_of(ctx)
    if not check_capture_is_safe(ctx, memory_dir):
        return

    # With an override, local settings.json = shared * override, so it can't
    # be copied into the shared file verbatim: keep the shared file as it
    # was before capture, so the override's leaves can be split back out.
    prof = current_profile(ctx)
    override = overlay.overlay_path(ctx.repo, prof)
    stamp = applied_override_stamp(ctx)
    local_settings = ctx.claude_home / "settings.json"
    shared = ctx.repo / "dot-claude" / "settings.json"
    with tempfile.TemporaryDirectory(prefix="claude-sync-") as tmp_name:
        tmp = Path(tmp_name)
        split = local_settings.is_file() and (override.is_file() or stamp.is_file())
        shared_before = tmp / "shared-before.json"
        override_before = tmp / "override-before.json"
        if split:
            # Every input is checked before capture touches the clone, so
            # a typo fails with the working tree as it was.
            for path in (local_settings, override, stamp):
                if path.is_file():
                    overlay.load_json(path)
            # From the effective commit, not the working tree: a run that
            # failed after capture left the merged live file there.
            had_shared = _export_shared_settings(ctx, effective_commit(ctx), shared_before, tmp)
            # What the last apply layered on. No record means nothing was:
            # local lacking an override key then means "not applied yet".
            override_before.write_bytes(stamp.read_bytes() if stamp.is_file() else b"{}\n")

        try:
            # Profile-owned entries stay out of the shared copy; a local edit
            # to one goes back into machines/<profile>/ instead.
            profile.profile_capture(
                ctx.repo, ctx.claude_home, ctx.agents_home, memory_dir, prof, True
            )
            if split:
                overlay.split_files(
                    local_settings, shared_before, override_before, override, shared
                )
        except BaseException:
            # Capture put the merged live file (shared plus override) in the
            # clone. Left there, a later 'clone edits' commit (pull,
            # bootstrap) would send the override's values out as shared.
            if split:
                _restore_shared_settings(shared, shared_before if had_shared else None)
            raise

    commit_worktree(ctx, f"sync: {ctx.hostname} {_now()}")
    # Live now equals the effective state by construction, so a failed push
    # after this point must not later look like a stale clone.
    record_applied_commit(ctx)


def do_integrate(ctx: SyncCtx) -> None:
    """Brings this machine onto origin's default branch: rebuilds the sync
    branch on top of it, fast-forwards the local default branch, and
    updates the working tree. Callers apply straight after."""
    default, repo = ctx.default, ctx.repo
    commit_worktree(ctx, f"sync: {ctx.hostname} clone edits {_now()}")
    target = _out(ctx, "rev-parse", f"refs/remotes/origin/{default}")
    rebuild_sync_branch(ctx, target)
    head = _out(ctx, "rev-parse", "HEAD")
    if head != target:
        if not _ok(ctx, "merge-base", "--is-ancestor", head, target):
            raise Fail(
                f"local {default} has commits that are not on origin/{default};"
                " it can only fast-forward.",
                hint=f"  See them with: git -C {repo} log origin/{default}..{default}",
            )
        result = _git(
            ctx, "update-ref", "-m", "claude-sync: fast-forward",
            f"refs/heads/{default}", target, head,
        )
        if result.returncode != 0:
            _show_stderr(result)
            raise Fail(f"could not fast-forward {default} to origin/{default}.")
    materialise(ctx)


def do_squash(ctx: SyncCtx) -> None:
    """Squashes the sync branch onto the local default branch without moving
    anything else ('push' doesn't integrate)."""
    commit_worktree(ctx, f"sync: {ctx.hostname} clone edits {_now()}")
    rebuild_sync_branch(ctx, _out(ctx, "rev-parse", "HEAD"))
    materialise(ctx)


def integrate_current_branch(ctx: SyncCtx) -> None:
    """'pull' on a clone not on its default branch, or with no origin:
    rebase onto the upstream if there is one. Never pushes."""
    if not _ok(ctx, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"):
        return
    fetched = _git(ctx, "fetch")
    if fetched.returncode != 0:
        _show_stderr(fetched)
        raise _fetch_failed()
    rebased = _git(ctx, "rebase")
    if rebased.returncode != 0:
        _show_stderr(rebased)
        if git_path(ctx, "rebase-merge").is_dir() or git_path(ctx, "rebase-apply").is_dir():
            _git(ctx, "rebase", "--abort")
        raise Fail(
            f"git rebase onto upstream failed (conflict). Resolve manually in {ctx.repo}"
            " and re-run."
        )


# ── apply ────────────────────────────────────────────────────────────────


def apply_settings_overlay(ctx: SyncCtx) -> None:
    """Layers this machine's override (if any) over the shared settings.json
    that sync_apply just copied into place, and records what was layered."""
    override = overlay.overlay_path(ctx.repo, current_profile(ctx))
    stamp = applied_override_stamp(ctx)
    shared = ctx.repo / "dot-claude" / "settings.json"
    if not override.is_file() or not shared.is_file():
        stamp.unlink(missing_ok=True)
        return
    merged = overlay.merge_files(shared, override)
    _write_atomic(ctx.claude_home / "settings.json", merged)
    _write_atomic(stamp, override.read_bytes())


def expected_local_settings(ctx: SyncCtx) -> bytes:
    """What this machine's settings.json should be after a clean apply."""
    override = overlay.overlay_path(ctx.repo, current_profile(ctx))
    shared = ctx.repo / "dot-claude" / "settings.json"
    if override.is_file():
        return overlay.merge_files(shared, override)
    return shared.read_bytes()


def apply_local_config(ctx: SyncCtx, memory_dir: Path, mirror: bool = True) -> None:
    """Every apply goes through here, so the profile is never left out.
    Skill links are wired last, after profile-disabled skills are gone."""
    prof = current_profile(ctx)
    profile.profile_unapply(ctx.claude_home, ctx.agents_home)
    syncfiles.sync_apply(
        ctx.repo, ctx.claude_home, ctx.agents_home, mirror=mirror, memory_dir=memory_dir
    )
    apply_settings_overlay(ctx)
    profile.profile_apply(ctx.repo, ctx.claude_home, ctx.agents_home, prof)
    tracked, disabled = skilllinks.tracked_skills(ctx.repo, profile.profile_dir(ctx.repo, prof))
    skilllinks.wire_skill_links(ctx.claude_home, ctx.agents_home, tracked, disabled)


def do_apply(ctx: SyncCtx) -> None:
    """Applies the clone's effective state to the live config and records it.

    INT, TERM and HUP are held off until the record is written (unlike
    bash), as for materialise: a stop between profile_unapply and
    profile_apply would leave live with the shared copy of a profile-owned
    entry, which the next capture would write into the profile. A signal
    that arrived meanwhile ends the run afterwards with 130 / 143 / 129.
    Power loss is not covered.
    """
    memory_dir = memory_dir_of(ctx)
    with _signals_held() as caught:
        apply_local_config(ctx, memory_dir)
        record_applied_commit(ctx, new_process_group=_HELD_GROUP)
    _exit_if_caught(caught)


def do_apply_merge(ctx: SyncCtx) -> None:
    """Non-destructive apply for a machine's first-ever sync: merges the
    repo's shared config into local paths without deleting local-only
    entries. Records nothing, as bash. Signals are held off as in do_apply."""
    memory_dir = memory_dir_of(ctx)
    with _signals_held() as caught:
        apply_local_config(ctx, memory_dir, mirror=False)
    _exit_if_caught(caught)
