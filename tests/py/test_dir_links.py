"""Folder links, with a junction on Windows and a symlink elsewhere.

The symlink tests elsewhere skip on Windows (a symlink needs admin rights or
Developer Mode there), so the `is_junction()` branches never ran in the
Windows CI job. These tests make the folder link with `make_dir_link`, which
needs no privilege, and so run on both. File links cannot be junctions and
stay in the symlink tests.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from helpers import make_dir_link

from claude_sync import profile
from claude_sync.syncfiles import mirror
from claude_sync.util import Fail, check_real_dir


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode())


def test_a_source_folder_link_is_skipped_with_a_warning(tmp_path, capsys):
    src, dst, outside = tmp_path / "src", tmp_path / "dst", tmp_path / "outside"
    _write(outside / "f.txt", "outside\n")
    _write(src / "real.txt", "real\n")
    make_dir_link(src / "linked", outside)
    mirror(src, dst, delete=True)
    assert (dst / "real.txt").read_bytes() == b"real\n"
    assert not os.path.lexists(dst / "linked")
    assert "linked" in capsys.readouterr().err
    assert (outside / "f.txt").read_bytes() == b"outside\n"


def test_delete_removes_a_receiver_folder_link_without_following_it(tmp_path):
    src, dst, precious = tmp_path / "src", tmp_path / "dst", tmp_path / "precious"
    _write(precious / "keep.txt", "do not delete\n")
    src.mkdir()
    dst.mkdir()
    make_dir_link(dst / "linkdir", precious)
    mirror(src, dst, delete=True)
    assert not os.path.lexists(dst / "linkdir")
    assert (precious / "keep.txt").read_bytes() == b"do not delete\n"


def test_a_receiver_folder_link_is_replaced_by_the_source_file_not_written_through(tmp_path):
    src, dst, precious = tmp_path / "src", tmp_path / "dst", tmp_path / "precious"
    _write(src / "name", "a file from the source\n")
    _write(precious / "keep.txt", "keep\n")
    dst.mkdir()
    make_dir_link(dst / "name", precious)
    mirror(src, dst, delete=True)
    assert (dst / "name").is_file() and not (dst / "name").is_symlink()
    assert (dst / "name").read_bytes() == b"a file from the source\n"
    assert sorted(p.name for p in precious.iterdir()) == ["keep.txt"]


def test_check_real_dir_refuses_a_folder_link(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    make_dir_link(tmp_path / "machines", outside)
    with pytest.raises(Fail, match="is a link or leads outside"):
        check_real_dir(tmp_path / "machines")
    check_real_dir(tmp_path / "missing")  # a missing folder is fine


def test_profile_dir_refuses_a_machines_folder_link(tmp_path):
    outside = tmp_path / "outside"
    (outside / "host-a").mkdir(parents=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    make_dir_link(repo / "machines", outside)
    with pytest.raises(Fail, match="is a link or leads outside"):
        profile.profile_dir(repo, "host-a")
    with pytest.raises(Fail, match="is a link or leads outside"):
        profile.profile_dir(repo, "host-new")


def test_profile_dir_refuses_a_profile_folder_link(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    repo = tmp_path / "repo"
    (repo / "machines").mkdir(parents=True)
    make_dir_link(repo / "machines" / "host-a", outside)
    with pytest.raises(Fail, match="is a link or leads outside"):
        profile.profile_dir(repo, "host-a")
