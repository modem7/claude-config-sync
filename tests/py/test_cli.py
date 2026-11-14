"""Tests for claude_sync.cli (the command line), ported from the cases of
tests/sync_cli.bats and tests/profile.bats that go through claude-sync.sh
itself: dispatch, usage, the feature-branch skip, the no-conf refusal,
doctor through the real nested sync, and how failures end the run.
World.cli runs cli.main in-process as one machine. Placeholder hostnames
only: this file is published.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from helpers import git, has_ref, merge_sync_pr

from claude_sync import cli, gitflow, pr
from claude_sync.lock import SyncLock

REPO_ROOT = Path(__file__).resolve().parents[2]

USAGE = """\
Usage: claude-sync.sh [push|pull|sync|bootstrap|doctor [remediate|install-plugins [--yes]]
                       |override add|remove|list [JQ_PATH]
                       |profile list|show NAME|copy FROM [--force] [--yes]|use NAME
                       |tier [show|basic|full [PROJECT_PATH]]|status --line [--json]]
  sync       capture local changes onto sync/<hostname>, merge the default branch in, apply, push, open/update its PR
  push       capture onto sync/<hostname>, push it and open/update its PR (no apply)
  pull       bring in the default branch (keeping this machine's unmerged changes) and apply
  Nothing is ever pushed to the default branch; changes reach other machines when the PR is merged.
  The PR step needs gh, logged in. CLAUDE_SYNC_GH=<path> picks the gh binary;
  CLAUDE_SYNC_GH_REPO=owner/repo sets the repo when origin's URL doesn't show it.
  Needs git 2.40+. A second run waits CLAUDE_SYNC_LOCK_WAIT seconds (default 10) for the first.
  Before apply overwrites or deletes a local file that differs, it copies it to
  ~/.claude/.claude-sync-backups/<UTC time>/. CLAUDE_SYNC_BACKUPS=N keeps the newest N (default 10; 0: none).
  e.g. claude-sync.sh override add '.enabledPlugins["name@marketplace"]'
       claude-sync.sh profile use other-host   # follow machines/other-host/ (agents, hooks, skills, CLAUDE.md)
       claude-sync.sh tier basic               # stop syncing memory here (TIER= in machines/<hostname>.conf)
  tier: basic syncs CLAUDE.md, settings.json, agents, hooks and skills; full (the default) adds memory.
  'tier full' needs the primary project path once (PROJECT_PATH) if the conf has none.
  status --line  one line when this machine needs attention (last hook sync failed, a doctor
             problem, its PR waiting over CLAUDE_SYNC_PR_DAYS days (default 3; 0: off), a tier change),
             else nothing. Local only (no fetch, no gh, no lock); always exits 0. The SessionStart hook runs it.
             --json prints the line as one hook JSON object (systemMessage for you, additionalContext for Claude).
  doctor install-plugins  run the claude plugin commands doctor lists (marketplaces first), after a
             y/N prompt; --yes skips it, no terminal and no --yes is refused. Stops at the first failure.
             Plain doctor and doctor remediate never install anything.
"""


def head_subject(repo: Path) -> str:
    return git("-C", repo, "log", "-1", "--format=%s").stdout.strip()


# ── usage and dispatch ────────────────────────────────────────────────────


def test_unknown_command_prints_usage_returns_1(world, capsys):
    a = world.machine("a")
    assert cli.main(["bogus"], a.env()) == 1
    captured = capsys.readouterr()
    assert captured.err == USAGE
    assert captured.out == ""


@pytest.mark.parametrize(
    "argv",
    [
        ["override", "add"],
        ["override", "remove"],
        ["override", "bogus"],
        ["profile", "show"],
        ["profile", "use"],
        ["profile", "bogus"],
        ["profile", "copy"],
        ["profile", "copy", "-x", "host-b"],
        ["profile", "copy", "host-b", "host-c"],
    ],
)
def test_bad_subcommand_arguments_print_usage_and_return_1(world, argv):
    a = world.machine("a")
    rc, out = world.cli(a, *argv)
    assert rc == 1
    assert out == USAGE
    assert not has_ref(a.repo, "refs/heads/sync/host-a")


def test_no_command_runs_sync(world, fake_gh):
    a = world.machine("a")
    (a.claude / "CLAUDE.md").write_bytes(b"# edited\n")
    rc, out = world.cli(a)
    assert rc == 0, out
    assert git("-C", world.origin, "show", "sync/host-a:dot-claude/CLAUDE.md").stdout == "# edited\n"


def test_push_captures_onto_the_sync_branch_and_opens_a_pr_never_touching_master(world, fake_gh):
    a = world.machine("a")
    (a.claude / "CLAUDE.md").write_bytes(b"# from machine A\n")
    memory = a.claude / "projects" / "-home-user-project" / "memory"
    memory.mkdir(parents=True, exist_ok=True)
    (memory / "MEMORY.md").write_bytes(b"machine A memory\n")
    master = git("-C", world.origin, "rev-parse", "master").stdout

    rc, out = world.cli(a, "push")
    assert rc == 0, out
    assert (a.repo / "dot-claude" / "CLAUDE.md").read_bytes() == b"# from machine A\n"
    assert git("-C", world.origin, "show", "sync/host-a:dot-claude/CLAUDE.md").stdout == "# from machine A\n"
    assert git("-C", world.origin, "show", "sync/host-a:memory/MEMORY.md").stdout == "machine A memory\n"
    assert git("-C", world.origin, "rev-parse", "master").stdout == master
    log = (fake_gh.parent / "calls.log").read_text(encoding="utf-8")
    assert "gh pr create --repo test/repo --base master --head sync/host-a " in log
    assert git("-C", a.repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "master"


@pytest.mark.parametrize(
    "argv",
    [["push"], ["sync"], ["bootstrap"], ["override", "add", ".theme"], ["override", "list"],
     ["profile", "use", "host-b"], ["profile", "copy", "host-b", "--yes"]],
)  # fmt: skip
def test_skip_on_feature_branch_returns_0(world, argv):
    a = world.machine("a")
    (a.claude / "CLAUDE.md").write_bytes(b"# local edit\n")
    git("-C", a.repo, "checkout", "-q", "-b", "some-feature-branch")
    before = head_subject(a.repo)
    rc, out = world.cli(a, *argv)
    assert rc == 0, out
    assert "claude-sync: sync SKIPPED, nothing was captured, pushed or applied." in out
    assert (
        "  Why: claude-config is on branch 'some-feature-branch', not its default branch 'master',"
        in out
    )
    assert "Switch back to the default branch to sync." in out
    assert head_subject(a.repo) == before
    assert not has_ref(a.repo, "refs/heads/sync/host-a")
    assert (a.repo / "dot-claude" / "CLAUDE.md").read_bytes() == b"# shared\n"


def test_profile_list_and_show_work_on_a_feature_branch(world):
    a = world.machine("a")
    git("-C", a.repo, "checkout", "-q", "-b", "some-feature")
    for argv in (["profile"], ["profile", "list"], ["profile", "show", "host-a"]):
        rc, out = world.cli(a, *argv)
        assert rc == 0, out
        assert "SKIPPED" not in out
    assert "host-a uses its own profile 'host-a':" in world.cli(a, "profile")[1]


def test_pull_on_a_feature_branch_applies_without_skipping(world):
    a = world.machine("a")
    git("-C", a.repo, "checkout", "-q", "-b", "some-feature")
    (a.repo / "dot-claude" / "CLAUDE.md").write_bytes(b"# from the feature branch\n")
    git("-C", a.repo, "commit", "-q", "-am", "feature edit")
    rc, out = world.cli(a, "pull")
    assert rc == 0, out
    assert "SKIPPED" not in out
    assert (a.claude / "CLAUDE.md").read_bytes() == b"# from the feature branch\n"


def test_pull_on_a_fresh_machine_applies_what_another_pushed(world):
    a = world.machine("a")
    (a.claude / "CLAUDE.md").write_bytes(b"# from machine A\n")
    rc, out = world.cli(a, "push")
    assert rc == 0, out
    merge_sync_pr(world.origin, "host-a")
    b = world.machine("b", pull=False)
    rc, out = world.cli(b, "pull")
    assert rc == 0, out
    assert (b.claude / "CLAUDE.md").read_bytes() == b"# from machine A\n"


def test_push_refuses_to_run_without_a_machine_conf(world):
    c = world.machine("c", pull=False)  # host-c has no machines/host-c.conf
    rc, out = world.cli(c, "push")
    assert rc == 1
    assert f"claude-sync: No machine config at {c.repo / 'machines' / 'host-c.conf'}. Run install.sh first." in out


def test_no_git_repo_says_run_install(world, tmp_path):
    a = world.machine("a")
    env = {**a.env(), "CLAUDE_SYNC_REPO_DIR": str(tmp_path / "nowhere")}
    rc, out = world.cli(a, "sync", env=env)
    assert rc == 1
    assert f"No git repo at {tmp_path / 'nowhere'}. Run install.sh first." in out


def test_an_invalid_lock_wait_is_a_clear_error(world):
    a = world.machine("a")
    env = {**a.env(), "CLAUDE_SYNC_LOCK_WAIT": "soon"}
    rc, out = world.cli(a, "sync", env=env)
    assert rc == 1
    assert "CLAUDE_SYNC_LOCK_WAIT must be a number of seconds, not 'soon'." in out


def test_the_lock_wait_comes_from_the_env(world):
    a = world.machine("a")
    env = {**a.env(), "CLAUDE_SYNC_LOCK_WAIT": "0"}
    with SyncLock(a.repo / ".git" / "claude-sync.flock", 0, repo_dir=a.repo):
        rc, out = world.cli(a, "sync", env=env)
    assert rc == 1
    assert f"another claude-sync run is still working on {a.repo} (waited 0s for its lock)." in out


def test_the_cli_binds_the_pr_state_to_github(world, monkeypatch):
    seen = []
    real = pr.bind_pr_state

    def spy(ctx):
        real(ctx)
        seen.append(ctx.pr_state("sync/host-a", "0" * 40))

    monkeypatch.setattr(pr, "bind_pr_state", spy)
    a = world.machine("a")
    world.cli_ok(a, "sync")
    assert seen and seen[0] == "nopr"  # the fake gh answered, not the "unknown" default


# ── doctor through the CLI ────────────────────────────────────────────────


def test_doctor_healthy_and_any_other_argument_is_a_plain_doctor(world):
    a = world.machine("a")
    world.cli_ok(a, "sync")
    for argv in (["doctor"], ["doctor", "please"]):
        rc, out = world.cli(a, *argv)
        assert rc == 0, out
        assert "No issues found - sync is healthy." in out


def test_doctor_remediate_runs_the_real_nested_sync(world):
    """sync_cli.bats: a legacy commit plus a conflicting merged change.
    The nested sync goes through cli.main, so its own failure is shown."""
    a = world.machine("a")
    world.cli_ok(a, "sync")
    world.commit_on_origin({"dot-claude/settings.json": b'{"theme":"light"}\n'}, "remote theme change")
    (a.claude / "settings.json").write_bytes(b'{"theme":"custom"}\n')
    (a.repo / "dot-claude" / "settings.json").write_bytes(b'{"theme":"custom"}\n')
    git("-C", a.repo, "commit", "-q", "-am", "sync: host-a legacy")
    (a.claude / ".claude-sync-applied-commit").write_text(
        git("-C", a.repo, "rev-parse", "HEAD").stdout, encoding="utf-8"
    )
    git("-C", a.repo, "fetch", "-q")

    rc, out = world.cli(a, "doctor")
    assert rc == 1
    assert "ahead" in out and "behind" in out

    rc, out = world.cli(a, "doctor", "remediate")
    assert rc == 1
    assert "moved 1 unpushed commit(s) from master onto sync/host-a" in out
    assert "real conflict" in out
    assert "Resolve manually" in out
    assert (a.claude / "settings.json").read_bytes() == b'{"theme":"custom"}\n'
    assert git("-C", a.repo, "log", "-1", "--format=%s", "sync/host-a").stdout.strip() == "sync: host-a legacy"


def test_doctor_remediate_nested_sync_that_succeeds(world):
    a = world.machine("a")
    world.cli_ok(a, "sync")
    # Live untouched since the last apply, so the nested sync skips capture
    # and brings the moved commit to live and out as a PR.
    (a.repo / "dot-claude" / "CLAUDE.md").write_bytes(b"# legacy\n")
    git("-C", a.repo, "commit", "-q", "-am", "sync: host-a legacy")
    rc, out = world.cli(a, "doctor", "remediate")
    assert "-> sync succeeded." in out, out
    assert (a.claude / "CLAUDE.md").read_bytes() == b"# legacy\n"
    assert git("-C", world.origin, "show", "sync/host-a:dot-claude/CLAUDE.md").stdout == "# legacy\n"


# ── profile.bats cases that need the CLI ──────────────────────────────────


def test_profile_use_is_skipped_on_a_feature_branch_and_list_still_works(world):
    world.seed_profiles()
    a = world.machine("a")
    (a.repo / "machines" / "host-a" / "agents").mkdir(parents=True)
    (a.repo / "machines" / "host-a" / "agents" / "helper.md").write_bytes(b"host-a helper\n")
    world.cli_ok(a, "sync")
    b = world.machine("b")
    git("-C", b.repo, "checkout", "-q", "-b", "some-feature")
    rc, out = world.cli_as(b, "profile", "use", "host-a")
    assert rc == 0
    assert "not its default branch" in out
    conf = (b.repo / "machines" / "host-b.conf").read_text(encoding="utf-8")
    assert "PROFILE=" not in conf
    rc, out = world.cli(b, "profile", "list")
    assert "host-b uses its own profile" in out


@pytest.mark.skipif(os.name == "nt", reason="skill links are junctions on Windows")
def test_a_disabled_skill_loses_its_link_after_a_cli_sync(world):
    """profile.bats skills case. wire_skill_links runs after sync_apply (and
    profile_apply), so the link of a skill the profile disables goes too."""
    world.seed_profiles()
    a = world.machine("a")
    assert (a.claude / "skills" / "other-skill").is_symlink()
    pa = a.repo / "machines" / "host-a" / "skills"
    (pa / "shared-skill").mkdir(parents=True)
    (pa / "only-a").mkdir()
    (pa / "shared-skill" / "SKILL.md").write_bytes(b"host-a skill\n")
    (pa / "only-a" / "SKILL.md").write_bytes(b"host-a only skill\n")
    (pa / "other-skill.disabled").write_bytes(b"")

    world.cli_ok(a, "sync")

    skills = a.agents / "skills"
    assert (skills / "shared-skill" / "SKILL.md").read_bytes() == b"host-a skill\n"
    assert not (skills / "shared-skill" / "notes.md").exists()
    assert (skills / "only-a" / "SKILL.md").read_bytes() == b"host-a only skill\n"
    assert (a.claude / "skills" / "only-a").is_symlink()
    assert not (skills / "other-skill").exists()
    assert not os.path.lexists(a.claude / "skills" / "other-skill")
    show = lambda p: git("-C", world.origin, "show", f"master:{p}", check=False)  # noqa: E731
    assert show("dot-agents/skills/shared-skill/SKILL.md").stdout == "shared skill\n"
    assert show("dot-agents/skills/other-skill/SKILL.md").returncode == 0
    assert show("dot-agents/skills/only-a/SKILL.md").returncode != 0


def test_skill_links_are_wired_after_sync_apply(world, monkeypatch):
    calls = []
    for mod, name in ((gitflow.syncfiles, "sync_apply"), (gitflow.profile, "profile_apply"),
                      (gitflow.skilllinks, "wire_skill_links")):  # fmt: skip
        real = getattr(mod, name)

        def wrapped(*args, _real=real, _name=name, **kw):
            calls.append(_name)
            return _real(*args, **kw)

        monkeypatch.setattr(mod, name, wrapped)
    a = world.machine("a")
    calls.clear()
    world.cli_ok(a, "pull")
    assert calls == ["sync_apply", "profile_apply", "wire_skill_links"]


# ── how a run ends ────────────────────────────────────────────────────────


def test_ctrl_c_returns_130_without_a_traceback(world, monkeypatch):
    def interrupted(*_a):
        raise KeyboardInterrupt

    a = world.machine("a")
    monkeypatch.setattr(gitflow, "begin_sync", interrupted)
    rc, out = world.cli(a, "sync")
    assert rc == 130
    assert "Traceback" not in out
    assert "claude-sync: stopped by Ctrl-C (exit 130)." in out


@pytest.mark.parametrize("code", [130, 143, 129])
def test_a_signal_exit_code_from_materialise_passes_through(world, monkeypatch, code):
    def signalled(_ctx):
        raise SystemExit(code)

    a = world.machine("a")
    monkeypatch.setattr(gitflow, "materialise", signalled)
    rc, out = world.cli(a, "sync")
    assert rc == code
    assert "Traceback" not in out
    by = {130: "Ctrl-C", 143: "SIGTERM", 129: "SIGHUP"}[code]
    assert out.count(f"claude-sync: stopped by {by} (exit {code}).") == 1


def test_a_signalled_nested_sync_ends_doctor_with_its_code_reported_once(world, monkeypatch):
    a = world.machine("a")
    world.cli_ok(a, "sync")
    (a.repo / "dot-claude" / "CLAUDE.md").write_bytes(b"# legacy\n")
    git("-C", a.repo, "commit", "-q", "-am", "sync: host-a legacy")

    def signalled(_ctx):
        raise SystemExit(143)

    monkeypatch.setattr(gitflow, "materialise", signalled)
    rc, out = world.cli(a, "doctor", "remediate")
    assert rc == 143
    assert out.count("stopped by SIGTERM (exit 143)") == 1
    assert "-> sync succeeded." not in out and "still failing" not in out


def test_an_unexpected_error_is_short_and_says_how_to_report(world, monkeypatch):
    def broken(*_a):
        raise ZeroDivisionError("division by zero")

    a = world.machine("a")
    monkeypatch.setattr(gitflow, "begin_sync", broken)
    rc, out = world.cli(a, "sync")
    assert rc == 1
    assert "Traceback" not in out
    assert "claude-sync: unexpected error: ZeroDivisionError: division by zero" in out
    assert "This is a bug in claude-sync" in out


def test_an_os_error_is_a_message_not_a_traceback(world, monkeypatch):
    def denied(*_a):
        raise PermissionError(13, "Permission denied", "/some/path")

    a = world.machine("a")
    monkeypatch.setattr(gitflow, "begin_sync", denied)
    rc, out = world.cli(a, "sync")
    assert rc == 1
    assert "Traceback" not in out
    assert "claude-sync: [Errno 13] Permission denied: '/some/path'" in out


def test_python_m_claude_sync_runs_the_cli(world):
    """__main__ wiring: python -m claude_sync <cmd> exits with cli.main's code."""
    a = world.machine("a")
    result = subprocess.run(
        [sys.executable, "-m", "claude_sync", "bogus"],
        cwd=REPO_ROOT, env=a.env(), capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert result.returncode == 1
    assert result.stderr == USAGE
    assert "Traceback" not in result.stderr

    result = subprocess.run(
        [sys.executable, "-m", "claude_sync", "doctor"],
        cwd=REPO_ROOT, env=a.env(), capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
    assert "healthy" in result.stdout


@pytest.mark.skipif(os.name == "nt", reason="POSIX signals")
@pytest.mark.parametrize(("sig", "code", "by"), [("SIGINT", 130, "Ctrl-C"), ("SIGTERM", 143, "SIGTERM")])
def test_a_real_process_stopped_by_a_signal_exits_with_its_code(world, sig, code, by):
    """The real entry point, stopped while it waits for the lock: the bash
    exit code, a line saying so, no traceback, and the lock let go."""
    import signal
    import time

    a = world.machine("a")
    lock = a.repo / ".git" / "claude-sync.flock"
    with SyncLock(lock, 0, repo_dir=a.repo):
        proc = subprocess.Popen(
            [sys.executable, "-m", "claude_sync", "sync"],
            cwd=REPO_ROOT, env={**a.env(), "CLAUDE_SYNC_LOCK_WAIT": "30"},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )  # fmt: skip
        time.sleep(2)  # well into the wait for the lock
        assert proc.poll() is None
        proc.send_signal(getattr(signal, sig))
        _out, err = proc.communicate(timeout=20)
    assert proc.returncode == code, err
    assert f"claude-sync: stopped by {by} (exit {code})." in err
    assert "Traceback" not in err


def test_non_ascii_output_to_a_narrow_stream_does_not_crash(world):
    """A redirected stdout/stderr in a legacy code page (PYTHONIOENCODING
    stands in for one) gets backslash escapes, not a UnicodeEncodeError."""
    a = world.machine("a")
    env = {**a.env(), "PYTHONIOENCODING": "ascii", "CLAUDE_SYNC_HOSTNAME": "höst"}
    result = subprocess.run(
        [sys.executable, "-m", "claude_sync", "doctor"],
        cwd=REPO_ROOT, env=env, capture_output=True, check=False,
    )  # fmt: skip
    err = result.stderr.decode("ascii")
    assert "Traceback" not in err and "UnicodeEncodeError" not in err
    assert "h\\xf6st" in err


@pytest.mark.parametrize("host", ["bad/host", "..", "a b", "x:y"])
def test_an_invalid_claude_sync_hostname_is_refused(world, host):
    a = world.machine("a")
    rc, out = world.cli(a, "doctor", env={**a.env(), "CLAUDE_SYNC_HOSTNAME": host})
    assert rc == 1
    assert f"claude-sync: Not a valid hostname: {host}" in out
    assert "CLAUDE_SYNC_HOSTNAME" in out
    assert "Traceback" not in out


def test_a_failure_message_and_hint_are_redacted(world, monkeypatch):
    a = world.machine("a")

    def leaky(*_a, **_k):
        raise cli.Fail(
            "push to https://user:s3cret@example.invalid/x.git failed",
            hint="token: ghp_abcdefghijklmnopqrstuvwxyz0123",
        )

    monkeypatch.setattr(gitflow, "begin_sync", leaky)
    rc, out = world.cli(a, "sync")
    assert rc == 1
    assert "s3cret" not in out and "ghp_" not in out
    assert "https://example.invalid/x.git" in out
