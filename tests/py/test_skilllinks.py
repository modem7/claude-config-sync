"""Port of tests/skills_symlink.bats plus real-directory, dangling and junction cases."""

from __future__ import annotations

import os

import pytest

from claude_sync.skilllinks import (
    make_link,
    read_link,
    remove_skill_links,
    tracked_skills,
    wire_skill_links,
)

posix_only = pytest.mark.skipif(os.name == "nt", reason="needs POSIX symlinks")


def wire(claude, agents):
    """wire_skill_links with every folder under <agents>/skills tracked."""
    skills = agents / "skills"
    tracked = sorted(e.name for e in skills.iterdir() if e.is_dir()) if skills.is_dir() else []
    wire_skill_links(claude, agents, tracked, [])


def test_tracked_and_disabled_are_required(homes):
    claude, agents = homes
    with pytest.raises(TypeError):
        wire_skill_links(claude, agents)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        wire_skill_links(claude, agents, ["release"])  # type: ignore[call-arg]


@pytest.fixture
def homes(fake_home):
    (fake_home.agents / "skills" / "release").mkdir(parents=True)
    (fake_home.claude / "skills").mkdir(parents=True)
    return fake_home.claude, fake_home.agents


@posix_only
def test_creates_link_for_tracked_skill(homes):
    claude, agents = homes
    wire(claude, agents)
    link = claude / "skills" / "release"
    assert os.path.islink(link)
    assert read_link(link) == agents / "skills" / "release"


@posix_only
def test_real_directory_is_never_replaced(homes):
    claude, agents = homes
    (claude / "skills" / "release").mkdir()
    (claude / "skills" / "release" / "marker.txt").write_text("builtin", encoding="utf-8")
    wire(claude, agents)
    link = claude / "skills" / "release"
    assert not os.path.islink(link)
    assert (link / "marker.txt").read_text(encoding="utf-8") == "builtin"
    remove_skill_links(claude, agents)
    assert (link / "marker.txt").is_file()


@posix_only
def test_idempotent(homes):
    claude, agents = homes
    wire(claude, agents)
    wire(claude, agents)
    assert read_link(claude / "skills" / "release") == agents / "skills" / "release"


@posix_only
def test_dangling_link_under_agents_skills_removed(homes):
    claude, agents = homes
    os.symlink(agents / "skills" / "gone", claude / "skills" / "gone")
    wire(claude, agents)
    assert not os.path.lexists(claude / "skills" / "gone")
    assert os.path.islink(claude / "skills" / "release")


@posix_only
def test_keeps_live_untracked_link(homes):
    claude, agents = homes
    (agents / "skills" / "local-only").mkdir()
    os.symlink(agents / "skills" / "local-only", claude / "skills" / "local-only")
    wire(claude, agents)
    assert os.path.islink(claude / "skills" / "local-only")


@posix_only
def test_leaves_dangling_link_that_points_elsewhere(homes, tmp_path):
    claude, agents = homes
    os.symlink(tmp_path / "nowhere", claude / "skills" / "foreign")
    wire(claude, agents)
    assert os.path.islink(claude / "skills" / "foreign")


@posix_only
def test_disabled_skill_absent_from_agents_loses_its_link(homes):
    claude, agents = homes
    wire(claude, agents)
    (agents / "skills" / "machine-only").mkdir()
    os.rmdir(agents / "skills" / "release")  # mirror dropped the disabled skill
    wire(claude, agents)
    assert os.path.islink(claude / "skills" / "machine-only")
    assert not os.path.lexists(claude / "skills" / "release")


@posix_only
def test_stale_link_to_other_target_is_replaced(homes, tmp_path):
    claude, agents = homes
    os.symlink(tmp_path, claude / "skills" / "release")
    wire(claude, agents)
    assert read_link(claude / "skills" / "release") == agents / "skills" / "release"


@posix_only
def test_remove_skill_links_only_removes_links_into_agents(homes, tmp_path):
    claude, agents = homes
    wire(claude, agents)
    other = tmp_path / "other"
    other.mkdir()
    os.symlink(other, claude / "skills" / "foreign")
    remove_skill_links(claude, agents)
    assert not os.path.lexists(claude / "skills" / "release")
    assert os.path.islink(claude / "skills" / "foreign")


def test_read_link_of_non_link_is_none(tmp_path):
    assert read_link(tmp_path) is None
    assert read_link(tmp_path / "missing") is None


@pytest.mark.skipif(os.name != "nt", reason="Windows junctions")
def test_junction_created_on_windows(homes):
    claude, agents = homes
    wire(claude, agents)
    link = claude / "skills" / "release"
    assert os.path.isjunction(link)
    assert read_link(link) == agents / "skills" / "release"
    remove_skill_links(claude, agents)
    assert not os.path.lexists(link)
    assert (agents / "skills" / "release").is_dir()


def test_make_link_creates_link(tmp_path):
    target = tmp_path / "t"
    target.mkdir()
    make_link(target, tmp_path / "l")
    assert read_link(tmp_path / "l") == target


# ── tracked set from the repo and profile (bash wire_skill_symlinks) ──────


@pytest.fixture
def repo_homes(tmp_path, homes):
    claude, agents = homes
    repo = tmp_path / "trepo"
    (repo / "dot-agents" / "skills" / "release").mkdir(parents=True)
    (repo / "dot-agents" / "skills" / "shared").mkdir()
    (repo / "dot-agents" / "skills" / ".gitkeep").write_bytes(b"")
    pdir = repo / "machines" / "host-a"
    (pdir / "skills" / "mine").mkdir(parents=True)
    (pdir / "skills" / "shared.disabled").write_bytes(b"")
    for name in ("shared", "mine"):
        (agents / "skills" / name).mkdir()
    return repo, pdir, claude, agents


def test_tracked_skills_come_from_the_repo_and_profile_minus_disabled(repo_homes):
    repo, pdir, _claude, _agents = repo_homes
    assert tracked_skills(repo, pdir) == (["mine", "release"], ["shared"])
    assert tracked_skills(repo) == (["release", "shared"], [])
    assert tracked_skills(repo, repo / "machines" / "no-profile") == (["release", "shared"], [])


@posix_only
def test_untracked_folder_in_agents_gets_no_link(repo_homes):
    repo, pdir, claude, agents = repo_homes
    (agents / "skills" / "stray").mkdir()
    wire_skill_links(claude, agents, *tracked_skills(repo, pdir))
    assert not os.path.lexists(claude / "skills" / "stray")
    assert os.path.islink(claude / "skills" / "release")
    assert os.path.islink(claude / "skills" / "mine")


@posix_only
def test_disabled_skill_still_on_disk_loses_its_link(repo_homes):
    repo, pdir, claude, agents = repo_homes
    make_link(agents / "skills" / "shared", claude / "skills" / "shared")
    assert (agents / "skills" / "shared").is_dir()
    wire_skill_links(claude, agents, *tracked_skills(repo, pdir))
    assert not os.path.lexists(claude / "skills" / "shared")
    assert (agents / "skills" / "shared").is_dir()


@posix_only
def test_disabled_name_keeps_a_link_that_points_elsewhere_and_a_real_folder(repo_homes, tmp_path):
    repo, pdir, claude, agents = repo_homes
    os.symlink(tmp_path, claude / "skills" / "shared")
    wire_skill_links(claude, agents, *tracked_skills(repo, pdir))
    assert read_link(claude / "skills" / "shared") == tmp_path
    os.unlink(claude / "skills" / "shared")
    (claude / "skills" / "shared").mkdir()
    wire_skill_links(claude, agents, *tracked_skills(repo, pdir))
    assert (claude / "skills" / "shared").is_dir() and not os.path.islink(claude / "skills" / "shared")


@posix_only
def test_dangling_links_into_agents_are_still_removed_with_a_tracked_set(repo_homes):
    repo, pdir, claude, agents = repo_homes
    os.symlink(agents / "skills" / "gone", claude / "skills" / "gone")
    wire_skill_links(claude, agents, *tracked_skills(repo, pdir))
    assert not os.path.lexists(claude / "skills" / "gone")
