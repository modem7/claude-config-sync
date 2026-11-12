"""Port of tests/safety.bats plus depth and symlink cases."""

from __future__ import annotations

import os

import pytest

from claude_sync.safety import assert_no_denylisted_files, assert_public_tree_clean
from claude_sync.util import Fail


def _write(path, text="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_denylist_passes_on_clean_tree(tmp_path):
    _write(tmp_path / "dot-claude" / "CLAUDE.md", "hello")
    assert_no_denylisted_files(tmp_path)


def test_denylist_catches_credentials(tmp_path):
    _write(tmp_path / "dot-claude" / ".credentials.json", "secret")
    with pytest.raises(Fail, match=r"\.credentials\.json"):
        assert_no_denylisted_files(tmp_path)


def test_match_at_depth_three(tmp_path):
    _write(tmp_path / "nested" / "deep" / "deeper" / ".claude.json", "secret")
    with pytest.raises(Fail, match=r"\.claude\.json"):
        assert_no_denylisted_files(tmp_path)


def test_denylist_does_not_follow_symlinks(tmp_path):
    outside = tmp_path / "outside"
    _write(outside / ".claude.json")
    tree = tmp_path / "tree"
    tree.mkdir()
    try:
        os.symlink(outside, tree / "link", target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    assert_no_denylisted_files(tree)


def test_public_clean_passes(tmp_path):
    _write(tmp_path / "dot-agents" / "skills" / "find-skills" / "SKILL.md", "skill")
    (tmp_path / ".git" / "machines").mkdir(parents=True)
    assert_public_tree_clean(tmp_path)


@pytest.mark.parametrize("top", ["machines", "memory", "private", "weekly-review"])
def test_public_blocks_top_level_dirs(tmp_path, top):
    _write(tmp_path / top / "host-a" / "f.txt")
    with pytest.raises(Fail) as exc:
        assert_public_tree_clean(tmp_path)
    assert f"./{top}" in str(exc.value)


def test_public_blocks_nested_machines_settings(tmp_path):
    _write(tmp_path / "docs" / "machines" / "host-a.settings.json", "{}")
    with pytest.raises(Fail, match=r"\./docs/machines/host-a\.settings\.json"):
        assert_public_tree_clean(tmp_path)


def test_machines_settings_json_blocked_anywhere(tmp_path):
    _write(tmp_path / "a" / "b" / "machines" / "c" / "host-b.settings.json", "{}")
    with pytest.raises(Fail, match=r"host-b\.settings\.json"):
        assert_public_tree_clean(tmp_path)


def test_nested_weekly_review_dir_is_allowed(tmp_path):
    (tmp_path / "docs" / "weekly-review").mkdir(parents=True)
    assert_public_tree_clean(tmp_path)


def test_settings_json_outside_machines_is_allowed(tmp_path):
    _write(tmp_path / "docs" / "host-a.settings.json", "{}")
    assert_public_tree_clean(tmp_path)


def test_public_does_not_follow_symlinks(tmp_path):
    outside = tmp_path / "outside"
    _write(outside / "machines" / "x.settings.json")
    tree = tmp_path / "tree"
    tree.mkdir()
    try:
        os.symlink(outside, tree / "link", target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    assert_public_tree_clean(tree)
