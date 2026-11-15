"""scripts/gitattributes_check.py: swapping .gitattributes must change nothing real.

Each test builds a throwaway repo under tmp_path, runs the checker against a
replacement .gitattributes and checks the exit code and report.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from helpers import git

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "gitattributes_check.py"
_NO_AUTOCRLF = ("-c", "core.autocrlf=false", "-c", "core.safecrlf=false")

BASE_ATTRS = (
    "# Line endings.\n"
    "* text=auto eol=lf\n"
    "\n"
    "*.cmd text eol=crlf\n"
    "*.md text\n"
    "memory/** -text\n"
)
FILES = {
    "run.cmd": b"@echo off\r\necho hi\r\n",
    "docs/read me.md": b"one\ntwo\n",
    "memory/note.md": b"remember\r\n",
    "src/x.py": b"print('hi')\n",
}


def _git(repo: Path, *args: str):
    return git("-C", repo, *_NO_AUTOCRLF, *args)


@pytest.fixture
def repo(tmp_path, fake_home) -> Path:
    path = tmp_path / "attrs"
    path.mkdir()
    git("init", "-q", path)
    (path / ".gitattributes").write_text(BASE_ATTRS, encoding="utf-8", newline="\n")
    for rel, data in FILES.items():
        target = path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "files")
    return path


def _check(repo: Path, tmp_path: Path, attrs: str, *extra: str):
    new = tmp_path / "new.gitattributes"
    new.write_text(attrs, encoding="utf-8", newline="\n")
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(repo), str(new), *extra],
        capture_output=True,
        text=True,
        check=False,
    )


def test_identical_file_passes(repo, tmp_path):
    result = _check(repo, tmp_path, BASE_ATTRS)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Real changes         0" in result.stdout
    assert "Rules (old -> new)   4 -> 4" in result.stdout


def test_soft_only_change_passes_and_shows_with_verbose(repo, tmp_path):
    attrs = BASE_ATTRS.replace("*.md text\n", "")
    result = _check(repo, tmp_path, attrs, "--verbose")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Soft attribute changes" in result.stdout
    assert "'docs/read me.md': text: set -> auto" in result.stdout
    assert "Renormalize changes  0" in result.stdout


def test_dropping_cmd_crlf_rule_is_a_real_change(repo, tmp_path):
    attrs = BASE_ATTRS.replace("*.cmd text eol=crlf\n", "")
    result = _check(repo, tmp_path, attrs)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "'run.cmd': eol: crlf -> lf" in result.stdout
    assert "Result               FAIL" in result.stdout


def test_dropping_minus_text_rule_is_a_real_change(repo, tmp_path):
    attrs = BASE_ATTRS.replace("memory/** -text\n", "")
    result = _check(repo, tmp_path, attrs)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "'memory/note.md': text: unset -> set" in result.stdout
    # The CRLF note would be restaged as LF.
    assert "Paths changed by git add --renormalize" in result.stdout
    assert "'memory/note.md'" in result.stdout.split("renormalize:")[1]


def test_missing_new_file_is_a_usage_error(repo, tmp_path):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(repo), str(tmp_path / "nope")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "not a file" in result.stderr


def test_repo_is_left_untouched(repo, tmp_path):
    head = _git(repo, "rev-parse", "HEAD").stdout
    status = _git(repo, "status", "--porcelain=v1", "-z").stdout
    attrs_bytes = (repo / ".gitattributes").read_bytes()
    _check(repo, tmp_path, "* text=auto eol=lf\n")
    assert _git(repo, "rev-parse", "HEAD").stdout == head
    assert _git(repo, "status", "--porcelain=v1", "-z").stdout == status
    assert (repo / ".gitattributes").read_bytes() == attrs_bytes
