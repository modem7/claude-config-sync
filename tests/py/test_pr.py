"""Tests for claude_sync.pr, ported from tests/sync_pr.bats (the PR and push
cases; the converge and merge cases live in test_gitflow.py, which now runs
on the real do_push too) plus the gh-missing and gh-not-logged-in messages.
"""

from __future__ import annotations

import os
import subprocess

import pytest
from helpers import git, refute, rev

from claude_sync import pr
from claude_sync.gitflow import SyncCtx
from claude_sync.util import Fail

NEW_TIP = "a" * 40


def calls(world) -> str:
    return world.gh_log.read_text(encoding="utf-8")


def pr_creates(world) -> int:
    return sum(1 for ln in calls(world).splitlines() if ln.startswith("gh pr create"))


def origin_refs(world) -> str:
    return git("-C", world.origin, "for-each-ref", "--format=%(refname) %(objectname)").stdout


def edit(m, text: str) -> None:
    (m.claude / "CLAUDE.md").write_text(text + "\n", encoding="utf-8", newline="\n")


# ── gh_repo_slug ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "slug"),
    [
        ("git@github.com:owner/repo.git", "owner/repo"),
        ("ssh://git@github.com/owner/repo.git", "owner/repo"),
        ("https://github.com/owner/repo", "owner/repo"),
        ("https://github.com/owner/repo/", "owner/repo"),
        ("https://user:secret@github.com/owner/repo.git", "owner/repo"),
        ("git@git.example.test:owner/repo.git", "git.example.test/owner/repo"),
        ("https://git.example.test:8443/owner/repo.git", "git.example.test/owner/repo"),
        ("ssh://git@ssh.github.com:443/owner/repo.git", "owner/repo"),
        ("git@GitHub.com:owner/repo.git", "owner/repo"),
        ("git@github-work:owner/repo.git", "github-work/owner/repo"),
        ("https://github.com/owner", None),
        ("https://github.com/a/b/c", None),
        ("/some/local/path.git", None),
    ],
)
def test_gh_repo_slug_from_the_origin_url(git_repo, monkeypatch, url, slug):
    monkeypatch.delenv("CLAUDE_SYNC_GH_REPO", raising=False)
    git("-C", git_repo.clone, "remote", "set-url", "origin", url)
    assert pr.gh_repo_slug(git_repo.clone) == slug


def test_gh_repo_slug_env_wins_and_no_origin_is_none(git_repo, monkeypatch):
    monkeypatch.setenv("CLAUDE_SYNC_GH_REPO", "test/repo")
    assert pr.gh_repo_slug(git_repo.clone) == "test/repo"
    monkeypatch.setenv("CLAUDE_SYNC_GH_REPO", "o/r")
    assert pr.gh_repo_slug(git_repo.clone) == "o/r"
    monkeypatch.delenv("CLAUDE_SYNC_GH_REPO")
    git("-C", git_repo.clone, "remote", "remove", "origin")
    assert pr.gh_repo_slug(git_repo.clone) is None


# ── gh_sync_pr_state ──────────────────────────────────────────────────────


def test_pr_state_open_done_nopr(world, fake_gh):
    st = world.gh_state
    assert pr.gh_sync_pr_state("test/repo", "sync/host-a", NEW_TIP) == "nopr"
    (st / "merged-sync_host-a").write_text(NEW_TIP + "\n")
    assert pr.gh_sync_pr_state("test/repo", "sync/host-a", NEW_TIP) == "done"
    assert pr.gh_sync_pr_state("test/repo", "sync/host-a", "b" * 40) == "nopr"
    (st / "open-sync_host-a").write_text("https://github.example/pr/1\n")
    assert pr.gh_sync_pr_state("test/repo", "sync/host-a", NEW_TIP) == "open"


def test_pr_state_unknown_when_gh_is_missing_logged_out_or_no_slug(world, monkeypatch, tmp_path):
    assert pr.gh_sync_pr_state(None, "sync/host-a", NEW_TIP) == "unknown"
    assert pr.gh_sync_pr_state("test/repo", "sync/host-a", "not-a-sha") == "unknown"
    monkeypatch.setenv("FAKE_GH_AUTH_RC", "1")
    assert pr.gh_sync_pr_state("test/repo", "sync/host-a", NEW_TIP) == "unknown"
    monkeypatch.delenv("FAKE_GH_AUTH_RC")
    monkeypatch.setenv("CLAUDE_SYNC_GH", str(tmp_path / "no-such-gh"))
    assert pr.gh_sync_pr_state("test/repo", "sync/host-a", NEW_TIP) == "unknown"


def test_pr_state_for_fits_the_ctx_callable(world):
    a = world.machine("a")
    assert a.ctx().pr_state("sync/host-a", NEW_TIP) == "nopr"


# ── redact ────────────────────────────────────────────────────────────────


def test_redact_removes_tokens_and_url_credentials(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "s3cr3t-value")
    text = (
        "Token: gho_abcdefghijklmnopqrstuvwxyz0123\n"
        "github_pat_11ABCDEFG0123456789_abcdef\n"
        "fatal: https://user:hunter2@github.com/o/r.git s3cr3t-value\n"
        "Logged in to github.com"
    )
    out = pr.redact(text)
    for leaked in ("gho_abc", "github_pat_11", "hunter2", "s3cr3t-value", "user:"):
        assert leaked not in out
    assert "Logged in to github.com" in out
    assert "https://github.com/o/r.git" in out


# ── the bats cases ────────────────────────────────────────────────────────


def test_sync_commits_onto_the_sync_branch_and_opens_a_pr(world):
    a = world.machine("a")
    master_before = rev(world.origin, "master")
    edit(a, "edit one")

    out = world.ok(a, "sync")
    assert "pushed sync/host-a" in out
    assert "opened PR" in out

    assert rev(world.origin, "master") == master_before
    assert git("-C", world.origin, "show", "sync/host-a:dot-claude/CLAUDE.md").stdout == "edit one\n"
    assert git("-C", world.origin, "rev-list", "--count", "master..sync/host-a").stdout.strip() == "1"
    assert git("-C", a.repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "master"
    assert rev(a.repo, "master") == master_before
    assert (a.repo / "dot-claude" / "CLAUDE.md").read_text() == "edit one\n"
    assert (a.claude / "CLAUDE.md").read_text() == "edit one\n"

    log = calls(world)
    assert "gh pr list --repo test/repo --head sync/host-a --state open " in log
    assert "gh pr create --repo test/repo --base master --head sync/host-a --title sync: host-a --body-file " in log
    assert "merge" not in log
    assert "--auto" not in log
    body = (world.gh_state / "body-sync_host-a").read_text().lower()
    assert "co-authored-by" not in body
    assert "generated with" not in body
    msgs = git("-C", world.origin, "log", "--format=%B", "master..sync/host-a").stdout.lower()
    assert "co-authored-by" not in msgs
    assert "generated with" not in msgs


def test_a_second_sync_updates_the_same_branch_and_pr(world):
    a = world.machine("a")
    edit(a, "edit one")
    world.ok(a, "sync")
    first = rev(world.origin, "sync/host-a")

    edit(a, "edit two")
    out = world.ok(a, "sync")
    assert "updated the open PR https://github.example/test/repo/pull/1" in out
    assert "refusing" not in out
    assert rev(world.origin, "sync/host-a") != first
    assert git("-C", world.origin, "show", "sync/host-a:dot-claude/CLAUDE.md").stdout == "edit two\n"
    assert git("-C", world.origin, "rev-list", "--count", "master..sync/host-a").stdout.strip() == "1"
    assert pr_creates(world) == 1


def test_a_sync_with_nothing_new_is_a_quiet_no_op(world):
    a = world.machine("a")
    edit(a, "edit one")
    world.ok(a, "sync")
    refs, n_calls = origin_refs(world), calls(world)

    out = world.ok(a, "sync")
    assert out == ""
    assert origin_refs(world) == refs
    assert calls(world) == n_calls


def push_without_pr(world, a, monkeypatch) -> None:
    """Pushes sync/host-a with gh working but its PR create failing, so the
    branch is on origin with no PR (gh must work for a push to happen)."""
    monkeypatch.setenv("FAKE_GH_CREATE_RC", "1")
    rc, out = world.run(a, "sync")
    assert rc == 1, out
    assert "'gh pr create' failed" in out
    monkeypatch.delenv("FAKE_GH_CREATE_RC")


def test_gh_gone_after_an_earlier_push_leaves_no_pr_and_sync_fails_loudly(world, monkeypatch, tmp_path):
    """The branch was pushed by an earlier run (its PR create failed), then
    gh went away: nothing new to push, so only the PR step complains."""
    a = world.machine("a")
    edit(a, "edit one")
    push_without_pr(world, a, monkeypatch)
    good_gh = os.environ["CLAUDE_SYNC_GH"]
    monkeypatch.setenv("CLAUDE_SYNC_GH", str(tmp_path / "no-such-gh"))

    rc, out = world.run(a, "sync")
    assert rc == 1
    assert "the GitHub CLI (gh) is not installed, so no PR was opened" in out
    assert "gh pr create --repo test/repo --base master --head sync/host-a" in out
    assert git("-C", world.origin, "show", "sync/host-a:dot-claude/CLAUDE.md").stdout == "edit one\n"
    assert (a.claude / "CLAUDE.md").read_text() == "edit one\n"

    # Not silent next time either, until a PR is open; then it is.
    rc, out = world.run(a, "sync")
    assert rc == 1
    tip = rev(a.repo, "refs/heads/sync/host-a")
    assert f"git -C {a.repo} config claude-sync.prVerified {tip}" in out
    monkeypatch.setenv("CLAUDE_SYNC_GH", good_gh)
    assert "opened PR" in world.ok(a, "sync")


def test_gh_missing_message(world, monkeypatch, tmp_path):
    a = world.machine("a")
    edit(a, "edit one")
    push_without_pr(world, a, monkeypatch)
    monkeypatch.setenv("CLAUDE_SYNC_GH", str(tmp_path / "no-such-gh"))
    rc, out = world.run(a, "sync")
    assert rc == 1
    assert "pushed sync/host-a, but the GitHub CLI (gh) is not installed, so no PR was opened." in out
    assert "Install gh and run 'gh auth login'" in out
    assert "Traceback" not in out


def test_gh_not_logged_in_message(world, monkeypatch):
    a = world.machine("a")
    edit(a, "edit one")
    push_without_pr(world, a, monkeypatch)
    monkeypatch.setenv("FAKE_GH_AUTH_RC", "1")
    rc, out = world.run(a, "sync")
    assert rc == 1
    assert "gh is not logged in" in out
    assert "You are not logged into any GitHub hosts" in out
    assert "gh pr create --repo test/repo" in out
    assert "Run 'gh auth login'" in out
    assert "Traceback" not in out


# ── origin must be private and not a template ─────────────────────────────


def assert_nothing_pushed(world, a) -> None:
    refute(["git", "-C", world.origin, "show-ref", "-q", "--verify", "refs/heads/sync/host-a"])
    assert "gh pr create" not in calls(world)
    refute(["git", "-C", a.repo, "config", "--get", "claude-sync.lastPushed"])
    # The live config was still applied; only the push was held back.
    assert (a.claude / "CLAUDE.md").read_text() == "edit one\n"


@pytest.mark.parametrize(
    ("kind", "reason"),
    [
        ("public", "origin (test/repo) is a public repo"),
        ("template", "origin (test/repo) is a GitHub template repo"),
        ("error", "could not verify origin is private (test/repo): 'gh api repos/test/repo' failed (see the gh error above)"),
        ("garbage", "could not verify origin is private (test/repo): gh gave an answer that could not be read"),
    ],
)
def test_a_public_template_or_unreadable_origin_is_refused(world, monkeypatch, kind, reason):
    a = world.machine("a")
    edit(a, "edit one")
    monkeypatch.setenv("FAKE_GH_VISIBILITY", kind)
    rc, out = world.run(a, "sync")
    assert rc == 1, out
    assert f"refusing to push sync/host-a: {reason}. Nothing was pushed." in out
    assert f"git -C {a.repo} remote set-url origin <url>" in out
    assert "PRIVATE repo that is not a template" in out
    assert "gh api --hostname github.com repos/test/repo --jq " in calls(world)
    if kind == "error":
        assert "Not Found (HTTP 404)" in out
    assert "Traceback" not in out
    assert_nothing_pushed(world, a)

    # Fixed: the next sync pushes.
    monkeypatch.delenv("FAKE_GH_VISIBILITY")
    assert "pushed sync/host-a" in world.ok(a, "sync")


def test_gh_missing_refuses_the_push(world, monkeypatch, tmp_path):
    a = world.machine("a")
    edit(a, "edit one")
    monkeypatch.setenv("CLAUDE_SYNC_GH", str(tmp_path / "no-such-gh"))
    rc, out = world.run(a, "sync")
    assert rc == 1
    assert (
        "refusing to push sync/host-a: could not verify origin is private (test/repo): "
        "the GitHub CLI (gh) is not installed. Nothing was pushed."
    ) in out
    assert_nothing_pushed(world, a)


def test_gh_not_logged_in_refuses_the_push(world, monkeypatch):
    a = world.machine("a")
    edit(a, "edit one")
    monkeypatch.setenv("FAKE_GH_AUTH_RC", "1")
    rc, out = world.run(a, "sync")
    assert rc == 1
    assert "could not verify origin is private (test/repo): gh is not logged in. Nothing was pushed." in out
    assert "You are not logged into any GitHub hosts" in out
    assert "gh api" not in calls(world)
    assert_nothing_pushed(world, a)


@pytest.mark.parametrize("forced", ["", "test/repo"])
def test_a_push_url_without_a_slug_refuses_the_push(world, monkeypatch, forced):
    """An explicit pushurl skips pushInsteadOf, so this push would go to the
    local path, which names no repo: refused, whatever CLAUDE_SYNC_GH_REPO says."""
    a = world.machine("a")
    edit(a, "edit one")
    if forced:
        monkeypatch.setenv("CLAUDE_SYNC_GH_REPO", forced)
    else:
        monkeypatch.delenv("CLAUDE_SYNC_GH_REPO")
    git("-C", a.repo, "config", "remote.origin.pushurl", world.origin)
    rc, out = world.run(a, "sync")
    assert rc == 1
    assert (
        "could not verify origin is private: could not work out owner/repo from origin's push URL "
        f"{world.origin}. Nothing was pushed."
    ) in out
    assert "CLAUDE_SYNC_GH_REPO" not in out
    assert "gh " not in calls(world)
    assert_nothing_pushed(world, a)


def test_the_env_repo_must_match_the_push_url(world, monkeypatch):
    """CLAUDE_SYNC_GH_REPO naming a private repo cannot vouch for a public origin."""
    a = world.machine("a")
    edit(a, "edit one")
    monkeypatch.setenv("FAKE_GH_PUBLIC", "test/repo")
    monkeypatch.setenv("CLAUDE_SYNC_GH_REPO", "someone/private")
    rc, out = world.run(a, "sync")
    assert rc == 1, out
    assert (
        "could not verify origin is private: CLAUDE_SYNC_GH_REPO is someone/private, but origin pushes"
        " to test/repo; unset it or make it match. Nothing was pushed."
    ) in out
    assert "gh " not in calls(world)
    assert_nothing_pushed(world, a)
    # Case and a github.com/ prefix do not count as a mismatch; then the real check runs.
    monkeypatch.setenv("CLAUDE_SYNC_GH_REPO", "github.com/Test/Repo")
    rc, out = world.run(a, "sync")
    assert rc == 1
    assert "origin (test/repo) is a public repo" in out


def test_the_push_url_is_checked_not_the_fetch_url(world, monkeypatch):
    a = world.machine("a")
    monkeypatch.delenv("CLAUDE_SYNC_GH_REPO")
    monkeypatch.setenv("FAKE_GH_PUBLIC", "pub/lic")
    git("-C", a.repo, "remote", "set-url", "origin", "git@github.com:priv/ate.git")
    git("-C", a.repo, "config", "remote.origin.pushurl", "git@github.com:pub/lic.git")
    assert pr.origin_visibility_problem(a.ctx()) == "origin (pub/lic) is a public repo"
    assert "gh api --hostname github.com repos/pub/lic " in calls(world)
    # The other way round it passes: the fetch URL's repo is never asked about.
    git("-C", a.repo, "remote", "set-url", "origin", "git@github.com:pub/lic.git")
    git("-C", a.repo, "config", "remote.origin.pushurl", "git@github.com:priv/ate.git")
    assert pr.origin_visibility_problem(a.ctx()) is None


def test_push_insteadof_is_followed(world, monkeypatch):
    a = world.machine("a")
    monkeypatch.delenv("CLAUDE_SYNC_GH_REPO")
    monkeypatch.setenv("FAKE_GH_PUBLIC", "pub/lic")
    git("-C", a.repo, "remote", "set-url", "origin", "https://github.com/priv/ate.git")
    git("-C", a.repo, "config", "url.git@github.com:pub/lic.git.pushInsteadOf", "https://github.com/priv/ate.git")
    assert pr.origin_visibility_problem(a.ctx()) == "origin (pub/lic) is a public repo"


def test_several_push_urls_must_name_one_repo(world, monkeypatch):
    """git push pushes to every pushurl; get-url --push alone shows the first."""
    a = world.machine("a")
    monkeypatch.delenv("CLAUDE_SYNC_GH_REPO")
    monkeypatch.setenv("FAKE_GH_PUBLIC", "pub/lic")
    git("-C", a.repo, "config", "remote.origin.pushurl", "git@github.com:priv/ate.git")
    git("-C", a.repo, "config", "--add", "remote.origin.pushurl", "git@github.com:pub/lic.git")
    reason = pr.origin_visibility_problem(a.ctx())
    assert reason == "could not verify origin is private: origin pushes to more than one repo (priv/ate, pub/lic)"
    git("-C", a.repo, "config", "--replace-all", "remote.origin.pushurl", "https://github.com/priv/ate")
    git("-C", a.repo, "config", "--add", "remote.origin.pushurl", "git@github.com:Priv/Ate.git")
    assert pr.origin_visibility_problem(a.ctx()) is None


def test_gh_host_cannot_redirect_the_check(world, monkeypatch):
    a = world.machine("a")
    edit(a, "edit one")
    monkeypatch.setenv("GH_HOST", "other.example")
    assert "pushed sync/host-a" in world.ok(a, "sync")
    log = calls(world)
    assert "gh auth status --hostname github.com\n" in log
    assert "gh api --hostname github.com repos/test/repo --jq " in log


def test_ssh_github_com_on_port_443_is_github_com(world, monkeypatch):
    a = world.machine("a")
    monkeypatch.delenv("CLAUDE_SYNC_GH_REPO")
    git("-C", a.repo, "config", "remote.origin.pushurl", "ssh://git@ssh.github.com:443/test/repo.git")
    assert pr.origin_visibility_problem(a.ctx()) is None
    assert "gh api --hostname github.com repos/test/repo " in calls(world)


def test_an_ssh_alias_that_gh_does_not_know_is_named(world, monkeypatch):
    a = world.machine("a")
    monkeypatch.delenv("CLAUDE_SYNC_GH_REPO")
    monkeypatch.setenv("FAKE_GH_AUTH_RC", "1")
    git("-C", a.repo, "config", "remote.origin.pushurl", "git@github-work:test/repo.git")
    reason = pr.origin_visibility_problem(a.ctx())
    assert reason.startswith(
        "could not verify origin is private (github-work/test/repo): gh is not logged in to github-work,"
        " an unknown host (an SSH alias?"
    )
    assert "gh auth status --hostname github-work\n" in calls(world)
    assert "gh api" not in calls(world)


def test_a_private_origin_pushes_and_an_internal_one_too(world, monkeypatch):
    a = world.machine("a")
    edit(a, "edit one")
    out = world.ok(a, "sync")
    assert "pushed sync/host-a" in out
    assert "gh api --hostname github.com repos/test/repo --jq " in calls(world)
    edit(a, "edit two")
    monkeypatch.setenv("FAKE_GH_VISIBILITY", "internal")
    assert "pushed sync/host-a" in world.ok(a, "sync")


def test_nothing_to_push_makes_no_api_call(world):
    a = world.machine("a")
    edit(a, "edit one")
    world.ok(a, "sync")
    before = calls(world)
    world.ok(a, "sync")
    assert "gh api" not in calls(world)[len(before):]


def test_an_enterprise_host_is_checked_with_hostname(world, monkeypatch):
    a = world.machine("a")
    edit(a, "edit one")
    monkeypatch.setenv("CLAUDE_SYNC_GH_REPO", "git.example.test/owner/repo")
    git("-C", a.repo, "config", "remote.origin.pushurl", "git@git.example.test:owner/repo.git")
    world.run(a, "sync")
    log = calls(world)
    assert "gh auth status --hostname git.example.test\n" in log
    assert "gh api --hostname git.example.test repos/owner/repo --jq " in log


@pytest.mark.parametrize("slug", ["a/b/c/d", "a//b", "/a/b"])
def test_an_odd_env_slug_is_refused(world, monkeypatch, slug):
    a = world.machine("a")
    edit(a, "edit one")
    monkeypatch.setenv("CLAUDE_SYNC_GH_REPO", slug)
    rc, out = world.run(a, "sync")
    assert rc == 1
    assert f"could not verify origin is private: CLAUDE_SYNC_GH_REPO is {slug}, but origin pushes to test/repo" in out
    assert_nothing_pushed(world, a)


def test_a_failed_pr_create_fails_loudly(world, monkeypatch):
    a = world.machine("a")
    edit(a, "edit one")
    monkeypatch.setenv("FAKE_GH_CREATE_RC", "1")
    rc, out = world.run(a, "sync")
    assert rc == 1
    assert "'gh pr create' failed" in out
    assert "gh pr create --repo test/repo" in out
    # Recovers once gh works again.
    monkeypatch.delenv("FAKE_GH_CREATE_RC")
    assert "opened PR" in world.ok(a, "sync")


def test_gh_stderr_is_redacted_before_it_is_shown(world, monkeypatch):
    a = world.machine("a")
    edit(a, "edit one")
    monkeypatch.setenv("FAKE_GH_AUTH_RC", "1")
    monkeypatch.setenv("GH_TOKEN", "hunter2-token")
    monkeypatch.setattr(
        pr, "_gh",
        lambda *args: subprocess.CompletedProcess(args, 1, "Token: gho_abcdefghijklmnopqrstuvwxyz0123 hunter2-token\n", ""),
    )  # fmt: skip
    rc, out = world.run(a, "sync")
    assert rc == 1
    assert "gh is not logged in" in out
    assert "gho_abc" not in out
    assert "hunter2-token" not in out


def test_a_rejected_push_fails_loudly_after_the_live_config_was_applied(world):
    a = world.machine("a")
    hook = world.origin / "hooks" / "pre-receive"
    hook.write_text('#!/bin/sh\necho "push rejected by test" >&2\nexit 1\n')
    hook.chmod(0o755)
    edit(a, "edit one")

    rc, out = world.run(a, "sync")
    assert rc == 1
    assert "git push of sync/host-a failed" in out
    assert "Nothing on master was touched" in out
    refute(["git", "-C", world.origin, "show-ref", "-q", "--verify", "refs/heads/sync/host-a"])
    assert (a.claude / "CLAUDE.md").read_text() == "edit one\n"
    assert "gh pr create" not in calls(world)

    hook.unlink()
    world.ok(a, "sync")
    assert git("-C", world.origin, "show", "sync/host-a:dot-claude/CLAUDE.md").stdout == "edit one\n"


def test_a_sync_branch_edited_on_github_is_not_overwritten(world, tmp_path):
    a = world.machine("a")
    edit(a, "edit one")
    world.ok(a, "sync")
    w = tmp_path / "gh_edit"
    git("clone", "-q", "-b", "sync/host-a", world.origin, w)
    (w / "memory" / "NOTE.md").write_text("reviewer note\n")
    git("-C", w, "add", "-A")
    git("-C", w, "commit", "-q", "-m", "review edit")
    git("-C", w, "push", "-q", "origin", "sync/host-a")
    edited = rev(world.origin, "sync/host-a")

    edit(a, "edit two")
    rc, out = world.run(a, "sync")
    assert rc == 1
    assert "has commits this machine did not push" in out
    assert rev(world.origin, "sync/host-a") == edited


def test_the_push_is_lease_guarded_and_names_only_the_sync_branch(world, monkeypatch):
    a = world.machine("a")
    edit(a, "edit one")
    seen: list[tuple] = []
    real_git = pr._git

    def spy(ctx, *args, **kw):
        if args and args[0] == "push":
            seen.append(args)
        return real_git(ctx, *args, **kw)

    monkeypatch.setattr(pr, "_git", spy)
    world.ok(a, "sync")
    (push,) = seen
    assert any(x.startswith("--force-with-lease=refs/heads/sync/host-a:") for x in push)
    assert push[-2] == "origin"
    assert push[-1].endswith(":refs/heads/sync/host-a")
    assert "--force" not in push


def test_no_origin_is_a_message_not_a_traceback(world):
    a = world.machine("a")
    edit(a, "edit one")
    world.ok(a, "sync")
    edit(a, "edit two")
    # begin_sync needs origin, so call the push step directly.
    ctx = a.ctx()
    ctx.default = "master"
    git("-C", a.repo, "remote", "remove", "origin")
    with pytest.raises(Fail) as err:
        pr.do_push(ctx)
    assert "no remote named 'origin'" in err.value.message
    assert "remote add origin" in err.value.hint


def test_do_push_without_a_sync_branch_does_nothing(world):
    a = world.machine("a")
    ctx = a.ctx()
    ctx.default = "master"
    before = calls(world)
    pr.do_push(ctx)
    assert calls(world) == before
    assert isinstance(ctx, SyncCtx)
