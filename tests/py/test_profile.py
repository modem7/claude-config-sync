"""Tests for claude_sync.profile, ported from tests/profile.bats.

profile.bats drives claude-sync.sh end to end. Here the same cases run at
module level: `simulate_sync` stands in for one `claude-sync.sh sync`
(capture, then unapply, mirror apply and profile apply). Cases that need the
CLI (profile list/use/copy, doctor, install.sh, the stale-clone refusal) are
ported with the CLI. Placeholder hostnames only: this file is published.
"""

from __future__ import annotations

import os
import shutil
import stat

import pytest

from claude_sync import machineconf, profile
from claude_sync.profile import (
    MARKER,
    profile_apply,
    profile_append_block,
    profile_capture,
    profile_dangling_hook_refs,
    profile_describe,
    profile_disabled,
    profile_entries,
    profile_excludes,
    profile_has_local_edits,
    profile_in_use,
    profile_live_mismatches,
    profile_owned_names,
    profile_resolve_name,
    profile_stamp_dir,
    profile_strip_claude_md,
    profile_unapply,
    profile_valid_name,
)
from claude_sync.syncfiles import sync_apply
from claude_sync.util import Fail

POSIX = os.name == "posix"


def _write(path, data: bytes | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode("utf-8")
    path.write_bytes(data)


def _is_exec(path) -> bool:
    return bool(path.stat().st_mode & stat.S_IXUSR)


@pytest.fixture
def env(tmp_path):
    """The bats seed: shared agents, an executable hook, two skills, CLAUDE.md,
    memory, and a live machine (host-a) with nothing applied yet."""

    class E:
        repo = tmp_path / "repo"
        claude = tmp_path / "claude_home"
        agents = tmp_path / "agents_home"
        memory = tmp_path / "memory"
        pa = tmp_path / "repo" / "machines" / "host-a"

    r = E.repo
    for d in ("machines", "dot-claude/agents", "dot-claude/hooks", "memory"):
        _write(r / d / ".gitkeep", b"")
    _write(r / "dot-claude/CLAUDE.md", "# shared instructions\n")
    _write(r / "dot-claude/settings.json", '{"theme":"dark"}\n')
    _write(r / "dot-claude/agents/helper.md", "shared helper\n")
    _write(r / "dot-claude/agents/reviewer.md", "shared reviewer\n")
    _write(r / "dot-claude/hooks/guard.sh", "#!/bin/bash\necho shared guard\n")
    (r / "dot-claude/hooks/guard.sh").chmod(0o755)
    _write(r / "dot-agents/skills/shared-skill/SKILL.md", "shared skill\n")
    _write(r / "dot-agents/skills/shared-skill/notes.md", "only in the shared copy\n")
    _write(r / "dot-agents/skills/other-skill/SKILL.md", "other skill\n")
    _write(r / "memory/MEMORY.md", "shared memory\n")
    machineconf.write_machine_conf(r, "host-a", "/home/user/project")
    for d in (E.claude, E.agents / "skills", E.memory):
        d.mkdir(parents=True)
    return E


def apply_all(e, name="host-a"):
    """The apply half of a sync: unapply, mirror the shared copy, apply."""
    profile_unapply(e.claude, e.agents)
    sync_apply(e.repo, e.claude, e.agents, mirror=True, memory_dir=e.memory)
    profile_apply(e.repo, e.claude, e.agents, name)


def simulate_sync(e, name="host-a"):
    """One `claude-sync.sh sync`: capture (updating the stamp), then apply."""
    profile_capture(e.repo, e.claude, e.agents, e.memory, name, update_stamp=True)
    apply_all(e, name)


def start_machine(e):
    """The shared config applied and no profile yet (bats start_machine)."""
    sync_apply(e.repo, e.claude, e.agents, mirror=False, memory_dir=e.memory)


# ── names ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", ["host-a", "host_b.lab", "A1", "...x", "-"])
def test_valid_name_accepts(name):
    assert profile_valid_name(name)


@pytest.mark.parametrize(
    "name", ["", ".", "..", "host a", "host/a", "host\\a", "host-a\n", "hôst", "a:b"]
)
def test_valid_name_rejects(name):
    assert not profile_valid_name(name)


def test_resolve_name_uses_profile_line_else_hostname(env):
    assert profile_resolve_name(env.repo, "host-a") == "host-a"
    assert profile_resolve_name(env.repo, "host-c") == "host-c"  # no conf at all
    machineconf.write_machine_profile(env.repo, "host-a", "host-b")
    assert profile_resolve_name(env.repo, "host-a") == "host-b"


def test_entries_and_disabled_skip_gitkeep_and_include_dotfiles(env):
    d = env.pa / "agents"
    for n in ("b.md", "a.md", ".hidden.md", ".gitkeep", "x.md.disabled"):
        _write(d / n, b"")
    assert profile_entries(env.pa, "agents") == [".hidden.md", "a.md", "b.md"]
    assert profile_disabled(env.pa, "agents") == ["x.md"]
    assert profile_entries(env.pa, "hooks") == []  # missing kind folder


def test_owned_names_include_the_stamp_and_excludes_are_keyed_by_kind(env):
    _write(env.pa / "agents/helper.md", "host-a helper\n")
    _write(env.pa / "agents/reviewer.md.disabled", b"")
    start_machine(env)
    profile_apply(env.repo, env.claude, env.agents, "host-a")
    (env.pa / "agents/helper.md").unlink()
    _write(env.pa / "skills/only-a/SKILL.md", "x\n")
    # helper.md left the profile but the last apply touched it: still owned.
    assert profile_owned_names(env.repo, env.claude, "host-a", "agents") == [
        "helper.md",
        "reviewer.md",
    ]
    assert profile_excludes(env.repo, env.claude, "host-a") == {
        "agents": {"helper.md", "reviewer.md"},
        "hooks": set(),
        "skills": {"only-a"},
    }


def test_in_use_when_profile_or_stamp_exists(env):
    assert not profile_in_use(env.repo, env.claude, "host-a")
    env.pa.mkdir(parents=True)
    assert profile_in_use(env.repo, env.claude, "host-a")
    env.pa.rmdir()
    profile_stamp_dir(env.claude).mkdir()
    assert profile_in_use(env.repo, env.claude, "host-a")


# ── apply: replace, add, disable ─────────────────────────────────────────


def test_agents_replace_add_and_disable_without_leaking(env):
    start_machine(env)
    _write(env.pa / "agents/helper.md", "host-a helper\n")
    _write(env.pa / "agents/only-a.md", "host-a only\n")
    _write(env.pa / "agents/reviewer.md.disabled", b"")

    simulate_sync(env)

    live = env.claude / "agents"
    assert (live / "helper.md").read_bytes() == b"host-a helper\n"
    assert (live / "only-a.md").read_bytes() == b"host-a only\n"
    assert not (live / "reviewer.md").exists()

    # A second sync captures nothing into the shared copy.
    simulate_sync(env)
    shared = env.repo / "dot-claude/agents"
    assert (shared / "helper.md").read_bytes() == b"shared helper\n"
    assert (shared / "reviewer.md").read_bytes() == b"shared reviewer\n"
    assert not (shared / "only-a.md").exists()
    assert (env.pa / "agents/helper.md").read_bytes() == b"host-a helper\n"


def test_disabled_marker_wins_over_an_entry_of_the_same_name(env):
    start_machine(env)
    _write(env.pa / "agents/helper.md", "host-a helper\n")
    _write(env.pa / "agents/helper.md.disabled", b"")
    profile_apply(env.repo, env.claude, env.agents, "host-a")
    assert not (env.claude / "agents/helper.md").exists()


@pytest.mark.skipif(not POSIX, reason="exec bit is POSIX only")
def test_hooks_replace_and_keep_exec_bit_then_disabled_hooks_go(env):
    start_machine(env)
    _write(env.pa / "hooks/guard.sh", "#!/bin/bash\necho host-a guard\n")
    _write(env.pa / "hooks/extra.sh", "#!/bin/bash\necho host-a extra\n")
    (env.pa / "hooks/guard.sh").chmod(0o755)
    (env.pa / "hooks/extra.sh").chmod(0o755)

    simulate_sync(env)

    hooks = env.claude / "hooks"
    assert _is_exec(hooks / "guard.sh") and _is_exec(hooks / "extra.sh")
    assert b"host-a guard" in (hooks / "guard.sh").read_bytes()
    assert b"shared guard" in (env.repo / "dot-claude/hooks/guard.sh").read_bytes()
    assert not (env.repo / "dot-claude/hooks/extra.sh").exists()

    for n in ("guard.sh", "extra.sh"):
        (env.pa / "hooks" / n).unlink()
    _write(env.pa / "hooks/guard.sh.disabled", b"")
    simulate_sync(env)
    assert not (hooks / "guard.sh").exists()
    assert not (hooks / "extra.sh").exists()
    assert _is_exec(env.repo / "dot-claude/hooks/guard.sh")


def test_skills_replace_the_whole_shared_directory_and_disable(env):
    start_machine(env)
    _write(env.pa / "skills/shared-skill/SKILL.md", "host-a skill\n")
    _write(env.pa / "skills/only-a/SKILL.md", "host-a only skill\n")
    _write(env.pa / "skills/other-skill.disabled", b"")

    simulate_sync(env)

    skills = env.agents / "skills"
    assert (skills / "shared-skill/SKILL.md").read_bytes() == b"host-a skill\n"
    assert not (skills / "shared-skill/notes.md").exists()  # replaced, not merged
    assert (skills / "only-a/SKILL.md").read_bytes() == b"host-a only skill\n"
    assert not (skills / "other-skill").exists()

    simulate_sync(env)
    shared = env.repo / "dot-agents/skills"
    assert (shared / "shared-skill/SKILL.md").read_bytes() == b"shared skill\n"
    assert (shared / "shared-skill/notes.md").is_file()
    assert (shared / "other-skill/SKILL.md").is_file()
    assert not (shared / "only-a").exists()


def test_unapply_removes_only_what_the_last_apply_put_in_place(env):
    start_machine(env)
    _write(env.pa / "agents/only-a.md", "host-a only\n")
    _write(env.pa / "agents/reviewer.md.disabled", b"")
    profile_apply(env.repo, env.claude, env.agents, "host-a")
    _write(env.claude / "agents/mine.md", "untouched\n")

    profile_unapply(env.claude, env.agents)

    live = env.claude / "agents"
    assert not (live / "only-a.md").exists()
    assert (live / "helper.md").is_file()
    assert (live / "mine.md").is_file()
    assert profile_stamp_dir(env.claude).is_dir()  # unapply keeps the record
    profile_unapply(env.claude / "nowhere", env.agents)  # no stamp: no-op


def test_entry_removed_from_profile_reverts_and_stale_copy_is_not_captured(env):
    start_machine(env)
    _write(env.pa / "agents/helper.md", "host-a helper\n")
    _write(env.pa / "agents/only-a.md", "host-a only\n")
    simulate_sync(env)

    (env.pa / "agents/helper.md").unlink()
    (env.pa / "agents/only-a.md").unlink()
    simulate_sync(env)

    assert (env.claude / "agents/helper.md").read_bytes() == b"shared helper\n"
    assert not (env.claude / "agents/only-a.md").exists()
    assert (env.repo / "dot-claude/agents/helper.md").read_bytes() == b"shared helper\n"
    assert not (env.repo / "dot-claude/agents/only-a.md").exists()


def test_apply_without_a_profile_dir_drops_the_stamp(env):
    start_machine(env)
    _write(env.pa / "agents/helper.md", "host-a helper\n")
    profile_apply(env.repo, env.claude, env.agents, "host-a")
    assert profile_stamp_dir(env.claude).is_dir()
    profile_apply(env.repo, env.claude, env.agents, "host-b")
    assert not profile_stamp_dir(env.claude).exists()


# ── stamp ────────────────────────────────────────────────────────────────


def test_stamp_written_via_tmp_then_rename(env, monkeypatch):
    start_machine(env)
    _write(env.pa / "agents/helper.md", "host-a helper\n")
    stamp = profile_stamp_dir(env.claude)
    tmp = stamp.with_name(stamp.name + ".tmp")
    _write(tmp / "agents/leftover.md", "from a killed run\n")
    calls = []
    real = profile._rename

    def spy(src, dst):
        calls.append((str(src), str(dst)))
        assert (src / ".profile-name").read_bytes() == b"host-a\n"
        assert not os.path.lexists(dst)  # the old record went first
        real(src, dst)

    monkeypatch.setattr(profile, "_rename", spy)
    profile_apply(env.repo, env.claude, env.agents, "host-a")

    assert calls == [(str(tmp), str(stamp))]
    assert not tmp.exists()
    assert (stamp / ".profile-name").read_bytes() == b"host-a\n"
    assert (stamp / "agents/helper.md").read_bytes() == b"host-a helper\n"
    assert not (stamp / "agents/leftover.md").exists()


def test_crash_before_the_stamp_rename_reads_as_not_applied(env, monkeypatch):
    start_machine(env)
    _write(env.pa / "agents/helper.md", "host-a helper\n")
    simulate_sync(env)

    def boom(src, dst):
        raise KeyboardInterrupt

    monkeypatch.setattr(profile, "_rename", boom)
    with pytest.raises(KeyboardInterrupt):
        profile_apply(env.repo, env.claude, env.agents, "host-a")
    assert not profile_stamp_dir(env.claude).exists()


# ── CLAUDE.md append block ───────────────────────────────────────────────


def test_claude_md_block_appended_after_marker_and_stripped_before_capture(env):
    start_machine(env)
    _write(env.pa / "CLAUDE.append.md", "host-a only\n")

    simulate_sync(env)

    want = b"# shared instructions\n\n" + MARKER + b"\nhost-a only\n"
    assert (env.claude / "CLAUDE.md").read_bytes() == want
    simulate_sync(env)
    assert (env.repo / "dot-claude/CLAUDE.md").read_bytes() == b"# shared instructions\n"
    assert (env.claude / "CLAUDE.md").read_bytes() == want  # never appended twice

    # A shared edit above the block still flows to the shared file.
    _write(
        env.claude / "CLAUDE.md",
        b"# shared instructions\nmore\n\n" + MARKER + b"\nhost-a only\n",
    )
    simulate_sync(env)
    assert (env.repo / "dot-claude/CLAUDE.md").read_bytes() == b"# shared instructions\nmore\n"
    assert (env.pa / "CLAUDE.append.md").read_bytes() == b"host-a only\n"


def test_claude_md_round_trips_without_trailing_newline(env):
    _write(env.repo / "dot-claude/CLAUDE.md", b"no newline")
    start_machine(env)
    _write(env.pa / "CLAUDE.append.md", b"host-a only")

    simulate_sync(env)
    simulate_sync(env)

    assert (env.repo / "dot-claude/CLAUDE.md").read_bytes() == b"no newline"
    assert (env.pa / "CLAUDE.append.md").read_bytes() == b"host-a only"
    assert (env.claude / "CLAUDE.md").read_bytes() == b"no newline\n" + MARKER + b"\nhost-a only"


def test_claude_md_crlf_preserved(env):
    shared = b"# shared\r\nline two\r\n"
    block = b"host-a only\r\nsecond\r\n"
    _write(env.repo / "dot-claude/CLAUDE.md", shared)
    start_machine(env)
    _write(env.pa / "CLAUDE.append.md", block)

    simulate_sync(env)
    assert (env.claude / "CLAUDE.md").read_bytes() == shared + b"\n" + MARKER + b"\n" + block
    simulate_sync(env)

    assert (env.repo / "dot-claude/CLAUDE.md").read_bytes() == shared
    assert (env.pa / "CLAUDE.append.md").read_bytes() == block
    assert profile_append_block((env.claude / "CLAUDE.md").read_bytes()) == block


def test_marker_only_matches_whole_line():
    for data in (
        b"text " + MARKER + b"\nrest\n",  # text before it
        b"top\n" + MARKER + b" trailing\nrest\n",  # text after it
        b"top\n" + MARKER + b"\r\nrest\n",  # CRLF: not an exact line, as with grep -x
        b"top\n<!-- claude-sync:profile-append --\nrest\n",
    ):
        assert profile_strip_claude_md(data) == data
        assert profile_append_block(data) is None
    # The first exact line wins, wherever it is.
    data = b"top\n" + MARKER + b"\nblock\n" + MARKER + b"\nmore\n"
    assert profile_strip_claude_md(data) == b"top"
    assert profile_append_block(data) == b"block\n" + MARKER + b"\nmore\n"
    assert profile_strip_claude_md(MARKER + b"\nx") == b""
    assert profile_append_block(b"a\n" + MARKER) == b""


# ── capture: local edits, removals ───────────────────────────────────────


def test_restored_machine_first_capture_keeps_unapplied_profile_entry(env):
    # A rebuilt machine: live files hold the shared version and no apply ran.
    _write(env.pa / "agents/helper.md", "host-b helper\n")
    _write(env.claude / "agents/helper.md", "shared helper\n")

    simulate_sync(env)

    assert (env.pa / "agents/helper.md").read_bytes() == b"host-b helper\n"
    assert (env.repo / "dot-claude/agents/helper.md").read_bytes() == b"shared helper\n"
    assert (env.claude / "agents/helper.md").read_bytes() == b"host-b helper\n"


def test_local_edit_goes_back_into_the_profile_not_the_shared_copy(env):
    start_machine(env)
    _write(env.pa / "agents/helper.md", "host-a helper\n")
    _write(env.pa / "CLAUDE.append.md", "host-a only\n")
    simulate_sync(env)

    _write(env.claude / "agents/helper.md", "host-a helper, edited\n")
    _write(env.claude / "CLAUDE.md", b"# shared instructions\n\n" + MARKER + b"\nhost-a edited\n")
    assert profile_has_local_edits(env.repo, env.claude, env.agents, "host-a")
    simulate_sync(env)

    assert (env.pa / "agents/helper.md").read_bytes() == b"host-a helper, edited\n"
    assert (env.repo / "dot-claude/agents/helper.md").read_bytes() == b"shared helper\n"
    assert (env.pa / "CLAUDE.append.md").read_bytes() == b"host-a edited\n"
    assert (env.repo / "dot-claude/CLAUDE.md").read_bytes() == b"# shared instructions\n"
    # The written-back state counts as applied: no churn.
    assert not profile_has_local_edits(env.repo, env.claude, env.agents, "host-a")
    assert profile_live_mismatches(env.repo, env.claude, env.agents, "host-a") == []


def test_capture_without_update_stamp_leaves_the_stamp_alone(env):
    start_machine(env)
    _write(env.pa / "skills/shared-skill/SKILL.md", "host-a skill\n")
    simulate_sync(env)
    _write(env.agents / "skills/shared-skill/SKILL.md", "edited\n")

    profile_capture(env.repo, env.claude, env.agents, env.memory, "host-a")

    assert (env.pa / "skills/shared-skill/SKILL.md").read_bytes() == b"edited\n"
    stamp = profile_stamp_dir(env.claude)
    assert (stamp / "skills/shared-skill/SKILL.md").read_bytes() == b"host-a skill\n"
    assert profile_has_local_edits(env.repo, env.claude, env.agents, "host-a")


@pytest.mark.skipif(not POSIX, reason="exec bit is POSIX only")
def test_an_exec_bit_change_counts_as_a_local_edit(env):
    start_machine(env)
    _write(env.pa / "hooks/extra.sh", "#!/bin/bash\n")
    (env.pa / "hooks/extra.sh").chmod(0o755)
    simulate_sync(env)
    (env.claude / "hooks/extra.sh").chmod(0o644)
    assert profile_has_local_edits(env.repo, env.claude, env.agents, "host-a")


def test_disabled_shared_entry_left_alone_in_repo_though_missing_locally(env):
    start_machine(env)
    _write(env.pa / "agents/reviewer.md.disabled", b"")
    simulate_sync(env)
    assert not (env.claude / "agents/reviewer.md").exists()
    assert (env.repo / "dot-claude/agents/reviewer.md").is_file()

    (env.pa / "agents/reviewer.md.disabled").unlink()
    simulate_sync(env)
    assert (env.claude / "agents/reviewer.md").read_bytes() == b"shared reviewer\n"
    assert (env.repo / "dot-claude/agents/reviewer.md").read_bytes() == b"shared reviewer\n"


def test_another_profiles_stamp_is_no_record_of_this_one(env):
    start_machine(env)
    _write(env.pa / "agents/helper.md", "host-a helper\n")
    _write(env.repo / "machines/host-b/agents/helper.md", "host-b helper\n")
    apply_all(env, "host-b")
    _write(env.claude / "agents/helper.md", "edited\n")
    assert not profile_has_local_edits(env.repo, env.claude, env.agents, "host-a")
    profile_capture(env.repo, env.claude, env.agents, env.memory, "host-a", update_stamp=True)
    assert (env.pa / "agents/helper.md").read_bytes() == b"host-a helper\n"


def test_follower_edits_go_to_the_profile_it_follows(env):
    _write(env.pa / "agents/helper.md", "host-a helper\n")
    machineconf.write_machine_conf(env.repo, "host-b", "/home/user/project")
    machineconf.write_machine_profile(env.repo, "host-b", "host-a")
    name = profile_resolve_name(env.repo, "host-b")
    start_machine(env)
    simulate_sync(env, name)
    _write(env.claude / "agents/helper.md", "edited on host-b\n")
    simulate_sync(env, name)
    assert (env.pa / "agents/helper.md").read_bytes() == b"edited on host-b\n"
    assert not (env.repo / "machines/host-b").exists()


def test_crash_after_unapply_reads_as_not_applied(env):
    start_machine(env)
    _write(env.pa / "agents/helper.md", "host-a helper\n")
    _write(env.pa / "agents/only-a.md", "host-a only\n")
    simulate_sync(env)
    profile_unapply(env.claude, env.agents)  # then the run dies

    profile_capture(env.repo, env.claude, env.agents, env.memory, "host-a", update_stamp=True)

    assert (env.pa / "agents/helper.md").read_bytes() == b"host-a helper\n"
    assert (env.pa / "agents/only-a.md").read_bytes() == b"host-a only\n"
    assert (env.repo / "dot-claude/agents/helper.md").read_bytes() == b"shared helper\n"
    assert not profile_has_local_edits(env.repo, env.claude, env.agents, "host-a")
    apply_all(env)
    assert (env.claude / "agents/helper.md").read_bytes() == b"host-a helper\n"


def test_crash_mid_apply_leaves_the_profile_alone_on_next_capture(env, monkeypatch):
    start_machine(env)
    _write(env.pa / "agents/helper.md", "host-a helper\n")
    simulate_sync(env)
    _write(env.pa / "agents/helper.md", "host-a helper v2\n")  # pulled, not applied
    monkeypatch.setattr(profile, "_rename", lambda s, d: (_ for _ in ()).throw(OSError("killed")))
    with pytest.raises(Fail) as info:
        apply_all(env)
    assert "killed" in info.value.message and info.value.hint
    monkeypatch.undo()
    _write(env.claude / "agents/helper.md", "stale\n")

    profile_capture(env.repo, env.claude, env.agents, env.memory, "host-a", update_stamp=True)

    assert (env.pa / "agents/helper.md").read_bytes() == b"host-a helper v2\n"


# ── checks: mismatches, dangling hooks, describe ─────────────────────────


def test_live_mismatches_report_unapplied_entries_and_the_block(env):
    start_machine(env)
    _write(env.pa / "agents/helper.md", "host-a helper\n")
    _write(env.pa / "agents/reviewer.md.disabled", b"")
    _write(env.pa / "CLAUDE.append.md", "host-a only\n")
    simulate_sync(env)
    assert profile_live_mismatches(env.repo, env.claude, env.agents, "host-a") == []
    home_before = sorted(p.name for p in env.claude.iterdir())

    _write(env.pa / "agents/helper.md", "host-a helper v2\n")
    _write(env.claude / "agents/reviewer.md", "back\n")
    _write(env.pa / "CLAUDE.append.md", "host-a v2\n")
    assert profile_live_mismatches(env.repo, env.claude, env.agents, "host-a") == [
        "agents/helper.md",
        "agents/reviewer.md",
        "CLAUDE.md append block",
    ]
    assert sorted(p.name for p in env.claude.iterdir()) == home_before

    apply_all(env)
    assert profile_live_mismatches(env.repo, env.claude, env.agents, "host-a") == []


def test_live_mismatches_report_a_block_that_should_not_be_there(env):
    start_machine(env)
    _write(env.pa / "agents/helper.md", "host-a helper\n")
    simulate_sync(env)
    _write(env.claude / "CLAUDE.md", b"# shared instructions\n\n" + MARKER + b"\nleft over\n")
    assert profile_live_mismatches(env.repo, env.claude, env.agents, "host-a") == [
        "CLAUDE.md append block"
    ]


def test_dangling_hook_refs(env):
    settings = env.claude / "settings.json"
    assert profile_dangling_hook_refs(env.claude, settings) == []  # no file
    _write(settings, "{not json")
    assert profile_dangling_hook_refs(env.claude, settings) == []
    _write(env.claude / "hooks/present.sh", "#!/bin/bash\n")
    _write(
        settings,
        """{"theme":"dark","statusLine":{"command":"~/.claude/hooks/not-a-hook.sh"},
        "hooks":{"PreToolUse":[{"matcher":"Bash","hooks":[
          {"type":"command","command":"\\"$HOME/.claude/hooks/guard.sh\\""},
          {"type":"command","command":"${HOME}/.claude/hooks/present.sh && ~/.claude/hooks/b.py"},
          {"type":"command","command":"/home/user/.claude/hooks/guard.sh"},
          {"type":"command","command":42}]}]}}""",
    )
    hooks = env.claude / "hooks"
    expected = [str(hooks / "b.py"), str(hooks / "guard.sh")]
    if profile.IS_WINDOWS:
        # By design: .sh hook references are skipped on Windows.
        expected = [str(hooks / "b.py")]
    assert profile_dangling_hook_refs(env.claude, settings) == expected


def test_describe_lists_entries_markers_append_and_override(env):
    _write(env.pa / "agents/helper.md", "x\n")
    _write(env.pa / "agents/reviewer.md.disabled", b"")
    _write(env.pa / "hooks/extra.sh", "x\n")
    _write(env.pa / "CLAUDE.append.md", "x\n")
    _write(env.repo / "machines/host-a.settings.json", "{}\n")
    assert profile_describe(env.repo, "host-a") == (
        "  agents/helper.md  (replaces the shared one)\n"
        "  agents/reviewer.md  (disabled)\n"
        "  hooks/extra.sh  (machine-only)\n"
        "  CLAUDE.append.md  (appended to CLAUDE.md)\n"
        "  host-a.settings.json  (settings override; see 'override list')\n"
    )
    assert profile_describe(env.repo, "host-b") == (
        "  (nothing: no machines/host-b/ entries and no settings override)\n"
    )


# ── Windows: Linux-only hook entries ─────────────────────────────────────


def test_linux_only_entry_skipped_on_windows(env, monkeypatch):
    monkeypatch.setattr(profile, "IS_WINDOWS", True)
    start_machine(env)
    _write(env.pa / "hooks/guard.sh", "#!/bin/bash\necho host-a guard\n")
    _write(env.pa / "hooks/only-a.sh", "#!/bin/bash\necho host-a only\n")
    _write(env.pa / "hooks/tool.py", "print('host-a')\n")
    _write(env.claude / "settings.json", '{"hooks":{"Stop":[{"hooks":[{"command":"~/.claude/hooks/only-a.sh"}]}]}}')
    pdir_before = {p: p.read_bytes() for p in env.pa.rglob("*") if p.is_file()}

    simulate_sync(env)
    simulate_sync(env)

    hooks = env.claude / "hooks"
    # .sh entries are not applied: the shared copy stays, the machine-only one never lands.
    assert b"shared guard" in (hooks / "guard.sh").read_bytes()
    assert not (hooks / "only-a.sh").exists()
    assert (hooks / "tool.py").read_bytes() == b"print('host-a')\n"
    # Still owned: never captured into the shared copy, profile bytes untouched.
    assert not (env.repo / "dot-claude/hooks/only-a.sh").exists()
    assert b"shared guard" in (env.repo / "dot-claude/hooks/guard.sh").read_bytes()
    assert {p: p.read_bytes() for p in env.pa.rglob("*") if p.is_file()} == pdir_before
    stamp = profile_stamp_dir(env.claude)
    assert not (stamp / "hooks/guard.sh").exists()
    assert (stamp / "hooks/tool.py").is_file()
    assert not profile_has_local_edits(env.repo, env.claude, env.agents, "host-a")
    assert profile_live_mismatches(env.repo, env.claude, env.agents, "host-a") == []
    assert profile_dangling_hook_refs(env.claude, env.claude / "settings.json") == []

    # The same profile on Linux applies them.
    monkeypatch.setattr(profile, "IS_WINDOWS", False)
    apply_all(env)
    assert b"host-a guard" in (hooks / "guard.sh").read_bytes()
    assert (hooks / "only-a.sh").is_file()


# ── security: names that escape machines/, links out of the live home ────


@pytest.mark.parametrize("bad", ["../x", "..", ".", "a/b", "a\\b", ""])
def test_profile_name_that_escapes_machines_is_refused(env, bad):
    with pytest.raises(Fail) as info:
        profile.profile_dir(env.repo, bad)
    assert info.value.hint
    start_machine(env)
    with pytest.raises(Fail):
        profile_apply(env.repo, env.claude, env.agents, bad)
    with pytest.raises(Fail):
        profile_capture(env.repo, env.claude, env.agents, env.memory, bad)
    assert not (env.repo / "x").exists()


def test_resolve_name_refuses_a_bad_profile_line(env):
    machineconf.write_machine_profile(env.repo, "host-a", "../../outside")
    with pytest.raises(Fail) as info:
        profile_resolve_name(env.repo, "host-a")
    assert "outside" in info.value.message and info.value.hint


def test_profile_dir_must_stay_under_machines(env, tmp_path):
    # machines/ itself is a valid name's parent; a link there must not lead out.
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "host-a").mkdir(parents=True)
    _write(env.repo / "machines/.gitkeep", b"")
    (env.repo / "machines/host-a").symlink_to(elsewhere / "host-a")
    with pytest.raises(Fail):
        profile.profile_dir(env.repo, "host-a")


@pytest.mark.skipif(not POSIX, reason="symlinks need POSIX in tests")
def test_writeback_copies_a_file_link_inside_the_home_as_bytes(env):
    start_machine(env)
    _write(env.pa / "skills/shared-skill/SKILL.md", "host-a skill\n")
    simulate_sync(env)
    live_skill = env.agents / "skills/shared-skill"
    _write(live_skill / "SKILL.md", "edited\n")
    _write(env.agents / "notes.md", "inside the live home\n")
    (live_skill / "inside.md").symlink_to(env.agents / "notes.md")

    profile_capture(env.repo, env.claude, env.agents, env.memory, "host-a", update_stamp=True)

    pskill = env.pa / "skills/shared-skill"
    assert (pskill / "SKILL.md").read_bytes() == b"edited\n"
    assert (pskill / "inside.md").read_bytes() == b"inside the live home\n"
    assert not (pskill / "inside.md").is_symlink()


@pytest.mark.skipif(not POSIX, reason="symlinks need POSIX in tests")
@pytest.mark.parametrize("bad", ["leak.txt", "leak-dir", "dangling", "loop"])
def test_writeback_skips_the_whole_entry_when_a_nested_link_is_refused(env, tmp_path, capsys, bad):
    secret = tmp_path / "secret.txt"
    _write(secret, "private\n")
    _write(tmp_path / "outside-dir/key", "private\n")
    start_machine(env)
    _write(env.pa / "skills/shared-skill/SKILL.md", "host-a skill\n")
    _write(env.pa / "skills/shared-skill/extra.md", "profile only\n")
    simulate_sync(env)

    live_skill = env.agents / "skills/shared-skill"
    _write(live_skill / "SKILL.md", "edited\n")
    target = {
        "leak.txt": secret,
        "leak-dir": tmp_path / "outside-dir",
        "dangling": tmp_path / "missing",
        "loop": live_skill,  # a cycle: must not recurse
    }[bad]
    (live_skill / bad).symlink_to(target)
    capsys.readouterr()

    assert not profile_has_local_edits(env.repo, env.claude, env.agents, "host-a")
    assert profile_live_mismatches(env.repo, env.claude, env.agents, "host-a") == [
        "skills/shared-skill"
    ]
    profile_capture(env.repo, env.claude, env.agents, env.memory, "host-a", update_stamp=True)

    # The profile keeps its own copy, children included.
    pskill = env.pa / "skills/shared-skill"
    assert (pskill / "SKILL.md").read_bytes() == b"host-a skill\n"
    assert (pskill / "extra.md").read_bytes() == b"profile only\n"
    assert not os.path.lexists(pskill / bad)
    assert bad in capsys.readouterr().err
    assert not any(p.is_symlink() for p in env.repo.rglob("*"))
    assert b"private" not in b"".join(p.read_bytes() for p in env.repo.rglob("*") if p.is_file())


@pytest.mark.skipif(not POSIX, reason="symlinks need POSIX in tests")
def test_a_top_level_link_out_of_the_home_is_never_read(env, tmp_path, capsys):
    secret = tmp_path / "secret.txt"
    _write(secret, "private\n")
    start_machine(env)
    _write(env.pa / "agents/helper.md", "host-a helper\n")
    simulate_sync(env)
    (env.claude / "agents/helper.md").unlink()
    (env.claude / "agents/helper.md").symlink_to(secret)
    capsys.readouterr()

    assert not profile_has_local_edits(env.repo, env.claude, env.agents, "host-a")
    assert profile_live_mismatches(env.repo, env.claude, env.agents, "host-a") == [
        "agents/helper.md"
    ]
    profile_capture(env.repo, env.claude, env.agents, env.memory, "host-a", update_stamp=True)
    assert (env.pa / "agents/helper.md").read_bytes() == b"host-a helper\n"
    assert "helper.md" in capsys.readouterr().err


@pytest.mark.skipif(not POSIX, reason="symlinks need POSIX in tests")
def test_same_never_follows_folder_links(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    _write(a / "sub/f", "x\n")
    _write(tmp_path / "real/f", "x\n")
    b.mkdir()
    (b / "sub").symlink_to(tmp_path / "real")
    assert not profile._same(a, b)
    (tmp_path / "loop").mkdir()
    (tmp_path / "loop/me").symlink_to(tmp_path / "loop")
    assert not profile._same(tmp_path / "loop", tmp_path / "loop")


@pytest.mark.skipif(not POSIX, reason="symlinks need POSIX in tests")
def test_a_profile_folder_that_is_a_link_is_refused_before_anything_changes(env):
    start_machine(env)
    _write(env.repo / "machines/host-b/agents/helper.md", "host-b helper\n")
    _write(env.pa / "agents/helper.md", "host-a helper\n")
    apply_all(env)
    stamp_before = (profile_stamp_dir(env.claude) / "agents/helper.md").read_bytes()
    _write(env.repo / "machines/host-c/agents/helper.md", "host-c helper\n")
    (env.repo / "machines/host-link").symlink_to(env.repo / "machines/host-c")

    with pytest.raises(Fail) as info:
        profile_apply(env.repo, env.claude, env.agents, "host-link")
    assert info.value.hint
    assert (env.claude / "agents/helper.md").read_bytes() == b"host-a helper\n"
    assert (profile_stamp_dir(env.claude) / "agents/helper.md").read_bytes() == stamp_before


@pytest.mark.skipif(not POSIX, reason="symlinks need POSIX in tests")
def test_a_link_inside_the_profile_is_refused_before_anything_changes(env, tmp_path):
    _write(tmp_path / "outside.md", "outside the repo\n")
    start_machine(env)
    _write(env.pa / "agents/helper.md", "host-a helper\n")
    apply_all(env)
    _write(env.pa / "agents/helper.md", "host-a helper v2\n")
    _write(env.pa / "skills/only-a/SKILL.md", "x\n")
    (env.pa / "skills/only-a/ref.md").symlink_to(tmp_path / "outside.md")

    with pytest.raises(Fail) as info:
        profile_apply(env.repo, env.claude, env.agents, "host-a")
    assert "ref.md" in info.value.message and info.value.hint
    assert (env.claude / "agents/helper.md").read_bytes() == b"host-a helper\n"
    assert not (env.agents / "skills/only-a").exists()
    assert profile_stamp_dir(env.claude).is_dir()


@pytest.mark.skipif(not POSIX, reason="modes are POSIX only")
def test_folder_mode_bits_are_kept(env):
    start_machine(env)
    _write(env.pa / "skills/only-a/SKILL.md", "x\n")
    (env.pa / "skills/only-a").chmod(0o700)
    profile_apply(env.repo, env.claude, env.agents, "host-a")
    assert stat.S_IMODE((env.agents / "skills/only-a").stat().st_mode) == 0o700


def test_errors_carry_a_hint_for_their_operation(env, monkeypatch):
    start_machine(env)
    env.pa.mkdir(parents=True)

    def boom(*_a, **_k):
        raise PermissionError("denied")

    monkeypatch.setattr(profile, "sync_capture", boom)
    with pytest.raises(Fail) as info:
        profile_capture(env.repo, env.claude, env.agents, env.memory, "host-a")
    assert "capture" in info.value.message and "re-applied" not in info.value.hint
    monkeypatch.setattr(profile, "_remove", boom)
    profile_stamp_dir(env.claude).mkdir()
    _write(profile_stamp_dir(env.claude) / "agents/x.md", b"")
    with pytest.raises(Fail) as info:
        profile_unapply(env.claude, env.agents)
    assert "re-applied" not in info.value.hint


@pytest.mark.skipif(os.name == "nt", reason="no executable bit or cheap symlinks on Windows")
def test_hook_scripts_are_executable_regular_files_sorted(tmp_path):
    pdir = tmp_path / "machines" / "host-a"
    hooks = pdir / "hooks"
    (hooks / "sub").mkdir(parents=True)
    for rel, mode in (("b.sh", 0o755), ("a.sh", 0o755), ("sub/c.sh", 0o700), ("notes.md", 0o644)):
        (hooks / rel).write_bytes(b"#!/bin/sh\n")
        (hooks / rel).chmod(mode)
    (hooks / "link.sh").symlink_to(hooks / "a.sh")
    assert profile.profile_hook_scripts(pdir) == [hooks / "a.sh", hooks / "b.sh", hooks / "sub" / "c.sh"]
    assert profile.profile_hook_scripts(tmp_path / "none") == []


def test_hook_scripts_on_windows_are_every_regular_file(tmp_path, monkeypatch):
    monkeypatch.setattr(profile, "IS_WINDOWS", True)
    pdir = tmp_path / "p"
    (pdir / "hooks").mkdir(parents=True)
    (pdir / "hooks" / "x.ps1").write_bytes(b"")
    (pdir / "hooks" / "x.ps1").chmod(0o644)
    assert profile.profile_hook_scripts(pdir) == [pdir / "hooks" / "x.ps1"]


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_profile_dir_refuses_a_machines_folder_that_is_a_link(tmp_path):
    outside = tmp_path / "outside"
    (outside / "host-a").mkdir(parents=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "machines").symlink_to(outside)
    with pytest.raises(Fail, match="is a link or leads outside"):
        profile.profile_dir(repo, "host-a")
    with pytest.raises(Fail, match="is a link or leads outside"):
        profile.profile_dir(repo, "host-new")  # missing profile folder too


@pytest.mark.skipif(not POSIX, reason="symlinks need POSIX in tests")
def test_capture_refuses_a_linked_profile_folder_before_writing_back(env, tmp_path):
    start_machine(env)
    _write(env.pa / "agents/helper.md", "host-a helper\n")
    apply_all(env)
    outside = tmp_path / "outside-agents"
    _write(outside / "helper.md", "outside\n")
    shutil.rmtree(env.pa / "agents")
    (env.pa / "agents").symlink_to(outside)
    _write(env.claude / "agents/helper.md", "host-a helper, edited\n")
    _write(env.claude / "agents/new.md", "new shared agent\n")

    with pytest.raises(Fail) as info:
        profile_capture(env.repo, env.claude, env.agents, env.memory, "host-a", update_stamp=True)
    assert "agents" in info.value.message and info.value.hint
    assert (outside / "helper.md").read_bytes() == b"outside\n"
    assert not (env.repo / "dot-claude/agents/new.md").exists()  # nothing captured
