"""Tests for `claude-sync status --line [--json]` (claude_sync/status.py)
and the SessionStart hook that runs it. Placeholder hostnames only: this file is
published.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from helpers import git, merge_sync_pr, rev

from claude_sync import machineconf, status

REPO_ROOT = Path(__file__).resolve().parents[2]
SETTINGS = REPO_ROOT / "dot-claude" / "settings.json"
DAY = 86400


def line_of(world, m, env: dict | None = None) -> str:
    rc, out = world.cli(m, "status", "--line", env={**m.env(), **(env or {})})
    assert rc == 0, out
    assert out.count("\n") <= 1, out
    return out.strip()


def json_of(world, m, env: dict | None = None) -> dict | None:
    """`status --line --json` as the parsed object, None when it printed
    nothing."""
    rc, out = world.cli(m, "status", "--line", "--json", env={**m.env(), **(env or {})})
    assert rc == 0, out
    if out == "":
        return None
    assert out.count("\n") == 1 and out.endswith("\n"), out
    assert out.isascii(), out
    return json.loads(out)


def assert_hook_object(obj: dict | None, line: str) -> None:
    """One SessionStart hook object carrying `line` to the user and Claude."""
    assert obj == {
        "systemMessage": line,
        "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": line},
    }


def log_result(m, event: str, rc: int) -> None:
    with open(m.claude / status.LOG_NAME, "a", encoding="utf-8", newline="\n") as f:
        f.write("claude-sync: some output of the run\n")
        f.write(f"claude-sync-hook: {event} exit={rc} 2026-01-01T00:00:00Z\n")


def make_pending(world, m, monkeypatch, days_ago: float) -> None:
    """A sync that leaves this machine's change on sync/<host>, created
    `days_ago` days ago (the reflog takes git's committer date)."""
    monkeypatch.setenv("GIT_COMMITTER_DATE", f"{int(time.time() - days_ago * DAY)} +0000")
    (m.claude / "CLAUDE.md").write_bytes(b"# shared\nlocal edit\n")
    world.ok(m, "sync")
    monkeypatch.delenv("GIT_COMMITTER_DATE")


def test_a_healthy_machine_prints_nothing(world):
    a = world.machine("a")
    log_result(a, "SessionStart", 0)
    assert line_of(world, a) == ""
    assert json_of(world, a) is None


def test_json_carries_the_same_line_to_the_user_and_claude(world):
    a = world.machine("a")
    log_result(a, "SessionEnd", 1)
    line = line_of(world, a)
    assert line.startswith("claude-config: the last claude-config sync (SessionEnd) failed")
    assert_hook_object(json_of(world, a), line)


def test_json_escapes_quotes_backslashes_and_non_ascii(world, tmp_path):
    a = world.machine("a")
    env = {"CLAUDE_SYNC_REPO_DIR": str(tmp_path / 'no "such" \\ dir \u00e9')}
    line = line_of(world, a, env)
    assert line.startswith("claude-config: no git repo at ")
    assert '"' in line and "\\" in line
    assert_hook_object(json_of(world, a, env), line)


def test_a_failed_last_hook_sync_is_one_line_until_a_run_succeeds(world):
    a = world.machine("a")
    log_result(a, "SessionEnd", 1)
    line = line_of(world, a)
    assert line.startswith("claude-config: the last claude-config sync (SessionEnd) failed with exit 1.")
    assert "claude-sync.sh sync" in line
    log_result(a, "SessionStart", 0)
    assert line_of(world, a) == ""


def test_only_the_end_of_a_long_log_is_read(world):
    a = world.machine("a")
    log_result(a, "SessionStart", 1)
    with open(a.claude / status.LOG_NAME, "a", encoding="utf-8") as f:
        f.write("x" * 100 + "\n")
    assert "failed" in line_of(world, a)
    with open(a.claude / status.LOG_NAME, "a", encoding="utf-8") as f:
        f.write(("y" * 99 + "\n") * (status.LOG_TAIL // 100 + 1))
    assert line_of(world, a) == ""


def test_a_pr_waiting_too_long_is_reported_and_a_rebuild_keeps_its_age(world, monkeypatch):
    a = world.machine("a")
    make_pending(world, a, monkeypatch, days_ago=5)
    line = line_of(world, a)
    assert line.startswith("claude-config: this machine's changes on sync/host-a have waited over 3 days")
    assert line_of(world, a, {"CLAUDE_SYNC_PR_DAYS": "7"}) == ""
    assert line_of(world, a, {"CLAUDE_SYNC_PR_DAYS": "0"}) == ""

    # Another machine's merge moves the default branch: the sync branch is
    # rebuilt as a new commit, but its age stays.
    before = rev(a.repo, "refs/heads/sync/host-a")
    world.commit_on_origin({"dot-claude/agents/other.md": b"other\n"}, "merged elsewhere")
    world.ok(a, "sync")
    assert rev(a.repo, "refs/heads/sync/host-a") != before
    assert "have waited over 3 days" in line_of(world, a)

    # Merged: the next sync deletes the branch, and the line goes away.
    merge_sync_pr(world.origin, a.host, gh_state=world.gh_state)
    world.ok(a, "sync")
    assert line_of(world, a) == ""


def test_a_new_pending_change_is_not_reported(world, monkeypatch):
    a = world.machine("a")
    make_pending(world, a, monkeypatch, days_ago=1)
    assert line_of(world, a) == ""


def test_without_a_reflog_the_tip_time_is_used(world, monkeypatch):
    a = world.machine("a")
    make_pending(world, a, monkeypatch, days_ago=5)
    git("-C", a.repo, "reflog", "expire", "--expire=all", "refs/heads/sync/host-a")
    assert "have waited over 3 days" in line_of(world, a)


def test_a_doctor_problem_is_one_line_naming_doctor(world):
    world.commit_on_origin({"dot-claude/hooks/guard.sh": b"#!/bin/sh\nexit 0\n"}, "a hook without the bit")
    a = world.machine("a")
    (a.repo / ".git" / "rebase-merge").mkdir()
    line = line_of(world, a)
    assert line.startswith("claude-config: 'claude-sync.sh doctor' reports a problem: the clone is stuck mid-rebase.")
    assert line.endswith("(1 more: run 'claude-sync.sh doctor'.)")


@pytest.mark.skipif(os.name == "nt", reason="doctor skips .sh hook references on Windows")
def test_a_missing_hook_script_is_reported(world):
    a = world.machine("a")
    (a.claude / "settings.json").write_text(
        json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": '"$HOME/.claude/hooks/gone.sh"'}]}]}}),
        encoding="utf-8",
    )
    assert "settings.json runs a hook script that does not exist" in line_of(world, a)


def test_a_hook_tracked_without_the_exec_bit_is_reported(world):
    world.commit_on_origin({"dot-claude/hooks/guard.sh": b"#!/bin/sh\nexit 0\n"}, "a hook without the bit")
    a = world.machine("a")
    assert "a hook script is tracked without the exec bit" in line_of(world, a)


def test_a_tier_change_is_reported_until_the_switch_has_run(world):
    a = world.machine("a")
    machineconf.write_machine_tier(a.repo, a.host, "basic")
    assert line_of(world, a).startswith("claude-config: the sync tier changed to basic;")
    (a.claude / ".claude-sync-applied-tier").write_bytes(b"basic\n")
    assert line_of(world, a) == ""

    machineconf.write_machine_tier(a.repo, a.host, "full")
    assert "the sync tier changed to full" in line_of(world, a)
    (a.claude / ".claude-sync-applied-tier").write_bytes(b"merged\n")
    assert "the sync tier changed to full" in line_of(world, a)
    (a.claude / ".claude-sync-applied-tier").unlink()
    assert line_of(world, a) == ""


def test_several_findings_still_give_one_line_most_urgent_first(world, monkeypatch):
    a = world.machine("a")
    make_pending(world, a, monkeypatch, days_ago=10)
    log_result(a, "SessionEnd", 124)
    machineconf.write_machine_tier(a.repo, a.host, "basic")
    line = line_of(world, a)
    assert line.startswith("claude-config: the last claude-config sync (SessionEnd) failed with exit 124.")
    assert line.endswith("(2 more: run 'claude-sync.sh doctor'.)")


def test_a_bad_pr_days_value_is_the_line_and_still_exits_0(world):
    a = world.machine("a")
    line = line_of(world, a, {"CLAUDE_SYNC_PR_DAYS": "soon"})
    assert line.startswith("claude-config: the status check failed: CLAUDE_SYNC_PR_DAYS=soon is not a whole number")


def test_an_unexpected_error_is_the_line_and_still_exits_0(world, monkeypatch):
    a = world.machine("a")

    def boom(*_args, **_kwargs):
        raise RuntimeError("a bug")

    monkeypatch.setattr(status, "findings", boom)
    line = line_of(world, a)
    assert line == (
        "claude-config: the status check hit an unexpected error (RuntimeError). Run 'claude-sync.sh doctor'."
    )


@pytest.mark.parametrize("args", [[], ["--json"], ["--json", "--line"], ["--line", "--bogus"], ["--line", "--json", "x"]])
def test_status_without_line_or_with_other_args_is_a_usage_error(world, args):
    a = world.machine("a")
    rc, out = world.cli(a, "status", *args)
    assert rc == 1
    assert "status --line [--json]" in out


def test_status_line_works_on_a_feature_branch(world):
    a = world.machine("a")
    git("-C", a.repo, "checkout", "-q", "-b", "feature")
    log_result(a, "SessionStart", 1)
    assert "failed with exit 1" in line_of(world, a)


def test_a_missing_clone_is_one_line(world, tmp_path):
    a = world.machine("a")
    line = line_of(world, a, {"CLAUDE_SYNC_REPO_DIR": str(tmp_path / "nowhere")})
    assert line.startswith("claude-config: no git repo at ")


@pytest.mark.parametrize("flags", [["--line"], ["--line", "--json"]])
def test_the_real_process_is_fast_and_never_uses_the_network(world, monkeypatch, flags):
    """A whole `python -m claude_sync status --line [--json]`, with every check
    running (a pending branch, hooks, a log), under 500 ms. origin points
    at a path that does not exist and the fake gh records no call."""
    world.commit_on_origin(
        {"dot-claude/hooks/guard.sh": b"#!/bin/sh\nexit 0\n"}, "a hook", executable=("dot-claude/hooks/guard.sh",)
    )
    a = world.machine("a")
    make_pending(world, a, monkeypatch, days_ago=1)
    log_result(a, "SessionEnd", 0)
    git("-C", a.repo, "remote", "set-url", "origin", str(world.tmp / "no-such-origin.git"))
    world.gh_log.write_bytes(b"")
    argv = [sys.executable, "-m", "claude_sync", "status", *flags]
    kwargs = {"cwd": REPO_ROOT, "env": a.env(), "capture_output": True, "text": True, "check": False}
    subprocess.run(argv, **kwargs)  # warm the file cache
    best = None
    for _ in range(3):
        start = time.perf_counter()
        result = subprocess.run(argv, **kwargs)
        elapsed = time.perf_counter() - start
        best = elapsed if best is None else min(best, elapsed)
        assert result.returncode == 0, result.stderr
        assert result.stdout == "", result.stdout
        assert result.stderr == "", result.stderr
    assert best < 0.5, f"status {' '.join(flags)} took {best:.3f}s"
    assert world.gh_log.read_bytes() == b""


# ── the hook commands in dot-claude/settings.json ────────────────────────


def _session_commands(event: str) -> list[str]:
    data = json.loads(SETTINGS.read_text(encoding="utf-8"))
    return [h["command"] for entry in data["hooks"][event] for h in entry["hooks"]]


def _command(event: str, needle: str) -> str:
    found = [c for c in _session_commands(event) if needle in c]
    assert len(found) == 1, found
    return found[0]


needs_sh = pytest.mark.skipif(
    shutil.which("sh") is None or shutil.which("timeout") is None, reason="needs sh and timeout"
)


def _fake_clone(home: Path, body: str) -> None:
    """$HOME/claude-config/claude-sync.sh as a stand-in script."""
    clone = home / "claude-config"
    clone.mkdir(parents=True, exist_ok=True)
    shim = clone / "claude-sync.sh"
    shim.write_text("#!/bin/sh\n" + body, encoding="utf-8", newline="\n")
    shim.chmod(0o755)
    (home / ".claude").mkdir(parents=True, exist_ok=True)


def _run_hook(command: str, home: Path, extra: dict | None = None):
    env = {**os.environ, **(extra or {}), "HOME": str(home)}
    return subprocess.run(["sh", "-c", command], env=env, capture_output=True, text=True, check=False, timeout=30)


def test_the_status_hook_is_registered_under_session_start():
    command = _command("SessionStart", "status --line")
    assert '"$HOME/claude-config/claude-sync.sh" status --line --json 2>/dev/null; true' in command
    assert "jq" not in command


@needs_sh
def test_the_status_hook_passes_one_line_on_and_never_fails(tmp_path):
    home = tmp_path / "home"
    command = _command("SessionStart", "status --line")

    result = _run_hook(command, home)  # no clone at all
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")

    obj = '{"systemMessage": "claude-config: a line"}'
    _fake_clone(home, f"echo \"$*\" > \"$HOME/args\"\necho '{obj}'\necho noise >&2\nexit 3\n")
    result = _run_hook(command, home)
    assert result.returncode == 0
    assert result.stdout == obj + "\n"
    assert result.stderr == ""
    assert (home / "args").read_text(encoding="utf-8") == "status --line --json\n"


@needs_sh
def test_the_status_hook_gives_the_real_json_and_nothing_when_healthy(world, tmp_path):
    """The hook command from settings.json, run under sh, through a stand-in
    shim that runs the real `python -m claude_sync` from this checkout."""
    a = world.machine("a")
    home = tmp_path / "home"
    _fake_clone(home, f'exec "{sys.executable}" -m claude_sync "$@"\n')
    command = _command("SessionStart", "status --line")
    extra = {**a.env(), "PYTHONPATH": str(REPO_ROOT)}

    log_result(a, "SessionStart", 0)
    result = _run_hook(command, home, extra)
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")

    log_result(a, "SessionEnd", 1)
    result = _run_hook(command, home, extra)
    assert result.returncode == 0
    assert result.stderr == ""
    assert result.stdout.count("\n") == 1 and result.stdout.isascii()
    assert_hook_object(json.loads(result.stdout), line_of(world, a))


@needs_sh
@pytest.mark.parametrize(("event", "word"), [("SessionStart", "loaded"), ("SessionEnd", "saved")])
@pytest.mark.parametrize("rc", [0, 2])
def test_the_sync_hooks_log_a_result_line_status_reads(tmp_path, event, word, rc):
    home = tmp_path / "home"
    _fake_clone(home, f'echo "sync output $*"\nexit {rc}\n')
    result = _run_hook(_command(event, " sync >>"), home)
    assert result.returncode == 0, result.stderr
    message = json.loads(result.stdout)["systemMessage"]
    assert message == (f"Synced config {word}." if rc == 0 else "claude-config sync failed - see claude-config-sync.log")
    log = (home / ".claude" / status.LOG_NAME).read_text(encoding="utf-8")
    assert log.startswith("sync output sync\nclaude-sync-hook: ")
    assert status.last_hook_result(home / ".claude") == (event, rc)
