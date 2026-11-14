"""Sync tiers (TIER=basic|full in machines/<hostname>.conf): basic never
touches memory on either side, switching is non-destructive, and a switch
from basic to full adopts memory without deleting. Placeholder hostnames
only: this file is published.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from helpers import git, has_ref
from test_doctor import run_doctor

from claude_sync import gitflow, machineconf
from claude_sync.machineconf import (
    machine_conf_path,
    read_machine_profile,
    read_machine_tier,
    read_primary_project_path,
    write_machine_conf,
    write_machine_profile,
    write_machine_tier,
)
from claude_sync.util import Fail

# ── machineconf ──────────────────────────────────────────────────────────


def test_tier_is_full_without_a_conf_a_line_or_a_value(tmp_path):
    assert read_machine_tier(tmp_path, "host-a") == "full"
    write_machine_conf(tmp_path, "host-a", "/p")
    assert read_machine_tier(tmp_path, "host-a") == "full"
    machine_conf_path(tmp_path, "host-a").write_bytes(b"PRIMARY_PROJECT_PATH=/p\nTIER=\n")
    assert read_machine_tier(tmp_path, "host-a") == "full"


def test_tier_line_tolerates_crlf(tmp_path):
    conf = machine_conf_path(tmp_path, "host-a")
    conf.parent.mkdir(parents=True)
    conf.write_bytes(b"PRIMARY_PROJECT_PATH=/p\r\nTIER=basic\r\n")
    assert read_machine_tier(tmp_path, "host-a") == "basic"
    assert read_primary_project_path(tmp_path, "host-a") == "/p"


@pytest.mark.parametrize("bad", ["Basic", "none", "full basic", "bäsic"])
def test_an_unknown_tier_is_a_clear_fail(tmp_path, bad):
    conf = machine_conf_path(tmp_path, "host-a")
    conf.parent.mkdir(parents=True)
    conf.write_bytes(f"TIER={bad}\n".encode())
    with pytest.raises(Fail) as err:
        read_machine_tier(tmp_path, "host-a")
    assert "Not a valid sync tier in machines/host-a.conf" in err.value.message
    assert "'basic' or 'full'" in err.value.hint
    with pytest.raises(Fail):
        write_machine_tier(tmp_path, "host-a", bad)


def test_write_machine_tier_keeps_other_lines_and_full_removes_the_line(tmp_path):
    conf = machine_conf_path(tmp_path, "host-a")
    conf.parent.mkdir(parents=True)
    original = b"# note \xff\nPRIMARY_PROJECT_PATH=/p\nPROFILE=host-b\nEXTRA=1"
    conf.write_bytes(original)
    write_machine_tier(tmp_path, "host-a", "basic")
    assert conf.read_bytes() == original + b"\nTIER=basic\n"
    write_machine_tier(tmp_path, "host-a", "basic")
    assert conf.read_bytes().count(b"TIER=") == 1
    assert read_machine_profile(tmp_path, "host-a") == "host-b"
    write_machine_tier(tmp_path, "host-a", "full")
    assert conf.read_bytes() == original + b"\n"
    write_machine_profile(tmp_path, "host-a", "")
    assert read_machine_tier(tmp_path, "host-a") == "full"


def test_a_basic_conf_needs_no_project_path(tmp_path):
    write_machine_tier(tmp_path, "host-a", "basic")
    assert machine_conf_path(tmp_path, "host-a").read_bytes() == b"TIER=basic\n"
    assert read_primary_project_path(tmp_path, "host-a") is None


# ── helpers ──────────────────────────────────────────────────────────────


def memdir(m) -> Path:
    return gitflow.memory_dir_of(m.ctx())


def tree_of_memory(repo: Path, ref: str) -> str:
    return git("-C", repo, "ls-tree", "-r", ref, "--", "memory").stdout


def origin_tree(world, ref: str) -> str:
    return git("-C", world.origin, "ls-tree", "-r", "--name-only", ref).stdout


def set_tier_on_origin(world, host: str, tier: str, project: str | None = "/home/user/project") -> None:
    line = f"PRIMARY_PROJECT_PATH={project}\n" if project else ""
    if tier == "basic":
        line += "TIER=basic\n"
    world.commit_on_origin({f"machines/{host}.conf": line.encode()}, f"{host} uses {tier}")


@pytest.fixture
def shared_memory(world):
    world.commit_on_origin(
        {"memory/shared.md": b"shared memory\n", "memory/MEMORY.md": b"- shared\n"},
        "shared memory",
    )
    return world


# ── basic never touches memory ───────────────────────────────────────────


def test_basic_sync_leaves_repo_memory_identical_and_local_memory_alone(shared_memory):
    world = shared_memory
    set_tier_on_origin(world, "host-a", "basic", project=None)
    a = world.machine("a")
    # basic with no project path: no memory folder is ever worked out.
    assert gitflow.memory_mode(a.ctx()) == gitflow.MEMORY_OFF
    (a.claude / "CLAUDE.md").write_bytes(b"# edited on a\n")
    world.ok(a, "sync")
    # The PR carries the CLAUDE.md edit, and memory/ exactly as on master.
    assert tree_of_memory(world.origin, "sync/host-a") == tree_of_memory(world.origin, "master")
    assert git("-C", world.origin, "diff", "--quiet", "master", "sync/host-a", "--", "memory",
               check=False).returncode == 0
    assert not (a.claude / "projects").exists()
    assert (a.claude / ".claude-sync-applied-tier").read_bytes() == b"basic\n"


def test_basic_with_a_project_path_never_mirrors_an_empty_local_folder(shared_memory):
    world = shared_memory
    set_tier_on_origin(world, "host-a", "basic")
    a = world.machine("a")
    local = memdir(a)
    assert not local.exists()  # pull did not create it
    local.mkdir(parents=True)
    (local / "mine.md").write_bytes(b"local only\n")
    (local / "MEMORY.md").write_bytes(b"- local\n")
    world.ok(a, "sync")
    world.ok(a, "pull")
    assert sorted(p.name for p in local.iterdir()) == ["MEMORY.md", "mine.md"]
    assert (local / "MEMORY.md").read_bytes() == b"- local\n"
    for ref in ("master", "sync/host-a"):
        if has_ref(world.origin, f"refs/heads/{ref}"):
            assert "memory/shared.md" in origin_tree(world, ref)
            assert "memory/mine.md" not in origin_tree(world, ref)


def test_basic_capture_check_ignores_a_clone_that_moved_only_in_memory(world):
    set_tier_on_origin(world, "host-a", "basic", project=None)
    a = world.machine("a")
    world.commit_on_origin({"memory/from-b.md": b"b's memory\n"}, "memory from b")
    world.ok(a, "pull")  # the clone now holds b's memory; live has none
    world.commit_on_origin({"memory/from-b.md": b"b's memory, edited\n"}, "memory again")
    git("-C", a.repo, "fetch", "-q", "origin")
    git("-C", a.repo, "update-ref", "refs/heads/master", "origin/master")
    git("-C", a.repo, "read-tree", "-u", "--reset", "HEAD")
    (a.claude / "CLAUDE.md").write_bytes(b"# a's edit\n")
    out = world.ok(a, "sync")
    assert "refusing to capture" not in out
    shown = git("-C", world.origin, "show", "sync/host-a:dot-claude/CLAUDE.md").stdout
    assert shown == "# a's edit\n"


def test_basic_with_a_profile_and_an_override_still_applies_both(world):
    world.seed_profiles()
    world.commit_on_origin(
        {
            "machines/host-a/agents/helper.md": b"a's helper\n",
            "machines/host-a.settings.json": b'{"theme":"light"}\n',
            "machines/host-a.conf": b"TIER=basic\n",
        },
        "profile and override for host-a",
    )
    a = world.machine("a")
    assert (a.claude / "agents" / "helper.md").read_bytes() == b"a's helper\n"
    assert b'"light"' in (a.claude / "settings.json").read_bytes()
    (a.claude / "CLAUDE.md").write_bytes(b"# edited on a\n")
    world.ok(a, "sync")
    # The profile's copy did not leak into the shared one, nor the override.
    shown = git("-C", world.origin, "show", "sync/host-a:dot-claude/agents/helper.md").stdout
    assert shown == "shared helper\n"
    shown = git("-C", world.origin, "show", "sync/host-a:dot-claude/settings.json").stdout
    assert "light" not in shown
    assert (a.claude / "agents" / "helper.md").read_bytes() == b"a's helper\n"
    assert b'"light"' in (a.claude / "settings.json").read_bytes()


# ── switching ────────────────────────────────────────────────────────────


def test_tier_basic_changes_only_the_conf_and_deletes_nothing(shared_memory):
    world = shared_memory
    a = world.machine("a")
    local = memdir(a)
    assert (local / "shared.md").is_file()
    world.cli_ok(a, "sync")
    before_local = {p.name: p.read_bytes() for p in local.iterdir()}
    rc, out = world.cli(a, "tier", "basic")
    assert rc == 0, out
    assert "nothing was deleted" in out
    changed = git("-C", world.origin, "diff", "--name-only", "master", "sync/host-a").stdout.split()
    assert changed == ["machines/host-a.conf"]
    assert {p.name: p.read_bytes() for p in local.iterdir()} == before_local
    # Later memory changes on either side stay where they are.
    (local / "later.md").write_bytes(b"local after basic\n")
    world.commit_on_origin({"memory/shared.md": None}, "shared memory removed elsewhere")
    world.ok(a, "sync")
    assert (local / "shared.md").is_file() and (local / "later.md").is_file()
    assert "memory/later.md" not in origin_tree(world, "sync/host-a")


def test_tier_full_needs_a_project_path_and_changes_nothing_without_one(world):
    set_tier_on_origin(world, "host-a", "basic", project=None)
    a = world.machine("a")
    head = git("-C", a.repo, "rev-parse", "HEAD").stdout
    rc, out = world.cli(a, "tier", "full")
    assert rc == 1
    assert "names no project path" in out and "tier full /absolute/path" in out
    assert git("-C", a.repo, "rev-parse", "HEAD").stdout == head
    assert not has_ref(world.origin, "refs/heads/sync/host-a")


def test_tier_full_refuses_to_change_an_existing_project_path(world):
    set_tier_on_origin(world, "host-a", "basic")
    a = world.machine("a")
    rc, out = world.cli(a, "tier", "full", "/somewhere/else")
    assert rc == 1
    assert "already names the project path" in out


def _basic_machine_with_local_memory(world):
    world.commit_on_origin(
        {"memory/shared.md": b"shared memory\n", "memory/MEMORY.md": b"- shared\n"},
        "shared memory",
    )
    set_tier_on_origin(world, "host-a", "basic", project=None)
    a = world.machine("a")
    world.cli_ok(a, "sync")
    return a


def _local_memory(a, project="/home/user/project"):
    from claude_sync.memorypath import resolve_memory_dir

    local = resolve_memory_dir(a.claude, project)
    local.mkdir(parents=True, exist_ok=True)
    (local / "mine.md").write_bytes(b"local only\n")
    (local / "MEMORY.md").write_bytes(b"- local index\n")
    return local


def _assert_adopted(world, a, local):
    # Nothing local deleted; the shared copy won the name clash, the local
    # one was kept in a backup.
    assert (local / "mine.md").read_bytes() == b"local only\n"
    assert (local / "shared.md").read_bytes() == b"shared memory\n"
    assert (local / "MEMORY.md").read_bytes() == b"- shared\n"
    backups = list((a.claude / ".claude-sync-memory-backup").rglob("MEMORY.md"))
    assert [b.read_bytes() for b in backups] == [b"- local index\n"]
    # The next sync adds the local-only file and deletes nothing shared.
    world.ok(a, "sync")
    tree = origin_tree(world, "sync/host-a")
    assert "memory/mine.md" in tree and "memory/shared.md" in tree
    assert not (a.claude / ".claude-sync-applied-tier").exists()
    # From then on memory mirrors again: a shared deletion reaches a.
    world.cli_ok(a, "sync")
    world.commit_on_origin({"memory/shared.md": None}, "shared memory removed")
    world.ok(a, "sync")
    assert not (local / "shared.md").exists()
    assert (local / "mine.md").is_file()


def test_tier_full_command_adopts_memory_without_deleting(world):
    a = _basic_machine_with_local_memory(world)
    local = _local_memory(a)
    rc, out = world.cli(a, "tier", "full", "/home/user/project")
    assert rc == 0, out
    assert "PRIMARY_PROJECT_PATH=/home/user/project" in git(
        "-C", world.origin, "show", "sync/host-a:machines/host-a.conf").stdout
    assert "TIER=" not in git("-C", world.origin, "show", "sync/host-a:machines/host-a.conf").stdout
    assert "memory/mine.md" not in origin_tree(world, "sync/host-a")
    _assert_adopted(world, a, local)


def test_full_tier_arriving_by_pull_adopts_memory_without_deleting(world):
    a = _basic_machine_with_local_memory(world)
    local = _local_memory(a)
    set_tier_on_origin(world, "host-a", "full")
    world.ok(a, "pull")
    _assert_adopted(world, a, local)


def test_a_hand_edited_conf_then_sync_adopts_memory_without_deleting(world):
    a = _basic_machine_with_local_memory(world)
    local = _local_memory(a)
    conf = machine_conf_path(a.repo, a.host)
    conf.write_bytes(b"PRIMARY_PROJECT_PATH=/home/user/project\n")
    world.ok(a, "sync")
    # The first sync merged memory in and captured it without deleting.
    assert (local / "mine.md").is_file() and (local / "shared.md").is_file()
    tree = origin_tree(world, "sync/host-a")
    assert "memory/mine.md" in tree and "memory/shared.md" in tree
    assert not (a.claude / ".claude-sync-applied-tier").exists()


def test_a_machine_from_before_tiers_still_mirrors_memory(shared_memory):
    world = shared_memory
    a = world.machine("a")
    local = memdir(a)
    assert gitflow.memory_mode(a.ctx()) == gitflow.MEMORY_ON
    world.commit_on_origin({"memory/shared.md": None}, "shared memory removed")
    world.ok(a, "sync")
    assert not (local / "shared.md").exists()
    assert not (a.claude / ".claude-sync-applied-tier").exists()
    assert not (a.claude / ".claude-sync-memory-backup").exists()


def test_tier_show_and_the_same_tier_change_nothing(world):
    a = world.machine("a")
    rc, out = world.cli(a, "tier")
    assert rc == 0 and "Sync tier: full" in out
    rc, out = world.cli(a, "tier", "full")
    assert rc == 0 and "already uses sync tier 'full'" in out
    assert not has_ref(world.origin, "refs/heads/sync/host-a")
    rc, out = world.cli(a, "tier", "middle")
    assert rc == 1


def test_tier_show_works_on_a_feature_branch_but_tier_basic_is_skipped(world):
    a = world.machine("a")
    git("-C", a.repo, "checkout", "-q", "-b", "feature")
    rc, out = world.cli(a, "tier", "show")
    assert rc == 0 and "Sync tier: full" in out
    rc, out = world.cli(a, "tier", "basic")
    assert rc == 0 and "sync SKIPPED" in out
    assert read_machine_tier(a.repo, a.host) == "full"


# ── doctor ───────────────────────────────────────────────────────────────


def test_doctor_on_basic_warns_about_an_agent_memory_plugin_but_passes(world):
    world.commit_on_origin(
        {
            "dot-claude/settings.json":
                b'{"enabledPlugins":{"agent-memory@market":true,"other@market":true}}\n',
            "machines/host-a.conf": b"TIER=basic\n",
        },
        "plugin on, host-a basic",
    )
    a = world.machine("a")
    rc, out = run_doctor(world, a)
    assert rc == 0, out
    assert "Sync tier: basic" in out
    assert "WARNING: settings.json enables agent-memory@market" in out
    assert "other@market" not in out
    assert "No issues found" in out


def test_doctor_on_full_does_not_warn_about_the_plugin(world):
    world.commit_on_origin(
        {"dot-claude/settings.json": b'{"enabledPlugins":{"agent-memory@market":true}}\n'},
        "plugin on",
    )
    a = world.machine("a")
    rc, out = run_doctor(world, a)
    assert rc == 0, out
    assert "Sync tier: full" in out and "WARNING" not in out


def test_doctor_on_basic_skips_the_long_project_path_issue(world):
    long_project = "/home/user/" + "deep/" * 45
    world.commit_on_origin(
        {"machines/host-a.conf": f"PRIMARY_PROJECT_PATH={long_project}\nTIER=basic\n".encode()},
        "long path, basic",
    )
    a = world.machine("a")
    rc, out = run_doctor(world, a)
    assert rc == 0, out
    assert "characters" not in out


def test_memory_mode_follows_the_tier_and_the_local_record(world):
    a = world.machine("a")
    ctx = a.ctx()
    assert gitflow.memory_mode(ctx) == gitflow.MEMORY_ON
    gitflow.applied_tier_stamp(ctx).write_bytes(b"basic\n")
    assert gitflow.memory_mode(ctx) == gitflow.MEMORY_ADOPT
    machineconf.write_machine_tier(a.repo, a.host, "basic")
    assert gitflow.memory_mode(ctx) == gitflow.MEMORY_OFF


# ── syncfiles ────────────────────────────────────────────────────────────


def test_sync_capture_and_apply_without_a_memory_dir_never_touch_memory(tmp_path):
    from claude_sync import syncfiles

    repo, claude, agents = tmp_path / "repo", tmp_path / "claude", tmp_path / "agents"
    (repo / "memory").mkdir(parents=True)
    (repo / "memory" / "shared.md").write_bytes(b"shared\n")
    claude.mkdir()
    (claude / "CLAUDE.md").write_bytes(b"# live\n")
    syncfiles.sync_capture(claude, agents, repo, memory_dir=None)
    assert (repo / "memory" / "shared.md").read_bytes() == b"shared\n"
    (repo / "memory" / "shared.md").unlink()
    (repo / "memory").rmdir()
    syncfiles.sync_capture(claude, agents, repo, memory_dir=None)
    assert not (repo / "memory").exists()  # not even created
    syncfiles.sync_apply(repo, claude, agents, mirror=True, memory_dir=None)
    assert not (claude / "projects").exists()


def test_differing_files_lists_only_same_named_files_with_other_bytes(tmp_path):
    from claude_sync import syncfiles

    a, b = tmp_path / "a", tmp_path / "b"
    (a / "sub").mkdir(parents=True)
    (b / "sub").mkdir(parents=True)
    (a / "same.md").write_bytes(b"x")
    (b / "same.md").write_bytes(b"x")
    (a / "sub" / "diff.md").write_bytes(b"repo")
    (b / "sub" / "diff.md").write_bytes(b"local")
    (a / "only-a.md").write_bytes(b"a")
    (b / "only-b.md").write_bytes(b"b")
    assert syncfiles.differing_files(a, b) == ["sub/diff.md"]
    assert syncfiles.differing_files(a, tmp_path / "missing") == []


def test_a_memory_edit_after_the_switch_survives_the_next_sync(world):
    a = _basic_machine_with_local_memory(world)
    local = _local_memory(a)
    rc, out = world.cli(a, "tier", "full", "/home/user/project")
    assert rc == 0, out
    assert (a.claude / ".claude-sync-applied-tier").read_bytes() == b"merged\n"
    (local / "MEMORY.md").write_bytes(b"- shared\n- mine\n")
    world.ok(a, "sync")
    assert (local / "MEMORY.md").read_bytes() == b"- shared\n- mine\n"
    shown = git("-C", world.origin, "show", "sync/host-a:memory/MEMORY.md").stdout
    assert shown == "- shared\n- mine\n"
    backups = list((a.claude / ".claude-sync-memory-backup").rglob("MEMORY.md"))
    assert [b.read_bytes() for b in backups] == [b"- local index\n"]
    assert "memory/mine.md" in origin_tree(world, "sync/host-a")


def test_a_full_conf_without_a_project_path_says_what_to_add(world):
    world.commit_on_origin({"machines/host-a.conf": b"EXTRA=1\n"}, "no project path")
    a = world.machine("a", pull=False)
    rc, out = world.run(a, "pull")
    assert rc == 1
    assert "has no PRIMARY_PROJECT_PATH= line" in out
    assert "TIER=basic" in out
