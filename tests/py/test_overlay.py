"""Port of tests/settings_overlay.bats and tests/settings_portability.bats."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from claude_sync.overlay import (
    dump_json,
    leaf_paths,
    load_json,
    merge_files,
    overlay_add,
    overlay_list,
    overlay_merge,
    overlay_path,
    overlay_remove,
    overlay_split,
    parse_path,
    split_files,
)
from claude_sync.util import Fail

REPO_ROOT = Path(__file__).resolve().parents[2]

SHARED_TEXT = """{
  "theme": "dark",
  "enabledPlugins": { "a@m": true, "b@m": true },
  "permissions": { "allow": ["Bash(*)", "Read(*)"] }
}
"""


class Env:
    def __init__(self, tmp: Path) -> None:
        self.repo = tmp / "repo"
        (self.repo / "machines").mkdir(parents=True)
        (self.repo / "dot-claude").mkdir(parents=True)
        self.shared = self.repo / "dot-claude" / "settings.json"
        self.ov = self.repo / "machines" / "host-a.settings.json"
        self.local = tmp / "local.json"
        self.out = tmp / "shared_out.json"
        self.base = tmp / "override_before.json"
        self.shared.write_bytes(SHARED_TEXT.encode())

    def merged(self, override: Path) -> dict:
        return json.loads(merge_files(self.shared, override))

    def split(self) -> None:
        split_files(self.local, self.shared, self.base, self.ov, self.out)


@pytest.fixture
def env(tmp_path) -> Env:
    return Env(tmp_path)


def write(path: Path, text: str) -> None:
    # Like `echo '...' > file`: compact, with a trailing newline.
    path.write_bytes(text.encode() + b"\n")


def read(path: Path):
    return json.loads(path.read_bytes())


def compact(path: Path) -> str:
    return json.dumps(read(path), separators=(",", ":"), ensure_ascii=False)


# --- settings_overlay.bats ---------------------------------------------------


def test_merge_deep_merges_replaces_arrays_and_deletes_null_keys(env):
    write(env.ov, '{"enabledPlugins":{"c@m":true,"b@m":null},"permissions":{"allow":["Read(*)"]}}')
    merged = env.merged(env.ov)
    assert merged["enabledPlugins"] == {"a@m": True, "c@m": True}
    assert list(merged["enabledPlugins"]) == ["a@m", "c@m"]
    assert merged["permissions"]["allow"] == ["Read(*)"]
    assert merged["theme"] == "dark"


def test_split_of_unchanged_merge_gives_back_shared_and_leaves_override_untouched(env):
    write(env.ov, '{"enabledPlugins":{"c@m":true,"b@m":null}}')
    before = env.ov.read_bytes()
    env.local.write_bytes(merge_files(env.shared, env.ov))
    env.base.write_bytes(env.ov.read_bytes())
    env.split()
    assert read(env.out) == read(env.shared)
    assert env.ov.read_bytes() == before


def test_split_sends_new_keys_to_shared(env):
    write(env.ov, '{"enabledPlugins":{"c@m":true}}')
    local = env.merged(env.ov)
    local["enabledPlugins"]["d@m"] = True
    local["model"] = "opus"
    env.local.write_bytes(dump_json(local))
    env.base.write_bytes(env.ov.read_bytes())
    env.split()
    assert json.dumps(read(env.out)["enabledPlugins"], separators=(",", ":")) == (
        '{"a@m":true,"b@m":true,"d@m":true}'
    )
    assert read(env.out)["model"] == "opus"
    assert compact(env.ov) == '{"enabledPlugins":{"c@m":true}}'


def test_split_writes_local_change_to_owned_key_into_override(env):
    write(env.ov, '{"enabledPlugins":{"b@m":false}}')
    local = env.merged(env.ov)
    local["enabledPlugins"]["b@m"] = True
    env.local.write_bytes(dump_json(local))
    env.base.write_bytes(env.ov.read_bytes())
    env.split()
    assert compact(env.ov) == '{"enabledPlugins":{"b@m":true}}'
    assert read(env.out) == read(env.shared)


def test_split_records_deleted_owned_key_as_null_and_readded_key_as_value(env):
    write(env.ov, '{"enabledPlugins":{"c@m":true,"a@m":null}}')
    local = env.merged(env.ov)
    del local["enabledPlugins"]["c@m"]
    local["enabledPlugins"]["a@m"] = False
    env.local.write_bytes(dump_json(local))
    env.base.write_bytes(env.ov.read_bytes())
    env.split()
    assert compact(env.ov) == '{"enabledPlugins":{"c@m":null,"a@m":false}}'
    assert read(env.out) == read(env.shared)


def test_nested_override_leaves_no_empty_skeleton_in_shared(env):
    write(env.ov, '{"extraKnownMarketplaces":{"mkt":{"source":{"source":"github","repo":"o/r"}}}}')
    env.local.write_bytes(merge_files(env.shared, env.ov))
    env.base.write_bytes(env.ov.read_bytes())
    env.split()
    assert read(env.out) == read(env.shared)
    assert "extraKnownMarketplaces" not in read(env.out)


def test_hand_added_override_key_is_kept_not_taken_as_local_deletion(env):
    write(env.base, "{}")
    env.local.write_bytes(env.shared.read_bytes())
    write(env.ov, '{"enabledPlugins":{"c@m":true}}')
    env.split()
    assert compact(env.ov) == '{"enabledPlugins":{"c@m":true}}'
    assert read(env.out) == read(env.shared)


def test_hand_removed_override_key_does_not_leak_into_shared(env):
    write(env.base, '{"enabledPlugins":{"c@m":true}}')
    env.local.write_bytes(merge_files(env.shared, env.base))
    write(env.ov, "{}")
    env.split()
    assert read(env.out) == read(env.shared)


def test_deleted_override_file_behaves_like_emptied_one_and_is_not_recreated(env):
    write(env.base, '{"theme":"light"}')
    env.local.write_bytes(merge_files(env.shared, env.base))
    env.split()
    assert read(env.out) == read(env.shared)
    assert not env.ov.exists()


def test_add_moves_key_from_shared_into_override(env):
    env.local.write_bytes(env.shared.read_bytes())
    overlay_add(env.repo, "host-a", env.local, '.enabledPlugins["b@m"]')
    assert compact(env.ov) == '{"enabledPlugins":{"b@m":true}}'
    assert read(env.shared)["enabledPlugins"] == {"a@m": True}


def test_add_refuses_key_not_set_locally(env):
    env.local.write_bytes(env.shared.read_bytes())
    with pytest.raises(Fail, match="isn't set in"):
        overlay_add(env.repo, "host-a", env.local, '.enabledPlugins["zzz@m"]')
    assert not env.ov.exists()


def test_remove_drops_key_prunes_empty_objects_and_deletes_empty_file(env):
    write(env.ov, '{"enabledPlugins":{"c@m":true},"theme":"light"}')
    overlay_remove(env.repo, "host-a", '.enabledPlugins["c@m"]')
    assert compact(env.ov) == '{"theme":"light"}'
    overlay_remove(env.repo, "host-a", ".theme")
    assert not env.ov.exists()


def test_remove_without_override_file_fails(env):
    with pytest.raises(Fail, match="No overrides for host-a"):
        overlay_remove(env.repo, "host-a", ".theme")


def test_remove_of_key_not_overridden_fails(env):
    write(env.ov, '{"theme":"light"}')
    with pytest.raises(Fail, match="isn't overridden on host-a"):
        overlay_remove(env.repo, "host-a", ".model")


def test_list_prints_one_readable_path_per_owned_key(env):
    write(env.ov, '{"enabledPlugins":{"c@m":true},"theme":"light"}')
    lines = overlay_list(env.repo, "host-a")
    assert lines[0] == '.enabledPlugins["c@m"] = true'
    assert lines[1] == '.theme = "light"'


def test_list_without_override_file_says_so(env):
    assert overlay_list(env.repo, "host-a") == ["No overrides for host-a."]


@pytest.mark.parametrize("text", ["[1,2]", '"light"', "3", "null"])
def test_list_and_remove_refuse_a_non_object_override(env, text):
    """Review focus 4: a clear error, not a silent "nothing overridden"."""
    write(env.ov, text)
    before = env.ov.read_bytes()
    with pytest.raises(Fail, match="must contain a JSON object"):
        overlay_list(env.repo, "host-a")
    with pytest.raises(Fail, match="must contain a JSON object"):
        overlay_remove(env.repo, "host-a", ".theme")
    assert env.ov.read_bytes() == before


def test_non_object_override_is_rejected_not_merged(env):
    write(env.ov, "[1,2]")
    with pytest.raises(Fail, match="must contain a JSON object"):
        merge_files(env.shared, env.ov)


def test_overlay_path(tmp_path):
    assert overlay_path(tmp_path, "host-b") == tmp_path / "machines" / "host-b.settings.json"


@pytest.mark.parametrize("name", ["../x", "a/b", "a\\b", "", ".", ".."])
def test_overlay_path_refuses_a_name_that_leaves_machines(tmp_path, name):
    with pytest.raises(Fail, match="not a valid profile name"):
        overlay_path(tmp_path, name)


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
@pytest.mark.parametrize("inside", [False, True])
def test_overlay_path_refuses_an_override_that_is_a_link(tmp_path, inside):
    """Security: a committed machines/<name>.settings.json link must never be
    read through (its target's bytes would be merged, copied or pushed)."""
    (tmp_path / "machines").mkdir()
    target = tmp_path / "machines" / "real.json" if inside else tmp_path / "secret.json"
    target.write_bytes(b'{"token":"s3cret"}\n')
    (tmp_path / "machines" / "host-a.settings.json").symlink_to(target)
    with pytest.raises(Fail, match="is a link or leads outside") as err:
        overlay_path(tmp_path, "host-a")
    assert "real file" in err.value.hint
    for func in (lambda: overlay_list(tmp_path, "host-a"), lambda: overlay_remove(tmp_path, "host-a", ".token")):
        with pytest.raises(Fail, match="is a link"):
            func()
    assert target.read_bytes() == b'{"token":"s3cret"}\n'


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_overlay_path_refuses_a_machines_folder_that_is_a_link(tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "host-a.settings.json").write_bytes(b"{}\n")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "machines").symlink_to(outside)
    with pytest.raises(Fail, match="leads outside"):
        overlay_path(repo, "host-a")


# --- extra cases --------------------------------------------------------------


def test_null_deletes_leaf():
    shared = {"a": {"x": 1, "y": 2}, "b": 3}
    assert overlay_merge(shared, {"a": {"x": None}, "b": None, "c": None}) == {"a": {"y": 2}}
    assert shared == {"a": {"x": 1, "y": 2}, "b": 3}  # input untouched


def test_arrays_replace_not_merge():
    assert overlay_merge({"l": [1, 2, 3]}, {"l": [9]}) == {"l": [9]}
    # A non-object on either side is replaced by the override.
    assert overlay_merge({"a": 1}, {"a": {}}) == {"a": {}}
    assert overlay_merge({"a": {"b": 1}}, {"a": [1]}) == {"a": [1]}


def test_empty_object_is_not_a_leaf():
    # The root is never a leaf: an empty override owns nothing.
    assert leaf_paths({}) == []
    # Descending stops at a non-object or an empty object (as in the jq
    # _leaf helper), so {"a": {}} owns "a" itself and nothing under it.
    assert leaf_paths({"a": {}, "b": {"c": None}, "d": [1]}) == [("a",), ("b", "c"), ("d",)]
    # An empty object as an override value merges as a no-op on an object.
    assert overlay_merge({"a": {"k": 1}}, {"a": {}}) == {"a": {"k": 1}}


def test_split_prunes_empty_parents():
    shared = {"theme": "dark", "keep": {}}
    ov = {"m": {"n": {"o": 1}}, "keep": {"x": 1}}
    local = overlay_merge(shared, ov)
    new_shared, new_ov = overlay_split(local, shared, ov, ov)
    assert new_shared == {"theme": "dark", "keep": {}}  # "keep" was in shared, so it stays
    assert new_ov == ov


def test_split_with_missing_override_and_local_edit_creates_nothing():
    shared = {"theme": "dark"}
    new_shared, new_ov = overlay_split({"theme": "light"}, shared, {}, None)
    assert new_shared == {"theme": "light"}
    assert new_ov == {}


def test_split_keeps_bool_and_number_distinct():
    # jq: true != 1. A local change from 1 to true must be recorded.
    shared = {}
    ov = {"k": 1}
    _, new_ov = overlay_split({"k": True}, shared, ov, ov)
    assert new_ov == {"k": True} and new_ov["k"] is True


def test_split_fails_when_local_has_scalar_on_owned_path():
    ov = {"enabledPlugins": {"x@y": True}}
    with pytest.raises(Fail):
        overlay_split({"enabledPlugins": 5}, {}, ov, ov)


def test_split_files_failure_writes_nothing(env):
    write(env.ov, '{"enabledPlugins":{"x@y":true}}')
    before = env.ov.read_bytes()
    write(env.local, '{"enabledPlugins":5}')
    write(env.base, "{}")
    with pytest.raises(Fail):
        env.split()
    assert not env.out.exists()
    assert env.ov.read_bytes() == before


def test_parse_path_simple_and_nested():
    assert parse_path(".theme") == ("theme",)
    assert parse_path(".a.b_c.D9") == ("a", "b_c", "D9")
    assert parse_path(".") == ()
    assert parse_path(".l[0]") == ("l", 0)


def test_parse_path_quoted_key_with_at_sign():
    assert parse_path('.enabledPlugins["x@y"]') == ("enabledPlugins", "x@y")
    assert parse_path('.["a b"]') == ("a b",)
    assert parse_path('.a["q\\"uote"]["é"]') == ("a", 'q"uote', "é")


@pytest.mark.parametrize(
    "expr",
    ["", "theme", '["x"]', ".a | .b", ".a[]", ".a..b", ".1a", '.a["x"', ".a[foo]", ".a?", "del(.a)"],
)
def test_parse_path_rejects_unsupported_expr(expr):
    with pytest.raises(Fail, match="Not a valid jq path"):
        parse_path(expr)


def test_add_rejects_invalid_path(env):
    env.local.write_bytes(env.shared.read_bytes())
    with pytest.raises(Fail, match="Not a valid jq path"):
        overlay_add(env.repo, "host-a", env.local, "enabledPlugins")
    assert not env.ov.exists()


def test_add_of_root_is_refused(env):
    env.local.write_bytes(env.shared.read_bytes())
    with pytest.raises(Fail, match="isn't set in"):
        overlay_add(env.repo, "host-a", env.local, ".")


def test_non_ascii_survives_roundtrip(env):
    write(env.ov, '{"statusLine":"café ✓ 日本"}')
    out = merge_files(env.shared, env.ov)
    assert "café ✓ 日本".encode() in out
    assert b"\\u" not in out
    env.local.write_bytes(out)
    env.base.write_bytes(env.ov.read_bytes())
    env.split()
    assert read(env.ov) == {"statusLine": "café ✓ 日本"}
    assert overlay_list(env.repo, "host-a") == ['.statusLine = "café ✓ 日本"']


def test_bom_is_accepted(tmp_path):
    p = tmp_path / "s.json"
    p.write_bytes(b'\xef\xbb\xbf{"a":1}')
    assert load_json(p) == {"a": 1}


@pytest.mark.parametrize("content", [b"", b"[1]", b'"s"', b"null", b"{bad", b"\xff\xfe", b'{"a":NaN}'])
def test_rejects_non_object_settings(tmp_path, content):
    p = tmp_path / "s.json"
    p.write_bytes(content)
    with pytest.raises(Fail) as exc:
        load_json(p)
    assert str(p) in exc.value.message
    assert exc.value.hint


def test_merge_rejects_non_object_inputs():
    with pytest.raises(Fail):
        overlay_merge([1], {})  # type: ignore[arg-type]
    with pytest.raises(Fail):
        overlay_merge({}, [1])  # type: ignore[arg-type]


def test_dump_json_format():
    assert dump_json({"a": [], "b": {}, "c": [1, {"d": "é"}]}) == (
        '{\n  "a": [],\n  "b": {},\n  "c": [\n    1,\n    {\n      "d": "é"\n    }\n  ]\n}\n'
    ).encode()


def test_output_bytes_match_existing_dot_claude_settings():
    path = REPO_ROOT / "dot-claude" / "settings.json"
    data = path.read_bytes()
    assert dump_json(load_json(path)) == data


def test_written_files_use_lf(env):
    write(env.ov, '{"theme":"light"}')
    env.local.write_bytes(env.shared.read_bytes())
    overlay_add(env.repo, "host-a", env.local, ".permissions")
    assert b"\r\n" not in env.ov.read_bytes()
    assert b"\r\n" not in env.shared.read_bytes()


# --- settings_portability.bats -----------------------------------------------


def _commands(node):
    if isinstance(node, dict):
        cmd = node.get("command")
        if cmd:  # jq `.command? // empty` drops null and false
            yield cmd if isinstance(cmd, str) else json.dumps(cmd)
        for v in node.values():
            yield from _commands(v)
    elif isinstance(node, list):
        for v in node:
            yield from _commands(v)


def _settings_commands() -> list[str]:
    return list(_commands(load_json(REPO_ROOT / "dot-claude" / "settings.json")))


def test_hook_commands_contain_no_hardcoded_home_directory():
    pattern = re.compile(r'(^|["\s])(/home/|/Users/|/root/)')
    bad = [c for line in _settings_commands() for c in line.split("\n") if pattern.search(c)]
    assert bad == []


@pytest.mark.skipif(os.name == "nt", reason="no executable bit on Windows")
def test_every_hook_script_referenced_by_settings_exists_and_is_executable():
    names = sorted(
        {
            m.split("/")[-1]
            for c in _settings_commands()
            for m in re.findall(r"\.claude/hooks/[A-Za-z0-9_.-]+", c)
        }
    )
    hooks = REPO_ROOT / "dot-claude" / "hooks"
    missing = [n for n in names if not os.access(hooks / n, os.X_OK) or not (hooks / n).is_file()]
    assert missing == []


# --- fix round 2: machines/ itself a link, no override yet -------------------

needs_links = pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")


def _linked_machines(tmp_path):
    """repo/machines -> outside/, with no override in it yet."""
    outside = tmp_path / "outside"
    outside.mkdir()
    repo = tmp_path / "repo"
    (repo / "dot-claude").mkdir(parents=True)
    (repo / "dot-claude" / "settings.json").write_bytes(b'{"theme":"dark"}\n')
    (repo / "machines").symlink_to(outside)
    return repo, outside


@needs_links
def test_a_linked_machines_folder_is_refused_even_with_no_override(tmp_path):
    repo, outside = _linked_machines(tmp_path)
    local = tmp_path / "local.json"
    local.write_bytes(b'{"theme":"dark"}\n')
    with pytest.raises(Fail, match="machines.* is a link or leads outside") as err:
        overlay_path(repo, "host-a")
    assert "real folder" in err.value.hint
    with pytest.raises(Fail, match="is a link"):
        overlay_add(repo, "host-a", local, ".theme")
    assert list(outside.iterdir()) == []
    assert (repo / "dot-claude" / "settings.json").read_bytes() == b'{"theme":"dark"}\n'


@needs_links
def test_split_refuses_to_write_an_override_through_a_linked_machines_folder(tmp_path):
    """The write itself re-checks, so a path built before the link appeared
    (or by hand) still cannot land outside the repo."""
    repo, outside = _linked_machines(tmp_path)
    local = tmp_path / "local.json"
    local.write_bytes(b'{"theme":"dark","model":"x"}\n')
    before = tmp_path / "before.json"
    before.write_bytes(b'{"theme":"dark"}\n')
    ov_before = tmp_path / "ovb.json"
    ov_before.write_bytes(b'{"model":"y"}\n')
    shared = repo / "dot-claude" / "settings.json"
    with pytest.raises(Fail, match="is a link"):
        split_files(local, before, ov_before, repo / "machines" / "host-a.settings.json", shared)
    assert list(outside.iterdir()) == []
    assert shared.read_bytes() == b'{"theme":"dark"}\n'  # nothing written at all


@needs_links
def test_a_repo_under_a_linked_parent_and_a_missing_machines_folder_still_work(tmp_path):
    real = tmp_path / "real"
    (real / "dot-claude").mkdir(parents=True)
    (real / "dot-claude" / "settings.json").write_bytes(b'{"theme":"dark","model":"x"}\n')
    via = tmp_path / "via"
    via.symlink_to(real)  # like /tmp -> /private/tmp
    assert overlay_path(via, "host-a") == via / "machines" / "host-a.settings.json"  # no machines/ yet
    local = tmp_path / "local.json"
    local.write_bytes(b'{"theme":"dark","model":"x"}\n')
    overlay_add(via, "host-a", local, ".model")
    assert json.loads((real / "machines" / "host-a.settings.json").read_bytes()) == {"model": "x"}
    assert overlay_list(via, "host-a") == ['.model = "x"']
