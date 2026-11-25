"""The PR step of a sync: push sync/<hostname> and make sure a PR is open.

Port of gh_repo_slug, gh_sync_pr_state, pr_fail, ensure_pr and do_push in
claude-sync.sh. Only the sync branch is ever pushed (with a lease), the
default branch is never touched, and a PR is never merged or set to
auto-merge: a human merges.

`CLAUDE_SYNC_GH` picks the gh binary, `CLAUDE_SYNC_GH_REPO` (owner/repo)
sets the repo the PR is opened in when origin's URL does not show it. The
push guard never takes the repo from it: it checks the repo origin's push
URL names, and refuses when CLAUDE_SYNC_GH_REPO names another. Nothing gh
or git prints is shown without `redact` first, so a token never reaches
the terminal.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

from claude_sync.gitflow import (
    SyncCtx,
    _git,
    _hint,
    _out,
    changes_contained,
    has_origin,
    has_sync_branch,
    sync_branch,
)
from claude_sync.util import Fail, redact, run

_GH_ENV = {"GH_PROMPT_DISABLED": "1"}


def _show(text: str) -> None:
    """gh's or git's own error, passed on to stderr (redacted)."""
    if text:
        sys.stderr.write(redact(text) if text.endswith("\n") else redact(text) + "\n")


def gh_bin() -> str:
    return os.environ.get("CLAUDE_SYNC_GH") or "gh"


def gh_installed() -> bool:
    """bash `command -v`: a path is checked as a file, a bare name on PATH."""
    binary = gh_bin()
    if os.sep in binary or (os.altsep and os.altsep in binary):
        return Path(binary).is_file()
    return shutil.which(binary) is not None


def _gh(*args: str):
    return run([gh_bin(), *args], env=_GH_ENV)


def _slug_from_url(url: str) -> str | None:
    """owner/repo from a remote URL (git@host:owner/repo.git,
    ssh://git@host/owner/repo.git, https://host/owner/repo). A host other
    than github.com is prefixed; ssh.github.com (SSH over port 443) is
    github.com. None when the URL names no owner/repo (a local path)."""
    if "://" in url:
        rest = url.split("://", 1)[1]
        host, _, path = rest.partition("/")
        host = host.rpartition("@")[2].split(":", 1)[0]
    elif "@" in url and ":" in url.split("@", 1)[1]:
        after = url.split("@", 1)[1]
        host = after.split(":", 1)[0]
        path = url.split(":", 1)[1]
    else:
        return None
    path = path.rstrip("/")
    path = path.removesuffix(".git")
    if not re.fullmatch(r"[^/]+/[^/]+", path):
        return None
    host = host.lower()
    return path if host in ("github.com", "ssh.github.com") else f"{host}/{path}"


def gh_repo_slug(repo: Path) -> str | None:
    """owner/repo for gh's PR calls, from CLAUDE_SYNC_GH_REPO or origin's
    (fetch) URL. None when it cannot be worked out. Not for the push guard:
    see push_repo_slug."""
    forced = os.environ.get("CLAUDE_SYNC_GH_REPO", "")
    if forced:
        return forced
    result = _git(repo, "remote", "get-url", "origin")
    if result.returncode != 0:
        return None
    return _slug_from_url(result.stdout.strip())


def push_repo_slug(repo: Path) -> tuple[str | None, str]:
    """(slug, reason) for the repo a push to origin really goes to: from
    every URL `git remote get-url --push --all origin` gives (pushurl and
    pushInsteadOf applied), never from CLAUDE_SYNC_GH_REPO. The slug is None,
    with the reason, when there is no push URL, when one names no
    owner/repo, or when they name more than one repo."""
    result = _git(repo, "remote", "get-url", "--push", "--all", "origin")
    urls = [u.strip() for u in result.stdout.splitlines() if u.strip()] if result.returncode == 0 else []
    if not urls:
        return None, "origin has no push URL"
    slugs = []
    for url in urls:
        slug = _slug_from_url(url)
        if slug is None or not _split_slug(slug):
            return None, f"could not work out owner/repo from origin's push URL {redact(url)}"
        slugs.append(slug)
    if len({s.lower() for s in slugs}) > 1:
        return None, f"origin pushes to more than one repo ({', '.join(slugs)})"
    return slugs[0], ""


def _same_repo(a: str, b: str) -> bool:
    """Whether two slugs name one repo: GitHub names ignore case, and
    github.com/owner/repo is owner/repo."""

    def norm(slug: str) -> str:
        slug = slug.strip().strip("/").lower()
        return slug.removeprefix("github.com/")

    return norm(a) == norm(b)


def gh_sync_pr_state(slug: str | None, branch: str, tip: str) -> str:
    """What GitHub knows about head branch `branch` at `tip`: "open" (a PR
    is open), "done" (no open PR, and a merged or closed one has exactly
    that tip), "nopr" (neither: e.g. pushed while gh was missing) or
    "unknown" (gh missing, not logged in, no slug, or failing)."""
    if not slug or not gh_installed() or not re.fullmatch(r"[0-9a-fA-F]{40,64}", tip):
        return "unknown"
    if _gh("auth", "status").returncode != 0:
        return "unknown"
    jq = (
        'if any(.[]; .state == "OPEN") then "open" '
        f'elif any(.[]; .headRefOid == "{tip}") then "done" else "nopr" end'
    )
    result = _gh(
        "pr", "list", "--repo", slug, "--head", branch, "--state", "all",
        "--json", "state,headRefOid", "--jq", jq,
    )  # fmt: skip
    state = result.stdout.strip() if result.returncode == 0 else ""
    return state if state in ("open", "done", "nopr") else "unknown"


def pr_state_for(repo: Path):
    """A `SyncCtx.pr_state` callable (branch, tip) -> state for this clone.
    The slug is worked out on each call, as the bash did."""

    def pr_state(branch: str, tip: str) -> str:
        return gh_sync_pr_state(gh_repo_slug(repo), branch, tip)

    return pr_state


def bind_pr_state(ctx: SyncCtx) -> None:
    """Makes ctx.pr_state answer from GitHub."""
    ctx.pr_state = pr_state_for(ctx.repo)


def _split_slug(slug: str) -> tuple[str, str] | None:
    """(hostname or "", owner/repo) from gh_repo_slug's answer, None if odd."""
    parts = slug.split("/")
    if any(not p for p in parts):
        return None
    if len(parts) == 2:
        return "", slug
    if len(parts) == 3:
        return parts[0], f"{parts[1]}/{parts[2]}"
    return None


def origin_visibility_problem(ctx: SyncCtx) -> str | None:
    """None when GitHub says the repo origin's push URL names is private and
    not a template, else why it is refused. Fails closed: when it cannot be
    checked (no slug, CLAUDE_SYNC_GH_REPO naming another repo, gh missing or
    logged out, the API failing or answering oddly) the reason starts "could
    not verify origin is private". Every gh call names the host with
    --hostname, so GH_HOST cannot send it elsewhere. gh's own error,
    redacted, goes to stderr. Guards the sync push only (publish.py is
    separate)."""
    unverified = "could not verify origin is private"
    slug, why = push_repo_slug(ctx.repo)
    split = _split_slug(slug) if slug else None
    if not slug or not split:
        return f"{unverified}: {why or 'could not work out owner/repo from origin push URL'}"
    forced = os.environ.get("CLAUDE_SYNC_GH_REPO", "")
    if forced and not _same_repo(forced, slug):
        return (
            f"{unverified}: CLAUDE_SYNC_GH_REPO is {forced}, but origin pushes to {slug};"
            " unset it or make it match"
        )
    host, name = split
    host = host or "github.com"
    if not gh_installed():
        return f"{unverified} ({slug}): the GitHub CLI (gh) is not installed"
    auth = _gh("auth", "status", "--hostname", host)
    if auth.returncode != 0:
        _show((auth.stdout + auth.stderr).rstrip("\n"))
        if host != "github.com":
            return (
                f"{unverified} ({slug}): gh is not logged in to {host}, an unknown host (an SSH alias?"
                " If so, use git@github.com:OWNER/REPO.git as origin's push URL)"
            )
        return f"{unverified} ({slug}): gh is not logged in"
    jq = '[.private, .is_template, .visibility] | map(tostring) | join(" ")'
    result = _gh("api", "--hostname", host, f"repos/{name}", "--jq", jq)
    if result.returncode != 0:
        _show(result.stderr)
        return f"{unverified} ({slug}): 'gh api repos/{name}' failed (see the gh error above)"
    fields = result.stdout.split()
    if len(fields) != 3 or fields[0] not in ("true", "false") or fields[1] not in ("true", "false"):
        return f"{unverified} ({slug}): gh gave an answer that could not be read"
    private, template, visibility = fields
    if private != "true" or visibility == "public":
        return f"origin ({slug}) is a public repo"
    if template == "true":
        return f"origin ({slug}) is a GitHub template repo"
    return None


def _config_get(ctx: SyncCtx, key: str) -> str:
    result = _git(ctx, "config", "--get", key)
    return result.stdout.strip() if result.returncode == 0 else ""


def _pr_fail(ctx: SyncCtx, tip: str, message: str, *lines: str) -> Fail:
    """The PR step's failure. The branch is already pushed by now."""
    return Fail(
        message,
        hint=_hint(
            *lines,
            "  This repeats on every sync until a PR is open. If you open it by hand, silence it with:",
            f"  git -C {ctx.repo} config claude-sync.prVerified {tip}",
        ),
    )


_BODY = """\
Config changes captured on machine `{host}` by claude-sync.sh.

Every later sync from that machine updates this same PR. Merge it to share the changes; each machine picks them up on its next sync or pull.

To drop the changes instead, close this PR and delete the `{branch}` branch, then on that machine run `git -C <clone> branch -D {branch} && git -C <clone> reset --hard && claude-sync.sh pull`.
"""


def ensure_pr(ctx: SyncCtx) -> None:
    """Opens a PR for the sync branch unless one is open. Never enables
    auto-merge. Needs ctx.default (set by begin_sync). Raises Fail, with the
    command to run by hand, when gh is missing, not logged in or failing;
    the branch is already pushed then. do_push only pushes after gh answered,
    so the gh-missing and logged-out cases here mean gh changed in between,
    or the branch was pushed by an earlier run."""
    sb = sync_branch(ctx.hostname)
    tip = _out(ctx, "rev-parse", f"refs/heads/{sb}")
    if tip == _config_get(ctx, "claude-sync.prVerified"):
        return
    manual = (
        f"gh pr create --repo <owner/repo> --base {ctx.default} --head {sb} "
        f"--title 'sync: {ctx.hostname}' --body 'Config sync from {ctx.hostname}.'"
    )
    slug = gh_repo_slug(ctx.repo)
    if not slug:
        raise _pr_fail(
            ctx, tip,
            f"pushed {sb}, but could not work out owner/repo from origin's URL, so no PR was opened.",
            "  Set CLAUDE_SYNC_GH_REPO=owner/repo (the repo origin pushes to), or open it by hand:", f"  {manual}",
        )  # fmt: skip
    manual = manual.replace("<owner/repo>", slug, 1)
    if not gh_installed():
        raise _pr_fail(
            ctx, tip,
            f"pushed {sb}, but the GitHub CLI (gh) is not installed, so no PR was opened.",
            "  Install gh and run 'gh auth login', then re-run 'claude-sync.sh sync', or open it by hand:",
            f"  {manual}",
        )  # fmt: skip
    auth = _gh("auth", "status")
    if auth.returncode != 0:
        shown = redact((auth.stdout + auth.stderr).rstrip("\n"))
        raise _pr_fail(
            ctx, tip,
            f"pushed {sb}, but gh is not logged in, so no PR was opened.",
            "\n".join("    " + ln for ln in shown.split("\n")),
            "  Run 'gh auth login', then re-run 'claude-sync.sh sync', or open it by hand:",
            f"  {manual}",
        )  # fmt: skip
    listed = _gh(
        "pr", "list", "--repo", slug, "--head", sb, "--state", "open",
        "--json", "url", "--jq", '.[0].url // ""',
    )  # fmt: skip
    if listed.returncode != 0:
        _show(listed.stderr)
        raise _pr_fail(
            ctx, tip,
            f"pushed {sb}, but 'gh pr list' failed, so it is unknown whether a PR is open.",
            "  Check the gh error above and re-run, or open it by hand:", f"  {manual}",
        )  # fmt: skip
    url = listed.stdout.strip()
    if url:
        print(f"claude-sync: updated the open PR {url}")
    else:
        fd, body = tempfile.mkstemp(prefix="claude-sync-pr.", suffix=".md")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(_BODY.format(host=ctx.hostname, branch=sb))
            created = _gh(
                "pr", "create", "--repo", slug, "--base", ctx.default, "--head", sb,
                "--title", f"sync: {ctx.hostname}", "--body-file", body,
            )  # fmt: skip
        finally:
            Path(body).unlink(missing_ok=True)
        if created.returncode != 0:
            _show(created.stderr)
            raise _pr_fail(
                ctx, tip,
                f"pushed {sb}, but 'gh pr create' failed (see the gh error above).",
                "  Fix it and re-run 'claude-sync.sh sync', or open it by hand:", f"  {manual}",
            )  # fmt: skip
        print(f"claude-sync: opened PR {created.stdout.strip()}")
    result = _git(ctx, "config", "claude-sync.prVerified", tip)
    if result.returncode != 0:
        raise Fail(f"could not record claude-sync.prVerified in {ctx.repo}.", hint=redact(result.stderr.strip()))


def do_push(ctx: SyncCtx) -> None:
    """Pushes the sync branch (never the default branch) and makes sure its
    PR is open. Does nothing when there is nothing pending or nothing new.
    Overwriting origin's branch is fine when it holds what this machine last
    pushed, or nothing it lacks; anything else (an edit made on GitHub) is
    kept and refused."""
    sb = sync_branch(ctx.hostname)
    if not has_sync_branch(ctx):
        return
    if not has_origin(ctx.repo):
        raise Fail(
            f"{ctx.repo} has no remote named 'origin', so {sb} was not pushed.",
            hint=f"  Add it: git -C {ctx.repo} remote add origin <url>, then re-run 'claude-sync.sh sync'.",
        )
    tip = _out(ctx, "rev-parse", f"refs/heads/{sb}")
    remote = _git(ctx, "rev-parse", "-q", "--verify", f"refs/remotes/origin/{sb}")
    remote_tip = remote.stdout.strip() if remote.returncode == 0 else ""
    if tip != remote_tip:
        problem = origin_visibility_problem(ctx)
        if problem:
            raise Fail(
                f"refusing to push {sb}: {problem}. Nothing was pushed.",
                hint=_hint(
                    "  claude-sync only pushes to a PRIVATE repo that is not a template, as your config can hold private notes and memory.",
                    f"  Point origin at one: git -C {ctx.repo} remote set-url origin <url>, then re-run 'claude-sync.sh sync'.",
                    "  If gh could not check it, install gh, run 'gh auth login' and re-run.",
                ),
            )
        last = _config_get(ctx, "claude-sync.lastPushed")
        if (
            remote_tip
            and remote_tip != last
            and not changes_contained(ctx, tip, remote_tip)
            and not changes_contained(ctx, f"refs/remotes/origin/{ctx.default}", remote_tip)
        ):
            raise Fail(
                f"origin's {sb} has commits this machine did not push (edited on GitHub?), so it was not overwritten.",
                hint=_hint(
                    "  Merge or close that PR first. To keep its changes, merge it, then re-run 'claude-sync.sh sync'.",
                    f"  To discard them: git -C {ctx.repo} push --force-with-lease=refs/heads/{sb}:{remote_tip} origin {sb}",
                ),
            )
        pushed = _git(
            ctx, "push", "-q", f"--force-with-lease=refs/heads/{sb}:{remote_tip}",
            "origin", f"{tip}:refs/heads/{sb}",
        )  # fmt: skip
        if pushed.returncode != 0:
            _show(pushed.stderr)
            raise Fail(
                f"git push of {sb} failed (see the git error above). Nothing on {ctx.default} was touched.",
                hint=f"  Fix the cause (network, auth, or {sb} changed on origin since the fetch) and re-run 'claude-sync.sh sync'.",
            )
        _git(ctx, "config", "claude-sync.lastPushed", tip)
        print(f"claude-sync: pushed {sb}.")
    ensure_pr(ctx)
