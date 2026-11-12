"""Tests for claude_sync.syncfiles, ported from tests/sync_files.bats plus
cases bash never needed (CRLF, spaces, stale same-size files, interrupted
copies, symlinks)."""

from __future__ import annotations

import os
import shutil
import stat
import sys

import pytest

from claude_sync import syncfiles
from claude_sync.syncfiles import mirror, sync_apply, sync_capture
from claude_sync.util import Fail

POSIX = os.name == "posix"


def _write(path, data: bytes | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode("utf-8")
    path.write_bytes(data)


def _is_exec(path) -> bool:
    return bool(path.stat().st_mode & stat.S_IXUSR)


@pytest.fixture
def env(tmp_path):
    """The bats setup(): a live machine with CLAUDE.md, settings, a skill,
    memory and an executable hook, plus an empty repo skeleton."""

    class E:
        repo = tmp_path / "repo"
        claude = tmp_path / "claude_home"
        agents = tmp_path / "agents_home"
        memory = tmp_path / "claude_home" / "projects" / "-tmp-project" / "memory"

    for d in ("dot-claude/agents", "dot-claude/hooks", "dot-agents/skills", "memory"):
        (E.repo / d).mkdir(parents=True)
    (E.claude / "hooks").mkdir(parents=True)
    (E.agents / "skills" / "release").mkdir(parents=True)
    E.memory.mkdir(parents=True)
    _write(E.claude / "CLAUDE.md", "# global instructions\n")
    _write(E.claude / "settings.json", '{"theme":"dark"}\n')
    _write(E.agents / "skills" / "release" / "SKILL.md", "release skill body\n")
    _write(E.memory / "MEMORY.md", "# Memory Index\n")
    hook = E.claude / "hooks" / "example-guard.sh"
    _write(hook, "#!/bin/bash\necho hook\n")
    hook.chmod(0o755)
    return E


def capture(e, excludes=None):
    sync_capture(e.claude, e.agents, e.repo, excludes, memory_dir=e.memory)


def apply(e, mirror_mode=True, claude=None, agents=None, memory=None):
    sync_apply(
        e.repo,
        claude or e.claude,
        agents or e.agents,
        mirror=mirror_mode,
        profile_excludes=None,
        memory_dir=memory or e.memory,
    )


# --- ported from sync_files.bats -------------------------------------------


def test_capture_copies_claude_md_and_settings(env):
    capture(env)
    assert (
        env.repo / "dot-claude/CLAUDE.md"
    ).read_bytes() == b"# global instructions\n"
    assert (env.repo / "dot-claude/settings.json").read_bytes() == b'{"theme":"dark"}\n'


def test_capture_copies_skills(env):
    capture(env)
    assert (
        env.repo / "dot-agents/skills/release/SKILL.md"
    ).read_bytes() == b"release skill body\n"


def test_capture_copies_memory(env):
    capture(env)
    assert (env.repo / "memory/MEMORY.md").read_bytes() == b"# Memory Index\n"


def test_capture_copies_hooks_keeping_exec_bit(env):
    capture(env)
    hook = env.repo / "dot-claude/hooks/example-guard.sh"
    assert hook.read_bytes() == b"#!/bin/bash\necho hook\n"
    if POSIX:
        assert _is_exec(hook)


def test_apply_to_fresh_machine(env, tmp_path):
    capture(env)
    fresh_claude = tmp_path / "fresh_claude_home"
    fresh_agents = tmp_path / "fresh_agents_home"
    fresh_memory = fresh_claude / "projects" / "-tmp-work" / "memory"
    apply(env, claude=fresh_claude, agents=fresh_agents, memory=fresh_memory)
    assert (fresh_claude / "CLAUDE.md").read_bytes() == b"# global instructions\n"
    assert (
        fresh_agents / "skills/release/SKILL.md"
    ).read_bytes() == b"release skill body\n"
    assert (fresh_memory / "MEMORY.md").read_bytes() == b"# Memory Index\n"
    assert (fresh_claude / "agents").is_dir()
    if POSIX:
        assert _is_exec(fresh_claude / "hooks/example-guard.sh")


@pytest.mark.parametrize("missing", ["agents", "hooks"])
def test_capture_tolerates_missing_live_dir(env, missing):
    import shutil

    shutil.rmtree(env.claude / missing, ignore_errors=True)
    capture(env)
    assert (env.repo / "dot-claude" / missing).is_dir()


def test_capture_tolerates_missing_everything(tmp_path):
    repo = tmp_path / "repo"
    sync_capture(tmp_path / "c", tmp_path / "a", repo, None, memory_dir=tmp_path / "m")
    for d in ("dot-claude/agents", "dot-claude/hooks", "dot-agents/skills", "memory"):
        assert (repo / d).is_dir()
    assert not (repo / "dot-claude/CLAUDE.md").exists()


def test_apply_mirror_true_deletes_local_only_hook(env):
    capture(env)
    _write(env.claude / "hooks/local-only.sh", "local only hook\n")
    apply(env, mirror_mode=True)
    assert not (env.claude / "hooks/local-only.sh").exists()
    assert (env.claude / "hooks/example-guard.sh").is_file()


def test_apply_mirror_false_keeps_local_only_hook(env):
    capture(env)
    _write(env.claude / "hooks/local-only.sh", "local only hook\n")
    apply(env, mirror_mode=False)
    assert (env.claude / "hooks/local-only.sh").is_file()


def test_no_delete_when_mirror_false(env):
    _write(env.agents / "skills/local-only/SKILL.md", "local only skill\n")
    apply(env, mirror_mode=False)
    assert (
        env.agents / "skills/local-only/SKILL.md"
    ).read_bytes() == b"local only skill\n"
    assert (env.agents / "skills/release/SKILL.md").is_file()


def test_apply_mirror_true_deletes_local_only_skill(env):
    _write(env.agents / "skills/local-only/SKILL.md", "local only skill\n")
    apply(env, mirror_mode=True)
    assert not (env.agents / "skills/local-only").exists()


def test_capture_with_excludes_neither_copies_nor_deletes(env):
    capture(env)
    _write(env.agents / "skills/machine-only/SKILL.md", "machine only\n")
    _write(env.agents / "skills/release/SKILL.md", "local copy\n")
    (env.claude / "hooks/example-guard.sh").unlink()
    excludes = {"skills": {"machine-only", "release"}, "hooks": {"example-guard.sh"}}
    capture(env, excludes)
    assert not (env.repo / "dot-agents/skills/machine-only").exists()
    assert (
        env.repo / "dot-agents/skills/release/SKILL.md"
    ).read_bytes() == b"release skill body\n"
    hook = env.repo / "dot-claude/hooks/example-guard.sh"
    assert hook.is_file()
    if POSIX:
        assert _is_exec(hook)


def test_capture_excludes_are_per_kind(env):
    """A skill exclude must not hide a hook of the same name."""
    _write(env.claude / "hooks/release", "hook named like a skill\n")
    capture(env, {"skills": {"release"}})
    assert (env.repo / "dot-claude/hooks/release").is_file()
    assert not (env.repo / "dot-agents/skills/release").exists()


def test_apply_ignores_profile_excludes_by_default(env):
    """Bash sync_apply takes no excludes: the shared copy is always restored."""
    capture(env)
    (env.claude / "hooks/example-guard.sh").unlink()
    apply(env)
    assert (env.claude / "hooks/example-guard.sh").is_file()


# --- mirror() ----------------------------------------------------------------


def test_mirror_deletes_only_missing(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    _write(src / "keep.txt", "new\n")
    _write(src / "sub/inner.txt", "inner\n")
    _write(dst / "keep.txt", "old\n")
    _write(dst / "gone.txt", "gone\n")
    _write(dst / "sub/inner.txt", "inner\n")
    _write(dst / "sub/stale.txt", "stale\n")
    _write(dst / "olddir/x.txt", "x\n")
    mirror(src, dst, delete=True)
    assert sorted(p.relative_to(dst).as_posix() for p in dst.rglob("*")) == [
        "keep.txt",
        "sub",
        "sub/inner.txt",
    ]
    assert (dst / "keep.txt").read_bytes() == b"new\n"


def test_mirror_without_delete_merges(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    _write(src / "a.txt", "a\n")
    _write(dst / "b.txt", "b\n")
    mirror(src, dst, delete=False)
    assert (dst / "a.txt").is_file() and (dst / "b.txt").is_file()


def test_mirror_creates_missing_dst(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "deep/dst"
    _write(src / "a.txt", "a\n")
    mirror(src, dst, delete=True)
    assert (dst / "a.txt").read_bytes() == b"a\n"


def test_exclude_is_top_level_only(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    _write(src / "skip/a.txt", "top\n")
    _write(src / "other/skip/b.txt", "nested\n")
    mirror(src, dst, delete=True, exclude={"skip"})
    assert not (dst / "skip").exists()
    assert (dst / "other/skip/b.txt").read_bytes() == b"nested\n"


def test_excluded_name_survives_delete(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    _write(src / "owned/SKILL.md", "shared\n")
    _write(dst / "owned/SKILL.md", "receiver\n")
    _write(dst / "owned2.txt", "receiver only\n")
    src.mkdir(exist_ok=True)
    mirror(src, dst, delete=True, exclude={"owned", "owned2.txt"})
    assert (dst / "owned/SKILL.md").read_bytes() == b"receiver\n"
    assert (dst / "owned2.txt").read_bytes() == b"receiver only\n"


def test_gitkeep_excluded_on_capture(env):
    _write(env.claude / "agents/.gitkeep", "")
    _write(env.agents / "skills/release/.gitkeep", "")
    # The repo's own .gitkeep files, top level and nested, survive delete.
    _write(env.repo / "dot-claude/agents/.gitkeep", "")
    _write(env.repo / "dot-agents/skills/release/.gitkeep", "")
    _write(env.repo / "memory/.gitkeep", "")
    (env.memory / ".gitkeep").write_bytes(b"live")
    capture(env)
    assert (env.repo / "dot-claude/agents/.gitkeep").read_bytes() == b""
    assert (env.repo / "dot-agents/skills/release/.gitkeep").is_file()
    assert (env.repo / "memory/.gitkeep").read_bytes() == b""
    # And one that only exists live is not copied.
    _write(env.agents / "skills/newskill/.gitkeep", "")
    _write(env.agents / "skills/newskill/SKILL.md", "s\n")
    capture(env)
    assert not (env.repo / "dot-agents/skills/newskill/.gitkeep").exists()


def test_apply_copies_gitkeep(env):
    """Bash apply excludes nothing, so .gitkeep reaches the live dirs."""
    _write(env.repo / "dot-claude/agents/.gitkeep", "")
    apply(env)
    assert (env.claude / "agents/.gitkeep").is_file()


def test_crlf_bytes_preserved(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    data = b"line one\r\nline two\r\nno newline \xe2\x9c\x93\r"
    _write(src / "crlf.md", data)
    mirror(src, dst, delete=True)
    assert (dst / "crlf.md").read_bytes() == data


def test_crlf_in_claude_md(env):
    _write(env.claude / "CLAUDE.md", b"a\r\nb\r\n")
    capture(env)
    assert (env.repo / "dot-claude/CLAUDE.md").read_bytes() == b"a\r\nb\r\n"


def test_path_with_spaces(tmp_path):
    src, dst = tmp_path / "src with space", tmp_path / "dst with space"
    _write(src / "my skill/SKILL file.md", "spaced\n")
    mirror(src, dst, delete=True, exclude={"keep me"})
    _write(dst / "keep me/x", "x\n")
    mirror(src, dst, delete=True, exclude={"keep me"})
    assert (dst / "my skill/SKILL file.md").read_bytes() == b"spaced\n"
    assert (dst / "keep me/x").is_file()


def test_mtime_preserved(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    _write(src / "a.txt", "a\n")
    os.utime(src / "a.txt", ns=(1_600_000_000_000_000_000, 1_600_000_000_000_000_000))
    mirror(src, dst, delete=True)
    assert (dst / "a.txt").stat().st_mtime_ns == (src / "a.txt").stat().st_mtime_ns


def test_same_size_and_mtime_different_content_is_detected(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    _write(src / "a.txt", "shared\n")
    _write(dst / "a.txt", "LOCAL!\n")
    t = 1_600_000_000_000_000_000
    os.utime(src / "a.txt", ns=(t, t))
    os.utime(dst / "a.txt", ns=(t, t))
    mirror(src, dst, delete=True)
    assert (dst / "a.txt").read_bytes() == b"shared\n"


def test_identical_file_is_not_rewritten(tmp_path, monkeypatch):
    src, dst = tmp_path / "src", tmp_path / "dst"
    _write(src / "a.txt", "same\n")
    mirror(src, dst, delete=True)
    calls = []
    real = os.replace
    monkeypatch.setattr(
        syncfiles.os, "replace", lambda a, b: (calls.append(b), real(a, b))
    )
    mirror(src, dst, delete=True)
    assert calls == []


def test_same_bytes_different_mtime_updates_mtime(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    _write(src / "a.txt", "same\n")
    _write(dst / "a.txt", "same\n")
    os.utime(src / "a.txt", ns=(1_500_000_000_000_000_000, 1_500_000_000_000_000_000))
    mirror(src, dst, delete=True)
    assert (dst / "a.txt").stat().st_mtime_ns == (src / "a.txt").stat().st_mtime_ns


@pytest.mark.skipif(not POSIX, reason="exec bit is POSIX only")
def test_exec_bit_change_alone_is_applied(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    _write(src / "h.sh", "x\n")
    mirror(src, dst, delete=True)
    (src / "h.sh").chmod(0o755)
    mirror(src, dst, delete=True)
    assert _is_exec(dst / "h.sh")


def test_interrupted_copy_leaves_no_partial_file(tmp_path, monkeypatch):
    src, dst = tmp_path / "src", tmp_path / "dst"
    _write(src / "a.txt", "new content\n")
    _write(dst / "a.txt", "old\n")

    def boom(a, b):
        raise OSError("interrupted")

    monkeypatch.setattr(syncfiles.os, "replace", boom)
    with pytest.raises(OSError):
        mirror(src, dst, delete=True)
    assert sorted(p.name for p in dst.iterdir()) == ["a.txt"]
    assert (dst / "a.txt").read_bytes() == b"old\n"


def test_overwrites_read_only_file(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    _write(src / "a.txt", "new\n")
    _write(dst / "a.txt", "old!\n")
    (dst / "a.txt").chmod(0o444)
    mirror(src, dst, delete=True)
    assert (dst / "a.txt").read_bytes() == b"new\n"


def test_deletes_read_only_tree(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    _write(dst / "old/ro.txt", "ro\n")
    (dst / "old/ro.txt").chmod(0o444)
    mirror(src, dst, delete=True)
    assert not (dst / "old").exists()


def test_type_flip_file_to_dir_and_back(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    _write(src / "x/inner.txt", "dir now\n")
    _write(dst / "x", "was a file\n")
    _write(src / "y", "file now\n")
    _write(dst / "y/inner.txt", "was a dir\n")
    _write(src / "z", "file now\n")
    (dst / "z").mkdir(parents=True)
    mirror(src, dst, delete=True)
    assert (dst / "x/inner.txt").read_bytes() == b"dir now\n"
    assert (dst / "y").read_bytes() == b"file now\n"
    assert (dst / "z").read_bytes() == b"file now\n"


def test_no_delete_keeps_non_empty_folder_on_type_flip(tmp_path, capsys):
    """rsync -a without --delete refuses to remove a non-empty folder."""
    src, dst = tmp_path / "src", tmp_path / "dst"
    _write(src / "y", "file now\n")
    _write(dst / "y/machine-only.txt", "machine only\n")
    _write(src / "empty", "file now\n")
    (dst / "empty").mkdir(parents=True)
    _write(src / "x/inner.txt", "dir now\n")
    _write(dst / "x", "was a file\n")
    mirror(src, dst, delete=False)
    assert (dst / "y/machine-only.txt").read_bytes() == b"machine only\n"
    assert "y" in capsys.readouterr().err
    assert (dst / "empty").read_bytes() == b"file now\n"
    assert (dst / "x/inner.txt").read_bytes() == b"dir now\n"


def test_apply_mirror_false_keeps_machine_only_folder_on_type_flip(env):
    _write(env.repo / "dot-claude/hooks/tools", "shared file\n")
    _write(env.claude / "hooks/tools/mine.sh", "machine only\n")
    apply(env, mirror_mode=False)
    assert (env.claude / "hooks/tools/mine.sh").read_bytes() == b"machine only\n"


def test_stale_temp_files_are_neither_captured_nor_kept(env):
    stale = ".x.sh.ab12" + syncfiles.TMP_SUFFIX
    _write(env.claude / "hooks" / stale, "partial")
    _write(env.agents / "skills/release" / stale, "partial")
    _write(env.memory / stale, "partial")
    _write(env.claude / (".CLAUDE.md.ab12" + syncfiles.TMP_SUFFIX), "partial")
    _write(env.repo / "dot-claude/hooks" / stale, "partial")
    _write(
        env.repo / "dot-claude" / (".CLAUDE.md.cd34" + syncfiles.TMP_SUFFIX), "partial"
    )
    _write(
        env.repo / "dot-claude" / (".settings.json.ef56" + syncfiles.TMP_SUFFIX), "p"
    )
    capture(env)
    leftovers = [
        p for p in env.repo.rglob("*") if p.name.endswith(syncfiles.TMP_SUFFIX)
    ]
    assert leftovers == []
    # Apply with mirror=False still removes them from the live side.
    _write(env.claude / "hooks" / stale, "partial")
    apply(env, mirror_mode=False)
    live = [
        p
        for root in (env.claude, env.agents)
        for p in root.rglob("*")
        if p.name.endswith(syncfiles.TMP_SUFFIX)
    ]
    assert live == []


def test_capture_does_not_delete_claude_md(env):
    _write(env.repo / "dot-claude/CLAUDE.md", "repo copy\n")
    (env.claude / "CLAUDE.md").unlink()
    capture(env)
    assert (env.repo / "dot-claude/CLAUDE.md").read_bytes() == b"repo copy\n"


# --- symlinks ----------------------------------------------------------------


def _symlink_or_skip(link, target, *, target_is_directory=False):
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available")


def test_symlink_to_file_is_copied_as_content(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    _write(tmp_path / "outside.txt", "target body\n")
    src.mkdir()
    _symlink_or_skip(src / "link.txt", tmp_path / "outside.txt")
    mirror(src, dst, delete=True)
    out = dst / "link.txt"
    assert not out.is_symlink()
    assert out.read_bytes() == b"target body\n"


def test_symlink_to_dir_and_dangling_link_are_skipped(tmp_path, capsys):
    src, dst = tmp_path / "src", tmp_path / "dst"
    _write(tmp_path / "outdir/f.txt", "f\n")
    src.mkdir()
    _symlink_or_skip(src / "dirlink", tmp_path / "outdir", target_is_directory=True)
    _symlink_or_skip(src / "dangling", tmp_path / "nowhere")
    _write(dst / "dangling", "receiver copy\n")
    mirror(src, dst, delete=True)
    assert not (dst / "dirlink").exists()
    # A skipped source link protects the receiver's same-name entry.
    assert (dst / "dangling").read_bytes() == b"receiver copy\n"
    err = capsys.readouterr().err
    assert "dirlink" in err and "dangling" in err


def test_delete_never_follows_receiver_symlink(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    precious = tmp_path / "precious"
    _write(precious / "keep.txt", "do not delete\n")
    src.mkdir()
    dst.mkdir()
    _symlink_or_skip(dst / "linkdir", precious, target_is_directory=True)
    mirror(src, dst, delete=True)
    assert not os.path.lexists(dst / "linkdir")
    assert (precious / "keep.txt").read_bytes() == b"do not delete\n"


def test_receiver_symlink_replaced_by_file(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    target = tmp_path / "target.txt"
    _write(target, "target\n")
    _write(src / "a.txt", "from src\n")
    dst.mkdir()
    _symlink_or_skip(dst / "a.txt", target)
    mirror(src, dst, delete=False)
    assert not (dst / "a.txt").is_symlink()
    assert (dst / "a.txt").read_bytes() == b"from src\n"
    assert target.read_bytes() == b"target\n"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows path separators")
def test_windows_backslash_paths(tmp_path):
    src = tmp_path / "src"
    _write(src / "a" / "b.txt", "b\n")
    dst = str(tmp_path / "dst").replace("/", "\\")
    from pathlib import Path

    mirror(src, Path(dst), delete=True)
    assert (Path(dst) / "a" / "b.txt").read_bytes() == b"b\n"


def test_top_level_dst_symlink_is_followed(tmp_path):
    """rsync into `dst/` follows a linked folder; so does mirror."""
    src, real = tmp_path / "src", tmp_path / "real"
    _write(src / "a.txt", "a\n")
    real.mkdir()
    _symlink_or_skip(tmp_path / "dst", real, target_is_directory=True)
    mirror(src, tmp_path / "dst", delete=True)
    assert (tmp_path / "dst").is_symlink()
    assert (real / "a.txt").read_bytes() == b"a\n"


@pytest.mark.skipif(not POSIX, reason="needs POSIX symlinks")
@pytest.mark.parametrize(
    "linked", ["dot-claude", "dot-agents", "dot-claude/agents", "dot-claude/hooks", "dot-agents/skills", "memory"]
)
def test_capture_refuses_a_linked_repo_folder(env, tmp_path, linked):
    outside = tmp_path / "outside"
    shutil.copytree(env.repo / linked, outside)
    _write(outside / "keep.md", "outside\n")
    shutil.rmtree(env.repo / linked)
    (env.repo / linked).symlink_to(outside)
    with pytest.raises(Fail) as info:
        capture(env)
    assert linked.rsplit("/", 1)[-1] in info.value.message and info.value.hint
    assert (outside / "keep.md").read_bytes() == b"outside\n"
    assert not any(outside.rglob("SKILL.md")) and not any(outside.rglob("MEMORY.md"))
    assert not any(outside.rglob("CLAUDE.md"))
