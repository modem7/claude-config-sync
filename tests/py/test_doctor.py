"""Tests for claude_sync.doctor (do_doctor), ported from the doctor cases of
tests/sync_cli.bats and tests/profile.bats. do_doctor is called directly
here, with the nested sync of `remediate` stubbed by World.run; the CLI
path is in test_cli.py. Placeholder hostnames only: this file is published.
"""

from __future__ import annotations

import io
import json
import os
import shlex
import shutil
import subprocess
import sys
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path

import pytest
from helpers import git

from claude_sync import doctor, gitflow, profile
from claude_sync.lock import SyncLock
from claude_sync.util import Fail


def run_doctor(world, m, remediate=False, run_sync=None):
    """(exit code, output) of do_doctor as machine m. The nested sync
    defaults to World.run(m, "sync"), its output passed on."""

    def nested() -> bool:
        rc, out = world.run(m, "sync")
        print(out, end="")
        return rc == 0

    ctx = m.ctx()
    buf = io.StringIO()
    with redirect_stdout(buf), redirect_stderr(buf):
        try:
            with ExitStack() as stack:
                rc = doctor.do_doctor(
                    ctx, stack, remediate=remediate, run_sync=run_sync or nested
                )
        except Fail as err:
            print(f"claude-sync: {err.message}\n{err.hint}")
            rc = 1
    return rc, buf.getvalue()


def stuck_rebase(world, m):
    """A genuine stuck rebase: origin and the clone both edit CLAUDE.md's
    line, then the clone rebases by hand so it pauses mid-conflict."""
    world.commit_on_origin({"dot-claude/CLAUDE.md": b"# from origin\n"}, "remote CLAUDE.md change")
    (m.repo / "dot-claude" / "CLAUDE.md").write_bytes(b"# from stuck local edit\n")
    git("-C", m.repo, "commit", "-q", "-am", "local CLAUDE.md change")
    git("-C", m.repo, "fetch", "-q", "origin")
    assert git("-C", m.repo, "rebase", "origin/master", check=False).returncode != 0
    assert (m.repo / ".git" / "rebase-merge").is_dir()


# ── sync_cli.bats ─────────────────────────────────────────────────────────


def test_doctor_reports_healthy_on_a_repo_in_sync(world):
    a = world.machine("a")
    world.sync_as(a, "sync")
    rc, out = run_doctor(world, a)
    assert rc == 0, out
    assert "No issues found - sync is healthy." in out


LONG_PROJECT = "/home/user/" + "deep/" * 45


def _use_long_project(m):
    from claude_sync import machineconf

    machineconf.write_machine_conf(m.repo, m.host, LONG_PROJECT)


def test_doctor_flags_a_project_path_with_a_very_long_folder_name(world):
    a = world.machine("a")
    world.sync_as(a, "sync")
    _use_long_project(a)
    rc, out = run_doctor(world, a)
    assert "ISSUE: The project path /home/user/deep/" in out
    assert "may not be the folder Claude Code uses" in out
    assert "No issues found" not in out


@pytest.mark.skipif(os.name == "nt", reason="the memory folder path would pass MAX_PATH under the temp dir")
def test_sync_warns_once_about_a_very_long_project_folder_name(world):
    a = world.machine("a")
    _use_long_project(a)
    rc, out = world.run(a, "sync")
    assert rc == 0, out
    assert out.count("claude-sync: warning: the project path /home/user/deep/") == 1


def test_sync_does_not_warn_for_a_normal_project_path(world):
    a = world.machine("a")
    rc, out = world.run(a, "sync")
    assert rc == 0, out
    assert "warning: the project path" not in out


def test_doctor_detects_a_stuck_rebase_without_touching_it(world):
    a = world.machine("a")
    world.sync_as(a, "sync")
    stuck_rebase(world, a)
    rc, out = run_doctor(world, a)
    assert rc != 0
    assert "ISSUE: repo is mid-rebase" in out
    assert (a.repo / ".git" / "rebase-merge").is_dir()


def test_doctor_remediate_clears_a_stuck_rebase(world):
    a = world.machine("a")
    world.sync_as(a, "sync")
    stuck_rebase(world, a)
    rc, out = run_doctor(world, a, remediate=True)
    assert "-> aborted the stuck rebase." in out
    assert not (a.repo / ".git" / "rebase-merge").exists()


def remote_theme_light(world):
    world.commit_on_origin({"dot-claude/settings.json": b'{"theme":"light"}\n'}, "remote theme change")


def test_doctor_remediate_moves_a_legacy_commit_and_reports_a_real_conflict(world):
    a = world.machine("a")
    world.sync_as(a, "sync")
    remote_theme_light(world)
    # An older claude-sync.sh committed a capture on master and then failed
    # to rebase it: the clone is ahead of and behind its upstream.
    (a.claude / "settings.json").write_bytes(b'{"theme":"custom"}\n')
    (a.repo / "dot-claude" / "settings.json").write_bytes(b'{"theme":"custom"}\n')
    git("-C", a.repo, "commit", "-q", "-am", "sync: host-a legacy")
    head = git("-C", a.repo, "rev-parse", "HEAD").stdout
    (a.claude / ".claude-sync-applied-commit").write_text(head, encoding="utf-8")
    git("-C", a.repo, "fetch", "-q")

    rc, out = run_doctor(world, a)
    assert rc != 0
    assert "1 ahead / 1 behind its upstream" in out

    rc, out = run_doctor(world, a, remediate=True)
    assert "moved 1 unpushed commit(s) from master onto sync/host-a" in out
    assert "real conflict" in out
    assert "Resolve manually" in out
    assert "-> still failing: a real conflict exists" in out
    # Live was never silently overwritten with either side's content.
    assert (a.claude / "settings.json").read_bytes() == b'{"theme":"custom"}\n'
    # The legacy commit now lives on the sync branch; master is back on origin's line.
    assert git("-C", a.repo, "log", "-1", "--format=%s", "sync/host-a").stdout.strip() == (
        "sync: host-a legacy"
    )
    git("-C", a.repo, "merge-base", "--is-ancestor", "master", "origin/master")


def test_doctor_reports_commits_only_ahead_as_going_out_as_a_pr(world):
    a = world.machine("a")
    world.sync_as(a, "sync")
    (a.repo / "dot-claude" / "CLAUDE.md").write_bytes(b"# legacy\n")
    git("-C", a.repo, "commit", "-q", "-am", "sync: host-a legacy")
    rc, out = run_doctor(world, a)
    assert rc == 1
    assert "1 commit(s) ahead of its upstream" in out
    assert "moves them onto sync/host-a as a PR" in out


def test_doctor_remediate_refreshes_local_settings_when_only_local_is_stale(world):
    a = world.machine("a")
    world.sync_as(a, "sync")
    (a.claude / "settings.json").write_bytes(b'{"theme":"stale-local-edit"}\n')

    rc, out = run_doctor(world, a)
    assert rc != 0
    assert "differs from the repo's dot-claude/settings.json" in out

    rc, out = run_doctor(world, a, remediate=True)
    assert "-> re-applied the repo's settings.json to local." in out
    assert (a.claude / "settings.json").read_bytes() == b'{"theme":"dark"}\n'
    # The re-apply is recorded, so the next sync does not refuse.
    rc, out = world.run(a, "sync")
    assert rc == 0, out


def test_doctor_treats_shared_plus_override_as_the_expected_settings(world):
    a = world.machine("a")
    (a.repo / "machines" / "host-a.settings.json").write_bytes(b'{"theme":"solarized"}\n')
    world.sync_as(a, "sync")
    rc, out = run_doctor(world, a)
    assert rc == 0, out
    assert "healthy" in out


def test_doctor_reports_an_override_that_is_not_an_object(world):
    """Review focus 4: a clear issue, no traceback, nothing changed."""
    a = world.machine("a")
    world.sync_as(a, "sync")
    ov = a.repo / "machines" / "host-a.settings.json"
    ov.write_bytes(b"[1]\n")
    before = (a.claude / "settings.json").read_bytes()
    rc, out = run_doctor(world, a, remediate=True)
    assert rc == 1
    assert "ISSUE: could not work out what settings.json should be" in out
    assert "must contain a JSON object" in out
    assert (a.claude / "settings.json").read_bytes() == before
    assert ov.read_bytes() == b"[1]\n"


def test_doctor_without_a_machine_conf_skips_the_settings_check(world):
    a = world.machine("a")
    world.sync_as(a, "sync")
    (a.claude / "settings.json").write_bytes(b'{"theme":"edited"}\n')
    ctx = a.ctx(hostname="host-unknown")
    buf = io.StringIO()
    with redirect_stdout(buf), ExitStack() as stack:
        rc = doctor.do_doctor(ctx, stack)
    assert rc == 0, buf.getvalue()
    assert "No issues found" in buf.getvalue()


def test_doctor_notes_pending_changes_on_the_sync_branch(world):
    a = world.machine("a")
    (a.claude / "CLAUDE.md").write_bytes(b"# pending\n")
    world.ok(a, "sync")  # pushed, PR not merged
    rc, out = run_doctor(world, a)
    assert rc == 0, out
    assert "Note: sync/host-a holds this machine's changes that are not merged yet" in out


# ── the lock ──────────────────────────────────────────────────────────────


def _hold_lock(m):
    return SyncLock(m.repo / ".git" / "claude-sync.flock", 0, repo_dir=m.repo)


def test_plain_doctor_needs_no_lock_but_remediate_waits_for_it(world):
    a = world.machine("a")
    world.sync_as(a, "sync")
    with _hold_lock(a):
        rc, out = run_doctor(world, a)
        assert rc == 0, out
        rc, out = run_doctor(world, a, remediate=True)
        assert rc == 1
        assert "another claude-sync run is still working" in out


def test_remediate_lets_the_lock_go_around_the_nested_sync(world):
    """SyncLock is not re-entrant: the nested sync must find it free, and
    remediate holds it again afterwards."""
    a = world.machine("a")
    world.sync_as(a, "sync")
    (a.repo / "dot-claude" / "CLAUDE.md").write_bytes(b"# legacy\n")
    git("-C", a.repo, "commit", "-q", "-am", "sync: host-a legacy")
    seen = []

    def nested() -> bool:
        with _hold_lock(a):  # wait 0: fails at once if remediate kept it
            seen.append("free")
        return True

    ctx = a.ctx()
    with redirect_stdout(io.StringIO()), ExitStack() as stack:
        doctor.do_doctor(ctx, stack, remediate=True, run_sync=nested)
        assert seen == ["free"]
        assert ctx._lock is not None  # taken again after the nested sync
        with pytest.raises(Fail, match="another claude-sync run"):
            with _hold_lock(a):
                pass
    with _hold_lock(a):  # released when the stack closed
        pass


def test_remediate_needs_a_nested_sync():
    with pytest.raises(ValueError):
        doctor.do_doctor(None, ExitStack(), remediate=True, run_sync=None)


# ── profile.bats ──────────────────────────────────────────────────────────


@pytest.fixture
def pworld(world):
    world.seed_profiles()
    return world


def profile_dir(m) -> Path:
    return m.repo / "machines" / "host-a"


def test_doctor_reports_a_disabled_marker_that_hides_nothing(pworld):
    a = pworld.machine("a")
    (profile_dir(a) / "agents").mkdir(parents=True)
    (profile_dir(a) / "agents" / "no-such-agent.md.disabled").write_bytes(b"")
    pworld.sync_as(a, "sync")
    rc, out = run_doctor(pworld, a)
    assert rc != 0
    assert (
        "ISSUE: machines/host-a/agents/no-such-agent.md.disabled hides nothing: there is no"
        " shared agents entry called no-such-agent.md. Delete the marker." in out
    )


def test_doctor_reports_a_profile_mismatch_and_remediate_reapplies_it(pworld):
    a = pworld.machine("a")
    (profile_dir(a) / "agents").mkdir(parents=True)
    (profile_dir(a) / "agents" / "helper.md").write_bytes(b"host-a helper\n")
    pworld.sync_as(a, "sync")
    # As if a pull brought a newer profile entry but nothing applied it.
    (profile_dir(a) / "agents" / "helper.md").write_bytes(b"host-a helper v2\n")

    rc, out = run_doctor(pworld, a)
    assert rc != 0
    assert "live config differs from the shared config plus profile 'host-a' for: agents/helper.md." in out

    rc, out = run_doctor(pworld, a, remediate=True)
    assert "-> re-applied the shared config and profile 'host-a' to local." in out
    assert (a.claude / "agents" / "helper.md").read_bytes() == b"host-a helper v2\n"


def test_doctor_lists_every_mismatch_comma_separated(pworld):
    a = pworld.machine("a")
    (profile_dir(a) / "agents").mkdir(parents=True)
    (profile_dir(a) / "agents" / "helper.md").write_bytes(b"host-a helper\n")
    (profile_dir(a) / "agents" / "reviewer.md").write_bytes(b"host-a reviewer\n")
    pworld.sync_as(a, "sync")
    (profile_dir(a) / "agents" / "helper.md").write_bytes(b"v2\n")
    (profile_dir(a) / "agents" / "reviewer.md").write_bytes(b"v2\n")
    rc, out = run_doctor(pworld, a)
    assert "for: agents/helper.md, agents/reviewer.md." in out


def test_doctor_remediate_does_not_discard_a_local_edit_to_a_profile_entry(pworld):
    a = pworld.machine("a")
    (profile_dir(a) / "agents").mkdir(parents=True)
    (profile_dir(a) / "agents" / "helper.md").write_bytes(b"host-a helper\n")
    pworld.sync_as(a, "sync")
    (a.claude / "agents" / "helper.md").write_bytes(b"local edit\n")

    rc, out = run_doctor(pworld, a, remediate=True)
    assert "-> not re-applied: an entry of profile 'host-a' was edited locally." in out
    assert (a.claude / "agents" / "helper.md").read_bytes() == b"local edit\n"


GUARD_SETTINGS = (
    b'{"theme":"dark","hooks":{"PreToolUse":[{"matcher":"Bash","hooks":[{"type":"command",'
    b'"command":"\\"$HOME/.claude/hooks/guard.sh\\""}]}]}}\n'
)


@pytest.mark.skipif(profile.IS_WINDOWS, reason=".sh hook refs are skipped on Windows")
def test_doctor_reports_a_hook_command_whose_script_does_not_exist(pworld):
    pworld.commit_on_origin({"dot-claude/settings.json": GUARD_SETTINGS}, "wire guard hook")
    a = pworld.machine("a")
    rc, out = run_doctor(pworld, a)
    assert rc == 0, out

    (profile_dir(a) / "hooks").mkdir(parents=True)
    (profile_dir(a) / "hooks" / "guard.sh.disabled").write_bytes(b"")
    pworld.sync_as(a, "sync")
    rc, out = run_doctor(pworld, a)
    assert rc != 0
    assert "hooks/guard.sh, which does not exist" in out


def test_a_profile_check_os_error_is_a_fail_not_a_traceback(pworld, monkeypatch):
    """T8 hand-off: profile.py's read-only checks raise OSError; doctor
    turns it into a Fail that names the problem."""
    a = pworld.machine("a")
    (profile_dir(a) / "agents").mkdir(parents=True)
    (profile_dir(a) / "agents" / "helper.md").write_bytes(b"host-a helper\n")
    pworld.sync_as(a, "sync")

    def boom(*_args):
        raise PermissionError(13, "Permission denied", str(a.claude / "agents"))

    monkeypatch.setattr(profile, "profile_live_mismatches", boom)
    with pytest.raises(Fail, match="could not compare the live config with profile 'host-a'"):
        with ExitStack() as stack, redirect_stdout(io.StringIO()):
            doctor.do_doctor(a.ctx(), stack)


def test_gitflow_is_untouched_by_doctor(world):
    """Plain doctor changes nothing: no sync branch, no stamp, same HEAD."""
    a = world.machine("a")
    world.sync_as(a, "sync")
    head = git("-C", a.repo, "rev-parse", "HEAD").stdout
    stamp = (a.claude / ".claude-sync-applied-commit").read_bytes()
    (a.claude / "settings.json").write_bytes(b'{"theme":"edited"}\n')
    run_doctor(world, a)
    assert git("-C", a.repo, "rev-parse", "HEAD").stdout == head
    assert (a.claude / ".claude-sync-applied-commit").read_bytes() == stamp
    assert not gitflow.has_sync_branch(a.ctx())
    assert (a.claude / "settings.json").read_bytes() == b'{"theme":"edited"}\n'


# ── hook modes ────────────────────────────────────────────────────────────


def _plain_hooks(world):
    """Hooks committed as 100644, as a Windows machine would, next to
    files the check must leave alone."""
    world.commit_on_origin(
        {
            "dot-claude/hooks/plain.sh": b"echo a .sh name\n",
            "dot-claude/hooks/check": b"#!/bin/sh\necho a shebang\n",
            "dot-claude/hooks/README.md": b"notes, not a script\n",
            "dot-claude/agents/runner.sh": b"#!/bin/bash\necho not a hook\n",
            "machines/host-b/hooks/extra.sh": b"#!/bin/bash\necho host-b\n",
            "machines/host-b/hooks/old.sh.disabled": b"",
        },
        "hooks without the exec bit",
    )


def test_doctor_flags_a_hook_tracked_without_the_exec_bit(world):
    _plain_hooks(world)
    a = world.machine("a")
    rc, out = run_doctor(world, a)
    assert rc == 1, out
    flagged = sorted(ln for ln in out.splitlines() if ln.startswith("ISSUE: ") and "exec bit" in ln)
    assert len(flagged) == 3, out
    for rel in ("dot-claude/hooks/plain.sh", "dot-claude/hooks/check", "machines/host-b/hooks/extra.sh"):
        assert any(f"ISSUE: {rel} " in ln for ln in flagged), out
    assert f"git -C {doctor._paste_arg(str(a.repo))} update-index --chmod=+x -- {doctor._paste_arg('dot-claude/hooks/plain.sh')}" in out
    for rel in ("README.md", "runner.sh", "old.sh.disabled"):
        assert rel not in out, out


def test_doctor_hook_mode_issue_clears_after_update_index_and_sync(world):
    """The Windows fix from the hint: set the mode in the index, then sync."""
    world.commit_on_origin({"dot-claude/hooks/plain.sh": b"#!/bin/bash\necho one\n"}, "plain hook")
    a = world.machine("a")
    git("-C", a.repo, "config", "core.filemode", "false")
    assert "dot-claude/hooks/plain.sh" in run_doctor(world, a)[1]
    git("-C", a.repo, "update-index", "--chmod=+x", "--", "dot-claude/hooks/plain.sh")
    world.sync_as(a, "sync")
    assert git("-C", world.origin, "ls-tree", "master", "dot-claude/hooks/plain.sh").stdout.startswith("100755")
    rc, out = run_doctor(world, a)
    assert rc == 0, out
    assert "No issues found - sync is healthy." in out


@pytest.mark.skipif(profile.IS_WINDOWS, reason="exec bit on disk is POSIX only")
def test_doctor_hook_mode_issue_clears_after_chmod_and_sync(world):
    """The Linux and macOS fix from the hint: chmod +x the live copy."""
    world.commit_on_origin({"dot-claude/hooks/plain.sh": b"#!/bin/bash\necho one\n"}, "plain hook")
    a = world.machine("a")
    assert "dot-claude/hooks/plain.sh" in run_doctor(world, a)[1]
    (a.claude / "hooks" / "plain.sh").chmod(0o755)
    (a.repo / "dot-claude" / "hooks" / "plain.sh").chmod(0o755)
    world.sync_as(a, "sync")
    assert git("-C", world.origin, "ls-tree", "master", "dot-claude/hooks/plain.sh").stdout.startswith("100755")
    rc, out = run_doctor(world, a)
    assert rc == 0, out


def test_doctor_hook_mode_hint_is_safe_to_paste(world, tmp_path):
    """A hook name with a space, ';' and '$(...)' is quoted in the hint, so
    pasting it sets the mode and runs nothing else."""
    rel = "dot-claude/hooks/a b;$(touch pwned).sh"
    world.commit_on_origin({rel: b"echo hi\n"}, "awkward hook name")
    a = world.machine("a")
    rc, out = run_doctor(world, a)
    assert rc == 1, out
    line = next(ln for ln in out.splitlines() if ln.startswith("ISSUE: ") and "exec bit" in ln)
    if os.name == "nt":
        assert "update-index --chmod=+x --" not in line  # '$' can't be quoted safely there
        return
    cmd = line.split("Fix: ", 1)[1].split(" (on Linux", 1)[0]
    assert cmd == f"git -C {shlex.quote(str(a.repo))} update-index --chmod=+x -- {shlex.quote(rel)}"
    subprocess.run(["sh", "-c", cmd], cwd=tmp_path, check=True)
    assert not (tmp_path / "pwned").exists() and not (a.repo / "pwned").exists()
    staged = git("-C", a.repo, "ls-files", "-s", "--", rel).stdout
    assert staged.startswith("100755"), staged


def test_paste_arg_drops_control_characters_and_quotes():
    assert "\x1b" not in (doctor._paste_arg("a\x1b[31mb\nc") or "")
    assert doctor._printable("a\x1b[31mb\nc\x07") == "a[31mbc"
    if os.name == "nt":
        assert doctor._paste_arg('x"y') is None
        assert doctor._paste_arg("x$y") is None
        assert doctor._paste_arg("a b;c") == '"a b;c"'
    else:
        assert doctor._paste_arg("a b;$(c)") == "'a b;$(c)'"


# ── plugins enabled but not installed (#76) ───────────────────────────────


PLUGIN_SETTINGS = {
    "theme": "dark",
    "enabledPlugins": {
        "tool@market-a": True,
        "off@market-a": False,
        "helper@claude-plugins-official": True,
    },
    "extraKnownMarketplaces": {
        "market-a": {"source": {"source": "github", "repo": "example-org/market-a"}},
    },
}


def cmd(verb, arg):
    """A command line as plugin_report prints it, quoted for this platform."""
    return f"claude plugin {verb} {doctor._paste_arg(arg)}"


def install_plugins(m, *ids, marketplaces=()):
    """Writes this machine's plugin records as Claude Code does (format 2)."""
    pdir = m.claude / "plugins"
    pdir.mkdir(parents=True, exist_ok=True)
    records = {i: [{"scope": "user", "installPath": f"/x/{i}", "version": "1.0.0"}] for i in ids}
    (pdir / "installed_plugins.json").write_text(json.dumps({"version": 2, "plugins": records}))
    known = {n: {"source": {"source": "github", "repo": f"example-org/{n}"}} for n in marketplaces}
    (pdir / "known_marketplaces.json").write_text(json.dumps(known))


def _plugin_machine(world, settings=PLUGIN_SETTINGS, override=None):
    files = {"dot-claude/settings.json": (json.dumps(settings) + "\n").encode()}
    if override is not None:
        files["machines/host-a.settings.json"] = (json.dumps(override) + "\n").encode()
    world.commit_on_origin(files, "plugins enabled")
    return world.machine("a")


def _plugin_lines(out):
    lines = out.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("Plugins: "))
    return [ln.strip() for ln in lines[start + 1:] if ln.startswith("  ")]


def test_doctor_lists_commands_for_an_enabled_plugin_that_is_not_installed(world):
    a = _plugin_machine(world)
    rc, out = run_doctor(world, a)
    assert rc == 0, out  # report only, never an ISSUE
    assert "No issues found" in out
    assert "doctor never installs anything" in out
    cmds = [ln for ln in _plugin_lines(out) if ln.startswith("claude plugin")]
    assert cmds == [
        cmd("marketplace add", "anthropics/claude-plugins-official"),
        cmd("marketplace add", "example-org/market-a"),
        cmd("install", "helper@claude-plugins-official"),
        cmd("install", "tool@market-a"),
    ]
    assert "off@market-a" not in out


def test_doctor_is_silent_about_plugins_when_everything_matches(world):
    a = _plugin_machine(world)
    install_plugins(
        a, "tool@market-a", "helper@claude-plugins-official",
        marketplaces=("market-a", "claude-plugins-official"),
    )  # fmt: skip
    rc, out = run_doctor(world, a)
    assert rc == 0, out
    assert "Plugin" not in out and "claude plugin" not in out
    assert doctor.plugin_report(a.ctx()) == []


def test_doctor_lists_only_a_missing_marketplace(world):
    a = _plugin_machine(world)
    install_plugins(a, "tool@market-a", "helper@claude-plugins-official", marketplaces=("claude-plugins-official",))
    rc, out = run_doctor(world, a)
    assert rc == 0, out
    assert _plugin_lines(out)[0] == cmd("marketplace add", "example-org/market-a")
    assert "claude plugin install" not in out


def test_doctor_does_not_report_a_plugin_an_override_turns_off(world):
    a = _plugin_machine(
        world, override={"enabledPlugins": {"tool@market-a": False, "helper@claude-plugins-official": False}}
    )
    install_plugins(a, marketplaces=("market-a",))
    rc, out = run_doctor(world, a)
    assert rc == 0, out
    assert "claude plugin" not in out


def test_doctor_notes_a_plugin_whose_marketplace_is_unknown(world):
    settings = {"enabledPlugins": {"thing@nowhere": True}}
    a = _plugin_machine(world, settings=settings)
    install_plugins(a)
    rc, out = run_doctor(world, a)
    assert rc == 0, out
    lines = _plugin_lines(out)
    assert cmd("install", "thing@nowhere") in lines
    assert any("its marketplace nowhere is not declared" in ln for ln in lines)


def test_doctor_gives_the_source_for_each_marketplace_type(world):
    markets = {
        "m-git": {"source": {"source": "git", "url": "https://git.example.com/team/plugins.git"}},
        "m-gh-ref": {"source": {"source": "github", "repo": "example-org/m", "ref": "v1"}},
        "m-dir": {"source": {"source": "directory", "path": "/srv/market"}},
        "m-url": {"source": {"source": "url", "url": "https://example.com/marketplace.json"}},
        "m-git-no-suffix": {"source": {"source": "git", "url": "https://git.example.com/team/plugins"}},
        "m-inline": {"source": {"source": "settings", "name": "m-inline", "plugins": []}},
        "claudeai-org": {"source": {"source": "github", "repo": "example-org/x"}},
    }
    a = _plugin_machine(world, settings={"extraKnownMarketplaces": markets})
    rc, out = run_doctor(world, a)
    assert rc == 0, out
    lines = _plugin_lines(out)
    adds = [ln for ln in lines if ln.startswith("claude plugin marketplace add")]
    assert adds == [
        cmd("marketplace add", "/srv/market"),
        cmd("marketplace add", "example-org/m#v1"),
        cmd("marketplace add", "https://git.example.com/team/plugins.git"),
        cmd("marketplace add", "https://example.com/marketplace.json"),
    ]
    assert any("m-git-no-suffix: no add command" in ln for ln in lines)
    assert any("m-inline: no add command" in ln for ln in lines)
    assert "claudeai-org" not in out


def test_doctor_reads_the_additional_marketplaces_alias(world):
    settings = {"additionalMarketplaces": {"m-alias": {"source": {"source": "github", "repo": "example-org/a"}}}}
    a = _plugin_machine(world, settings=settings)
    rc, out = run_doctor(world, a)
    assert cmd("marketplace add", "example-org/a") in out


@pytest.mark.parametrize(
    "installed",
    [b"[]", b"{not json", b'{"version": 2, "plugins": []}', b'{"version": 99, "plugins": {}}'],
)
def test_doctor_skips_the_plugin_check_on_an_unknown_file_shape(world, installed):
    a = _plugin_machine(world)
    install_plugins(a)
    (a.claude / "plugins" / "installed_plugins.json").write_bytes(installed)
    rc, out = run_doctor(world, a)
    assert rc == 0, out
    skips = [ln for ln in out.splitlines() if ln.startswith("Plugin check skipped: ")]
    assert len(skips) == 1, out
    assert "claude plugin" not in out
    assert "Traceback" not in out


def test_doctor_skips_on_an_unknown_marketplaces_shape(world):
    a = _plugin_machine(world)
    install_plugins(a)
    (a.claude / "plugins" / "known_marketplaces.json").write_bytes(b'["market-a"]')
    rc, out = run_doctor(world, a)
    assert rc == 0, out
    assert sum(ln.startswith("Plugin check skipped: ") for ln in out.splitlines()) == 1


def test_doctor_accepts_the_older_dict_record_form(world):
    a = _plugin_machine(world)
    pdir = a.claude / "plugins"
    pdir.mkdir(parents=True)
    records = {"tool@market-a": {"version": "1"}, "helper@claude-plugins-official": {"version": "1"}}
    (pdir / "installed_plugins.json").write_text(json.dumps({"plugins": records}))
    (pdir / "known_marketplaces.json").write_text(json.dumps({"market-a": {}, "claude-plugins-official": {}}))
    rc, out = run_doctor(world, a)
    assert "claude plugin" not in out and "Plugin check skipped" not in out


def test_doctor_ignores_synced_plugins_and_a_missing_settings_file(world):
    a = _plugin_machine(world, settings={"enabledPlugins": {"x@synced": True}})
    assert doctor.plugin_report(a.ctx()) == []
    (a.claude / "settings.json").unlink()
    assert doctor.plugin_report(a.ctx()) == []


def test_plugin_install_hint_is_safe_to_paste(world, tmp_path):
    plugin_id = "x;touch pwned@m$(touch pwned)"
    a = _plugin_machine(world, settings={"enabledPlugins": {plugin_id: True}})
    install_plugins(a, marketplaces=("m$(touch pwned)",))
    lines = doctor.plugin_report(a.ctx())
    if os.name == "nt":
        assert not any(ln.strip().startswith("claude plugin install") for ln in lines)
        return
    cmd = next(ln.strip() for ln in lines if ln.strip().startswith("claude plugin install"))
    assert shlex.split(cmd) == ["claude", "plugin", "install", plugin_id]


def test_the_plugin_check_never_runs_anything(world, monkeypatch):
    a = _plugin_machine(world)

    def refuse(*args, **kwargs):
        raise AssertionError("the plugin check ran a command")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(os, "system", refuse)
    lines = doctor.plugin_report(a.ctx())
    assert cmd("install", "tool@market-a") in [ln.strip() for ln in lines]


# ── doctor install-plugins: the opt-in installer ──────────────────────────

FAKE_CLAUDE = r'''
import json
import os
import sys
from pathlib import Path

here = Path(__file__).resolve().parent
with open(here / "calls.jsonl", "a", encoding="utf-8") as log:
    log.write(json.dumps(sys.argv[1:]) + "\n")
print("fake claude: " + " ".join(sys.argv[1:]))
fail = os.environ.get("FAKE_CLAUDE_FAIL_ON")
if fail and fail in sys.argv[1:]:
    print("fake claude: refused", file=sys.stderr)
    sys.exit(3)
'''

needs_posix = pytest.mark.skipif(os.name == "nt", reason="the fake claude is a POSIX script")


def _path_without_claude() -> str:
    """PATH minus every folder that holds a `claude`, so git still runs
    but a real Claude Code CLI can never be reached."""
    dirs = os.environ.get("PATH", "").split(os.pathsep)
    return os.pathsep.join(d for d in dirs if d and not shutil.which("claude", path=d))


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    """A `claude` that logs its argv, the only one on PATH."""
    bin_dir = tmp_path / "fake-claude-bin"
    bin_dir.mkdir()
    script = bin_dir / "claude"
    script.write_text(f"#!{sys.executable}\n{FAKE_CLAUDE}")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join([str(bin_dir), _path_without_claude()]))
    assert shutil.which("claude") == str(script)
    log = bin_dir / "calls.jsonl"

    def calls():
        if not log.exists():
            return []
        return [json.loads(ln) for ln in log.read_text().splitlines()]

    return calls


def run_install(m, *args, ask=None, interactive=None):
    """(exit code, output) of install_plugins as machine m."""
    buf = io.StringIO()
    with redirect_stdout(buf), redirect_stderr(buf):
        try:
            rc = doctor.install_plugins(m.ctx(), list(args), ask=ask, interactive=interactive)
        except Fail as err:
            print(f"claude-sync: {err.message}\n{err.hint}")
            rc = 1
    return rc, buf.getvalue()


EXPECTED_ORDER = [
    ["plugin", "marketplace", "add", "anthropics/claude-plugins-official"],
    ["plugin", "marketplace", "add", "example-org/market-a"],
    ["plugin", "install", "helper@claude-plugins-official"],
    ["plugin", "install", "tool@market-a"],
]


@needs_posix
def test_install_plugins_declined_runs_nothing(world, fake_claude):
    a = _plugin_machine(world)
    asked = []
    rc, out = run_install(a, ask=lambda p: asked.append(p) or "n", interactive=True)
    assert rc == 1, out
    assert asked == ["Run them? [y/N] "]
    assert "Aborted: nothing was installed." in out
    assert cmd("install", "tool@market-a") in out  # shown before the question
    assert fake_claude() == []


@needs_posix
def test_install_plugins_yes_answer_runs_marketplaces_first_in_order(world, fake_claude):
    a = _plugin_machine(world)
    rc, out = run_install(a, ask=lambda p: "y", interactive=True)
    assert rc == 0, out
    assert fake_claude() == EXPECTED_ORDER
    assert "off@market-a" not in out
    assert "fake claude: plugin install tool@market-a" in out


@needs_posix
def test_install_plugins_with_yes_flag_does_not_ask(world, fake_claude):
    a = _plugin_machine(world)

    def never(prompt):
        raise AssertionError("asked despite --yes")

    rc, out = run_install(a, "--yes", ask=never, interactive=False)
    assert rc == 0, out
    assert fake_claude() == EXPECTED_ORDER


@needs_posix
def test_install_plugins_refuses_without_a_terminal_or_yes(world, fake_claude):
    a = _plugin_machine(world)
    rc, out = world.cli(a, "doctor", "install-plugins", stdin="y\n")
    assert rc == 1, out
    assert "Refused: no terminal to ask on and no --yes" in out
    assert fake_claude() == []


@needs_posix
def test_install_plugins_through_the_cli_with_yes(world, fake_claude):
    a = _plugin_machine(world)
    rc, out = world.cli(a, "doctor", "install-plugins", "--yes")
    assert rc == 0, out
    assert fake_claude() == EXPECTED_ORDER


@needs_posix
@pytest.mark.parametrize("argv", [["--force"], ["--yes", "--yes"], ["yes"]])
def test_install_plugins_bad_arguments_print_usage(world, fake_claude, argv):
    a = _plugin_machine(world)
    rc, out = world.cli(a, "doctor", "install-plugins", *argv)
    assert rc == 1
    assert out.startswith("Usage: ")
    assert fake_claude() == []


@needs_posix
def test_install_plugins_stops_at_the_first_failure(world, fake_claude, monkeypatch):
    a = _plugin_machine(world)
    monkeypatch.setenv("FAKE_CLAUDE_FAIL_ON", "example-org/market-a")
    rc, out = run_install(a, "--yes")
    assert rc == 1, out
    assert fake_claude() == EXPECTED_ORDER[:2]
    assert f"{cmd('marketplace add', 'example-org/market-a')} failed (exit 3); stopped there." in out
    assert f"Succeeded: {cmd('marketplace add', 'anthropics/claude-plugins-official')}" in out
    assert f"Not run: {cmd('install', 'helper@claude-plugins-official')}" in out
    assert f"Not run: {cmd('install', 'tool@market-a')}" in out
    assert "fake claude: refused" in out


def test_install_plugins_without_the_claude_cli_fails(world, monkeypatch):
    a = _plugin_machine(world)
    monkeypatch.setenv("PATH", _path_without_claude())
    assert shutil.which("claude") is None
    rc, out = run_install(a, "--yes")
    assert rc == 1, out
    assert "the claude CLI is not on PATH, so nothing was installed." in out


@needs_posix
def test_install_plugins_skips_what_the_check_only_notes(world, fake_claude):
    settings = {
        "enabledPlugins": {
            "thing@nowhere": True,
            "x@synced": True,
            "p@m-git-no-suffix": True,
            "--evil@market-a": True,
            "ok@market-a": True,
        },
        "extraKnownMarketplaces": {
            "market-a": {"source": {"source": "github", "repo": "example-org/market-a"}},
            "m-git-no-suffix": {"source": {"source": "git", "url": "https://git.example.com/team/plugins"}},
            "claudeai-org": {"source": {"source": "github", "repo": "example-org/x"}},
        },
    }
    a = _plugin_machine(world, settings=settings)
    install_plugins(a, marketplaces=("market-a",))
    rc, out = run_install(a, "--yes")
    assert rc == 0, out
    assert fake_claude() == [["plugin", "install", "ok@market-a"]]
    assert "its marketplace nowhere is not declared" in out
    assert "m-git-no-suffix: no add command" in out
    assert "p@m-git-no-suffix: its marketplace m-git-no-suffix is not on this machine" in out
    assert sum("thing@nowhere" in ln for ln in out.splitlines()) == 1  # its note, once
    assert "--evil@market-a: it starts with '-'" in out
    assert "claudeai-org" not in out


@needs_posix
def test_install_plugins_with_nothing_missing_runs_nothing(world, fake_claude):
    a = _plugin_machine(world)
    install_plugins(
        a, "tool@market-a", "helper@claude-plugins-official",
        marketplaces=("market-a", "claude-plugins-official"),
    )  # fmt: skip
    rc, out = run_install(a)
    assert rc == 0, out
    assert "nothing to install" in out
    assert fake_claude() == []


@needs_posix
def test_install_plugins_refuses_on_an_unknown_file_shape(world, fake_claude):
    a = _plugin_machine(world)
    install_plugins(a)
    (a.claude / "plugins" / "installed_plugins.json").write_bytes(b"[]")
    rc, out = run_install(a, "--yes")
    assert rc == 1, out
    assert "Plugin check skipped" in out and "Nothing was installed." in out
    assert fake_claude() == []


@needs_posix
def test_plain_doctor_and_remediate_never_call_the_claude_cli(world, fake_claude):
    a = _plugin_machine(world)
    for argv in (["doctor"], ["doctor", "remediate"]):
        rc, out = world.cli(a, *argv, stdin="y\n")
        assert rc == 0, out
        assert "claude plugin install" in out, out
    assert fake_claude() == []
