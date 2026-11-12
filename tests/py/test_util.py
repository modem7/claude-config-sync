"""Tests for claude_sync.util and the shared fixtures in conftest.py."""

import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

import pytest

from claude_sync import util
from claude_sync.__main__ import install_signal_handlers
from claude_sync.util import Fail, run

ECHO_ARGV = "import json, sys; sys.stdout.write(json.dumps(sys.argv[1:]))"


def test_run_never_uses_shell():
    literal = ["a; echo injected", "two words", "$HOME", "x && y", "|", "%PATH%"]
    result = run([sys.executable, "-c", ECHO_ARGV, *literal])
    assert result.returncode == 0
    assert json.loads(result.stdout) == literal


def test_run_rejects_a_string_command():
    with pytest.raises(TypeError):
        run("echo hi")  # type: ignore[arg-type]


def test_run_decodes_utf8():
    text = "héllo ✓ 日本"
    code = f"import sys; sys.stdout.buffer.write({text!r}.encode('utf-8'))"
    result = run([sys.executable, "-c", code])
    assert result.stdout == text


def test_run_merges_env_over_os_environ():
    code = "import os, sys; sys.stdout.write(os.environ['CS_T'] + '|' + str('PATH' in os.environ))"
    result = run([sys.executable, "-c", code], env={"CS_T": "set"})
    assert result.stdout == "set|True"


def test_run_uses_cwd(tmp_path):
    code = "import os, sys; sys.stdout.write(os.getcwd())"
    result = run([sys.executable, "-c", code], cwd=tmp_path)
    assert Path(result.stdout).resolve() == tmp_path.resolve()


def test_run_check_raises_fail_with_stderr_as_hint():
    code = "import sys; sys.stderr.write('boom'); sys.exit(3)"
    with pytest.raises(Fail) as info:
        run([sys.executable, "-c", code], check=True)
    assert "exit code 3" in info.value.message
    assert info.value.hint == "boom"


def test_run_without_check_returns_failure():
    result = run([sys.executable, "-c", "import sys; sys.exit(4)"])
    assert result.returncode == 4


def test_run_missing_program_returns_127(tmp_path):
    missing = str(tmp_path / "no-such-program")
    result = run([missing])
    assert result.returncode == 127
    assert "not found" in result.stderr


def test_run_missing_program_with_check_raises_fail(tmp_path):
    missing = str(tmp_path / "no-such-program")
    with pytest.raises(Fail) as info:
        run([missing], check=True)
    assert "not found" in info.value.message
    assert info.value.hint


def test_run_missing_cwd_raises_fail(tmp_path):
    with pytest.raises(Fail) as info:
        run([sys.executable, "-c", "pass"], cwd=tmp_path / "gone")
    assert "folder not found" in info.value.message


def test_run_does_not_wait_on_stdin():
    result = run([sys.executable, "-c", "import sys; sys.stdout.write(repr(sys.stdin.read()))"])
    assert result.stdout == "''"


@pytest.mark.skipif(sys.platform == "win32", reason="process groups are POSIX here")
def test_run_new_process_group_puts_the_child_in_its_own_group():
    code = "import os, sys; sys.stdout.write(str(os.getpgrp() == os.getpid()))"
    assert run([sys.executable, "-c", code]).stdout == "False"
    assert run([sys.executable, "-c", code], new_process_group=True).stdout == "True"


# ── a stop while a child runs (stale git .lock files) ────────────────────

posix_signals = pytest.mark.skipif(os.name == "nt", reason="sends itself SIGTERM")

SLOW_CHILD = """
import os, signal, subprocess, sys, time
from pathlib import Path
here = Path(sys.argv[1])
if sys.argv[2] == "ignore-term":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
if sys.argv[2] == "grandchild":
    # Inherits stdout and stderr, as ssh under git push would.
    g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    (here / "grandchild").write_text(str(g.pid))
(here / "pid").write_text(str(os.getpid()))
(here / "index.lock").write_text("")
time.sleep(float(sys.argv[3]))
(here / "index.lock").unlink()
(here / "done").write_text("")
"""


@pytest.fixture
def term_raises_systemexit():
    """The entry point's SIGTERM/SIGHUP handlers, put back afterwards."""
    saved = {n: signal.getsignal(getattr(signal, n)) for n in ("SIGTERM", "SIGHUP")}
    install_signal_handlers()
    yield
    for n, h in saved.items():
        signal.signal(getattr(signal, n), h)


def _stopped_run(tmp_path, mode, seconds):
    script = tmp_path / "slow.py"
    script.write_text(SLOW_CHILD, encoding="utf-8")
    stop = threading.Event()

    def send_term():
        # Only once the child is set up (pid written last), so each case
        # really tests what it names.
        deadline = time.monotonic() + 20
        while not (tmp_path / "pid").exists() and time.monotonic() < deadline:
            if stop.wait(0.05):
                return
        os.kill(os.getpid(), signal.SIGTERM)

    sender = threading.Thread(target=send_term, daemon=True)
    start = time.monotonic()
    sender.start()
    try:
        with pytest.raises(SystemExit) as e:
            run([str(script), str(tmp_path), mode, str(seconds)], new_process_group=True)
    finally:
        stop.set()
        sender.join()
    assert e.value.code == 143
    return time.monotonic() - start


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


@posix_signals
def test_a_stop_lets_the_child_finish_and_remove_its_lock(tmp_path, term_raises_systemexit):
    elapsed = _stopped_run(tmp_path, "plain", 1.5)
    assert (tmp_path / "done").exists()
    assert not (tmp_path / "index.lock").exists()
    assert elapsed < util.STOP_GRACE


@posix_signals
def test_a_stop_terminates_a_child_that_outlives_the_grace(tmp_path, term_raises_systemexit, monkeypatch):
    monkeypatch.setattr(util, "STOP_GRACE", 0.5)
    elapsed = _stopped_run(tmp_path, "plain", 30)
    assert not (tmp_path / "done").exists()
    assert _gone(int((tmp_path / "pid").read_text()))
    assert elapsed < 10


@posix_signals
def test_a_stop_kills_a_child_that_ignores_terminate(tmp_path, term_raises_systemexit, monkeypatch):
    monkeypatch.setattr(util, "STOP_GRACE", 0.3)
    monkeypatch.setattr(util, "KILL_GRACE", 0.3)
    elapsed = _stopped_run(tmp_path, "ignore-term", 30)
    assert not (tmp_path / "done").exists()
    assert _gone(int((tmp_path / "pid").read_text()))
    assert elapsed < 10


@posix_signals
def test_a_stop_does_not_wait_for_a_grandchild_holding_the_pipes(tmp_path, term_raises_systemexit, monkeypatch):
    monkeypatch.setattr(util, "STOP_GRACE", 0.3)
    monkeypatch.setattr(util, "KILL_GRACE", 0.3)
    try:
        elapsed = _stopped_run(tmp_path, "grandchild", 30)
        assert _gone(int((tmp_path / "pid").read_text()))
        assert elapsed < 10
    finally:
        grandchild = tmp_path / "grandchild"
        if grandchild.exists():
            try:
                os.kill(int(grandchild.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_run_starts_py_scripts_with_current_python(tmp_path):
    script = tmp_path / "tool.py"
    script.write_text(ECHO_ARGV + "\n", encoding="utf-8")
    result = run([str(script), "one", "a b"])
    assert result.returncode == 0
    assert json.loads(result.stdout) == ["one", "a b"]


def test_fail_carries_hint():
    err = Fail("could not push", hint="Run: git pull, then try again.")
    assert err.message == "could not push"
    assert err.hint == "Run: git pull, then try again."
    assert str(err) == "could not push"
    assert Fail("no hint").hint == ""


def test_fake_home_never_equals_real_home(fake_home, real_home, tmp_path):
    root = tmp_path.resolve()
    for p in (fake_home.claude, fake_home.agents, fake_home.repo):
        assert p.resolve().is_relative_to(root)
    assert fake_home.claude.resolve() != (real_home / ".claude").resolve()
    assert fake_home.claude.is_dir() and fake_home.agents.is_dir()


def test_fake_home_env_points_into_tmp_path(fake_home, tmp_path):
    import os

    root = tmp_path.resolve()
    assert fake_home.env["CLAUDE_SYNC_HOSTNAME"] == "host-a"
    for key, value in fake_home.env.items():
        assert key.startswith("CLAUDE_SYNC_")
        assert os.environ[key] == value
        if key != "CLAUDE_SYNC_HOSTNAME":
            assert Path(value).resolve().is_relative_to(root)
    assert Path.home().resolve().is_relative_to(root)


def test_fake_gh_logs_calls(fake_gh):
    import os

    assert os.environ["CLAUDE_SYNC_GH"] == str(fake_gh)
    assert os.environ["CLAUDE_SYNC_GH_REPO"] == "test/repo"
    result = run([str(fake_gh), "auth", "status"])
    assert result.returncode == 0
    assert "Logged in" in result.stdout
    log = (fake_gh.parent / "calls.log").read_text(encoding="utf-8")
    assert log == "gh auth status\n"


def test_fake_gh_pr_flow(fake_gh, tmp_path):
    body = tmp_path / "body with space.md"
    body.write_text("hello\n", encoding="utf-8")
    listed = run([str(fake_gh), "pr", "list", "--head", "sync/host-a"])
    assert listed.stdout == ""
    created = run([str(fake_gh), "pr", "create", "--head", "sync/host-a", "--body-file", str(body)])
    assert created.stdout.strip() == "https://github.example/test/repo/pull/1"
    state = fake_gh.parent / "state"
    assert (state / "body-sync_host-a").read_text(encoding="utf-8") == "hello\n"
    again = run([str(fake_gh), "pr", "list", "--head", "sync/host-a"])
    assert again.stdout.strip() == "https://github.example/test/repo/pull/1"
    status = run([str(fake_gh), "pr", "list", "--head", "sync/host-a", "--state", "all", "--jq", "x"])
    assert status.stdout.strip() == "open"


def test_fake_gh_merged_and_failure_knobs(fake_gh, monkeypatch):
    sha = "a" * 40
    (fake_gh.parent / "state" / "merged-sync_host-a").write_text(sha + "\n", encoding="utf-8")
    jq = f'.[] | select(.headRefOid == "{sha}")'
    done = run([str(fake_gh), "pr", "list", "--head", "sync/host-a", "--state", "all", "--jq", jq])
    assert done.stdout.strip() == "done"
    nopr = run([str(fake_gh), "pr", "list", "--head", "sync/host-b", "--state", "all", "--jq", jq])
    assert nopr.stdout.strip() == "nopr"
    assert run([str(fake_gh), "repo", "view"]).returncode == 1
    monkeypatch.setenv("FAKE_GH_AUTH_RC", "4")
    assert run([str(fake_gh), "auth", "status"]).returncode == 4
    monkeypatch.setenv("FAKE_GH_CREATE_RC", "2")
    assert run([str(fake_gh), "pr", "create", "--head", "sync/host-a"]).returncode == 2


def test_git_repo_clone_tracks_origin_master(git_repo, fake_home):
    assert git_repo.clone == fake_home.repo
    branch = run(["git", "-C", str(git_repo.clone), "branch", "--show-current"], check=True)
    assert branch.stdout.strip() == "master"
    upstream = run(
        ["git", "-C", str(git_repo.clone), "rev-parse", "--abbrev-ref", "@{upstream}"], check=True
    )
    assert upstream.stdout.strip() == "origin/master"
    assert (git_repo.clone / "machines" / ".gitkeep").is_file()
    head = run(["git", "-C", str(git_repo.origin), "symbolic-ref", "HEAD"], check=True)
    assert head.stdout.strip() == "refs/heads/master"


def test_git_repo_alone_never_reads_real_home(git_repo, tmp_path):
    import os

    root = tmp_path.resolve()
    assert Path(os.environ["HOME"]).resolve().is_relative_to(root)
    assert Path(os.environ["USERPROFILE"]).resolve().is_relative_to(root)
    assert "GIT_CONFIG_GLOBAL" not in os.environ
    # git's global config is the fake HOME's: a marker written there is seen.
    (tmp_path / "home" / ".gitconfig").write_text("[test]\n\tmarker = fake-home\n", encoding="utf-8")
    got = run(["git", "-C", str(git_repo.clone), "config", "--global", "--get", "test.marker"], check=True)
    assert got.stdout.strip() == "fake-home"
    origin = run(["git", "config", "--global", "--show-origin", "--get", "test.marker"], check=True)
    shown = origin.stdout.split("\t", 1)[0].removeprefix("file:")
    assert Path(shown).resolve().is_relative_to(root)
