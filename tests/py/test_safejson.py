"""claude_sync.safejson: read JSON, write it back atomically and alike."""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
from pathlib import Path

import pytest

from claude_sync import safejson

# Made by Node's JSON.stringify(o, null, 2), the way Claude Code writes
# ~/.claude.json: nested, empty {} and [], non-BMP, escapes, control chars.
NODE_FIXTURE = (
    '{\n  "numStartups": 42,\n  "installMethod": "native",\n  "autoUpdates": false,\n'
    '  "userID": "0f3c",\n  "tipsHistory": {\n    "new-user-warmup": 3,\n'
    '    "memory-command": 12\n  },\n  "projects": {\n    "/home/x/project": {\n'
    '      "allowedTools": [],\n      "history": [],\n      "mcpContextUris": [],\n'
    '      "mcpServers": {},\n      "enabledMcpjsonServers": [],\n'
    '      "disabledMcpjsonServers": [],\n      "hasTrustDialogAccepted": true,\n'
    '      "projectOnboardingSeenCount": 4,\n      "lastCost": 1.25,\n'
    '      "lastTotalInputTokens": 1234\n    },\n    "C:\\\\Users\\\\x\\\\proj": {\n'
    '      "mcpServers": {\n        "plex": {\n          "type": "stdio",\n'
    '          "command": "npx",\n          "args": [\n            "-y",\n'
    '            "plex-mcp"\n          ],\n          "env": {}\n        }\n      },\n'
    '      "disabledMcpServers": [\n        "plex"\n      ],\n'
    '      "note": "caf\u00e9 \u00fcber \u4e2d\u6587 \U0001f680 \u2028 tab\\tnl\\nq\\"bs\\\\ctl'
    '\\u0001\\u001f del\x7f /slash"\n    }\n  },\n  "oauthAccount": {\n'
    '    "emailAddress": "x@example.invalid",\n    "organizationRole": "admin"\n  },\n'
    '  "nested": {\n    "a": {\n      "b": {\n        "c": [\n          1,\n          [\n'
    '            2,\n            [\n              3,\n              {}\n            ]\n'
    '          ],\n          null,\n          true,\n          -0.5,\n          1e+21\n'
    '        ]\n      }\n    }\n  }\n}'
)


def _secret() -> str:
    return "-".join(["tok", "ABCD" * 4, "zz"])


def _put(path: Path, text: str) -> Path:
    path.write_bytes(text.encode("utf-8"))
    return path


def _tmp_left(folder: Path) -> list[str]:
    return [p.name for p in folder.iterdir() if p.name.endswith(".tmp")]


@pytest.fixture
def cj(tmp_path) -> Path:
    return _put(tmp_path / ".claude.json", NODE_FIXTURE)


def test_read_meta(cj):
    data, meta = safejson.read_json_with_meta(cj, "~/.claude.json")
    assert data["numStartups"] == 42
    assert meta.indent == 2 and not meta.trailing_newline and not meta.crlf
    assert meta.raw == cj.read_bytes() and meta.size == len(meta.raw)
    assert meta.mtime_ns == cj.stat().st_mtime_ns and meta.label == "~/.claude.json"


@pytest.mark.parametrize("tail", ["", "\n"])
def test_noop_write_is_byte_identical_and_skipped(cj, tmp_path, tail):
    _put(cj, NODE_FIXTURE + tail)
    before, mtime = cj.read_bytes(), cj.stat().st_mtime_ns
    data, meta = safejson.read_json_with_meta(cj)
    assert safejson.render(data, meta) == before
    assert safejson.write_json_atomic(cj, data, meta, backup_dir=tmp_path / "bk") is False
    assert cj.read_bytes() == before and cj.stat().st_mtime_ns == mtime
    assert not (tmp_path / "bk").exists()


def test_one_key_change_leaves_other_bytes(cj, tmp_path):
    data, meta = safejson.read_json_with_meta(cj)
    data["numStartups"] = 43
    assert safejson.write_json_atomic(cj, data, meta, backup_dir=tmp_path / "bk") is True
    want = NODE_FIXTURE.replace('"numStartups": 42,', '"numStartups": 43,', 1)
    assert cj.read_bytes() == want.encode("utf-8")


def test_nested_change_leaves_other_bytes(cj, tmp_path):
    data, meta = safejson.read_json_with_meta(cj)
    data["projects"]["C:\\Users\\x\\proj"]["disabledMcpServers"] = []
    safejson.write_json_atomic(cj, data, meta, backup_dir=tmp_path / "bk")
    old = '"disabledMcpServers": [\n        "plex"\n      ],'
    want = NODE_FIXTURE.replace(old, '"disabledMcpServers": [],', 1)
    assert cj.read_bytes() == want.encode("utf-8")


@pytest.mark.parametrize("indent", [2, 4, "\t"])
@pytest.mark.parametrize("eol", ["\n", "\r\n"])
@pytest.mark.parametrize("tail", [True, False])
def test_layout_kept(tmp_path, indent, eol, tail):
    text = json.dumps({"a": {"b": [1, 2]}, "c": "x"}, indent=indent)
    text = (text + ("\n" if tail else "")).replace("\n", eol)
    f = _put(tmp_path / "s.json", text)
    data, meta = safejson.read_json_with_meta(f)
    data["c"] = "y"
    safejson.write_json_atomic(f, data, meta, backup_dir=tmp_path / "bk")
    assert f.read_bytes() == text.replace('"x"', '"y"').encode("utf-8")


def test_backup_naming_and_pruning(cj, tmp_path):
    root = tmp_path / "bk"
    original = cj.read_bytes()
    for i in range(5):
        data, meta = safejson.read_json_with_meta(cj)
        data["numStartups"] = 100 + i
        assert safejson.write_json_atomic(cj, data, meta, backup_dir=root, keep=3)
    (root / "mine").mkdir()
    kept = sorted(p.name for p in root.iterdir() if safejson.STAMP.match(p.name))
    assert len(kept) == 3
    assert (root / "mine").is_dir()
    assert all((root / k / ".claude.json").is_file() for k in kept)
    assert original not in [(root / k / ".claude.json").read_bytes() for k in kept]


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes only")
def test_modes(tmp_path):
    secret = _put(tmp_path / ".claude.json", '{"a": 1}')
    plain = _put(tmp_path / "settings.json", '{"a": 1}')
    secret.chmod(0o644)
    plain.chmod(0o644)
    for f, mode in ((secret, safejson.SECRET_MODE), (plain, None)):
        data, meta = safejson.read_json_with_meta(f)
        data["a"] = 2
        safejson.write_json_atomic(f, data, meta, backup_dir=tmp_path / "bk", mode=mode)
    assert secret.stat().st_mode & 0o777 == 0o600
    assert plain.stat().st_mode & 0o777 == 0o644
    copies = list((tmp_path / "bk").rglob(".claude.json"))
    assert [c.stat().st_mode & 0o777 for c in copies] == [0o600]


def test_changed_before_write_refused(cj, tmp_path):
    data, meta = safejson.read_json_with_meta(cj)
    _put(cj, NODE_FIXTURE + "\n")
    data["numStartups"] = 1
    with pytest.raises(safejson.Changed, match="changed while editing"):
        safejson.write_json_atomic(cj, data, meta, backup_dir=tmp_path / "bk")
    assert cj.read_bytes() == (NODE_FIXTURE + "\n").encode("utf-8")
    assert not (tmp_path / "bk").exists() and _tmp_left(tmp_path) == []


def test_size_change_with_same_mtime_refused(cj, tmp_path):
    data, meta = safejson.read_json_with_meta(cj)
    _put(cj, NODE_FIXTURE + "\n\n")
    os.utime(cj, ns=(meta.mtime_ns, meta.mtime_ns))
    data["numStartups"] = 1
    with pytest.raises(safejson.Changed):
        safejson.write_json_atomic(cj, data, meta, backup_dir=tmp_path / "bk")
    assert cj.read_bytes().endswith(b"}\n\n") and _tmp_left(tmp_path) == []


def test_changed_just_before_replace_refused(cj, tmp_path, monkeypatch):
    """Claude Code rewrites the file after the backup, before os.replace."""
    data, meta = safejson.read_json_with_meta(cj)
    real = shutil.copy2

    def copy_then_rewrite(src, dst, **kw):
        out = real(src, dst, **kw)
        _put(cj, NODE_FIXTURE + " ")
        return out

    monkeypatch.setattr(safejson.shutil, "copy2", copy_then_rewrite)
    data["numStartups"] = 1
    with pytest.raises(safejson.Changed):
        safejson.write_json_atomic(cj, data, meta, backup_dir=tmp_path / "bk")
    assert cj.read_bytes() == (NODE_FIXTURE + " ").encode("utf-8")
    assert _tmp_left(tmp_path) == []


def test_replace_failure_cleans_up(cj, tmp_path, monkeypatch):
    data, meta = safejson.read_json_with_meta(cj)

    def boom(*_a, **_k):
        raise PermissionError("nope")

    monkeypatch.setattr(safejson.os, "replace", boom)
    data["numStartups"] = 1
    with pytest.raises(safejson.SafeJsonError, match="^cannot write x: PermissionError$"):
        safejson.write_json_atomic(cj, data, dataclasses.replace(meta, label="x"),
                                   backup_dir=tmp_path / "bk")
    assert cj.read_bytes() == NODE_FIXTURE.encode("utf-8") and _tmp_left(tmp_path) == []


def test_unencodable_value_is_a_clean_error(cj, tmp_path):
    data, meta = safejson.read_json_with_meta(cj, "~/.claude.json")
    bad = _secret() + "\udc00"
    data["userID"] = bad
    with pytest.raises(safejson.SafeJsonError) as info:
        safejson.write_json_atomic(cj, data, meta, backup_dir=tmp_path / "bk")
    assert str(info.value) == "cannot write ~/.claude.json: UnicodeEncodeError"
    assert cj.read_bytes() == NODE_FIXTURE.encode("utf-8") and _tmp_left(tmp_path) == []


def test_read_errors_never_show_contents(tmp_path):
    tok = _secret()
    bad = _put(tmp_path / "a.json", '{"token": "' + tok + '"')
    with pytest.raises(safejson.InvalidJson) as info:
        safejson.read_json_with_meta(bad, "~/.claude.json")
    assert str(info.value) == "~/.claude.json is not valid JSON, left unchanged"
    bad.write_bytes(b'{"t": "' + tok.encode() + b'\xff"}')
    with pytest.raises(safejson.SafeJsonError) as info:
        safejson.read_json_with_meta(bad, "L")
    assert str(info.value) == "cannot read L: UnicodeDecodeError"
    with pytest.raises(safejson.NotFound, match="^L not found$"):
        safejson.read_json_with_meta(tmp_path / "missing.json", "L")


def test_label_defaults_to_file_name(tmp_path):
    with pytest.raises(safejson.NotFound, match="^gone.json not found$"):
        safejson.read_json_with_meta(tmp_path / "gone.json")
