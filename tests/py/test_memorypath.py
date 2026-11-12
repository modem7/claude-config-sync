from pathlib import Path

from claude_sync.memorypath import encode_project_path, resolve_memory_dir


def test_encode_replaces_every_slash_with_a_dash():
    assert encode_project_path("/home/user/project") == "-home-user-project"


def test_encode_handles_nested_subproject_paths():
    assert (
        encode_project_path("/home/user/project/sub-project")
        == "-home-user-project-sub-project"
    )


def test_encode_windows_drive_path():
    # Backslash, slash and colon each become a dash, like any non-alphanumeric.
    assert encode_project_path("Z:\\projects\\x") == "Z--projects-x"
    assert encode_project_path("/z/projects/x") == "-z-projects-x"


def test_resolve_memory_dir_builds_full_path_under_claude_home():
    result = resolve_memory_dir(Path("/home/user/.claude"), "/home/user/project")
    assert result == Path("/home/user/.claude/projects/-home-user-project/memory")


def test_encode_turns_every_non_alphanumeric_into_a_dash():
    # Claude Code's rule: anything but A-Z, a-z and 0-9 becomes a dash.
    assert encode_project_path("C:\\Users\\John Smith\\project") == "C--Users-John-Smith-project"
    assert encode_project_path("/home/user/my.project") == "-home-user-my-project"
    assert encode_project_path("/home/user/.config/x_y") == "-home-user--config-x-y"
    assert encode_project_path("/srv/a b/c") == "-srv-a-b-c"


def test_long_name_warning_only_over_the_limit():
    from claude_sync.memorypath import LONG_NAME, long_name_warning

    at_limit = "/" + "a" * (LONG_NAME - 1)
    assert len(encode_project_path(at_limit)) == LONG_NAME
    assert long_name_warning(at_limit) is None
    over = at_limit + "b"
    warning = long_name_warning(over)
    assert warning is not None
    assert f"{LONG_NAME + 1} characters" in warning
    assert "may not be the folder Claude Code uses" in warning


def test_long_name_warning_drops_control_characters():
    from claude_sync.memorypath import LONG_NAME, long_name_warning

    path = "/home/\x1b[2J\x1b]0;x\x07user/" + "a" * LONG_NAME
    warning = long_name_warning(path)
    assert warning is not None
    assert "\x1b" not in warning and "\x07" not in warning
    assert "/home/[2J]0;xuser/" in warning
