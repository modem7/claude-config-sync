"""Tests for the apply backups (issue 72): before apply overwrites or deletes
a live file whose bytes differ, it copies it to
~/.claude/.claude-sync-backups/<UTC time>/<path>. Placeholder hostnames
only: this file is published.
"""

from __future__ import annotations

import io
import os
import stat
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path

import pytest
from helpers import git, make_dir_link

from claude_sync import doctor, gitflow, install, syncfiles
from claude_sync.syncfiles import BACKUP_DIR, Backup, backups_from_env, prune_backups
from claude_sync.util import Fail

POSIX = os.name == "posix"


def root_of(m) -> Path:
    return m.claude / BACKUP_DIR


def runs(m) -> list[Path]:
    return syncfiles.backup_runs(root_of(m))


def files_in(folder: Path) -> dict[str, bytes]:
    found = {}
    for dirpath, _dirs, names in os.walk(folder):
        for name in names:
            p = Path(dirpath) / name
            found[p.relative_to(folder).as_posix()] = p.read_bytes()
    return found


def memory_of(m) -> Path:
    return gitflow.memory_dir_of(m.ctx())


def pull_with(world, m, backups: int) -> tuple[int, str]:
    ctx = m.ctx()
    ctx.backups = backups
    return world.run(m, "pull", ctx)


def run_doctor(world, m, remediate=False):
    def nested() -> bool:
        rc, out = world.run(m, "sync")
        print(out, end="")
        return rc == 0

    buf = io.StringIO()
    with redirect_stdout(buf), redirect_stderr(buf), ExitStack() as stack:
        rc = doctor.do_doctor(m.ctx(), stack, remediate=remediate, run_sync=nested)
    return rc, buf.getvalue()


@pytest.fixture
def pworld(world):
    world.seed_profiles()
    return world


# ── acceptance: recoverable byte for byte ────────────────────────────────


def test_a_local_edit_overwritten_by_apply_is_recoverable_byte_for_byte(world):
    a = world.machine("a")
    edited = b"# my local edit\r\nno newline at the end \x00\xff"
    (a.claude / "CLAUDE.md").write_bytes(edited)

    out = world.ok(a, "pull")

    assert (a.claude / "CLAUDE.md").read_bytes() == b"# shared\n"
    newest = syncfiles.newest_backup(root_of(a))
    assert newest is not None
    assert files_in(newest) == {"CLAUDE.md": edited}
    assert f"the old copies are in {newest}" in out


def test_files_apply_deletes_are_backed_up_under_their_own_paths(world):
    a = world.machine("a")
    (a.claude / "agents" / "local-only.md").write_bytes(b"local agent\n")
    (a.agents / "skills" / "mine" / "SKILL.md").parent.mkdir(parents=True)
    (a.agents / "skills" / "mine" / "SKILL.md").write_bytes(b"local skill\n")
    memory = memory_of(a)
    (memory / "note.md").write_bytes(b"local memory\n")

    world.ok(a, "pull")

    assert not (a.claude / "agents" / "local-only.md").exists()
    assert not (memory / "note.md").exists()
    mem_rel = memory.relative_to(a.claude).as_posix()
    assert files_in(syncfiles.newest_backup(root_of(a))) == {
        "agents/local-only.md": b"local agent\n",
        ".agents/skills/mine/SKILL.md": b"local skill\n",
        f"{mem_rel}/note.md": b"local memory\n",
    }


def test_a_locally_edited_profile_entry_is_backed_up_when_pull_replaces_it(pworld):
    a = pworld.machine("a")
    pdir = a.repo / "machines" / "host-a" / "agents"
    pdir.mkdir(parents=True)
    (pdir / "helper.md").write_bytes(b"host-a helper\n")
    pworld.sync_as(a, "sync")
    assert runs(a) == [], [files_in(r) for r in runs(a)]
    (a.claude / "agents" / "helper.md").write_bytes(b"edited here\n")

    pworld.ok(a, "pull")

    assert (a.claude / "agents" / "helper.md").read_bytes() == b"host-a helper\n"
    assert files_in(syncfiles.newest_backup(root_of(a))) == {"agents/helper.md": b"edited here\n"}


# ── acceptance: nothing differs, no backup ───────────────────────────────


def test_no_backup_folder_when_nothing_differs(world):
    a = world.machine("a")
    world.sync_as(a, "sync")
    world.ok(a, "pull")
    world.ok(a, "pull")
    assert not root_of(a).exists()


def test_an_override_and_a_profile_layered_back_on_leave_no_backup(pworld):
    """sync_apply writes the shared settings.json and CLAUDE.md over the
    merged ones, then the overlay and profile put them back: not a change.
    (The sync that first commits the override does replace the old live
    settings.json: the split rewrites the shared file's JSON layout, so the
    compact seed bytes differ and that one copy is kept.)"""
    a = pworld.machine("a")
    pdir = a.repo / "machines" / "host-a"
    (pdir / "agents").mkdir(parents=True)
    (pdir / "agents" / "helper.md").write_bytes(b"host-a helper\n")
    (pdir / "CLAUDE.append.md").write_bytes(b"host-a notes\n")
    (a.repo / "machines" / "host-a.settings.json").write_bytes(b'{"theme":"solarized"}\n')
    pworld.sync_as(a, "sync")
    assert b"host-a notes" in (a.claude / "CLAUDE.md").read_bytes()
    assert b"solarized" in (a.claude / "settings.json").read_bytes()
    before = runs(a)
    assert [files_in(r) for r in before] == [{"settings.json": b'{"theme":"dark"}\n'}]

    pworld.ok(a, "pull")
    pworld.ok(a, "pull")

    assert runs(a) == before


def test_a_mode_or_mtime_only_difference_makes_no_backup(world):
    a = world.machine("a")
    os.utime(a.claude / "CLAUDE.md", (1, 1))
    world.ok(a, "pull")
    assert not root_of(a).exists()


# ── acceptance: keep N, 0 disables ───────────────────────────────────────


def test_backups_beyond_n_are_pruned_oldest_first(world):
    a = world.machine("a")
    for n in range(3):
        (a.claude / "CLAUDE.md").write_bytes(f"edit {n}\n".encode())
        rc, out = pull_with(world, a, 2)
        assert rc == 0, out
    kept = runs(a)
    assert len(kept) == 2
    assert [files_in(r)["CLAUDE.md"] for r in kept] == [b"edit 1\n", b"edit 2\n"]


def test_zero_backups_makes_none_and_keeps_existing_ones(world):
    a = world.machine("a")
    (a.claude / "CLAUDE.md").write_bytes(b"edit\n")
    rc, out = pull_with(world, a, 0)
    assert rc == 0, out
    assert not root_of(a).exists()

    (a.claude / "CLAUDE.md").write_bytes(b"kept\n")
    world.ok(a, "pull")
    first = runs(a)
    assert len(first) == 1
    (a.claude / "CLAUDE.md").write_bytes(b"not kept\n")
    rc, out = pull_with(world, a, 0)
    assert rc == 0, out
    assert runs(a) == first


def test_prune_leaves_other_names_alone(tmp_path):
    root = tmp_path / BACKUP_DIR
    names = [f"2026010{d}T000000.000000Z" for d in range(1, 5)]
    for name in [*names, "keep-me", "20260101T000000Z"]:
        (root / name).mkdir(parents=True)
    prune_backups(root, 2)
    assert sorted(p.name for p in root.iterdir()) == sorted(
        [*names[2:], "keep-me", "20260101T000000Z"]
    )


def test_prune_removes_read_only_backups(tmp_path):
    """A read-only file (Windows) or folder (POSIX) in an old backup."""
    root = tmp_path / BACKUP_DIR
    old, new = root / "20260101T000000.000000Z", root / "20260102T000000.000000Z"
    for run in (old, new):
        (run / "agents").mkdir(parents=True)
        f = run / "agents" / "x.md"
        f.write_bytes(b"x\n")
        os.chmod(f, stat.S_IREAD)
        if POSIX:
            os.chmod(run / "agents", 0o500)
    prune_backups(root, 1)
    assert syncfiles.backup_runs(root) == [new]


def test_a_failed_prune_warns_and_does_not_fail(tmp_path, monkeypatch, capsys):
    root = tmp_path / BACKUP_DIR
    for d in range(1, 4):
        (root / f"2026010{d}T000000.000000Z").mkdir(parents=True)

    def refuse(_path):
        raise PermissionError("in use")

    monkeypatch.setattr(syncfiles, "_remove", refuse)
    prune_backups(root, 1)
    err = capsys.readouterr().err
    assert "could not remove old backup" in err
    assert "3 backup folders" in err
    assert len(syncfiles.backup_runs(root)) == 3


@pytest.mark.parametrize(("raw", "want"), [("", 10), ("  ", 10), ("0", 0), ("3", 3), (" 25 ", 25)])
def test_backups_from_env_reads_a_whole_number(raw, want):
    assert backups_from_env({"CLAUDE_SYNC_BACKUPS": raw}) == want
    assert backups_from_env({}) == 10


@pytest.mark.parametrize("raw", ["-1", "ten", "1.5", "1e3", "٣"])
def test_backups_from_env_refuses_anything_else(raw):
    with pytest.raises(Fail, match="CLAUDE_SYNC_BACKUPS must be a whole number"):
        backups_from_env({"CLAUDE_SYNC_BACKUPS": raw})


def test_an_invalid_backups_value_is_a_clear_cli_error(world):
    a = world.machine("a")
    (a.claude / "CLAUDE.md").write_bytes(b"edit\n")
    rc, out = world.cli(a, "pull", env={**a.env(), "CLAUDE_SYNC_BACKUPS": "lots"})
    assert rc == 1
    assert "CLAUDE_SYNC_BACKUPS must be a whole number, not 'lots'." in out
    assert (a.claude / "CLAUDE.md").read_bytes() == b"edit\n"


def test_the_backups_value_comes_from_the_env(world):
    a = world.machine("a")
    for n in range(2):
        (a.claude / "CLAUDE.md").write_bytes(f"edit {n}\n".encode())
        rc, out = world.cli(a, "pull", env={**a.env(), "CLAUDE_SYNC_BACKUPS": "1"})
        assert rc == 0, out
    assert len(runs(a)) == 1


def test_install_refuses_a_bad_backups_value_before_cloning(tmp_path, capsys):
    repo = tmp_path / "clone"
    env = {
        "CLAUDE_SYNC_BACKUPS": "x",
        "CLAUDE_SYNC_REPO_URL": str(tmp_path / "nowhere.git"),
        "CLAUDE_SYNC_REPO_DIR": str(repo),
        "HOME": str(tmp_path),
        "USERPROFILE": str(tmp_path),
    }
    assert install.main([], env) == 1
    assert "CLAUDE_SYNC_BACKUPS must be a whole number" in capsys.readouterr().err
    assert not repo.exists()


# ── acceptance: never captured, committed or published ──────────────────


def test_the_backup_folder_is_never_captured_or_committed(world):
    a = world.machine("a")
    (a.claude / "CLAUDE.md").write_bytes(b"lost edit\n")
    world.ok(a, "pull")
    assert len(runs(a)) == 1
    (a.claude / "agents" / "new.md").write_bytes(b"new agent\n")

    world.ok(a, "sync")

    tracked = git(
        "-C", world.origin, "ls-tree", "-r", "--name-only", "sync/host-a"
    ).stdout.splitlines()
    assert "dot-claude/agents/new.md" in tracked
    assert not [p for p in tracked if BACKUP_DIR in p or "lost edit" in p]
    shown = git("-C", world.origin, "grep", "-l", "lost edit", "sync/host-a", check=False)
    assert shown.stdout == ""
    assert not [p for p in a.repo.rglob("*") if BACKUP_DIR in p.as_posix()]


def test_bootstrap_backs_up_what_its_merge_overwrites_and_never_commits_it(world):
    b = world.machine("b", pull=False)
    original = b"# host-b's own CLAUDE.md\r\n"
    (b.claude / "CLAUDE.md").write_bytes(original)

    world.ok(b, "bootstrap")

    assert (b.claude / "CLAUDE.md").read_bytes() == b"# shared\n"
    assert files_in(syncfiles.newest_backup(root_of(b))) == {"CLAUDE.md": original}
    for ref in ("master", "sync/host-b"):
        listed = git("-C", world.origin, "ls-tree", "-r", "--name-only", ref, check=False).stdout
        assert BACKUP_DIR not in listed, ref


def test_the_backup_folder_is_never_published(world, tmp_path):
    from claude_sync import publish

    world.commit_on_origin({"README.public.md": b"# public\n"}, "public readme")
    a = world.machine("a")
    (a.claude / "CLAUDE.md").write_bytes(b"lost edit\n")
    world.ok(a, "pull")
    assert len(runs(a)) == 1
    (a.claude / "agents" / "new.md").write_bytes(b"new agent\n")
    world.sync_as(a, "sync")
    assert git("-C", a.repo, "status", "--porcelain").stdout == ""

    public = tmp_path / "public.git"
    git("init", "-q", "--bare", public)
    git("-C", public, "symbolic-ref", "HEAD", "refs/heads/main")
    env = {**os.environ, "CLAUDE_CONFIG_PUBLIC_REPO_URL": str(public)}
    env.pop("CLAUDE_CONFIG_PUBLIC_BRANCH", None)
    env.pop("GITHUB_REPOSITORY", None)
    buf = io.StringIO()
    with redirect_stdout(buf), redirect_stderr(buf):
        rc = publish.publish(a.repo, env)
    assert rc == 0, buf.getvalue()

    tree = git("-C", public, "ls-tree", "-r", "--name-only", "main").stdout.splitlines()
    assert "dot-claude/agents/new.md" in tree  # the export did run
    assert not [p for p in tree if BACKUP_DIR in p]
    assert git("-C", public, "grep", "-l", "lost edit", "main", check=False).stdout == ""


def test_sync_capture_never_copies_the_backup_folder(tmp_path):
    claude, agents, repo = tmp_path / "c", tmp_path / "a", tmp_path / "r"
    (claude / BACKUP_DIR / "20260101T000000.000000Z" / "agents").mkdir(parents=True)
    (claude / BACKUP_DIR / "20260101T000000.000000Z" / "agents" / "x.md").write_bytes(b"x\n")
    (claude / "agents").mkdir()
    (claude / "CLAUDE.md").write_bytes(b"# c\n")
    agents.mkdir()
    syncfiles.sync_capture(claude, agents, repo, memory_dir=claude / "projects" / "p" / "memory")
    assert not [p for p in repo.rglob("*") if BACKUP_DIR in p.as_posix() or p.name == "x.md"]


def test_backups_do_not_make_live_differ_from_the_commit(world):
    """live_matches_commit ignores the folder, so a moved clone with
    backups present skips capture instead of refusing (risks row 3)."""
    a = world.machine("a")
    world.sync_as(a, "sync")
    (a.claude / "CLAUDE.md").write_bytes(b"lost edit\n")
    world.ok(a, "pull")
    assert len(runs(a)) == 1
    ctx = a.ctx()
    sha = gitflow.applied_commit_stamp(ctx).read_text().strip()
    assert gitflow.live_matches_commit(ctx, sha, gitflow.memory_dir_of(ctx))

    world.commit_on_origin({"dot-claude/CLAUDE.md": b"# newer\n"}, "newer shared")
    git("-C", a.repo, "fetch", "-q", "origin")
    git("-C", a.repo, "merge", "-q", "--ff-only", "origin/master")
    out = world.ok(a, "sync")
    assert "skipping capture" in out
    assert (a.claude / "CLAUDE.md").read_bytes() == b"# newer\n"


def test_doctor_does_not_report_the_backup_folder(world):
    a = world.machine("a")
    world.sync_as(a, "sync")
    (a.claude / "CLAUDE.md").write_bytes(b"lost edit\n")
    world.ok(a, "pull")
    assert len(runs(a)) == 1
    rc, out = run_doctor(world, a)
    assert rc == 0, out
    assert "healthy" in out
    assert BACKUP_DIR not in out


# ── doctor mentions the newest backup on drift ───────────────────────────


def test_doctor_names_the_newest_backup_when_it_finds_drift(world):
    a = world.machine("a")
    world.sync_as(a, "sync")
    (a.claude / "settings.json").write_bytes(b'{"theme":"edited"}\n')

    rc, out = run_doctor(world, a)
    assert rc == 1
    assert "Note: no backups of replaced local files exist" in out

    rc, out = run_doctor(world, a, remediate=True)
    newest = syncfiles.newest_backup(root_of(a))
    assert newest is not None
    assert files_in(newest) == {"settings.json": b'{"theme":"edited"}\n'}
    assert f"newest backup of local files an apply replaced or removed is {newest}" in out


def test_doctor_names_the_backup_on_profile_drift(pworld):
    a = pworld.machine("a")
    pdir = a.repo / "machines" / "host-a" / "agents"
    pdir.mkdir(parents=True)
    (pdir / "helper.md").write_bytes(b"host-a helper\n")
    pworld.sync_as(a, "sync")
    (a.claude / "CLAUDE.md").write_bytes(b"lost edit\n")
    pworld.ok(a, "pull")
    newest = syncfiles.newest_backup(root_of(a))
    (pdir / "helper.md").write_bytes(b"host-a helper v2\n")

    rc, out = run_doctor(pworld, a)
    assert rc == 1
    assert "live config differs from the shared config plus profile" in out
    assert f"is {newest}" in out


# ── mode, links and the root ─────────────────────────────────────────────


@pytest.mark.skipif(not POSIX, reason="POSIX modes")
def test_backup_folders_are_private(world):
    a = world.machine("a")
    (a.claude / "agents" / "sub").mkdir()
    (a.claude / "agents" / "sub" / "x.md").write_bytes(b"x\n")
    old = os.umask(0o022)
    try:
        world.ok(a, "pull")
    finally:
        os.umask(old)
    run = syncfiles.newest_backup(root_of(a))
    for folder in (root_of(a), run, run / "agents", run / "agents" / "sub"):
        assert stat.S_IMODE(folder.stat().st_mode) == 0o700, folder


@pytest.mark.skipif(not POSIX, reason="POSIX modes")
def test_an_existing_root_is_made_private(world):
    a = world.machine("a")
    root_of(a).mkdir(mode=0o755)
    os.chmod(root_of(a), 0o755)
    (a.claude / "CLAUDE.md").write_bytes(b"edit\n")
    world.ok(a, "pull")
    assert stat.S_IMODE(root_of(a).stat().st_mode) == 0o700


def test_a_root_that_is_a_link_is_refused_before_anything_changes(world, tmp_path):
    a = world.machine("a")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    make_dir_link(root_of(a), elsewhere)
    (a.claude / "CLAUDE.md").write_bytes(b"edit\n")
    (a.claude / "agents" / "local.md").write_bytes(b"local\n")

    rc, out = world.run(a, "pull")

    assert rc == 1
    assert "apply cannot keep backups there" in out
    assert (a.claude / "CLAUDE.md").read_bytes() == b"edit\n"
    assert (a.claude / "agents" / "local.md").read_bytes() == b"local\n"
    assert list(elsewhere.iterdir()) == []


def test_links_are_never_followed_into_the_backup(world, tmp_path):
    a = world.machine("a")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_bytes(b"outside\n")
    make_dir_link(a.claude / "agents" / "linked", outside)
    (a.claude / "agents" / "real").mkdir()
    (a.claude / "agents" / "real" / "x.md").write_bytes(b"x\n")
    make_dir_link(a.claude / "agents" / "real" / "inner", outside)

    world.ok(a, "pull")

    assert (outside / "secret.md").read_bytes() == b"outside\n"
    assert files_in(syncfiles.newest_backup(root_of(a))) == {"agents/real/x.md": b"x\n"}


def test_a_read_only_live_file_is_backed_up_and_replaced(world):
    a = world.machine("a")
    f = a.claude / "CLAUDE.md"
    f.write_bytes(b"read-only edit\n")
    os.chmod(f, stat.S_IREAD)
    world.ok(a, "pull")
    assert f.read_bytes() == b"# shared\n"
    assert files_in(syncfiles.newest_backup(root_of(a))) == {"CLAUDE.md": b"read-only edit\n"}


def test_the_first_bytes_of_a_path_win(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    f = home / "a.md"
    f.write_bytes(b"original\n")
    b = Backup(tmp_path / BACKUP_DIR, ((home, ""),))
    b.save(f)
    f.write_bytes(b"in between\n")
    b.save(f)
    f.write_bytes(b"final\n")
    run = b.finish()
    assert files_in(run) == {"a.md": b"original\n"}


def test_two_applies_in_a_row_get_their_own_folders(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    made = []
    for n in range(3):
        f = home / "a.md"
        f.write_bytes(f"{n}\n".encode())
        b = Backup(tmp_path / BACKUP_DIR, ((home, ""),))
        b.save(f)
        f.write_bytes(b"other\n")
        made.append(b.finish())
    assert len(set(made)) == 3
    assert syncfiles.backup_runs(tmp_path / BACKUP_DIR) == made
