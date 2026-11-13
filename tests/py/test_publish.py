"""publish: the Python twin of publish-public.sh (the public mirror export).

Every test publishes to a local bare repo named by CLAUDE_CONFIG_PUBLIC_REPO_URL;
nothing is ever pushed anywhere else.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

import pytest

from claude_sync import publish
from claude_sync.util import run

REPO_ROOT = Path(__file__).resolve().parents[2]


def _git(repo: Path, *args: str) -> str:
    result = run(["git", "-C", str(repo), *args])
    assert result.returncode == 0, result.stderr
    return result.stdout


def _write(root: Path, rel: str, data: bytes) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


SOURCE_FILES = {
    "install.sh": b"#!/usr/bin/env bash\necho install\n",
    "claude-sync.sh": b"#!/usr/bin/env bash\necho sync\n",
    "CLAUDE.md": b"# Working here\n",
    "LICENSE": b"MIT\n",
    ".gitattributes": b"* -text\n*.bat eol=crlf\n",
    ".gitignore": b"*.tmp\n",
    "docs/guide.md": b"line one\r\nline two\r\n",  # CRLF must survive
    "docs/tool.bat": b"@echo off\r\n",
    "lib/common.sh": b"helper() { :; }\n",
    "tests/test_x.bats": b"@test x { :; }\n",
    "dot-claude/CLAUDE.md": b"# Global\n",
    "dot-claude/settings.json": b"{}\n",
    "dot-claude/hooks/hook.sh": b"#!/bin/sh\n",
    "dot-claude/agents/a.md": b"agent\n",
    "dot-agents/skills/s/SKILL.md": b"skill\n",
    ".github/settings.yml": (
        b"repository:\n"
        b"  description: Private sync repo for host-a\n"
        b"  private: true\n"
        b"labels:\n"
        b"  - name: bug\n"
        b"    description: \"Something isn't working\"\n"
    ),
    ".github/ISSUE_TEMPLATE/config.yml": (
        b"contact_links:\n    url: https://github.com/owner-a/src-repo/security/advisories/new\n"
    ),
    ".github/workflows/ci.yml": b"name: ci\n",
    ".github/workflows/publish-public.yml": b"name: publish\n",
    "README.public.md": b"# Public readme\n",
    # Never whitelisted.
    "README.md": b"# Private readme\n",
    "machines/host-a.conf": b"PROJECT_PATH=/home/someone/projects\n",
    "machines/host-a.settings.json": b"{}\n",
    "machines/prof/NOTES.md": b"private notes\n",
    "memory/MEMORY.md": b"personal memory\n",
    "private/notes.md": b"private\n",
    "weekly-review/run.py": b"print(1)\n",
    "dot-claude/projects/x.json": b"{}\n",
    "publish-public.sh": b"#!/usr/bin/env bash\n",
}


EXECUTABLE = ("install.sh", "claude-sync.sh", "dot-claude/hooks/hook.sh")


@pytest.fixture
def world(tmp_path, fake_home, monkeypatch):
    """A committed source repo, an empty bare public remote, and a private temp root."""
    source = tmp_path / "source"
    source.mkdir()
    for rel, data in SOURCE_FILES.items():
        _write(source, rel, data)
    for rel in EXECUTABLE:
        (source / rel).chmod(0o755)
    _git(source, "init", "-q", "-b", "master")
    _git(source, "config", "core.autocrlf", "false")
    _git(source, "remote", "add", "origin", "git@github.com:owner-a/src-repo.git")
    _git(source, "add", "-A")
    # chmod does not reach the index on Windows (core.filemode=false).
    _git(source, "update-index", "--chmod=+x", "--", *EXECUTABLE)
    _git(source, "commit", "-q", "-m", "seed")

    public = tmp_path / "public.git"
    _git(tmp_path, "init", "-q", "--bare", str(public))
    _git(public, "symbolic-ref", "HEAD", "refs/heads/main")  # as on GitHub

    temp_root = tmp_path / "tmp"
    temp_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
    env = {**os.environ, "CLAUDE_CONFIG_PUBLIC_REPO_URL": str(public)}
    env.pop("CLAUDE_CONFIG_PUBLIC_BRANCH", None)
    env.pop("GITHUB_REPOSITORY", None)
    return source, public, temp_root, env


def _commit_more(source: Path, files: dict[str, bytes]) -> None:
    for rel, data in files.items():
        _write(source, rel, data)
    _git(source, "add", "-A")
    _git(source, "commit", "-q", "-m", "more")


def _pushed(public: Path, branch: str = "main") -> dict[str, tuple[str, str]]:
    """{path: (mode, blob)} of the public branch."""
    out = _git(public, "ls-tree", "-r", branch)
    tree = {}
    for line in out.splitlines():
        meta, path = line.split("\t", 1)
        mode, _kind, blob = meta.split()
        tree[path] = (mode, blob)
    return tree


def _refs(public: Path) -> str:
    return _git(public, "for-each-ref")


def test_export_contains_only_whitelist(world, capsys):
    source, public, temp_root, env = world
    assert publish.publish(source, env) == 0

    tree = _pushed(public)
    src = {}
    for line in _git(source, "ls-tree", "-r", "HEAD").splitlines():
        meta, path = line.split("\t", 1)
        mode, _kind, blob = meta.split()
        src[path] = (mode, blob)

    edited = {".github/settings.yml", ".github/ISSUE_TEMPLATE/config.yml"}
    expected = {
        p for p in src
        if any(p == w or p.startswith(w + "/") for w in publish.PATHS)
        and p != ".github/workflows/publish-public.yml"
    } | {"README.md"}
    assert set(tree) == expected
    # Plain files: same bytes and same mode (exec bit kept).
    for path in expected - edited - {"README.md"}:
        assert tree[path] == src[path], path
    assert tree["README.md"][1] == src["README.public.md"][1]
    assert tree["install.sh"][0] == "100755"

    settings = _git(public, "show", "main:.github/settings.yml")
    assert "  private: false\n" in settings
    assert "  description: Sync tool for Claude Code global config" in settings
    assert "    description: \"Something isn't working\"" in settings

    log = _git(public, "log", "-1", "--format=%s", "main")
    sha = _git(source, "rev-parse", "--short", "HEAD").strip()
    assert log.strip() == f"sync: publish from owner-a/src-repo@{sha}"
    assert list(temp_root.iterdir()) == []


def test_export_keeps_the_committed_mode_without_a_file_mode(world):
    """As on Windows: the disk mode is not the committed one, so the
    mirror must take each mode from HEAD, not from the copied file."""
    source, public, _temp_root, env = world
    _git(source, "config", "core.filemode", "false")
    for rel in EXECUTABLE:
        (source / rel).chmod(0o644)  # exec at HEAD, not on disk
    (source / "dot-claude" / "CLAUDE.md").chmod(0o755)  # the other way round
    assert _git(source, "status", "--porcelain") == ""
    assert publish.publish(source, env) == 0
    tree = _pushed(public)
    for rel in EXECUTABLE:
        assert tree[rel][0] == "100755", rel
    assert tree["dot-claude/CLAUDE.md"][0] == "100644"


def test_export_never_contains_personal_paths(world):
    source, public, _temp, env = world
    assert publish.publish(source, env) == 0
    tree = _pushed(public)
    for path in tree:
        top = path.split("/", 1)[0]
        assert top not in ("machines", "memory", "private", "weekly-review"), path
        assert "/machines/" not in f"/{path}", path
    blobs = run(["git", "-C", str(public), "grep", "-I", "-e", "someone", "main"])
    assert blobs.returncode == 1, blobs.stdout  # no match


@pytest.mark.parametrize(
    "rel",
    ["docs/.credentials.json", "lib/.claude.json", "docs/machines/host-a.settings.json"],
)
def test_export_aborts_on_denylisted_file(world, capsys, rel):
    source, public, temp_root, env = world
    _commit_more(source, {rel: b"{\"secret\": 1}\n"})
    before = _refs(public)
    assert publish.publish(source, env) == 1
    err = capsys.readouterr().err
    assert Path(rel).name in err
    assert "Nothing was published" in err
    assert _refs(public) == before == ""  # nothing pushed
    assert list(temp_root.iterdir()) == []


def test_guard_trip_pushes_nothing_to_a_remote_with_history(world, capsys):
    source, public, _temp, env = world
    assert publish.publish(source, env) == 0
    before = _refs(public)
    _commit_more(source, {"docs/new.md": b"new\n", "tests/.credentials.json": b"x\n"})
    assert publish.publish(source, env) == 1
    assert _refs(public) == before


def test_ignored_files_under_whitelisted_paths_are_not_exported(world, tmp_path):
    source, public, _temp, env = world
    ignored = {
        "tests/__pycache__/x.cpython-312.pyc": str(tmp_path).encode() + b"/home-path-in-pyc",
        "tests/py/__pycache__/y.pyc": b"cache",
        "docs/.env": b"TOKEN=abc\n",
        "lib/key.pem": b"-----BEGIN PRIVATE KEY-----\n",
        "dot-claude/hooks/secrets/api.txt": b"secret\n",
    }
    # Ignored only by the source clone's own exclude file (as a global
    # excludes file would), so the exported .gitignore cannot hide them.
    _write(source, ".git/info/exclude", b"__pycache__/\n.env\n*.pem\nsecrets/\n")
    for rel, data in ignored.items():
        _write(source, rel, data)
    assert _git(source, "status", "--porcelain") == ""  # all ignored: the tree is clean
    _commit_more(source, {"docs/tracked.md": b"tracked\n"})
    assert publish.publish(source, env) == 0
    tree = _pushed(public)
    assert "docs/tracked.md" in tree
    for rel in ignored:
        assert rel not in tree, rel
    assert not any("__pycache__" in p or p.endswith((".env", ".pem")) for p in tree)


def test_symlinks_are_not_exported(world, tmp_path, capsys):
    source, public, _temp, env = world
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"outside secret\n")
    try:
        os.symlink(outside / "secret.txt", source / "docs" / "leak.md")
        os.symlink(outside, source / "lib" / "leakdir", target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are not available here")
    _git(source, "add", "-A")
    _git(source, "commit", "-q", "-m", "links")
    assert publish.publish(source, env) == 0
    tree = _pushed(public)
    assert not any(p.startswith(("docs/leak.md", "lib/leakdir")) for p in tree)
    assert all(mode != "120000" for mode, _ in tree.values())
    grep = run(["git", "-C", str(public), "grep", "-I", "-e", "outside secret", "main"])
    assert grep.returncode == 1
    err = capsys.readouterr().err
    assert "leak.md" in err and "leakdir" in err


def test_line_endings_and_gitattributes_kept(world):
    source, public, _temp, env = world
    assert publish.publish(source, env) == 0
    for rel in (".gitattributes", "docs/guide.md", "docs/tool.bat"):
        assert _raw(public, rel) == SOURCE_FILES[rel], rel


def _raw(public: Path, rel: str) -> bytes:
    import subprocess

    return subprocess.run(
        ["git", "-C", str(public), "cat-file", "blob", f"main:{rel}"],
        capture_output=True, check=True,
    ).stdout


def test_no_changes_second_run(world, capsys):
    source, public, _temp, env = world
    assert publish.publish(source, env) == 0
    before = _refs(public)
    assert publish.publish(source, env) == 0
    assert "No changes to publish." in capsys.readouterr().out
    assert _refs(public) == before


def test_regenerates_from_scratch(world):
    source, public, _temp, env = world
    assert publish.publish(source, env) == 0
    (source / "docs" / "tool.bat").unlink()
    _git(source, "add", "-A")
    _git(source, "commit", "-q", "-m", "drop")
    assert publish.publish(source, env) == 0
    assert "docs/tool.bat" not in _pushed(public)


def test_branch_env_var(world):
    source, public, _temp, env = world
    assert publish.publish(source, {**env, "CLAUDE_CONFIG_PUBLIC_BRANCH": "trunk"}) == 0
    assert "refs/heads/trunk" in _refs(public)


def test_refuses_uncommitted_changes(world, capsys):
    source, public, temp_root, env = world
    (source / "docs" / "dirty.md").write_bytes(b"x\n")
    assert publish.publish(source, env) == 1
    assert "uncommitted changes" in capsys.readouterr().err
    assert _refs(public) == ""


def test_needs_public_repo_url(world, capsys):
    source, public, _temp, env = world
    env = {k: v for k, v in env.items() if k != "CLAUDE_CONFIG_PUBLIC_REPO_URL"}
    assert publish.publish(source, {**env, "CLAUDE_CONFIG_PUBLIC_REPO_URL": ""}) == 1
    err = capsys.readouterr().err
    assert "CLAUDE_CONFIG_PUBLIC_REPO_URL" in err
    assert "Traceback" not in err


def test_failed_push_is_redacted(world, capsys):
    source, _public, _temp, env = world
    url = "https://user:ghp_abcdefghijklmnopqrstuvwxyz0123@127.0.0.1:9/none.git"
    rc = publish.publish(source, {**env, "CLAUDE_CONFIG_PUBLIC_REPO_URL": url})
    assert rc == 1
    captured = capsys.readouterr()
    assert "ghp_abcdefghijklmnopqrstuvwxyz0123" not in captured.out + captured.err
    assert "Traceback" not in captured.err


def test_issue_template_rewrite():
    data = b"    url: https://github.com/owner-a/src-repo/security/advisories/new\r\n"
    out = publish.rewrite_issue_config(data, "owner-a/src-repo", "owner-a/src-repo-public")
    assert out == b"    url: https://github.com/owner-a/src-repo-public/security/advisories/new\r\n"


@pytest.mark.parametrize(
    ("url", "slug"),
    [
        ("git@github.com:owner-a/repo-b.git", "owner-a/repo-b"),
        ("https://github.com/owner-a/repo-b.git", "owner-a/repo-b"),
        ("https://x:tok@github.com/owner-a/repo-b", "owner-a/repo-b"),
        ("/tmp/public.git", ""),
    ],
)
def test_github_slug(url, slug):
    assert publish.github_slug(url) == slug


def test_whitelist_matches_bash_script():
    script = REPO_ROOT / "publish-public.sh"
    if not script.is_file():
        pytest.skip("publish-public.sh is not part of the public mirror")
    text = script.read_text(encoding="utf-8")
    block = re.search(r"^PATHS=\(\n(.*?)^\)", text, re.S | re.M).group(1)
    bash_paths = tuple(line.strip() for line in block.splitlines() if line.startswith("  "))
    assert publish.PATHS == bash_paths


def test_workflow_triggers_match_whitelist():
    """publish-public.yml runs on a push touching any whitelisted path, and
    on nothing else (so sync PRs that touch only memory/ or machines/ never
    publish). Directories trigger as <dir>/**."""
    workflow = REPO_ROOT / ".github" / "workflows" / "publish-public.yml"
    if not workflow.is_file():
        pytest.skip("publish-public.yml is not part of the public mirror")
    text = workflow.read_text(encoding="utf-8")
    block = re.search(r"^    paths:\n((?:      - .*\n)+)", text, re.M).group(1)
    triggers = [line.strip().removeprefix("- ") for line in block.splitlines()]
    assert len(triggers) == len(set(triggers))
    # Each whitelisted path triggers once, as itself (a file) or <dir>/**.
    for p in publish.PATHS:
        assert (p in triggers) != (p + "/**" in triggers), p
    # The README the mirror gets, and the bash publisher, also trigger a run.
    allowed = {"README.public.md", "publish-public.sh"} | set(publish.PATHS) | {p + "/**" for p in publish.PATHS}
    assert set(triggers) <= allowed
