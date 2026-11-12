"""Tests for the override and profile subcommands (claude_sync.doctor),
ported from the override cases of tests/sync_cli.bats and the profile
list/show/use/copy cases of tests/profile.bats. The functions are called
directly; test_cli.py covers dispatch, usage and the feature-branch skip.
Placeholder hostnames only: this file is published.
"""

from __future__ import annotations

import io
import json
import sys
from contextlib import ExitStack, redirect_stderr, redirect_stdout

import pytest
from helpers import git, has_ref, merge_and_converge

from claude_sync import doctor, gitflow, machineconf
from claude_sync.util import Fail, UsageError

NEEDS_STACK = {
    doctor.override_add, doctor.override_remove, doctor.profile_use, doctor.profile_copy,
}  # fmt: skip


def sub(world, m, func, *args, stdin=""):
    """(rc, output) of one subcommand as machine m: 0, 1 on Fail, 2 on a
    usage error."""
    ctx = m.ctx()
    buf = io.StringIO()
    saved, sys.stdin = sys.stdin, io.StringIO(stdin)
    try:
        with redirect_stdout(buf), redirect_stderr(buf):
            try:
                with ExitStack() as stack:
                    if func in NEEDS_STACK:
                        func(ctx, stack, *args)
                    else:
                        func(ctx, *args)
                rc = 0
            except Fail as err:
                print(f"claude-sync: {err.message}\n{err.hint}")
                rc = 1
            except UsageError:
                rc = 2
    finally:
        sys.stdin = saved
    return rc, buf.getvalue()


def sub_as(world, m, func, *args, stdin=""):
    """bats sync_as: on success, merge the PR and sync once more."""
    rc, out = sub(world, m, func, *args, stdin=stdin)
    if rc == 0 and func in NEEDS_STACK:
        merge_and_converge(world.origin, m.host, lambda c: world.run(m, c))
    return rc, out


def jload(path):
    return json.loads(path.read_bytes())


def origin_cat(world, path):
    return git("-C", world.origin, "show", f"master:{path}").stdout


def origin_has(world, path):
    return git("-C", world.origin, "cat-file", "-e", f"master:{path}", check=False).returncode == 0


# ── override (sync_cli.bats) ──────────────────────────────────────────────


@pytest.fixture
def two(world):
    """bats setup_two_machines."""
    a = world.machine("a")
    (a.claude / "settings.json").write_bytes(b'{"theme":"dark","enabledPlugins":{"shared@m":true}}\n')
    world.sync_as(a, "sync")
    b = world.machine("b", pull=False)
    world.sync_as(b, "bootstrap")
    return a, b


def test_override_add_makes_a_shared_key_machine_specific(world, two):
    a, b = two
    assert jload(b.claude / "settings.json")["enabledPlugins"]["shared@m"] is True

    rc, out = sub_as(world, a, doctor.override_add, '.enabledPlugins["shared@m"]')
    assert rc == 0, out
    assert (
        '.enabledPlugins["shared@m"] is now specific to host-a; other machines drop it once'
        " sync/host-a's PR is merged." in out
    )
    assert jload(a.repo / "machines" / "host-a.settings.json") == {"enabledPlugins": {"shared@m": True}}
    assert jload(a.claude / "settings.json")["enabledPlugins"]["shared@m"] is True

    world.sync_as(b, "sync")
    assert "shared@m" not in jload(b.claude / "settings.json").get("enabledPlugins", {})

    rc, out = sub(world, a, doctor.override_list)
    assert out.splitlines()[0] == '.enabledPlugins["shared@m"] = true'

    # doctor treats shared * override as the expected local state.
    with redirect_stdout(io.StringIO()) as buf, ExitStack() as stack:
        assert doctor.do_doctor(a.ctx(), stack) == 0, buf.getvalue()
    assert "healthy" in buf.getvalue()


def test_override_remove_hands_the_key_back_to_the_shared_settings(world, two):
    a, _b = two
    (a.repo / "machines" / "host-a.settings.json").write_bytes(b'{"theme":"solarized"}\n')
    world.sync_as(a, "sync")
    assert jload(a.claude / "settings.json")["theme"] == "solarized"

    rc, out = sub_as(world, a, doctor.override_remove, ".theme")
    assert rc == 0, out
    assert ".theme on host-a now follows the shared settings." in out
    assert not (a.repo / "machines" / "host-a.settings.json").exists()
    assert jload(a.claude / "settings.json")["theme"] == "dark"
    assert jload(a.repo / "dot-claude" / "settings.json")["theme"] == "dark"


def test_override_list_without_an_override(world, two):
    a, _b = two
    rc, out = sub(world, a, doctor.override_list)
    assert rc == 0
    assert out == "No overrides for host-a.\n"


def test_override_list_of_a_non_object_override_is_a_clear_error(world, two):
    a, _b = two
    (a.repo / "machines" / "host-a.settings.json").write_bytes(b'"dark"\n')
    rc, out = sub(world, a, doctor.override_list)
    assert rc == 1
    assert "must contain a JSON object" in out


def test_override_add_with_a_bad_path_changes_nothing(world, two):
    a, _b = two
    (a.claude / "CLAUDE.md").write_bytes(b"# pending edit\n")
    rc, out = sub(world, a, doctor.override_add, ".enabledPlugins[")
    assert rc == 1
    assert not has_ref(a.repo, "refs/heads/sync/host-a")  # nothing captured either


@pytest.mark.parametrize("func", [doctor.override_add, doctor.override_remove])
def test_override_add_and_remove_need_a_path(world, two, func):
    a, _b = two
    assert sub(world, a, func, "")[0] == 2


# ── profile list / show / use / copy (profile.bats) ───────────────────────


@pytest.fixture
def seeded(world):
    """bats seed_host_a_profile."""
    world.seed_profiles()
    a = world.machine("a")
    pa = a.repo / "machines" / "host-a"
    (pa / "agents").mkdir(parents=True)
    (pa / "hooks").mkdir()
    (pa / "agents" / "helper.md").write_bytes(b"host-a helper\n")
    (pa / "agents" / "reviewer.md.disabled").write_bytes(b"")
    (pa / "hooks" / "extra.sh").write_bytes(b"#!/bin/bash\necho extra\n")
    (pa / "hooks" / "extra.sh").chmod(0o755)
    (pa / "CLAUDE.append.md").write_bytes(b"host-a only\n")
    (a.repo / "machines" / "host-a.settings.json").write_bytes(b'{"theme":"light"}\n')
    world.sync_as(a, "sync")
    b = world.machine("b")
    return a, b


def test_profile_list_and_show_describe_every_entry_marker_and_append(world, seeded):
    a, b = seeded
    rc, out = sub(world, a, doctor.profile_list)
    assert rc == 0
    assert out.splitlines()[0] == "host-a uses its own profile 'host-a':"
    assert "  agents/helper.md  (replaces the shared one)\n" in out
    assert "  agents/reviewer.md  (disabled)\n" in out
    assert "  hooks/extra.sh  (machine-only)\n" in out
    assert "CLAUDE.append.md" in out
    assert "host-a.settings.json" in out

    rc, out = sub(world, b, doctor.profile_show, "host-a")
    assert rc == 0
    assert out.startswith("Profile 'host-a':\n")
    assert "agents/helper.md" in out

    rc, out = sub(world, b, doctor.profile_list)
    assert "(nothing" in out


def test_profile_show_and_use_refuse_a_bad_name(world, seeded):
    _a, b = seeded
    for func in (doctor.profile_show, doctor.profile_use):
        rc, out = sub(world, b, func, "../x")
        assert rc == 1
        assert "Not a valid profile name: ../x" in out
        assert sub(world, b, func, "")[0] == 2


def test_profile_use_points_the_machine_at_another_profile_and_applies_it(world, seeded):
    _a, b = seeded
    conf = machineconf.machine_conf_path(b.repo, "host-b")

    rc, out = sub_as(world, b, doctor.profile_use, "no-such-profile")
    assert rc != 0
    assert "No profile called 'no-such-profile': machines/no-such-profile/ does not exist." in out

    rc, out = sub_as(world, b, doctor.profile_use, "host-a")
    assert rc == 0, out
    assert "host-b now follows profile 'host-a'; edits to its entries go to machines/host-a/." in out
    assert "PROFILE=host-a" in conf.read_text().splitlines()
    assert "PROFILE=host-a" in origin_cat(world, "machines/host-b.conf").splitlines()
    assert (b.claude / "agents" / "helper.md").read_bytes() == b"host-a helper\n"
    assert jload(b.claude / "settings.json")["theme"] == "light"

    rc, out = sub(world, b, doctor.profile_list)
    assert "host-b follows profile 'host-a' (PROFILE= in machines/host-b.conf):" in out

    # Back to its own profile: the PROFILE= line goes.
    rc, out = sub_as(world, b, doctor.profile_use, "host-b")
    assert rc == 0, out
    assert not any(ln.startswith("PROFILE=") for ln in conf.read_text().splitlines())
    assert (b.claude / "agents" / "helper.md").read_bytes() == b"shared helper\n"


def test_profile_copy_shows_hooks_asks_first_and_needs_force_to_overwrite(world, seeded):
    _a, b = seeded
    pb = b.repo / "machines" / "host-b"

    rc, out = sub_as(world, b, doctor.profile_copy, ["host-a"], stdin="n\n")
    assert rc != 0
    assert "These executable hook scripts will be copied, and run by Claude Code on this machine:" in out
    assert "  machines/host-a/hooks/extra.sh\n" in out
    assert "Aborted: nothing was copied." in out
    assert not pb.exists()
    assert not has_ref(b.repo, "refs/heads/sync/host-b")

    rc, out = sub_as(world, b, doctor.profile_copy, ["host-a"], stdin="y\n")
    assert rc == 0, out
    assert "host-b now has its own copy of profile 'host-a' in machines/host-b/; edit it freely." in out
    assert (pb / "agents" / "helper.md").read_bytes() == b"host-a helper\n"
    assert jload(b.repo / "machines" / "host-b.settings.json") == {"theme": "light"}
    assert origin_has(world, "machines/host-b/agents/helper.md")
    # The hook keeps its executable bit in git, on Windows too: the commit
    # step marks a new hook executable.
    mode = git("-C", world.origin, "ls-tree", "master", "machines/host-b/hooks/extra.sh").stdout.split()[0]
    assert mode == "100755"
    assert (b.claude / "agents" / "helper.md").read_bytes() == b"host-a helper\n"
    assert jload(b.claude / "settings.json")["theme"] == "light"

    # Independent: editing the copy leaves host-a's profile alone.
    (b.claude / "agents" / "helper.md").write_bytes(b"host-b's own helper\n")
    world.sync_as(b, "sync")
    assert origin_cat(world, "machines/host-b/agents/helper.md") == "host-b's own helper\n"
    assert origin_cat(world, "machines/host-a/agents/helper.md") == "host-a helper\n"

    rc, out = sub_as(world, b, doctor.profile_copy, ["host-a", "--yes"])
    assert rc != 0
    assert "Re-run with --force to replace it." in out

    rc, out = sub_as(world, b, doctor.profile_copy, ["host-a", "--force", "--yes"])
    assert rc == 0, out
    assert origin_cat(world, "machines/host-b/agents/helper.md") == "host-a helper\n"


def test_profile_copy_commits_the_hook_executable_without_a_file_mode(world, seeded):
    """As on Windows: git add stages the copied hook as 100644."""
    _a, b = seeded
    git("-C", b.repo, "config", "core.filemode", "false")
    rc, out = sub(world, b, doctor.profile_copy, ["host-a", "--yes"])
    assert rc == 0, out
    tree = git("-C", world.origin, "ls-tree", "-r", "sync/host-b", "--", "machines/host-b").stdout
    modes = {line.split("\t", 1)[1]: line.split(" ", 1)[0] for line in tree.splitlines()}
    assert modes["machines/host-b/hooks/extra.sh"] == "100755"
    assert modes["machines/host-b/agents/helper.md"] == "100644"


def test_profile_copy_with_no_answer_at_all_aborts(world, seeded):
    _a, b = seeded
    rc, out = sub(world, b, doctor.profile_copy, ["host-a"], stdin="")
    assert rc == 1
    assert "Aborted" in out


def test_profile_copy_drops_a_profile_line_so_the_copy_takes_effect(world, seeded):
    _a, b = seeded
    assert sub_as(world, b, doctor.profile_use, "host-a")[0] == 0
    rc, out = sub_as(world, b, doctor.profile_copy, ["host-a", "--yes"])
    assert rc == 0, out
    conf = machineconf.machine_conf_path(b.repo, "host-b")
    assert not any(ln.startswith("PROFILE=") for ln in conf.read_text().splitlines())
    assert (b.repo / "machines" / "host-b").is_dir()


@pytest.mark.parametrize("args", [[], ["-x", "host-a"], ["host-a", "other"], ["--yes"]])
def test_profile_copy_bad_arguments_are_a_usage_error(world, seeded, args):
    _a, b = seeded
    assert sub(world, b, doctor.profile_copy, args)[0] == 2


def test_profile_copy_refuses_its_own_profile_and_a_missing_one(world, seeded):
    _a, b = seeded
    rc, out = sub(world, b, doctor.profile_copy, ["host-b", "--yes"])
    assert rc == 1
    assert "'host-b' is already this machine's own profile." in out
    rc, out = sub(world, b, doctor.profile_copy, ["host-z", "--yes"])
    assert rc == 1
    assert (
        "No profile called 'host-z': neither machines/host-z/ nor machines/host-z.settings.json exists."
        in out
    )


def test_profile_copy_of_an_override_only_profile(world, seeded):
    a, b = seeded
    (a.repo / "machines" / "host-c.settings.json").write_bytes(b'{"theme":"c"}\n')
    world.sync_as(a, "sync")
    world.run(b, "pull")
    rc, out = sub_as(world, b, doctor.profile_copy, ["host-c", "--yes"])
    assert rc == 0, out
    assert not (b.repo / "machines" / "host-b").exists()
    assert jload(b.repo / "machines" / "host-b.settings.json") == {"theme": "c"}
    assert "These executable hook scripts" not in out  # nothing to show, so no prompt


# ── fix round 1: links in the repo are never read through ─────────────────

needs_links = pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")


@needs_links
def test_profile_copy_refuses_a_source_override_that_is_a_link(world, seeded, tmp_path):
    _a, b = seeded
    secret = tmp_path / "secret.json"
    secret.write_bytes(b'{"token":"s3cret"}\n')
    (b.repo / "machines" / "host-c.settings.json").symlink_to(secret)
    (b.claude / "CLAUDE.md").write_bytes(b"# pending edit\n")  # a capture would commit this
    rc, out = sub(world, b, doctor.profile_copy, ["host-c", "--yes"])
    assert rc == 1
    assert "host-c.settings.json is a link or leads outside" in out
    assert not (b.repo / "machines" / "host-b.settings.json").exists()
    assert not has_ref(b.repo, "refs/heads/sync/host-b")
    assert "s3cret" not in git("-C", b.repo, "log", "--all", "-p").stdout


@needs_links
def test_profile_copy_refuses_a_source_folder_with_a_link_before_capturing(world, seeded, tmp_path):
    a, b = seeded
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.md").write_bytes(b"outside\n")
    (b.repo / "machines" / "host-a" / "agents" / "linked").symlink_to(outside)
    (b.claude / "CLAUDE.md").write_bytes(b"# pending edit\n")
    rc, out = sub(world, b, doctor.profile_copy, ["host-a", "--yes"])
    assert rc == 1
    assert "machines/host-a/ was not copied" in out
    assert not (b.repo / "machines" / "host-b").exists()
    assert not has_ref(b.repo, "refs/heads/sync/host-b")  # nothing captured or committed


@needs_links
def test_a_linked_override_stops_sync_and_list_before_anything_changes(world, tmp_path):
    a = world.machine("a")
    world.sync_as(a, "sync")
    secret = tmp_path / "secret.json"
    secret.write_bytes(b'{"token":"s3cret"}\n')
    (a.repo / "machines" / "host-a.settings.json").symlink_to(secret)
    before = (a.claude / "settings.json").read_bytes()
    (a.claude / "CLAUDE.md").write_bytes(b"# pending edit\n")

    rc, out = world.run(a, "sync")
    assert rc == 1
    assert "host-a.settings.json is a link or leads outside" in out
    assert (a.claude / "settings.json").read_bytes() == before
    assert not has_ref(a.repo, "refs/heads/sync/host-a")
    for func, args in ((doctor.override_list, ()), (doctor.override_add, (".theme",))):
        rc, out = sub(world, a, func, *args)
        assert rc == 1, out
        assert "is a link" in out
    assert not has_ref(a.repo, "refs/heads/sync/host-a")


# ── fix round 2: machines/ itself a link ──────────────────────────────────


def _link_machines_out(m, tmp_path):
    """Moves m's machines/ out of the clone and leaves a link to it."""
    outside = tmp_path / f"outside-{m.name}"
    (m.repo / "machines").rename(outside)
    (m.repo / "machines").symlink_to(outside)
    return outside, sorted(p.name for p in outside.iterdir())


@needs_links
def test_profile_copy_refuses_a_linked_machines_folder_and_writes_nothing_outside(world, seeded, tmp_path):
    _a, b = seeded
    (b.repo / "machines" / "host-a.settings.json").unlink()  # no override on either side
    outside, names = _link_machines_out(b, tmp_path)
    rc, out = sub(world, b, doctor.profile_copy, ["host-a", "--yes"])
    assert rc == 1
    assert "is a link or leads outside" in out
    assert sorted(p.name for p in outside.iterdir()) == names
    assert not has_ref(b.repo, "refs/heads/sync/host-b")


@needs_links
def test_sync_and_override_add_refuse_a_linked_machines_folder(world, tmp_path):
    a = world.machine("a")
    world.sync_as(a, "sync")
    outside, names = _link_machines_out(a, tmp_path)
    before = (a.claude / "settings.json").read_bytes()
    rc, out = world.run(a, "sync")
    assert rc == 1
    assert "is a link or leads outside" in out
    rc, out = sub(world, a, doctor.override_add, ".theme")
    assert rc == 1
    assert "is a link" in out
    assert sorted(p.name for p in outside.iterdir()) == names
    assert (a.claude / "settings.json").read_bytes() == before
    assert not has_ref(a.repo, "refs/heads/sync/host-a")


@needs_links
def test_profile_show_refuses_a_linked_override(world, seeded, tmp_path):
    _a, b = seeded
    secret = tmp_path / "secret.json"
    secret.write_bytes(b"{}\n")
    (b.repo / "machines" / "host-a.settings.json").unlink()
    (b.repo / "machines" / "host-a.settings.json").symlink_to(secret)
    rc, out = sub(world, b, doctor.profile_show, "host-a")
    assert rc == 1
    assert "is a link or leads outside" in out


@needs_links
def test_profile_copy_refusing_an_override_link_leaves_its_own_profile_untouched(
    world, seeded, tmp_path, monkeypatch
):
    """A link made at machines/<own>.settings.json after the first check is
    refused before machines/<own>/ is replaced."""
    _a, b = seeded
    pb = b.repo / "machines" / "host-b"
    (pb / "agents").mkdir(parents=True)
    (pb / "agents" / "own.md").write_bytes(b"host-b own\n")
    world.sync_as(b, "sync")
    before = sorted((p.relative_to(pb).as_posix(), p.read_bytes()) for p in pb.rglob("*") if p.is_file())
    secret = tmp_path / "secret.json"
    secret.write_bytes(b'{"token":"s3cret"}\n')
    real = gitflow.do_capture_and_commit

    def capture_then_link(ctx):
        real(ctx)
        (b.repo / "machines" / "host-b.settings.json").symlink_to(secret)

    monkeypatch.setattr(gitflow, "do_capture_and_commit", capture_then_link)
    rc, out = sub(world, b, doctor.profile_copy, ["host-a", "--force", "--yes"])
    assert rc == 1
    assert "host-b.settings.json is a link or leads outside" in out
    after = sorted((p.relative_to(pb).as_posix(), p.read_bytes()) for p in pb.rglob("*") if p.is_file())
    assert after == before
    assert secret.read_bytes() == b'{"token":"s3cret"}\n'
