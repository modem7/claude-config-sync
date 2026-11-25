from pathlib import Path

import pytest

from claude_sync import gitflow, machineconf
from claude_sync.machineconf import (
    machine_conf_path,
    read_machine_profile,
    read_primary_project_path,
    write_machine_conf,
    write_machine_profile,
)
from claude_sync.memorypath import resolve_memory_dir
from claude_sync.util import Fail


def test_machine_conf_path_builds_path_from_repo_and_hostname():
    assert machine_conf_path(Path("/tmp/repo"), "my-host") == Path(
        "/tmp/repo/machines/my-host.conf"
    )


def test_write_then_read_primary_project_path_round_trips(tmp_path):
    write_machine_conf(tmp_path, "my-host", "/home/user/project")
    assert read_primary_project_path(tmp_path, "my-host") == "/home/user/project"


def test_read_primary_project_path_is_none_when_conf_missing(tmp_path):
    assert read_primary_project_path(tmp_path, "nope") is None


def test_read_machine_profile_is_none_without_profile_line(tmp_path):
    write_machine_conf(tmp_path, "my-host", "/home/user/project")
    assert read_machine_profile(tmp_path, "my-host") is None


def test_read_machine_profile_is_none_when_conf_missing(tmp_path):
    assert read_machine_profile(tmp_path, "nope") is None


def test_write_machine_profile_sets_replaces_and_removes(tmp_path):
    conf = machine_conf_path(tmp_path, "my-host")
    write_machine_conf(tmp_path, "my-host", "/home/user/project")
    write_machine_profile(tmp_path, "my-host", "host-a")
    assert read_machine_profile(tmp_path, "my-host") == "host-a"
    write_machine_profile(tmp_path, "my-host", "host-b")
    assert read_machine_profile(tmp_path, "my-host") == "host-b"
    assert conf.read_bytes().count(b"PROFILE=") == 1
    write_machine_profile(tmp_path, "my-host", "")
    assert b"PROFILE=" not in conf.read_bytes()
    assert read_primary_project_path(tmp_path, "my-host") == "/home/user/project"


def test_conf_roundtrip_preserves_other_lines(tmp_path):
    conf = machine_conf_path(tmp_path, "my-host")
    conf.parent.mkdir(parents=True)
    original = b"# note \xff\xfe\nPRIMARY_PROJECT_PATH=/p\nEXTRA=1\n\nPROFILE=old\nLAST=z\n"
    conf.write_bytes(original)
    write_machine_profile(tmp_path, "my-host", "host-a")
    assert conf.read_bytes() == original.replace(b"PROFILE=old\n", b"") + b"PROFILE=host-a\n"
    write_machine_profile(tmp_path, "my-host", "")
    assert conf.read_bytes() == original.replace(b"PROFILE=old\n", b"")


def test_write_machine_profile_on_file_without_trailing_newline(tmp_path):
    conf = machine_conf_path(tmp_path, "my-host")
    conf.parent.mkdir(parents=True)
    conf.write_bytes(b"PRIMARY_PROJECT_PATH=/p")
    write_machine_profile(tmp_path, "my-host", "host-a")
    assert conf.read_bytes() == b"PRIMARY_PROJECT_PATH=/p\nPROFILE=host-a\n"


# ── Windows project path form (faked, so these run on Linux too) ─────────

@pytest.mark.parametrize(
    ("given", "native"),
    [
        ("/z/foo", "Z:\\foo"),
        ("/c/Users/John Smith/project", "C:\\Users\\John Smith\\project"),
        ("/z", "Z:\\"),
        ("Z:\\foo", "Z:\\foo"),
        ("z:/foo/bar", "Z:\\foo\\bar"),
        ("/tmp/x", "/tmp/x"),
        ("//server/share/x", "//server/share/x"),
        ("/zz/x", "/zz/x"),
    ],
)
def test_windows_native_path(given, native):
    assert machineconf.windows_native_path(given) == native


def test_windows_reads_a_git_bash_conf_as_native_and_keeps_the_file(tmp_path, monkeypatch):
    monkeypatch.setattr(machineconf, "IS_WINDOWS", True)
    write_machine_conf(tmp_path, "host-a", "/z/foo")
    before = machine_conf_path(tmp_path, "host-a").read_bytes()
    assert read_primary_project_path(tmp_path, "host-a") == "Z:\\foo"
    assert machine_conf_path(tmp_path, "host-a").read_bytes() == before


def test_windows_stores_and_reads_a_native_path_as_is(tmp_path, monkeypatch):
    monkeypatch.setattr(machineconf, "IS_WINDOWS", True)
    write_machine_conf(tmp_path, "host-a", "Z:\\foo")
    assert machine_conf_path(tmp_path, "host-a").read_bytes() == b"PRIMARY_PROJECT_PATH=Z:\\foo\n"
    assert read_primary_project_path(tmp_path, "host-a") == "Z:\\foo"


def test_windows_memory_folder_is_the_same_for_both_forms(tmp_path, monkeypatch):
    monkeypatch.setattr(machineconf, "IS_WINDOWS", True)
    claude = tmp_path / "claude"
    write_machine_conf(tmp_path, "host-a", "/z/foo")
    write_machine_conf(tmp_path, "host-b", "Z:\\foo")
    dirs = [
        gitflow.memory_dir_of(gitflow.SyncCtx(tmp_path, claude, tmp_path / "agents", host))
        for host in ("host-a", "host-b")
    ]
    assert dirs[0] == dirs[1] == resolve_memory_dir(claude, "Z:\\foo")
    assert dirs[0].parent.name == "Z--foo"


def test_posix_keeps_a_drive_like_path_as_stored(tmp_path, monkeypatch):
    monkeypatch.setattr(machineconf, "IS_WINDOWS", False)
    write_machine_conf(tmp_path, "host-a", "/z/foo")
    assert read_primary_project_path(tmp_path, "host-a") == "/z/foo"


# ── MCP_SYNC= ────────────────────────────────────────────────────────────


def test_mcp_sync_is_off_without_a_conf_a_line_a_value_or_with_off(tmp_path):
    assert machineconf.read_machine_mcp_sync(tmp_path, "host-a") is False
    write_machine_conf(tmp_path, "host-a", "/p")
    assert machineconf.read_machine_mcp_sync(tmp_path, "host-a") is False
    conf = machine_conf_path(tmp_path, "host-a")
    for line in (b"MCP_SYNC=\n", b"MCP_SYNC=off\n", b"MCP_SYNC=  \n", b"MCP_SYNC=off\r\n"):
        conf.write_bytes(b"PRIMARY_PROJECT_PATH=/p\n" + line)
        assert machineconf.read_machine_mcp_sync(tmp_path, "host-a") is False, line


def test_mcp_sync_is_on_only_for_on(tmp_path):
    conf = machine_conf_path(tmp_path, "host-a")
    conf.parent.mkdir(parents=True)
    for line in (b"MCP_SYNC=on\n", b"MCP_SYNC= on \n", b"MCP_SYNC=on\r\n"):
        conf.write_bytes(b"PRIMARY_PROJECT_PATH=/p\n" + line)
        assert machineconf.read_machine_mcp_sync(tmp_path, "host-a") is True, line


@pytest.mark.parametrize("bad", ["On", "yes", "true", "1", "on off"])
def test_an_unknown_mcp_sync_value_is_a_clear_fail(tmp_path, bad):
    conf = machine_conf_path(tmp_path, "host-a")
    conf.parent.mkdir(parents=True)
    conf.write_bytes(f"MCP_SYNC={bad}\n".encode())
    with pytest.raises(Fail) as err:
        machineconf.read_machine_mcp_sync(tmp_path, "host-a")
    assert "machines/host-a.conf" in err.value.message
    assert "'on' or 'off'" in err.value.hint
    assert str(conf) in err.value.hint


def test_write_machine_mcp_sync_keeps_other_lines_and_off_removes_the_line(tmp_path):
    conf = machine_conf_path(tmp_path, "host-a")
    conf.parent.mkdir(parents=True)
    original = b"# note \xff\nPRIMARY_PROJECT_PATH=/p\nTIER=full\nEXTRA=1"
    conf.write_bytes(original)
    machineconf.write_machine_mcp_sync(tmp_path, "host-a", True)
    assert conf.read_bytes() == original + b"\nMCP_SYNC=on\n"
    machineconf.write_machine_mcp_sync(tmp_path, "host-a", True)
    assert conf.read_bytes().count(b"MCP_SYNC=") == 1
    assert machineconf.read_machine_mcp_sync(tmp_path, "host-a") is True
    assert read_primary_project_path(tmp_path, "host-a") == "/p"
    machineconf.write_machine_mcp_sync(tmp_path, "host-a", False)
    assert conf.read_bytes() == original + b"\n"
    assert machineconf.read_machine_mcp_sync(tmp_path, "host-a") is False


# ── PLUGINS= ─────────────────────────────────────────────────────────────


def test_plugins_is_off_without_a_conf_a_line_a_value_or_with_off(tmp_path):
    assert machineconf.read_machine_plugins(tmp_path, "host-a") is False
    write_machine_conf(tmp_path, "host-a", "/p")
    assert machineconf.read_machine_plugins(tmp_path, "host-a") is False
    conf = machine_conf_path(tmp_path, "host-a")
    for line in (b"PLUGINS=\n", b"PLUGINS=off\n", b"PLUGINS=  \n", b"PLUGINS=off\r\n"):
        conf.write_bytes(b"PRIMARY_PROJECT_PATH=/p\n" + line)
        assert machineconf.read_machine_plugins(tmp_path, "host-a") is False, line


def test_plugins_is_on_only_for_full(tmp_path):
    conf = machine_conf_path(tmp_path, "host-a")
    conf.parent.mkdir(parents=True)
    for line in (b"PLUGINS=full\n", b"PLUGINS= full \n", b"PLUGINS=full\r\n"):
        conf.write_bytes(b"PRIMARY_PROJECT_PATH=/p\n" + line)
        assert machineconf.read_machine_plugins(tmp_path, "host-a") is True, line


@pytest.mark.parametrize("bad", ["Full", "on", "yes", "true", "1", "full off"])
def test_an_unknown_plugins_value_is_a_clear_fail(tmp_path, bad):
    conf = machine_conf_path(tmp_path, "host-a")
    conf.parent.mkdir(parents=True)
    conf.write_bytes(f"PLUGINS={bad}\n".encode())
    with pytest.raises(Fail) as err:
        machineconf.read_machine_plugins(tmp_path, "host-a")
    assert "PLUGINS" in err.value.message and "machines/host-a.conf" in err.value.message
    assert "'full' or 'off'" in err.value.hint
    assert str(conf) in err.value.hint


def test_write_machine_plugins_keeps_other_lines_and_off_removes_the_line(tmp_path):
    conf = machine_conf_path(tmp_path, "host-a")
    conf.parent.mkdir(parents=True)
    original = b"# note \xff\nPRIMARY_PROJECT_PATH=/p\nTIER=basic\nMCP_SYNC=on\nEXTRA=1"
    conf.write_bytes(original)
    machineconf.write_machine_plugins(tmp_path, "host-a", True)
    assert conf.read_bytes() == original + b"\nPLUGINS=full\n"
    machineconf.write_machine_plugins(tmp_path, "host-a", True)
    assert conf.read_bytes().count(b"PLUGINS=") == 1
    assert machineconf.read_machine_plugins(tmp_path, "host-a") is True
    assert machineconf.read_machine_mcp_sync(tmp_path, "host-a") is True
    assert machineconf.read_machine_tier(tmp_path, "host-a") == "basic"
    machineconf.write_machine_plugins(tmp_path, "host-a", False)
    assert conf.read_bytes() == original + b"\n"
    assert machineconf.read_machine_plugins(tmp_path, "host-a") is False
