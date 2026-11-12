"""The repo's own .gitattributes must leave synced files byte-for-byte alone.

Each test builds a throwaway repo with a copy of the real .gitattributes and
checks what git stores and checks out. core.autocrlf is forced off, as the
Python's own git calls do, so a system-wide autocrlf=true on a Windows runner
does not mask the rules.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from helpers import git

REAL_ATTRS = Path(__file__).resolve().parents[2] / ".gitattributes"
_NO_AUTOCRLF = ("-c", "core.autocrlf=false", "-c", "core.safecrlf=false")

# Synced paths with content that the old rules would have rewritten.
SYNCED = {
    "dot-claude/CLAUDE.md": b"crlf line\r\nlf line\n",
    "dot-claude/hooks/crlf.ps1": b"Write-Host hi\r\n",
    "dot-claude/hooks/lf.ps1": b"Write-Host hi\n",
    "dot-claude/settings.json": b'{\r\n  "a": 1\r\n}\r\n',
    "dot-agents/skills/s/SKILL.md": b"# skill\r\n",
    "memory/note.md": b"remember\r\n",
}


def _git(repo: Path, *args: str):
    return git("-C", repo, *_NO_AUTOCRLF, *args)


@pytest.fixture
def attrs_repo(tmp_path, fake_home) -> Path:
    repo = tmp_path / "attrs"
    repo.mkdir()
    git("init", "-q", repo)
    shutil.copyfile(REAL_ATTRS, repo / ".gitattributes")
    return repo


def _commit_and_recheckout(repo: Path, files: dict[str, bytes]) -> None:
    for rel, data in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "files")
    for rel in files:
        (repo / rel).unlink()
    _git(repo, "checkout", "--", ".")


def test_synced_files_keep_their_exact_bytes(attrs_repo):
    _commit_and_recheckout(attrs_repo, SYNCED)
    for rel, data in SYNCED.items():
        assert (attrs_repo / rel).read_bytes() == data, rel
        out = _git(attrs_repo, "ls-files", "--eol", "--", rel).stdout
        assert "attr/-text" in out, out
    # Stored as given too, not just checked out as given.
    shown = _git(attrs_repo, "ls-files", "--eol", "--", "dot-claude/CLAUDE.md").stdout
    assert shown.startswith("i/mixed"), shown
    crlf = _git(attrs_repo, "ls-files", "--eol", "--", "memory/note.md").stdout
    assert crlf.startswith("i/crlf"), crlf


def test_text_is_unset_for_each_synced_root(attrs_repo):
    out = _git(attrs_repo, "check-attr", "text", "--", *SYNCED).stdout
    for rel in SYNCED:
        assert f"{rel}: text: unset" in out, out


def test_other_text_files_are_still_normalised(attrs_repo):
    files = {"docs/x.md": b"one\r\ntwo\r\n", "scripts/x.ps1": b"Write-Host hi\n"}
    _commit_and_recheckout(attrs_repo, files)
    # docs/x.md: stored as LF (text), checked out as LF (eol=lf).
    md = _git(attrs_repo, "ls-files", "--eol", "--", "docs/x.md").stdout
    assert md.startswith("i/lf"), md
    assert (attrs_repo / "docs/x.md").read_bytes() == b"one\ntwo\n"
    # *.ps1 outside the synced roots still checks out as CRLF.
    assert (attrs_repo / "scripts/x.ps1").read_bytes() == b"Write-Host hi\r\n"
