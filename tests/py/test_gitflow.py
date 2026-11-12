"""Tests for claude_sync.gitflow, ported from the git-flow cases of
tests/sync_cli.bats and tests/sync_pr.bats.

There is no CLI yet (T11), so `World.run` (tests/py/world.py) composes the
same steps claude-sync.sh's main does, around the real pr.do_push and the
fake gh.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path

import pytest
from helpers import (
    effective_show,
    git,
    has_ref,
    merge_sync_pr,
    refute,
    rev,
)
from world import Machine, World

from claude_sync import gitflow, machineconf
from claude_sync.gitflow import SyncCtx
from claude_sync.lock import SyncLock
from claude_sync.util import Fail

PKG_ROOT = Path(__file__).resolve().parents[2]
POSIX = os.name != "nt"
posix_only = pytest.mark.skipif(not POSIX, reason="needs a POSIX shell stub and signals")


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def settings(p: Path) -> dict:
    return json.loads(p.read_bytes())


def commit_on_master(world: World, path: str, content: str) -> None:
    """Commits a change to origin's master directly, as a merged PR would."""
    w = world.tmp / f"direct-{time.monotonic_ns()}"
    git("clone", "-q", world.origin, w)
    (w / path).parent.mkdir(parents=True, exist_ok=True)
    (w / path).write_text(content + "\n", encoding="utf-8", newline="\n")
    git("-C", w, "add", "-A")
    git("-C", w, "commit", "-q", "-m", f"direct: {path}")
    git("-C", w, "push", "-q", "origin", "master")


def origin_refs(world: World) -> str:
    return git("-C", world.origin, "for-each-ref", "--format=%(refname) %(objectname)").stdout


# ── branch helpers ────────────────────────────────────────────────────────


def test_default_branch_of_a_clone_and_of_a_bare_init(git_repo, tmp_path):
    assert gitflow.default_branch(git_repo.clone) == "master"
    assert gitflow.on_default_branch(git_repo.clone)
    plain = tmp_path / "plain"
    git("init", "-q", plain)
    assert gitflow.default_branch(plain) == ""
    assert gitflow.on_default_branch(plain)  # nothing to compare against
    git("-C", git_repo.clone, "checkout", "-q", "-b", "feature")
    assert not gitflow.on_default_branch(git_repo.clone)


def test_sync_branch_name():
    assert gitflow.sync_branch("host-a") == "sync/host-a"


def test_has_origin(git_repo, tmp_path):
    assert gitflow.has_origin(git_repo.clone)
    git("-C", git_repo.clone, "remote", "remove", "origin")
    assert not gitflow.has_origin(git_repo.clone)


def test_parse_git_version():
    assert gitflow.parse_git_version("git version 2.53.0") == (2, 53)
    assert gitflow.parse_git_version("git version 2.39.2.windows.1") == (2, 39)
    assert gitflow.parse_git_version("") is None


def fake_git(tmp_path: Path, version: str) -> Path:
    """A git that reports `version` and defers to the real one, except that
    it fails merge-tree when FAIL_MERGE_TREE is set (any argument position,
    since gitflow passes -c and -C first)."""
    real = shutil.which("git")
    d = tmp_path / "fake-git"
    d.mkdir()
    script = d / "git"
    script.write_text(
        "#!/bin/sh\n"
        f'[ "$1" != "--version" ] || {{ echo "git version {version}"; exit 0; }}\n'
        'if [ -n "${FAIL_MERGE_TREE:-}" ]; then\n'
        '  for a in "$@"; do\n'
        '    [ "$a" != "merge-tree" ] || { echo "fatal: simulated merge-tree failure" >&2; exit 128; }\n'
        "  done\n"
        "fi\n"
        f'exec "{real}" "$@"\n',
        encoding="utf-8",
        newline="\n",
    )
    script.chmod(0o755)
    return d


@posix_only
def test_old_git_message(world, monkeypatch, tmp_path):
    a = world.machine("a")
    (a.claude / "CLAUDE.md").write_text("edit one\n")
    stub = fake_git(tmp_path, "2.39.2")
    monkeypatch.setenv("PATH", f"{stub}{os.pathsep}{os.environ['PATH']}")
    rc, out = world.run(a, "sync")
    assert rc == 1
    assert "git 2.40 or newer is needed (found: git version 2.39.2)" in out
    assert "Upgrade git and re-run. Nothing was changed." in out
    assert not has_ref(a.repo, "refs/heads/sync/host-a")


def test_no_git_message(monkeypatch, tmp_path):
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    with pytest.raises(Fail) as e:
        gitflow.require_git_version()
    assert "(found: no git)" in e.value.message


def test_no_origin_message(world):
    a = world.machine("a")
    git("-C", a.repo, "remote", "remove", "origin")
    rc, out = world.run(a, "sync")
    assert rc == 1
    assert "has no 'origin' remote, so there is nowhere to push" in out
    assert f"git -C {a.repo} remote add origin <url>" in out


def test_empty_origin_refused_with_what_to_do(fake_home, tmp_path):
    empty = tmp_path / "empty.git"
    git("init", "--bare", "-q", empty)
    first = tmp_path / "first"
    git("init", "-q", first)
    git("-C", first, "remote", "add", "origin", empty)
    machineconf.write_machine_conf(first, "host-first", "/home/user/project")
    git("-C", first, "add", "-A")
    git("-C", first, "commit", "-q", "-m", "seed")
    ctx = SyncCtx(first, tmp_path / "ch", tmp_path / "ah", "host-first", lock_wait=0.5)
    with ExitStack() as stack, pytest.raises(Fail) as e:
        gitflow.begin_sync(ctx, stack)
    assert "default branch" in e.value.message
    assert "push an initial commit" in e.value.hint
    assert git("-C", empty, "for-each-ref").stdout == ""


def test_default_branch_name_is_never_a_sync_branch(world):
    a = world.machine("a")
    rc, out = world.run(a, "push", ctx=a.ctx(hostname="../master"))
    assert rc == 1
    assert "can't be used as this machine's sync branch name" in out


def test_unreachable_remote_is_a_fetch_failure_not_a_conflict(world):
    a = world.machine("a")
    git("-C", a.repo, "remote", "set-url", "origin", str(world.tmp / "does-not-exist.git"))
    rc, out = world.run(a, "sync")
    assert rc == 1
    assert "git fetch failed" in out
    assert "conflict" not in out


def test_a_held_lock_makes_the_second_run_wait_then_fail(world):
    a = world.machine("a")
    (a.claude / "CLAUDE.md").write_text("edit one\n")
    with SyncLock(a.repo / ".git" / "claude-sync.flock", 1):
        rc, out = world.run(a, "sync")
    assert rc == 1
    assert "another claude-sync run is still working" in out
    assert not has_ref(world.origin, "refs/heads/sync/host-a")
    world.ok(a, "sync")  # released once the holder lets go
    assert has_ref(world.origin, "refs/heads/sync/host-a")


def test_lock_is_released_when_the_run_ends(world):
    a = world.machine("a")
    ctx = a.ctx()
    with ExitStack() as stack:
        gitflow.begin_sync(ctx, stack)
        gitflow.acquire_lock(ctx, stack)  # held already: no self-deadlock
    with SyncLock(a.repo / ".git" / "claude-sync.flock", 0):
        pass


# ── materialise ──────────────────────────────────────────────────────────


MATERIALISE = r"""
import sys
from pathlib import Path
from claude_sync.gitflow import SyncCtx, materialise
repo, home = Path(sys.argv[1]), Path(sys.argv[2])
materialise(SyncCtx(repo, home / ".claude", home / ".agents", "host-a"))
print("finished", flush=True)
"""


def _pending_branch_with_old_worktree(world: World) -> Machine:
    """machine a with sync/host-a ahead of the working tree, which still
    shows master: materialise has real work to do."""
    a = world.machine("a")
    for i in range(50):
        (a.repo / "memory" / f"note-{i}.md").write_text(f"pending {i}\n")
    (a.repo / "dot-claude" / "CLAUDE.md").write_text("pending edit\n")
    gitflow.commit_worktree(a.ctx(), "sync: host-a pending")
    git("-C", a.repo, "reset", "-q", "--hard")
    git("-C", a.repo, "clean", "-q", "-fd")
    assert read(a.repo / "dot-claude" / "CLAUDE.md") == "# shared\n"
    return a


def _assert_materialised(a: Machine) -> None:
    assert read(a.repo / "dot-claude" / "CLAUDE.md") == "pending edit\n"
    assert read(a.repo / "memory" / "note-49.md") == "pending 49\n"
    # Working tree = sync branch, index = HEAD (nothing staged).
    assert git("-C", a.repo, "diff", "--cached", "--name-only").stdout == ""
    tracked = set(git("-C", a.repo, "ls-tree", "-r", "--name-only", "sync/host-a").stdout.split())
    on_disk = {
        p.relative_to(a.repo).as_posix()
        for p in a.repo.rglob("*")
        if p.is_file() and ".git" not in p.relative_to(a.repo).parts
    }
    assert on_disk == tracked
    for name in tracked:
        blob = subprocess.run(
            ["git", "-C", str(a.repo), "cat-file", "blob", f"sync/host-a:{name}"],
            capture_output=True, check=True,
        ).stdout
        assert (a.repo / name).read_bytes() == blob, name


def test_materialise_updates_tree_and_leaves_nothing_staged(world):
    a = _pending_branch_with_old_worktree(world)
    gitflow.materialise(a.ctx())
    _assert_materialised(a)


@posix_only
@pytest.mark.parametrize(("sig", "code"), [(signal.SIGINT, 130), (signal.SIGTERM, 143)])
def test_ctrl_c_during_materialise_leaves_tree_consistent(world, tmp_path, sig, code):
    """The signal goes to the whole process group mid read-tree, as a
    terminal's Ctrl-C does: git must still finish, and the run then exits
    with the signal's code."""
    a = _pending_branch_with_old_worktree(world)
    real = shutil.which("git")
    marker = tmp_path / "read-tree-started"
    stub = tmp_path / "slow-git"
    stub.mkdir()
    (stub / "git").write_text(
        "#!/bin/sh\n"
        'for a in "$@"; do\n'
        f'  if [ "$a" = "read-tree" ]; then : > "{marker}"; sleep 1; fi\n'
        "done\n"
        f'exec "{real}" "$@"\n',
        encoding="utf-8",
        newline="\n",
    )
    (stub / "git").chmod(0o755)
    env = {
        **os.environ,
        "PYTHONPATH": str(PKG_ROOT),
        "PATH": f"{stub}{os.pathsep}{os.environ['PATH']}",
    }
    proc = subprocess.Popen(
        [sys.executable, "-c", MATERIALISE, str(a.repo), str(tmp_path / "home")],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    deadline = time.monotonic() + 20
    while not marker.exists():
        assert proc.poll() is None, proc.communicate()
        assert time.monotonic() < deadline, "read-tree never started"
        time.sleep(0.02)
    os.killpg(proc.pid, sig)
    out, err = proc.communicate(timeout=30)
    assert proc.returncode == code, (out, err)
    assert "finished" not in out
    _assert_materialised(a)


# ── commit and rebuild ───────────────────────────────────────────────────


def test_never_commits_on_default_branch(world):
    a = world.machine("a")
    master_before = rev(world.origin, "master")
    local_master = rev(a.repo, "master")
    (a.claude / "CLAUDE.md").write_text("edit one\n")
    out = world.ok(a, "sync")
    assert "pushed sync/host-a" in out
    assert rev(world.origin, "master") == master_before
    assert rev(a.repo, "master") == local_master
    assert git("-C", world.origin, "show", "sync/host-a:dot-claude/CLAUDE.md").stdout == "edit one\n"
    assert git("-C", world.origin, "rev-list", "--count", "master..sync/host-a").stdout.strip() == "1"
    assert git("-C", a.repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "master"
    assert read(a.repo / "dot-claude" / "CLAUDE.md") == "edit one\n"
    assert read(a.claude / "CLAUDE.md") == "edit one\n"


def test_a_second_sync_keeps_one_commit_on_the_same_branch(world):
    a = world.machine("a")
    (a.claude / "CLAUDE.md").write_text("edit one\n")
    world.ok(a, "sync")
    (a.claude / "CLAUDE.md").write_text("edit two\n")
    world.ok(a, "sync")
    assert git("-C", world.origin, "rev-list", "--count", "master..sync/host-a").stdout.strip() == "1"
    assert effective_show(a.repo, a.host, "dot-claude/CLAUDE.md") == "edit two\n"


def test_a_sync_with_nothing_new_changes_nothing(world):
    a = world.machine("a")
    (a.claude / "CLAUDE.md").write_text("edit one\n")
    world.ok(a, "sync")
    refs = origin_refs(world)
    tip = rev(a.repo, "sync/host-a")
    out = world.ok(a, "sync")
    assert "pushed" not in out
    assert origin_refs(world) == refs
    assert rev(a.repo, "sync/host-a") == tip


def test_pending_changes_are_not_staged(world):
    a = world.machine("a")
    (a.claude / "CLAUDE.md").write_text("pending edit\n")
    world.ok(a, "sync")
    assert git("-C", a.repo, "diff", "--cached", "--name-only").stdout == ""
    git("-C", a.repo, "checkout", "-q", "-b", "some-feature")
    (a.repo / "feature.txt").write_text("feature\n")
    git("-C", a.repo, "add", "feature.txt")
    git("-C", a.repo, "commit", "-q", "-m", "feature")
    assert git("-C", a.repo, "show", "--name-only", "--format=", "HEAD").stdout.strip() == "feature.txt"


def test_denylisted_file_refuses_the_commit(world):
    a = world.machine("a")
    (a.repo / "dot-claude" / ".credentials.json").write_text("{}")
    with pytest.raises(Fail) as e:
        gitflow.commit_worktree(a.ctx(), "msg")
    assert "refusing to commit: denylisted file present in repo tree." in e.value.message
    assert "it must never be synced" in e.value.hint
    assert not has_ref(a.repo, "refs/heads/sync/host-a")


def test_a_real_conflict_fails_loudly_and_changes_nothing(world):
    a = world.machine("a")
    b = world.machine("b")
    (a.claude / "settings.json").write_text('{"theme":"custom"}\n')
    world.ok(a, "sync")
    (b.claude / "settings.json").write_text('{"theme":"light"}\n')
    world.sync_as(b, "sync")
    master_before = rev(world.origin, "master")
    a_remote_before = rev(world.origin, "sync/host-a")

    (a.claude / "settings.json").write_text('{"theme":"custom2"}\n')
    rc, out = world.run(a, "sync")
    assert rc == 1
    assert "real conflict" in out
    assert "Resolve manually" in out
    assert "    dot-claude/settings.json" in out
    assert read(a.claude / "settings.json") == '{"theme":"custom2"}\n'
    assert rev(world.origin, "master") == master_before
    assert rev(world.origin, "sync/host-a") == a_remote_before
    assert git("-C", a.repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "master"

    rc, out = world.run(a, "sync")  # stays loud
    assert rc == 1
    assert "real conflict" in out


def test_squash_merge_then_same_line_edit_converges(world):
    a = world.machine("a")
    b = world.machine("b")
    (a.claude / "CLAUDE.md").write_text("edit one\n")
    world.ok(a, "sync")
    merge_sync_pr(world.origin, "host-a", "squash", gh_state=world.gh_state)

    (a.claude / "CLAUDE.md").write_text("edit two\n")
    world.ok(a, "sync")
    assert git("-C", world.origin, "rev-list", "--count", "master..sync/host-a").stdout.strip() == "1"
    names = git("-C", world.origin, "diff", "--name-only", "master", "sync/host-a").stdout
    assert names.strip() == "dot-claude/CLAUDE.md"
    merge_sync_pr(world.origin, "host-a", "squash", gh_state=world.gh_state)

    world.ok(a, "sync")
    assert not has_ref(a.repo, "refs/heads/sync/host-a")
    assert rev(a.repo, "master") == rev(world.origin, "master")
    assert git("-C", a.repo, "status", "--porcelain").stdout == ""

    world.ok(b, "sync")
    assert read(b.claude / "CLAUDE.md") == "edit two\n"
    assert not has_ref(world.origin, "refs/heads/sync/host-b")


def test_merge_commit_that_kept_the_branch_is_reused(world):
    a = world.machine("a")
    (a.claude / "CLAUDE.md").write_text("edit one\n")
    world.ok(a, "sync")
    merge_sync_pr(world.origin, "host-a", "merge", keep=True, gh_state=world.gh_state)
    world.ok(a, "sync")
    assert not has_ref(a.repo, "refs/heads/sync/host-a")

    (a.claude / "CLAUDE.md").write_text("edit two\n")
    world.ok(a, "sync")
    assert git("-C", world.origin, "show", "sync/host-a:dot-claude/CLAUDE.md").stdout == "edit two\n"
    assert rev(world.origin, "sync/host-a^") == rev(world.origin, "master")


def test_squash_kept_branch_converges_after_master_changes_same_file(world):
    a = world.machine("a")
    (a.claude / "CLAUDE.md").write_text("edit one\n")
    world.ok(a, "sync")
    merge_sync_pr(world.origin, "host-a", "squash", keep=True, gh_state=world.gh_state)
    world.ok(a, "sync")
    assert not has_ref(a.repo, "refs/heads/sync/host-a")
    assert has_ref(world.origin, "refs/heads/sync/host-a")

    commit_on_master(world, "dot-claude/CLAUDE.md", "changed on master")
    for _ in range(2):
        out = world.ok(a, "sync")
        assert "picked up" not in out
    assert read(a.claude / "CLAUDE.md") == "changed on master\n"
    assert not has_ref(a.repo, "refs/heads/sync/host-a")

    (a.claude / "CLAUDE.md").write_text("edit two\n")
    world.ok(a, "sync")
    assert git("-C", world.origin, "show", "sync/host-a:dot-claude/CLAUDE.md").stdout == "edit two\n"
    assert rev(world.origin, "sync/host-a^") == rev(world.origin, "master")


def test_unmerged_own_pr_plus_another_merged_change_is_not_refused(world):
    a = world.machine("a")
    b = world.machine("b")
    (a.claude / "CLAUDE.md").write_text("edit one\n")
    world.ok(a, "sync")
    (b.claude / "agents" / "b-agent.md").write_text("from b\n")
    world.ok(b, "sync")
    merge_sync_pr(world.origin, "host-b", gh_state=world.gh_state)

    (a.claude / "CLAUDE.md").write_text("edit two\n")
    out = world.ok(a, "sync")
    assert "refusing" not in out
    assert "skipping capture" not in out
    assert read(a.claude / "CLAUDE.md") == "edit two\n"
    assert read(a.claude / "agents" / "b-agent.md") == "from b\n"
    assert rev(world.origin, "sync/host-a^") == rev(world.origin, "master")
    assert git("-C", world.origin, "show", "sync/host-a:dot-claude/CLAUDE.md").stdout == "edit two\n"


def test_merge_tree_failure_is_a_git_error_not_a_conflict(world, monkeypatch, tmp_path):
    if not POSIX:
        pytest.skip("needs a POSIX shell stub")
    a = world.machine("a")
    (a.claude / "CLAUDE.md").write_text("edit one\n")
    stub = fake_git(tmp_path, "2.53.0")
    monkeypatch.setenv("PATH", f"{stub}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAIL_MERGE_TREE", "1")
    rc, out = world.run(a, "sync")
    assert rc == 1
    assert "git merge-tree failed (exit 128" in out
    assert "conflict with changes merged" not in out
    assert not has_ref(world.origin, "refs/heads/sync/host-a")


# ── adopt and migrate ────────────────────────────────────────────────────


def _reclone(world: World, m: Machine) -> None:
    """As after install.sh --repair: a new clone, same live config."""
    shutil.move(m.repo, world.tmp / f"old_repo_{m.name}")
    git("clone", "-q", "-c", "core.autocrlf=false", world.origin, m.repo)


def test_fresh_clone_picks_up_the_open_sync_branch(world):
    a = world.machine("a")
    (a.claude / "CLAUDE.md").write_text("pending edit\n")
    world.ok(a, "sync")
    pushed = rev(world.origin, "sync/host-a")
    _reclone(world, a)
    out = world.ok(a, "sync")
    assert "picked up this machine's open sync/host-a" in out
    assert read(a.claude / "CLAUDE.md") == "pending edit\n"
    assert read(a.repo / "dot-claude" / "CLAUDE.md") == "pending edit\n"
    assert rev(world.origin, "sync/host-a") == pushed


def test_fresh_clone_skips_a_kept_branch_whose_pr_is_done(world):
    a = world.machine("a")
    (a.claude / "CLAUDE.md").write_text("edit one\n")
    world.ok(a, "sync")
    merge_sync_pr(world.origin, "host-a", "squash", keep=True, gh_state=world.gh_state)
    commit_on_master(world, "dot-claude/CLAUDE.md", "changed on master")
    _reclone(world, a)
    (a.claude / ".claude-sync-applied-commit").unlink()
    out = world.ok(a, "pull")
    assert "belongs to a PR that was merged or closed, so it was not picked up" in out
    assert read(a.claude / "CLAUDE.md") == "changed on master\n"
    assert not has_ref(a.repo, "refs/heads/sync/host-a")

    (a.claude / "CLAUDE.md").write_text("edit two\n")
    world.ok(a, "sync")
    assert git("-C", world.origin, "show", "sync/host-a:dot-claude/CLAUDE.md").stdout == "edit two\n"


def test_fresh_clone_picks_up_a_branch_pushed_without_a_pr(world):
    a = world.machine("a")
    (a.claude / "CLAUDE.md").write_text("pending edit\n")
    a.gh_down = True
    rc, _ = world.run(a, "sync")
    assert rc == 1  # pushed, but no PR
    pushed = rev(world.origin, "sync/host-a")
    a.gh_down = False
    _reclone(world, a)
    out = world.ok(a, "sync")
    assert "picked up this machine's open sync/host-a" in out
    assert read(a.claude / "CLAUDE.md") == "pending edit\n"
    assert rev(world.origin, "sync/host-a") == pushed


def test_unknown_pr_state_picks_up_the_branch_loudly(world):
    a = world.machine("a")
    (a.claude / "CLAUDE.md").write_text("pending edit\n")
    world.ok(a, "sync")
    (world.gh_state / "open-sync_host-a").unlink()
    _reclone(world, a)
    a.gh_down = True
    rc, out = world.run(a, "pull")
    assert rc == 0, out
    assert "gh could not say whether sync/host-a still has an open PR" in out
    assert "picked up this machine's open sync/host-a" in out


def test_adopt_merges_uncommitted_clone_edits_on_top(world):
    a = world.machine("a")
    (a.claude / "CLAUDE.md").write_text("pending edit\n")
    world.ok(a, "sync")
    _reclone(world, a)
    (a.repo / "memory" / "hand.md").write_text("hand edit\n")
    out = world.ok(a, "pull")
    assert "picked up" in out
    assert read(a.repo / "dot-claude" / "CLAUDE.md") == "pending edit\n"
    assert effective_show(a.repo, a.host, "memory/hand.md") == "hand edit\n"


def test_migrate_moves_a_legacy_commit_off_the_default_branch(world):
    a = world.machine("a")
    (a.repo / "dot-claude" / "CLAUDE.md").write_text("legacy\n")
    git("-C", a.repo, "commit", "-q", "-am", "sync: host-a legacy")
    legacy = rev(a.repo, "HEAD")
    rc, out = world.run(a, "pull")
    assert rc == 0, out
    assert "moved 1 unpushed commit(s) from master onto sync/host-a" in out
    assert git("-C", a.repo, "log", "-1", "--format=%s", "sync/host-a").stdout.strip() != ""
    assert git("-C", a.repo, "merge-base", "--is-ancestor", legacy, "sync/host-a",
               check=False).returncode == 0
    assert rev(a.repo, "master") == rev(a.repo, "origin/master")
    assert read(a.claude / "CLAUDE.md") == "legacy\n"


def test_migrate_with_a_sync_branch_already_there_refuses(world):
    a = world.machine("a")
    (a.claude / "CLAUDE.md").write_text("pending\n")
    world.ok(a, "sync")
    (a.repo / "memory" / "x.md").write_text("x\n")
    git("-C", a.repo, "add", "-A")
    git("-C", a.repo, "commit", "-q", "-m", "stray")
    rc, out = world.run(a, "sync")
    assert rc == 1
    assert "local master has 1 commit(s) that are not on origin, and sync/host-a also exists." in out


def test_migrate_then_conflict_is_reported_and_live_kept(world):
    """doctor remediate's flow: a legacy commit plus a conflicting merged change."""
    a = world.machine("a")
    b = world.machine("b")
    (b.claude / "settings.json").write_text('{"theme":"light"}\n')
    world.sync_as(b, "sync")
    (a.claude / "settings.json").write_text('{"theme":"custom"}\n')
    (a.repo / "dot-claude" / "settings.json").write_text('{"theme":"custom"}\n')
    git("-C", a.repo, "commit", "-q", "-am", "sync: host-a legacy")
    (a.claude / ".claude-sync-applied-commit").write_text(rev(a.repo, "HEAD") + "\n")
    rc, out = world.run(a, "sync")
    assert rc == 1
    assert "moved 1 unpushed commit(s) from master onto sync/host-a" in out
    assert "real conflict" in out
    assert read(a.claude / "settings.json") == '{"theme":"custom"}\n'
    assert git("-C", a.repo, "log", "-1", "--format=%s", "sync/host-a").stdout.strip() == "sync: host-a legacy"
    assert git("-C", a.repo, "merge-base", "--is-ancestor", "master", "origin/master",
               check=False).returncode == 0


# ── capture and commit ───────────────────────────────────────────────────


def test_record_applied_commit_writes_stamp_and_pins_ref(world):
    a = world.machine("a")
    assert has_ref(a.repo, "refs/claude-sync/applied")
    eff = gitflow.effective_commit(a.ctx())
    assert read(a.claude / ".claude-sync-applied-commit") == eff + "\n"
    assert rev(a.repo, "refs/claude-sync/applied") == eff


def _fast_forward_clone_without_apply(world: World) -> tuple[Machine, Machine]:
    a = world.machine("a")
    b = world.machine("b")
    (b.claude / "hooks").mkdir(exist_ok=True)
    (b.claude / "hooks" / "new-hook.sh").write_text("echo new-hook\n")
    world.sync_as(b, "sync")
    git("-C", a.repo, "pull", "-q", "--ff-only")
    assert (a.repo / "dot-claude" / "hooks" / "new-hook.sh").is_file()
    assert not (a.claude / "hooks" / "new-hook.sh").exists()
    return a, b


def test_sync_does_not_revert_a_newer_commit_when_live_was_never_applied(world):
    a, _ = _fast_forward_clone_without_apply(world)
    out = world.ok(a, "sync")
    assert "skipping capture so stale files are not committed" in out
    assert (a.repo / "dot-claude" / "hooks" / "new-hook.sh").is_file()
    assert (a.claude / "hooks" / "new-hook.sh").is_file()
    assert rev(a.repo, "HEAD") == rev(a.repo, "origin/master")
    assert not has_ref(a.repo, "refs/heads/sync/host-a")


def test_push_also_refuses_to_revert_when_live_is_stale(world):
    a, _ = _fast_forward_clone_without_apply(world)
    before = rev(a.repo, "HEAD")
    world.ok(a, "push")
    assert rev(a.repo, "HEAD") == before
    assert (a.repo / "dot-claude" / "hooks" / "new-hook.sh").is_file()
    assert not has_ref(a.repo, "refs/heads/sync/host-a")


def test_clone_moved_and_live_edited_is_refused_and_changes_nothing(world):
    a, _ = _fast_forward_clone_without_apply(world)
    (a.claude / "CLAUDE.md").write_text("local edit\n")
    before = rev(a.repo, "HEAD")
    refs = origin_refs(world)
    rc, out = world.run(a, "sync")
    assert rc == 1
    assert "refusing to capture" in out
    assert rev(a.repo, "HEAD") == before
    assert read(a.claude / "CLAUDE.md") == "local edit\n"
    assert (a.repo / "dot-claude" / "hooks" / "new-hook.sh").is_file()
    assert origin_refs(world) == refs
    assert not has_ref(a.repo, "refs/heads/sync/host-a")


def test_pending_pr_plus_manual_pull_plus_live_edit_keeps_both(world):
    a = world.machine("a")
    b = world.machine("b")
    (a.claude / "CLAUDE.md").write_text("edit one\n")
    world.ok(a, "sync")
    (b.claude / "agents" / "b-agent.md").write_text("from b\n")
    world.ok(b, "sync")
    merge_sync_pr(world.origin, "host-b", gh_state=world.gh_state)
    git("-C", a.repo, "pull", "-q", "--ff-only")
    (a.claude / "CLAUDE.md").write_text("edit two\n")
    out = world.ok(a, "sync")
    assert "refusing" not in out
    assert read(a.claude / "CLAUDE.md") == "edit two\n"
    assert read(a.claude / "agents" / "b-agent.md") == "from b\n"


def test_a_missing_last_applied_commit_gets_its_own_refusal(world):
    a = world.machine("a")
    stamp = a.claude / ".claude-sync-applied-commit"
    stamp.write_text("0123456789abcdef0123456789abcdef01234567\n")
    (a.claude / "CLAUDE.md").write_text("local edit\n")
    refs = origin_refs(world)
    rc, out = world.run(a, "sync")
    assert rc == 1
    assert "is no longer in" in out
    assert f"delete {stamp}" in out
    assert read(a.claude / "CLAUDE.md") == "local edit\n"
    assert origin_refs(world) == refs


def test_a_failed_push_does_not_make_the_next_sync_refuse(world):
    a = world.machine("a")
    (a.claude / "CLAUDE.md").write_text("edit one\n")
    good = git("-C", a.repo, "remote", "get-url", "origin").stdout.strip()
    git("-C", a.repo, "remote", "set-url", "origin", str(world.tmp / "does-not-exist.git"))
    rc, _ = world.run(a, "sync")
    assert rc == 1
    git("-C", a.repo, "remote", "set-url", "origin", good)
    (a.claude / "CLAUDE.md").write_text("edit two\n")
    world.sync_as(a, "sync")
    assert git("-C", world.origin, "show", "master:dot-claude/CLAUDE.md").stdout == "edit two\n"


def test_hand_edits_in_the_clone_go_out_with_the_pr(world):
    a = world.machine("a")
    (a.repo / "machines" / "host-a.settings.json").write_text('{"theme":"solarized"}\n')
    world.ok(a, "sync")
    assert settings(a.claude / "settings.json")["theme"] == "solarized"
    (a.repo / "machines" / "host-a.settings.json").write_text('{"theme":"nord"}\n')
    world.ok(a, "sync")
    assert settings(a.claude / "settings.json")["theme"] == "nord"
    shown = git("-C", world.origin, "show", "sync/host-a:machines/host-a.settings.json").stdout
    assert json.loads(shown)["theme"] == "nord"


def test_a_machine_override_stays_on_its_machine(world):
    a = world.machine("a")
    b = world.machine("b")
    (a.repo / "machines" / "host-a.settings.json").write_text('{"enabledPlugins":{"dnd@m":true}}\n')
    world.sync_as(a, "sync")
    assert settings(a.claude / "settings.json")["enabledPlugins"]["dnd@m"] is True
    before = rev(a.repo, "HEAD")
    world.sync_as(a, "sync")
    assert rev(a.repo, "HEAD") == before  # no churn
    assert "enabledPlugins" not in settings(a.repo / "dot-claude" / "settings.json")
    world.sync_as(b, "sync")
    assert "enabledPlugins" not in settings(b.claude / "settings.json")

    s = settings(a.claude / "settings.json")
    s["theme"] = "light"
    (a.claude / "settings.json").write_text(json.dumps(s))
    world.sync_as(a, "sync")
    world.sync_as(b, "sync")
    assert settings(b.claude / "settings.json")["theme"] == "light"

    s = settings(a.claude / "settings.json")
    s["enabledPlugins"]["dnd@m"] = False
    (a.claude / "settings.json").write_text(json.dumps(s))
    world.sync_as(a, "sync")
    assert settings(a.repo / "machines" / "host-a.settings.json") == {"enabledPlugins": {"dnd@m": False}}
    assert "enabledPlugins" not in settings(a.repo / "dot-claude" / "settings.json")


def test_a_pre_override_apply_is_not_mistaken_for_deleting_keys(world):
    a = world.machine("a")
    (a.repo / "machines" / "host-a.settings.json").write_text('{"enabledPlugins":{"dnd@m":true}}\n')
    world.sync_as(a, "sync")
    shutil.copyfile(a.repo / "dot-claude" / "settings.json", a.claude / "settings.json")
    (a.claude / ".claude-sync-applied-override.json").unlink()
    world.sync_as(a, "sync")
    assert settings(a.repo / "machines" / "host-a.settings.json") == {"enabledPlugins": {"dnd@m": True}}
    assert settings(a.claude / "settings.json")["enabledPlugins"]["dnd@m"] is True


def _override_machine(world: World) -> Machine:
    """Machine a with an enabledPlugins override merged and applied."""
    a = world.machine("a")
    (a.repo / "machines" / "host-a.settings.json").write_text('{"enabledPlugins":{"dnd@m":true}}\n')
    world.sync_as(a, "sync")
    assert settings(a.claude / "settings.json")["enabledPlugins"]["dnd@m"] is True
    return a


def test_a_typo_in_the_override_does_not_leak_it_into_shared_settings(world):
    a = _override_machine(world)
    ov = a.repo / "machines" / "host-a.settings.json"
    s = settings(a.claude / "settings.json")
    s["theme"] = "light"
    (a.claude / "settings.json").write_text(json.dumps(s))
    ov.write_text('{"enabledPlugins":{"dnd@m":true}\n')  # missing brace
    rc, _ = world.run(a, "sync")
    assert rc == 1
    ov.write_text('{"enabledPlugins":{"dnd@m":true}}\n')
    world.ok(a, "sync")
    shared = json.loads(effective_show(a.repo, a.host, "dot-claude/settings.json"))
    assert shared["theme"] == "light"
    assert "enabledPlugins" not in shared


def test_a_merged_file_left_in_the_clone_is_not_the_split_base(world):
    """A merged live settings.json left in the clone's working tree (an
    older run, or a restore that failed) is not taken as the shared base:
    the split reads the effective commit."""
    a = _override_machine(world)
    s = settings(a.claude / "settings.json")
    s["theme"] = "light"
    (a.claude / "settings.json").write_text(json.dumps(s))
    shutil.copyfile(a.claude / "settings.json", a.repo / "dot-claude" / "settings.json")
    world.ok(a, "sync")
    shared = json.loads(effective_show(a.repo, a.host, "dot-claude/settings.json"))
    assert shared["theme"] == "light"
    assert "enabledPlugins" not in shared



@pytest.mark.parametrize("stop", [Fail("split failed"), SystemExit(143)])
def test_a_split_that_stopped_after_capture_leaves_nothing_for_pull_to_commit(world, monkeypatch, stop):
    """A run stopped between capture and split (a refusal, a write error, a
    SIGTERM) must not leave the merged live file in the clone, where
    pull's 'clone edits' commit would take it as shared."""
    a = _override_machine(world)
    committed = effective_show(a.repo, a.host, "dot-claude/settings.json")

    real, calls = gitflow.overlay.split_files, []

    def stopped_once(*args):
        calls.append(1)
        if len(calls) == 1:
            raise stop
        return real(*args)

    monkeypatch.setattr(gitflow.overlay, "split_files", stopped_once)
    rc, _ = world.run(a, "sync")
    assert rc != 0
    assert read(a.repo / "dot-claude" / "settings.json") == committed
    world.ok(a, "pull")
    shared = json.loads(effective_show(a.repo, a.host, "dot-claude/settings.json"))
    assert "enabledPlugins" not in shared


# ── apply ────────────────────────────────────────────────────────────────


def test_pull_on_a_fresh_machine_applies_what_another_pushed(world):
    a = world.machine("a")
    (a.claude / "CLAUDE.md").write_text("# from machine A\n")
    mem = a.claude / "projects" / "-home-user-project" / "memory"
    mem.mkdir(parents=True, exist_ok=True)
    (mem / "MEMORY.md").write_text("machine A memory\n")
    world.ok(a, "push")
    merge_sync_pr(world.origin, "host-a", gh_state=world.gh_state)
    b = world.machine("b")
    assert read(b.claude / "CLAUDE.md") == "# from machine A\n"
    assert read(b.claude / "projects" / "-home-user-work" / "memory" / "MEMORY.md") == "machine A memory\n"


def test_pull_keeps_this_machines_unmerged_changes_live(world):
    a = world.machine("a")
    b = world.machine("b")
    (a.claude / "CLAUDE.md").write_text("pending edit\n")
    world.ok(a, "sync")
    (b.claude / "agents" / "b-agent.md").write_text("from b\n")
    world.ok(b, "sync")
    merge_sync_pr(world.origin, "host-b", gh_state=world.gh_state)
    before = rev(world.origin, "sync/host-a")
    world.ok(a, "pull")
    assert read(a.claude / "CLAUDE.md") == "pending edit\n"
    assert read(a.claude / "agents" / "b-agent.md") == "from b\n"
    assert rev(world.origin, "sync/host-a") == before


def test_bootstrap_adopts_shared_config_before_contributing_local_only(world):
    a = world.machine("a")
    (a.agents / "skills" / "shared-skill").mkdir()
    (a.agents / "skills" / "shared-skill" / "SKILL.md").write_text("shared skill from A\n")
    (a.claude / "CLAUDE.md").write_text("# from machine A\n")
    world.ok(a, "push")
    merge_sync_pr(world.origin, "host-a", gh_state=world.gh_state)

    b = world.machine("b", pull=False)
    (b.agents / "skills" / "local-only-b").mkdir()
    (b.agents / "skills" / "local-only-b" / "SKILL.md").write_text("local only skill from B\n")
    world.ok(b, "bootstrap")
    assert read(b.claude / "CLAUDE.md") == "# from machine A\n"
    assert read(b.agents / "skills" / "shared-skill" / "SKILL.md") == "shared skill from A\n"
    assert read(b.agents / "skills" / "local-only-b" / "SKILL.md") == "local only skill from B\n"
    shown = git("-C", world.origin, "show", "sync/host-b:dot-agents/skills/local-only-b/SKILL.md")
    assert shown.stdout == "local only skill from B\n"
    # Skill links are wired after the apply.
    assert (b.claude / "skills" / "shared-skill" / "SKILL.md").is_file()


def test_apply_without_machine_conf_says_run_install(world):
    a = world.machine("a")
    (a.repo / "machines" / "host-a.conf").unlink()
    with pytest.raises(Fail) as e:
        gitflow.do_apply(a.ctx())
    assert "No machine config at" in e.value.message
    assert "Run install.sh first." in e.value.message


def test_line_endings_survive_the_round_trip(world):
    a = world.machine("a")
    (a.claude / "CLAUDE.md").write_bytes(b"crlf line\r\nlf line\n")
    world.sync_as(a, "sync")
    assert (a.claude / "CLAUDE.md").read_bytes() == b"crlf line\r\nlf line\n"
    b = world.machine("b")
    assert (b.claude / "CLAUDE.md").read_bytes() == b"crlf line\r\nlf line\n"
    assert (b.claude / "settings.json").read_bytes() == b'{"theme":"dark"}\n'


@posix_only
def test_a_signal_during_apply_waits_until_the_apply_is_recorded(world, monkeypatch):
    """Review focus 1: a stop between profile_unapply and profile_apply must
    not leave live with the shared copy of a profile-owned entry."""
    a = world.machine("a")
    (a.repo / "machines" / "host-a" / "agents").mkdir(parents=True)
    (a.repo / "machines" / "host-a" / "agents" / "own.md").write_text("profile copy\n")
    gitflow.commit_worktree(a.ctx(), "profile entry")
    real_apply = gitflow.profile.profile_apply

    def interrupted_apply(*args):
        os.kill(os.getpid(), signal.SIGINT)
        real_apply(*args)

    monkeypatch.setattr(gitflow.profile, "profile_apply", interrupted_apply)
    ctx = a.ctx()
    with pytest.raises(SystemExit) as e:
        gitflow.do_apply(ctx)
    assert e.value.code == 130
    assert read(a.claude / "agents" / "own.md") == "profile copy\n"
    assert read(a.claude / ".claude-sync-applied-commit") == gitflow.effective_commit(ctx) + "\n"


def test_refute_helper():
    refute(["git", "rev-parse", "--verify", "refs/heads/does-not-exist-anywhere"])


def test_begin_sync_checks_the_git_version_before_the_remote(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    git("init", "-q", repo)  # no origin

    def old_git(*_a, **_k):
        raise Fail("git 2.40 or newer is needed (found: git version 2.1.0).")

    monkeypatch.setattr(gitflow, "require_git_version", old_git)
    ctx = SyncCtx(repo=repo, claude_home=tmp_path / "c", agents_home=tmp_path / "g", hostname="host-a")
    with ExitStack() as stack, pytest.raises(Fail) as info:
        gitflow.begin_sync(ctx, stack)
    assert "git 2.40" in info.value.message


def test_git_errors_shown_on_stderr_are_redacted(capsys):
    url = "https://user:ghp_abcdefghijklmnopqrstuvwxyz0123@example.invalid/owner/repo.git"
    result = subprocess.CompletedProcess([], 128, "", f"fatal: unable to access '{url}': nope\n")
    gitflow._show_stderr(result)
    err = capsys.readouterr().err
    assert "ghp_" not in err and "user:" not in err
    assert "https://example.invalid/owner/repo.git" in err


def test_a_failed_fetch_hides_the_url_credentials(world, capsys):
    a = world.machine("a")
    git("-C", a.repo, "remote", "set-url", "origin", "https://user:s3cret@127.0.0.1:9/none.git")
    rc, out = world.run(a, "sync")
    assert rc == 1
    assert "git fetch failed" in out
    assert "s3cret" not in out and "s3cret" not in capsys.readouterr().err


@pytest.mark.parametrize("held", [True, False])
def test_git_calls_in_the_apply_window_follow_the_held_group_flag(world, monkeypatch, held):
    """Windows has no signal mask, so git inside do_apply's held window gets
    its own process group there (Ctrl-C off); on POSIX the blocked mask is
    inherited and the flag stays off."""
    assert gitflow._HELD_GROUP is (os.name == "nt")
    a = world.machine("a")
    ctx = a.ctx()
    calls = []
    real = gitflow.run

    def spy(args, **kwargs):
        calls.append(kwargs.get("new_process_group", False))
        return real(args, **kwargs)

    monkeypatch.setattr(gitflow, "_HELD_GROUP", held)
    monkeypatch.setattr(gitflow, "run", spy)
    gitflow.do_apply(ctx)
    assert calls and all(c is held for c in calls)


def test_a_moved_clone_with_an_applied_profile_skips_capture_not_refuses(world):
    """profile.bats stale-clone case: live_matches_commit rebuilds the
    expected tree with the profile applied, so an applied profile is not
    taken for a local edit (risks row 3)."""
    world.seed_profiles()
    a = world.machine("a")
    pa = a.repo / "machines" / "host-a"
    (pa / "agents").mkdir(parents=True)
    (pa / "skills" / "shared-skill").mkdir(parents=True)
    (pa / "agents" / "helper.md").write_bytes(b"host-a helper\n")
    (pa / "skills" / "shared-skill" / "SKILL.md").write_bytes(b"host-a skill\n")
    (pa / "agents" / "reviewer.md.disabled").write_bytes(b"")
    (pa / "CLAUDE.append.md").write_bytes(b"host-a only\n")
    world.sync_as(a, "sync")
    world.commit_on_origin(
        {"dot-claude/hooks/newer.sh": b"#!/bin/bash\necho newer\n"},
        "newer hook",
        executable=("dot-claude/hooks/newer.sh",),
    )
    git("-C", a.repo, "pull", "-q", "--ff-only")

    out = world.sync_as(a, "sync")
    assert "skipping capture" in out and "refusing" not in out
    assert (a.claude / "hooks" / "newer.sh").is_file()
    assert (a.claude / "agents" / "helper.md").read_bytes() == b"host-a helper\n"


# ── hook modes: Windows git has no exec bit ──────────────────────────────
# core.filemode=false fakes Windows on Linux: git add then stages a new
# file as 100644 and ignores the exec bit on disk.


def _mode(repo: Path, rev_: str, path: str) -> str:
    out = git("-C", repo, "ls-tree", rev_, "--", path).stdout
    return out.split(" ", 1)[0] if out else ""


def _no_file_mode(m: Machine) -> None:
    git("-C", m.repo, "config", "core.filemode", "false")


def _write_file(path: Path, data: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(mode)


def test_a_new_hook_is_committed_executable_without_a_file_mode(world):
    a = world.machine("a")
    _no_file_mode(a)
    hooks = a.claude / "hooks"
    _write_file(hooks / "guard.sh", b"echo no shebang, but a .sh name\n")
    _write_file(hooks / "check", b"#!/usr/bin/env python3\nprint('hi')\n")
    _write_file(hooks / "notes.txt", b"plain notes\n")
    _write_file(a.repo / "machines" / "host-a" / "hooks" / "extra", b"#!/bin/sh\necho extra\n")
    world.ok(a, "sync")
    sb = "sync/host-a"
    assert _mode(world.origin, sb, "dot-claude/hooks/guard.sh") == "100755"
    assert _mode(world.origin, sb, "dot-claude/hooks/check") == "100755"
    assert _mode(world.origin, sb, "machines/host-a/hooks/extra") == "100755"
    assert _mode(world.origin, sb, "dot-claude/hooks/notes.txt") == "100644"
    assert git("-C", a.repo, "diff", "--cached", "--name-only").stdout == ""


def test_a_new_script_outside_the_hooks_is_left_alone(world):
    a = world.machine("a")
    _no_file_mode(a)
    _write_file(a.claude / "agents" / "runner.sh", b"#!/bin/bash\necho agent\n")
    world.ok(a, "sync")
    assert _mode(world.origin, "sync/host-a", "dot-claude/agents/runner.sh") == "100644"


def test_an_existing_100644_hook_keeps_its_mode(world):
    world.commit_on_origin({"dot-claude/hooks/plain.sh": b"#!/bin/bash\necho one\n"}, "plain hook")
    a = world.machine("a")
    _no_file_mode(a)
    assert _mode(a.repo, "HEAD", "dot-claude/hooks/plain.sh") == "100644"
    (a.claude / "hooks" / "plain.sh").write_bytes(b"#!/bin/bash\necho two\n")
    world.ok(a, "sync")
    assert git("-C", world.origin, "show", "sync/host-a:dot-claude/hooks/plain.sh").stdout.endswith("two\n")
    assert _mode(world.origin, "sync/host-a", "dot-claude/hooks/plain.sh") == "100644"


def test_an_existing_100755_hook_keeps_its_mode_when_the_file_lost_its_exec_bit(world):
    world.seed_profiles()
    a = world.machine("a")
    assert _mode(a.repo, "HEAD", "dot-claude/hooks/guard.sh") == "100755"
    live = a.claude / "hooks" / "guard.sh"
    live.write_bytes(b"#!/bin/bash\necho edited guard\n")
    live.chmod(0o644)
    world.ok(a, "sync")
    assert git("-C", world.origin, "show", "sync/host-a:dot-claude/hooks/guard.sh").stdout.endswith(
        "edited guard\n"
    )
    assert _mode(world.origin, "sync/host-a", "dot-claude/hooks/guard.sh") == "100755"


def _drop_exec_bit_on_origin(world, rel: str) -> None:
    """A hand-made PR, merged: the mode change alone, made with update-index
    so it works without a file mode too."""
    edit = world.tmp / "edit"  # made by seed_profiles
    git("-C", edit, "pull", "-q", "--ff-only")
    git("-C", edit, "update-index", "--chmod=-x", "--", rel)
    git("-C", edit, "commit", "-q", "-m", f"drop the exec bit of {rel}")
    git("-C", edit, "push", "-q", "origin", "master")


@pytest.mark.parametrize("file_mode", [True, False])
def test_an_exec_bit_removed_on_the_default_branch_reaches_the_machine(world, file_mode):
    """The one way to remove an exec bit: change the mode on the default
    branch through a PR. A machine's next sync takes it and does not put
    the bit back (with or without a file mode)."""
    world.seed_profiles()
    a = world.machine("a")
    if not file_mode:
        _no_file_mode(a)
    rel = "dot-claude/hooks/guard.sh"
    _drop_exec_bit_on_origin(world, rel)
    world.ok(a, "sync")
    assert _mode(a.repo, "origin/master", rel) == "100644"
    assert _mode(a.repo, gitflow.effective_commit(a.ctx()), rel) == "100644"
    assert not has_ref(world.origin, "refs/heads/sync/host-a")
    if POSIX and file_mode:
        assert (a.claude / "hooks" / "guard.sh").stat().st_mode & 0o111 == 0
    world.ok(a, "sync")
    assert _mode(a.repo, gitflow.effective_commit(a.ctx()), rel) == "100644"
    assert not has_ref(world.origin, "refs/heads/sync/host-a")


def test_an_exec_bit_only_on_the_sync_branch_is_kept_without_a_file_mode(world):
    """The base is the sync branch, not HEAD: once materialised, the index
    matches the default branch, so git add stages this file anew."""
    a = world.machine("a")
    _no_file_mode(a)
    tool = a.repo / "tools" / "run"
    _write_file(tool, b"echo one\n")
    git("-C", a.repo, "add", "--", "tools/run")
    git("-C", a.repo, "update-index", "--chmod=+x", "--", "tools/run")
    world.ok(a, "sync")
    assert _mode(world.origin, "sync/host-a", "tools/run") == "100755"
    tool.write_bytes(b"echo two\n")
    world.ok(a, "sync")
    assert git("-C", world.origin, "show", "sync/host-a:tools/run").stdout == "echo two\n"
    assert _mode(world.origin, "sync/host-a", "tools/run") == "100755"
