"""Tests for claude_sync.install, ported from the install.sh cases of
tests/sync_cli.bats and tests/profile.bats, plus the installer's new
checks. install.main runs in-process against the `world` origin and the
fake gh; the three tool steps are replaced by recorders, so no test runs
npm, pip or a package manager. Placeholder hostnames only: this file is
published.
"""

from __future__ import annotations

import io
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest
from helpers import git, has_ref, make_dir_link, rev
from test_subcommands import seeded  # noqa: F401 - the fixture

from claude_sync import install, tools
from claude_sync.machineconf import write_machine_conf


@pytest.fixture(autouse=True)
def tool_calls(monkeypatch):
    """Every tool step is a recorder: nothing real is ever installed."""
    calls: list[str] = []
    for name in ("install_npm_tools", "install_gh_tools", "install_python_tools"):
        monkeypatch.setattr(tools, name, lambda *a, _n=name, **k: calls.append(_n))
    return calls


@pytest.fixture(autouse=True)
def _cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


@pytest.fixture(autouse=True)
def _not_windows(monkeypatch):
    """The ported cases write project paths that do not exist; on a Windows
    runner they would fall back to home. The Windows tests set it back."""
    monkeypatch.setattr(install, "IS_WINDOWS", False)


def run_install(world, host, repo, claude, agents, *argv, stdin="", project="/home/user/project", **extra):
    """bats run_install: install.main as one machine; (exit code, output).
    `project=None` leaves CLAUDE_SYNC_PROJECT_PATH unset."""
    Path(claude).mkdir(parents=True, exist_ok=True)
    (Path(agents) / "skills").mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "CLAUDE_SYNC_REPO_URL": str(world.origin),
        "CLAUDE_SYNC_REPO_DIR": str(repo),
        "CLAUDE_SYNC_HOSTNAME": host,
        "CLAUDE_SYNC_CLAUDE_HOME": str(claude),
        "CLAUDE_SYNC_AGENTS_HOME": str(agents),
        "CLAUDE_SYNC_LOCK_WAIT": "2",
        **extra,
    }
    env.pop("CLAUDE_SYNC_PROJECT_PATH", None)
    if project is not None:
        env["CLAUDE_SYNC_PROJECT_PATH"] = project
    buf = io.StringIO()
    saved, sys.stdin = sys.stdin, io.StringIO(stdin)
    try:
        with redirect_stdout(buf), redirect_stderr(buf):
            rc = install.main(list(argv), env)
    finally:
        sys.stdin = saved
    return rc, buf.getvalue()


def fresh(tmp_path, name):
    """A fresh machine's paths, with a little live config."""
    claude, agents = tmp_path / f"{name}_claude", tmp_path / f"{name}_agents"
    claude.mkdir()
    (claude / "CLAUDE.md").write_bytes(b"# fresh machine\n")
    (claude / "settings.json").write_bytes(b"{}\n")
    return tmp_path / f"{name}_repo", claude, agents


def conf_text(repo, host):
    return (Path(repo) / "machines" / f"{host}.conf").read_text(encoding="utf-8")


def origin_show(world, ref, path):
    return git("-C", world.origin, "show", f"{ref}:{path}", check=False)


# ── resolve_project_path ─────────────────────────────────────────────────


def test_project_path_preferred_when_exists(tmp_path):
    (tmp_path / "work").mkdir()
    assert install.resolve_project_path(str(tmp_path / "work"), tmp_path / "h") == str(tmp_path / "work")


def test_project_path_falls_back_to_home_when_missing(tmp_path):
    home = tmp_path / "h"
    assert install.resolve_project_path(str(tmp_path / "nope"), home) == str(home)
    assert install.resolve_project_path(None, home) == str(home)
    assert install.resolve_project_path("", home) == str(home)
    (tmp_path / "file").write_bytes(b"")
    assert install.resolve_project_path(str(tmp_path / "file"), home) == str(home)


def test_install_on_windows_falls_back_to_home_for_a_missing_project(world, tmp_path, monkeypatch):
    monkeypatch.setattr(install, "IS_WINDOWS", True)
    repo, claude, agents = fresh(tmp_path, "win")
    rc, out = run_install(world, "host-win", repo, claude, agents, project=str(tmp_path / "no-such"))
    assert rc == 0, out
    assert conf_text(repo, "host-win") == f"PRIMARY_PROJECT_PATH={Path.home()}\n"
    assert "does not exist" in out


def test_install_on_windows_keeps_an_existing_project(world, tmp_path, monkeypatch):
    monkeypatch.setattr(install, "IS_WINDOWS", True)
    repo, claude, agents = fresh(tmp_path, "win")
    (tmp_path / "proj").mkdir()
    rc, out = run_install(world, "host-win", repo, claude, agents, project=str(tmp_path / "proj"))
    assert rc == 0, out
    assert conf_text(repo, "host-win") == f"PRIMARY_PROJECT_PATH={tmp_path / 'proj'}\n"


def test_install_on_windows_stores_a_git_bash_path_in_native_form(world, tmp_path, monkeypatch):
    monkeypatch.setattr(install, "IS_WINDOWS", True)
    seen = []
    monkeypatch.setattr(install, "resolve_project_path", lambda p, home: seen.append(p) or p)
    repo, claude, agents = fresh(tmp_path, "win")
    rc, out = run_install(world, "host-win", repo, claude, agents, project="/z/work")
    assert rc == 0, out
    assert seen == ["Z:\\work"]
    assert conf_text(repo, "host-win") == "PRIMARY_PROJECT_PATH=Z:\\work\n"


def test_install_on_windows_names_the_missing_path_in_native_form(world, tmp_path, monkeypatch):
    monkeypatch.setattr(install, "IS_WINDOWS", True)
    repo, claude, agents = fresh(tmp_path, "win")
    rc, out = run_install(world, "host-win", repo, claude, agents, project="/q/no-such-claude-sync-dir")
    assert rc == 0, out
    assert "The project path Q:\\no-such-claude-sync-dir does not exist" in out
    assert conf_text(repo, "host-win") == f"PRIMARY_PROJECT_PATH={Path.home()}\n"


# ── fresh install (sync_cli.bats) ────────────────────────────────────────


def test_fresh_install_clones_creates_conf_and_seeds_repo(world, tmp_path, tool_calls):
    repo, claude, agents = fresh(tmp_path, "fresh")
    rc, out = run_install(world, "host-fresh", repo, claude, agents, project="/home/user/fresh")
    assert rc == 0, out
    assert (repo / ".git").exists()
    assert conf_text(repo, "host-fresh") == "PRIMARY_PROJECT_PATH=/home/user/fresh\n"
    assert (repo / "dot-claude" / "CLAUDE.md").is_file()
    assert tool_calls == ["install_npm_tools", "install_gh_tools", "install_python_tools"]
    # The first sync went out on its own branch with a PR.
    assert has_ref(world.origin, "refs/heads/sync/host-fresh")
    assert "host-fresh.conf" in git("-C", world.origin, "ls-tree", "-r", "--name-only", "sync/host-fresh").stdout
    assert "gh pr create" in world.gh_log.read_text(encoding="utf-8")


def test_clone_keeps_line_endings(world, tmp_path):
    repo, claude, agents = fresh(tmp_path, "crlf")
    rc, out = run_install(world, "host-crlf", repo, claude, agents)
    assert rc == 0, out
    assert git("-C", repo, "config", "--local", "core.autocrlf").stdout.strip() == "false"


def test_project_path_defaults_to_home_project_when_the_prompt_is_empty(world, tmp_path):
    repo, claude, agents = fresh(tmp_path, "default")
    rc, out = run_install(world, "host-default", repo, claude, agents, project=None, stdin="\n")
    assert rc == 0, out
    assert conf_text(repo, "host-default") == f"PRIMARY_PROJECT_PATH={Path.home() / 'project'}\n"


def test_project_path_prompt_answer_and_end_of_input(world, tmp_path):
    repo, claude, agents = fresh(tmp_path, "typed")
    rc, out = run_install(world, "host-typed", repo, claude, agents, project=None, stdin="/srv/work\n")
    assert rc == 0, out
    assert conf_text(repo, "host-typed") == "PRIMARY_PROJECT_PATH=/srv/work\n"
    repo2, claude2, agents2 = fresh(tmp_path, "eof")
    rc, out = run_install(world, "host-eof", repo2, claude2, agents2, project=None, stdin="")
    assert rc == 0, out
    assert conf_text(repo2, "host-eof") == f"PRIMARY_PROJECT_PATH={Path.home() / 'project'}\n"


@pytest.mark.parametrize("form", ["two", "equals"])
def test_project_path_flag_wins_over_the_environment(world, tmp_path, form):
    repo, claude, agents = fresh(tmp_path, "flag")
    args = ["--project-path", "/srv/flag"] if form == "two" else ["--project-path=/srv/flag"]
    rc, out = run_install(world, "host-flag", repo, claude, agents, *args, project="/srv/env")
    assert rc == 0, out
    assert conf_text(repo, "host-flag") == "PRIMARY_PROJECT_PATH=/srv/flag\n"


def test_install_never_touches_real_home(world, fake_home, real_home, monkeypatch):
    """No CLAUDE_SYNC_REPO_DIR / _CLAUDE_HOME / _AGENTS_HOME: every default
    must come from the (fake) HOME, never the real one."""
    for key in ("CLAUDE_SYNC_REPO_DIR", "CLAUDE_SYNC_CLAUDE_HOME", "CLAUDE_SYNC_AGENTS_HOME"):
        monkeypatch.delenv(key, raising=False)
    assert fake_home.root != real_home
    (fake_home.claude / "CLAUDE.md").write_bytes(b"# fake home\n")
    real_repo = real_home / "claude-config"
    had_repo = real_repo.exists()
    real_claude = real_home / ".claude"
    before = sorted(os.listdir(real_claude)) if real_claude.is_dir() else None
    env = {k: v for k, v in os.environ.items()}
    env.update({"CLAUDE_SYNC_REPO_URL": str(world.origin), "CLAUDE_SYNC_HOSTNAME": "host-home"})
    buf = io.StringIO()
    with redirect_stdout(buf), redirect_stderr(buf):
        rc = install.main([], env)
    assert rc == 0, buf.getvalue()
    assert (fake_home.root / "claude-config" / "machines" / "host-home.conf").is_file()
    assert (fake_home.claude / "skills").is_dir()  # wire_skill_links ran on the default CLAUDE_HOME
    # bootstrap adopted the shared CLAUDE.md into the default (fake) CLAUDE_HOME
    assert (fake_home.claude / "CLAUDE.md").read_bytes() == b"# shared\n"
    assert real_repo.exists() == had_repo
    assert (sorted(os.listdir(real_claude)) if real_claude.is_dir() else None) == before


# ── --repair (sync_cli.bats) ─────────────────────────────────────────────


def _broken_machine(world, tmp_path):
    """A machine that ran once but pushed the wrong project path to the
    shared repo, and left a stale skill link."""
    repo, claude, agents = fresh(tmp_path, "broken")
    git("clone", "-q", "-c", "core.autocrlf=false", world.origin, repo)
    write_machine_conf(repo, "host-broken", "/wrong/path")
    git("-C", repo, "add", "-A")
    git("-C", repo, "commit", "-q", "-m", "seed broken conf")
    git("-C", repo, "push", "-q", "origin", "master")
    (agents / "skills" / "stale-skill").mkdir(parents=True)
    (claude / "skills").mkdir()
    os.symlink(agents / "skills" / "stale-skill", claude / "skills" / "stale-skill")
    return repo, claude, agents


@pytest.mark.skipif(os.name == "nt", reason="symlink needs privileges on Windows")
def test_repair_resets_a_misconfigured_machine_with_the_correct_project_path(world, tmp_path):
    repo, claude, agents = _broken_machine(world, tmp_path)
    master_before = rev(world.origin, "master")
    rc, out = run_install(world, "host-broken", repo, claude, agents, "--repair", project="/correct/path")
    assert rc == 0, out
    assert "Repairing: resetting local sync state for host-broken..." in out
    assert conf_text(repo, "host-broken") == "PRIMARY_PROJECT_PATH=/correct/path\n"
    shown = origin_show(world, "sync/host-broken", "machines/host-broken.conf").stdout
    assert "PRIMARY_PROJECT_PATH=/correct/path" in shown
    assert rev(world.origin, "master") == master_before
    assert not os.path.lexists(claude / "skills" / "stale-skill")


@pytest.mark.skipif(os.name == "nt", reason="symlink needs privileges on Windows")
def test_repair_leaves_no_stale_skill_link_after_the_sync(world, tmp_path):
    """bats: the stale link is gone; its untracked folder gets no new link."""
    repo, claude, agents = _broken_machine(world, tmp_path)
    rc, out = run_install(world, "host-broken", repo, claude, agents, "--repair", project="/correct/path")
    assert rc == 0, out
    assert not os.path.lexists(claude / "skills" / "stale-skill")


@pytest.mark.skipif(os.name == "nt", reason="symlink needs privileges on Windows")
def test_repair_removes_the_skill_links_it_made_before_syncing(world, tmp_path, monkeypatch):
    from claude_sync import cli

    repo, claude, agents = _broken_machine(world, tmp_path)
    (claude / "skills" / "real-skill").mkdir()
    os.symlink(tmp_path, claude / "skills" / "foreign")
    seen = []
    monkeypatch.setattr(cli, "main", lambda argv, env: seen.append(argv) or 0)
    rc, out = run_install(world, "host-broken", repo, claude, agents, "--repair")
    assert rc == 0, out
    assert seen == [["bootstrap"]]
    assert not os.path.lexists(claude / "skills" / "stale-skill")
    assert (claude / "skills" / "real-skill").is_dir()
    assert os.path.islink(claude / "skills" / "foreign")
    assert (agents / "skills" / "stale-skill").is_dir()


@pytest.mark.skipif(os.name == "nt", reason="symlink needs privileges on Windows")
def test_repair_goes_through_pr(world, tmp_path):
    repo, claude, agents = _broken_machine(world, tmp_path)
    master_before = rev(world.origin, "master")
    rc, out = run_install(world, "host-broken", repo, claude, agents, "--reinstall", project="/fixed")
    assert rc == 0, out
    assert rev(world.origin, "master") == master_before
    assert "PRIMARY_PROJECT_PATH=/fixed" in origin_show(world, "sync/host-broken", "machines/host-broken.conf").stdout
    assert "PRIMARY_PROJECT_PATH=/wrong/path" in origin_show(world, "master", "machines/host-broken.conf").stdout
    assert "gh pr create" in world.gh_log.read_text(encoding="utf-8")
    # The clone stays on the default branch.
    assert git("-C", repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "master"


def test_repair_works_from_a_cwd_inside_the_repo(world, tmp_path, monkeypatch):
    repo, claude, agents = fresh(tmp_path, "cwd")
    git("clone", "-q", "-c", "core.autocrlf=false", world.origin, repo)
    (repo / "machines").mkdir(exist_ok=True)
    monkeypatch.chdir(repo / "machines")
    rc, out = run_install(world, "host-cwd", repo, claude, agents, "--repair", project="/some/path")
    assert rc == 0, out
    assert (repo / "machines" / "host-cwd.conf").is_file()
    assert not Path.cwd().resolve().is_relative_to(repo.resolve())


def test_repair_refuses_when_the_clone_has_uncommitted_changes(world, tmp_path):
    repo, claude, agents = fresh(tmp_path, "dirty")
    git("clone", "-q", "-c", "core.autocrlf=false", world.origin, repo)
    (repo / "dot-claude" / "CLAUDE.md").write_bytes(b"# local edit\n")
    rc, out = run_install(world, "host-dirty", repo, claude, agents, "--repair")
    assert rc == 1
    assert "pending sync PR" in out
    assert "Do not use git stash or git reset --hard" in out
    assert (repo / ".git").exists()
    assert (repo / "dot-claude" / "CLAUDE.md").read_bytes() == b"# local edit\n"


def test_repair_refuses_when_the_default_branch_has_unpushed_commits(world, tmp_path):
    repo, claude, agents = fresh(tmp_path, "ahead")
    git("clone", "-q", "-c", "core.autocrlf=false", world.origin, repo)
    (repo / "extra.txt").write_bytes(b"x\n")
    git("-C", repo, "add", "-A")
    git("-C", repo, "commit", "-q", "-m", "local only")
    rc, out = run_install(world, "host-ahead", repo, claude, agents, "--repair")
    assert rc == 1
    assert "commits on its default branch that are not on origin" in out
    assert "never push them" in out
    assert (repo / "extra.txt").is_file()


def test_repair_refuses_a_folder_that_is_not_a_clone(world, tmp_path):
    repo, claude, agents = fresh(tmp_path, "notclone")
    repo.mkdir()
    (repo / "keep.txt").write_bytes(b"precious\n")
    rc, out = run_install(world, "host-x", repo, claude, agents, "--repair")
    assert rc == 1
    assert "not a git clone" in out
    assert (repo / "keep.txt").read_bytes() == b"precious\n"


def test_repair_refuses_a_repo_dir_that_is_a_link(world, tmp_path):
    repo, claude, agents = fresh(tmp_path, "link")
    real = tmp_path / "real_clone"
    git("clone", "-q", "-c", "core.autocrlf=false", world.origin, real)
    make_dir_link(repo, real)  # a junction on Windows
    rc, out = run_install(world, "host-x", repo, claude, agents, "--repair")
    assert rc == 1
    assert "is a link" in out
    assert (real / ".git").exists()


def test_repair_on_a_machine_with_no_prior_state_is_a_fresh_install(world, tmp_path):
    repo, claude, agents = fresh(tmp_path, "never")
    rc, out = run_install(world, "host-never-run", repo, claude, agents, "--repair", project="/some/path")
    assert rc == 0, out
    assert (repo / "machines" / "host-never-run.conf").is_file()


# ── --profile (profile.bats) ─────────────────────────────────────────────


def test_profile_flag_writes_profile_line_and_applies_that_profile(world, seeded, tmp_path):  # noqa: F811
    repo, home = tmp_path / "repo_c", tmp_path / "claude_home_c"
    rc, out = run_install(world, "host-c", repo, home, tmp_path / "agents_home_c", "--profile", "host-a")
    assert rc == 0, out
    assert "PROFILE=host-a" in conf_text(repo, "host-c").splitlines()
    assert (home / "agents" / "helper.md").read_bytes() == b"host-a helper\n"
    assert origin_show(world, "master", "machines/host-c.conf").returncode != 0
    git("-C", world.origin, "cat-file", "-e", "sync/host-c:machines/host-c.conf")
    assert git("-C", world.origin, "cat-file", "-e", "sync/host-c:machines/host-c", check=False).returncode != 0


def test_profile_flag_refuses_a_profile_that_does_not_exist(world, tmp_path):
    repo = tmp_path / "repo_c"
    rc, out = run_install(
        world, "host-c", repo, tmp_path / "claude_home_c", tmp_path / "agents_home_c", "--profile=no-such-profile"
    )
    assert rc != 0
    assert "does not exist" in out
    assert not (repo / "machines" / "host-c.conf").exists()


def test_claude_sync_profile_works_like_the_flag(world, seeded, tmp_path):  # noqa: F811
    repo = tmp_path / "repo_c"
    rc, out = run_install(
        world, "host-c", repo, tmp_path / "claude_home_c", tmp_path / "agents_home_c", CLAUDE_SYNC_PROFILE="host-a"
    )
    assert rc == 0, out
    assert "PROFILE=host-a" in conf_text(repo, "host-c").splitlines()


@pytest.mark.parametrize("bad", ["..", ".", "a/b", "../x", "-x y"])
def test_bad_profile_name_is_refused(world, tmp_path, bad):
    repo = tmp_path / "repo_c"
    rc, out = run_install(world, "host-c", repo, tmp_path / "c", tmp_path / "a", "--profile", bad)
    assert rc == 1
    assert "Not a valid profile name" in out
    assert not repo.exists()


@pytest.mark.parametrize("bad", ["../evil", "a/b", ".."])
def test_bad_hostname_is_refused(world, tmp_path, bad):
    repo = tmp_path / "repo_c"
    rc, out = run_install(world, bad, repo, tmp_path / "c", tmp_path / "a")
    assert rc == 1
    assert "Not a valid hostname" in out
    assert not repo.exists()


def test_profile_flag_is_ignored_when_the_conf_exists(world, seeded, tmp_path):  # noqa: F811
    _a, b = seeded
    rc, out = run_install(world, b.host, b.repo, b.claude, b.agents, "--profile", "host-a")
    assert rc == 0, out
    assert "--profile is ignored. Use: claude-sync.sh profile use host-a" in out
    assert "PROFILE=" not in conf_text(b.repo, b.host)


def test_repair_keeps_profile_and_override_and_profile_flag_restores_pointer(world, seeded, tmp_path):  # noqa: F811
    from test_subcommands import sub_as

    from claude_sync import doctor

    a, b = seeded
    sub_as(world, b, doctor.profile_use, "host-a")

    rc, out = run_install(world, a.host, a.repo, a.claude, a.agents, "--repair")
    assert rc == 0, out
    assert origin_show(world, "master", "machines/host-a/agents/helper.md").returncode == 0
    assert origin_show(world, "master", "machines/host-a.settings.json").returncode == 0
    assert (a.claude / "agents" / "helper.md").read_bytes() == b"host-a helper\n"

    rc, out = run_install(world, b.host, b.repo, b.claude, b.agents, "--repair", "--profile", "host-a")
    assert rc == 0, out
    assert "PROFILE=host-a" in conf_text(b.repo, b.host).splitlines()
    assert (b.claude / "agents" / "helper.md").read_bytes() == b"host-a helper\n"
    assert origin_show(world, "master", "machines/host-a/agents/helper.md").returncode == 0


def test_repair_on_a_machine_that_synced_before_sends_its_new_conf(world, seeded, tmp_path):  # noqa: F811
    from test_subcommands import sub_as

    from claude_sync import doctor

    _a, b = seeded
    sub_as(world, b, doctor.profile_use, "host-a")
    assert (b.claude / ".claude-sync-applied-commit").is_file()
    assert "PROFILE=host-a" in origin_show(world, "master", "machines/host-b.conf").stdout.splitlines()

    rc, out = run_install(world, b.host, b.repo, b.claude, b.agents, "--repair")
    assert rc == 0, out
    assert "PROFILE=" not in conf_text(b.repo, b.host)
    assert "PROFILE=" not in origin_show(world, "sync/host-b", "machines/host-b.conf").stdout
    assert "PROFILE=host-a" in origin_show(world, "master", "machines/host-b.conf").stdout.splitlines()


# ── arguments, prompts and failures ──────────────────────────────────────


def test_unknown_option_is_rejected(world, tmp_path):
    rc, out = run_install(world, "host-c", tmp_path / "r", tmp_path / "c", tmp_path / "a", "--bogus")
    assert rc != 0
    assert "Unknown option: --bogus" in out
    assert "Usage: install.sh" in out


@pytest.mark.parametrize("flag", ["--profile", "--project-path"])
def test_flag_without_its_value_is_rejected(world, tmp_path, flag):
    rc, out = run_install(world, "host-c", tmp_path / "r", tmp_path / "c", tmp_path / "a", flag)
    assert rc == 1
    assert f"{flag} needs" in out


def test_no_repo_url_and_no_terminal_says_how_to_rerun(world, tmp_path):
    rc, out = run_install(world, "host-c", tmp_path / "r", tmp_path / "c", tmp_path / "a", CLAUDE_SYNC_REPO_URL="")
    assert rc == 1
    assert "CLAUDE_SYNC_REPO_URL is not set and no terminal is attached" in out
    assert "CLAUDE_SYNC_REPO_URL=<your sync repo url>" in out
    assert not (tmp_path / "r").exists()


def test_clone_failure_is_reported_without_the_credentials(world, tmp_path):
    url = "https://someone:sekrit-pass@127.0.0.1:9/repo.git"
    rc, out = run_install(world, "host-c", tmp_path / "r", tmp_path / "c", tmp_path / "a", CLAUDE_SYNC_REPO_URL=url)
    assert rc == 1
    assert "Could not clone" in out
    assert "sekrit-pass" not in out
    assert "Traceback" not in out


def test_a_failing_tool_step_does_not_stop_the_install(world, tmp_path, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("npm exploded")

    monkeypatch.setattr(tools, "install_npm_tools", boom)
    repo, claude, agents = fresh(tmp_path, "boom")
    rc, out = run_install(world, "host-boom", repo, claude, agents)
    assert rc == 0, out
    assert "npm exploded" in out
    assert (repo / "machines" / "host-boom.conf").is_file()


def test_gh_logged_out_gives_a_note(world, tmp_path, monkeypatch):
    repo, claude, agents = fresh(tmp_path, "nogh")
    monkeypatch.setenv("FAKE_GH_AUTH_RC", "1")
    rc, out = run_install(world, "host-nogh", repo, claude, agents)
    assert "gh is not logged in" in out
    assert rc != 0  # the PR step itself then fails, as in bash


def test_gh_not_installed_note(monkeypatch, capsys, tmp_path):
    install._gh_notes({"CLAUDE_SYNC_GH": str(tmp_path / "no-gh")}, "host-x")
    err = capsys.readouterr().err
    assert "the GitHub CLI (gh) is not installed" in err
    assert "sync/host-x" in err


def test_conf_existing_runs_sync_not_bootstrap(world, tmp_path, monkeypatch):
    from claude_sync import cli

    b = world.machine("b")
    seen = []
    monkeypatch.setattr(cli, "main", lambda argv, env: seen.append((argv, env["CLAUDE_SYNC_REPO_DIR"])) or 0)
    rc, out = run_install(world, b.host, b.repo, b.claude, b.agents)
    assert rc == 0, out
    assert seen == [(["sync"], str(b.repo))]
    repo, claude, agents = fresh(tmp_path, "new")
    seen.clear()
    rc, out = run_install(world, "host-new", repo, claude, agents)
    assert seen == [(["bootstrap"], str(repo))]


@pytest.mark.parametrize("bad", ["/srv/a\nPROFILE=evil", "/srv/a\rb", "/srv/a\0b"])
def test_project_path_with_a_line_break_is_refused_before_anything_changes(world, tmp_path, bad):
    repo = tmp_path / "repo_c"
    rc, out = run_install(world, "host-c", repo, tmp_path / "c", tmp_path / "a", project=bad)
    assert rc == 1
    assert "line break" in out
    assert not repo.exists()


def test_module_entry_point_runs_without_a_traceback(fake_home):
    """`python -m claude_sync.install`, the shim's way in."""
    from claude_sync.util import run

    root = Path(install.__file__).resolve().parent.parent
    result = run([sys.executable, "-m", "claude_sync.install", "--bogus"], cwd=root)
    assert result.returncode == 1
    assert "Unknown option: --bogus" in result.stderr
    assert "Traceback" not in result.stderr + result.stdout
