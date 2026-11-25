"""Tests for `machine NAME ...` (claude_sync.machinecmd): editing another
machine's tier, profile and settings override from this machine, and a
throwaway cloud session picking it up through `remote`.
Placeholder hostnames only: this file is published.
"""

from __future__ import annotations

import json

from helpers import git, has_ref

SHARED = {"enabledPlugins": {"p@m": True, "q@m": True}}


def _origin_file(world, path):
    return json.loads(git("-C", world.origin, "show", f"HEAD:{path}").stdout)


def _shared_world(world):
    a = world.machine("a")
    (a.claude / "settings.json").write_text(json.dumps(SHARED))
    assert world.cli_as(a, "sync")[0] == 0
    return a


def test_plugin_disable_reaches_a_cloud_session_through_remote(world):
    a = _shared_world(world)
    rc, out = world.cli_as(a, "machine", "cloud", "plugin", "disable", "p@m")
    assert rc == 0, out
    assert _origin_file(world, "machines/cloud.settings.json") == {"enabledPlugins": {"p@m": False}}
    # this machine keeps its own plugins
    assert json.loads((a.claude / "settings.json").read_text())["enabledPlugins"] == SHARED["enabledPlugins"]
    c = world.machine("c", pull=False)
    rc, out = world.cli(c, "remote", env={**{k: v for k, v in c.env().items() if k != "CLAUDE_SYNC_HOSTNAME"}, "CLAUDE_SYNC_REMOTE": "1"})
    assert rc == 0, out
    assert json.loads((c.claude / "settings.json").read_text())["enabledPlugins"] == {"p@m": False, "q@m": True}


def test_a_named_cloud_machine_has_its_own_override(world):
    a = _shared_world(world)
    assert world.cli_as(a, "machine", "cloud-rust", "plugin", "disable", "q@m")[0] == 0
    c = world.machine("c", pull=False)
    env = {**c.env(), "CLAUDE_SYNC_REMOTE": "1", "CLAUDE_SYNC_HOSTNAME": "cloud-rust"}
    assert world.cli(c, "remote", env=env)[0] == 0
    assert json.loads((c.claude / "settings.json").read_text())["enabledPlugins"] == {"p@m": True, "q@m": False}
    other = world.machine("d", pull=False)
    assert world.cli(other, "remote", env={**other.env(), "CLAUDE_SYNC_REMOTE": "1", "CLAUDE_SYNC_HOSTNAME": "cloud"})[0] == 0
    assert json.loads((other.claude / "settings.json").read_text())["enabledPlugins"] == SHARED["enabledPlugins"]


def test_the_same_change_twice_changes_nothing_the_second_time(world):
    a = _shared_world(world)
    assert world.cli_as(a, "machine", "cloud", "plugin", "disable", "p@m")[0] == 0
    before = git("-C", world.origin, "rev-parse", "HEAD").stdout
    rc, out = world.cli_as(a, "machine", "cloud", "plugin", "disable", "p@m")
    assert rc == 0, out
    assert git("-C", world.origin, "rev-parse", "HEAD").stdout == before
    assert not has_ref(world.origin, "refs/heads/sync/host-a")


def test_inherit_drops_the_override_and_is_a_no_op_without_one(world):
    a = _shared_world(world)
    rc, out = world.cli_as(a, "machine", "cloud", "plugin", "inherit", "p@m")
    assert rc == 0 and "Nothing was changed" in out
    assert world.cli_as(a, "machine", "cloud", "plugin", "enable", "p@m")[0] == 0
    assert world.cli_as(a, "machine", "cloud", "plugin", "inherit", "p@m")[0] == 0
    show = world.cli(a, "machine", "cloud", "show")[1]
    assert "No overrides for cloud." in show


def test_override_set_and_unset_take_any_settings_key(world):
    a = _shared_world(world)
    assert world.cli_as(a, "machine", "cloud", "override", "set", ".model", '"haiku"')[0] == 0
    assert _origin_file(world, "machines/cloud.settings.json") == {"model": "haiku"}
    assert world.cli_as(a, "machine", "cloud", "override", "unset", ".model")[0] == 0
    assert not json.loads(git("-C", world.origin, "show", "HEAD:machines/cloud.settings.json", check=False).stdout or "{}")


def test_override_set_refuses_a_value_that_is_not_json(world):
    a = _shared_world(world)
    rc, out = world.cli(a, "machine", "cloud", "override", "set", ".model", "haiku")
    assert rc == 1 and "not JSON" in out


def test_tier_and_profile_are_set_in_the_machines_conf(world):
    a = _shared_world(world)
    assert world.cli_as(a, "machine", "cloud", "tier", "basic")[0] == 0
    assert "TIER=basic" in git("-C", world.origin, "show", "HEAD:machines/cloud.conf").stdout
    rc, out = world.cli(a, "machine", "cloud", "tier", "full")
    assert rc == 1 and "names no project path" in out
    rc, out = world.cli(a, "machine", "cloud", "profile", "use", "no-such-profile")
    assert rc == 1 and "No profile called" in out


def test_show_is_read_only_and_works_on_a_feature_branch(world):
    a = _shared_world(world)
    git("-C", a.repo, "checkout", "-q", "-b", "some-feature")
    rc, out = world.cli(a, "machine", "cloud", "show")
    assert rc == 0 and "SKIPPED" not in out
    assert "no machines/cloud.conf yet" in out and "tier: full" in out


def test_bad_names_and_ids_are_refused_before_anything_changes(world):
    a = _shared_world(world)
    assert world.cli(a, "machine", "../x", "show")[0] == 1
    assert world.cli(a, "machine", "cloud", "plugin", "enable", 'bad"id')[0] == 1
    assert world.cli(a, "machine", "cloud", "bogus")[0] == 1


def test_a_cloud_session_may_look_but_never_change(world):
    a = _shared_world(world)
    env = {**a.env(), "CLAUDE_CODE_REMOTE": "true"}
    assert world.cli(a, "machine", "cloud", "show", env=env)[0] == 0
    for argv in (("machine", "cloud", "plugin", "disable", "p@m"), ("override", "list-x"), ("tier", "basic"), ("profile", "use", "x")):
        rc, out = world.cli(a, *argv, env=env)
        assert rc == 1 and "throwaway cloud session" in out, argv
    assert not has_ref(world.origin, "refs/heads/sync/host-a")


def _profile_world(world):
    a = _shared_world(world)
    prof = a.repo / "machines" / "work" / "agents"
    prof.mkdir(parents=True)
    (prof / "w.md").write_text("# work agent\n")
    assert world.cli_as(a, "sync")[0] == 0
    return a


def test_profile_use_on_a_new_cloud_machine_keeps_it_on_the_basic_tier(world):
    a = _profile_world(world)
    assert world.cli_as(a, "machine", "cloud", "profile", "use", "work")[0] == 0
    conf = git("-C", world.origin, "show", "HEAD:machines/cloud.conf").stdout
    assert "TIER=basic" in conf and "PROFILE=work" in conf
    c = world.machine("c", pull=False)
    rc, out = world.cli(c, "remote", env={**c.env(), "CLAUDE_SYNC_REMOTE": "1", "CLAUDE_SYNC_HOSTNAME": "cloud"})
    assert rc == 0, out
    assert (c.claude / "agents" / "w.md").is_file()


def test_a_shared_profile_is_not_edited_through_one_machines_name(world):
    a = _profile_world(world)
    assert world.cli_as(a, "profile", "use", "work")[0] == 0
    assert world.cli_as(a, "machine", "cloud", "profile", "use", "work")[0] == 0
    before = (a.claude / "settings.json").read_text()
    rc, out = world.cli(a, "machine", "cloud", "plugin", "disable", "p@m")
    assert rc == 1 and "shared profile 'work'" in out and "machine work" in out
    assert (a.claude / "settings.json").read_text() == before
    assert "shared" in world.cli(a, "machine", "cloud", "show")[1]
    # naming the profile itself is the deliberate way to change it for everyone on it
    assert world.cli_as(a, "machine", "work", "plugin", "disable", "p@m")[0] == 0


def test_a_typo_in_the_machine_name_does_not_create_a_machine(world):
    a = _shared_world(world)
    rc, out = world.cli(a, "machine", "clould", "tier", "basic")
    assert rc == 1 and "typo" in out
    assert not has_ref(world.origin, "refs/heads/sync/host-a")


def test_changing_this_machines_own_config_says_it_applied_here(world):
    a = _shared_world(world)
    rc, out = world.cli_as(a, "machine", "host-a", "plugin", "disable", "q@m")
    assert rc == 0 and "applied on this machine now" in out


def test_a_deeply_nested_json_value_is_a_message_not_a_traceback(world):
    a = _shared_world(world)
    rc, out = world.cli(a, "machine", "cloud", "override", "set", ".a", "[" * 100000)
    assert rc == 1 and "not JSON" in out and "bug" not in out
