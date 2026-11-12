"""Shared pytest fixtures, the Python twin of tests/test_helper.bash.

Layout inside each test's tmp_path:
  home/            fake HOME (and USERPROFILE)
  home/.claude     fake_home.claude   (CLAUDE_SYNC_CLAUDE_HOME)
  home/.agents     fake_home.agents   (CLAUDE_SYNC_AGENTS_HOME)
  project/         CLAUDE_SYNC_PROJECT_PATH
  repo/            fake_home.repo (CLAUDE_SYNC_REPO_DIR); git_repo clones here
  origin.git/      git_repo.origin (bare)
  fake-gh/gh.py    fake_gh; calls.log and state/ sit next to it
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from claude_sync.util import run

# Taken at import, before any fixture points HOME elsewhere.
REAL_HOME = Path.home()


@pytest.fixture
def real_home() -> Path:
    """The real home directory, captured before any monkeypatching."""
    return REAL_HOME


@pytest.fixture(autouse=True)
def git_identity(monkeypatch):
    """CI runners have no git identity and the fake HOME has no ~/.gitconfig."""
    for key, value in {
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }.items():
        monkeypatch.setenv(key, value)


@dataclass
class FakeHome:
    root: Path
    claude: Path
    agents: Path
    repo: Path
    project: Path
    env: dict[str, str] = field(default_factory=dict)


@pytest.fixture
def fake_home(tmp_path, monkeypatch) -> FakeHome:
    """A fake HOME under tmp_path. Its CLAUDE_SYNC_* vars, HOME and USERPROFILE
    are set in os.environ for the test, and `.env` holds the CLAUDE_SYNC_* ones
    for passing to run(env=...). `.repo` is not created (git_repo clones there).
    """
    root = tmp_path / "home"
    home = FakeHome(
        root=root,
        claude=root / ".claude",
        agents=root / ".agents",
        repo=tmp_path / "repo",
        project=tmp_path / "project",
    )
    for d in (home.claude, home.agents, home.project):
        d.mkdir(parents=True)
    home.env = {
        "CLAUDE_SYNC_CLAUDE_HOME": str(home.claude),
        "CLAUDE_SYNC_AGENTS_HOME": str(home.agents),
        "CLAUDE_SYNC_REPO_DIR": str(home.repo),
        "CLAUDE_SYNC_PROJECT_PATH": str(home.project),
        "CLAUDE_SYNC_HOSTNAME": "host-a",
    }
    for key, value in home.env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("HOME", str(root))
    monkeypatch.setenv("USERPROFILE", str(root))
    # git must read the fake HOME's ~/.gitconfig, not one named by the caller.
    monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
    return home


# A port of install_fake_gh's stub. Paths are relative to the script, so it
# needs no env; FAKE_GH_AUTH_RC / FAKE_GH_CREATE_RC make those calls fail.
FAKE_GH_SCRIPT = r'''
import os
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG = HERE / "calls.log"
STATE = HERE / "state"


def out(text):
    sys.stdout.buffer.write((text + "\n").encode("utf-8"))


def err(text):
    sys.stderr.buffer.write((text + "\n").encode("utf-8"))


def main(args):
    with open(LOG, "a", encoding="utf-8", newline="\n") as f:
        f.write("gh " + " ".join(args) + "\n")
    opts = {}
    for i, a in enumerate(args[:-1]):
        if a in ("--head", "--body-file", "--state", "--jq"):
            opts[a] = args[i + 1]
    key = opts.get("--head", "").replace("/", "_")
    cmd = " ".join(args[:2])
    open_pr = STATE / f"open-{key}"
    if cmd == "auth status":
        rc = int(os.environ.get("FAKE_GH_AUTH_RC", "0") or 0)
        if rc:
            err("You are not logged into any GitHub hosts.")
            return rc
        out("Logged in to github.com")
    elif cmd == "pr list":
        if opts.get("--state") == "all":
            m = re.search(r"[0-9a-f]{40}", opts.get("--jq", ""))
            merged = STATE / f"merged-{key}"
            if open_pr.is_file():
                out("open")
            elif m and merged.is_file() and m.group(0) in merged.read_text(encoding="utf-8").split():
                out("done")
            else:
                out("nopr")
        elif open_pr.is_file():
            sys.stdout.buffer.write(open_pr.read_bytes())
    elif cmd == "pr create":
        rc = int(os.environ.get("FAKE_GH_CREATE_RC", "0") or 0)
        if rc:
            err("pull request create failed")
            return rc
        body = opts.get("--body-file", "")
        if body and Path(body).is_file():
            shutil.copyfile(body, STATE / f"body-{key}")
        count = STATE / "count"
        n = int(count.read_text(encoding="utf-8").strip() or 0) + 1 if count.is_file() else 1
        count.write_text(f"{n}\n", encoding="utf-8", newline="\n")
        url = f"https://github.example/test/repo/pull/{n}"
        open_pr.write_text(url + "\n", encoding="utf-8", newline="\n")
        out(url)
    else:
        err("fake gh: unexpected call: " + " ".join(args))
        return 1
    return 0


sys.exit(main(sys.argv[1:]))
'''


@pytest.fixture
def fake_gh(tmp_path, monkeypatch) -> Path:
    """A stand-in gh (a .py script; run() starts it with sys.executable).

    Every call is appended to `<path>.parent / "calls.log"` as `gh <args>`.
    Open PRs, bodies and the PR counter live in `<path>.parent / "state"`,
    keyed by head branch with "/" turned into "_" (a merged branch's tip
    shas go in `state/merged-<key>`). Sets CLAUDE_SYNC_GH to the script and
    CLAUDE_SYNC_GH_REPO=test/repo.
    """
    d = tmp_path / "fake-gh"
    (d / "state").mkdir(parents=True)
    (d / "calls.log").write_bytes(b"")
    script = d / "gh.py"
    script.write_text(FAKE_GH_SCRIPT.lstrip(), encoding="utf-8", newline="\n")
    monkeypatch.setenv("CLAUDE_SYNC_GH", str(script))
    monkeypatch.setenv("CLAUDE_SYNC_GH_REPO", "test/repo")
    return script


@dataclass
class GitRepo:
    origin: Path
    clone: Path


@pytest.fixture
def git_repo(tmp_path, fake_home) -> GitRepo:
    """Uses fake_home, so its git calls never read the real HOME's config.

    A bare origin (HEAD pinned to master) with one seed commit holding the
    repo skeleton, and a clone of it on master at tmp_path / "repo" (the
    same path as fake_home.repo).
    """
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    clone = tmp_path / "repo"
    run(["git", "init", "--bare", "-q", str(origin)], check=True)
    # Before any commit, so clones check out master whatever init.defaultBranch is.
    run(["git", "-C", str(origin), "symbolic-ref", "HEAD", "refs/heads/master"], check=True)
    run(["git", "clone", "-q", str(origin), str(seed)], check=True)
    run(["git", "-C", str(seed), "symbolic-ref", "HEAD", "refs/heads/master"], check=True)
    for sub in ("machines", "dot-claude/agents", "dot-agents/skills", "memory"):
        (seed / sub).mkdir(parents=True)
        (seed / sub / ".gitkeep").write_bytes(b"")
    run(["git", "-C", str(seed), "add", "-A"], check=True)
    run(["git", "-C", str(seed), "commit", "-q", "-m", "seed"], check=True)
    run(["git", "-C", str(seed), "push", "-q", "origin", "master"], check=True)
    run(["git", "clone", "-q", str(origin), str(clone)], check=True)
    return GitRepo(origin=origin, clone=clone)


@pytest.fixture
def world(tmp_path, git_repo, fake_gh):
    """tests/sync_pr.bats' world: origin seeded with two machines' configs,
    the fake gh (gh_state = its state dir)."""
    from world import World

    return World(tmp_path, git_repo.origin, fake_gh)
