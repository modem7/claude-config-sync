"""scripts/integration.py: switches one plugin, MCP server or skill on or off.

Each test fills the fake HOME, runs the script there and checks the files.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "integration.py"
SECRET = "SECRET-TOKEN-123"
REMINDER = "Start a new Claude Code session to load the change."
PROJ = "/p/one"
WIN = r"C:\Users\x\project\homelab-docs"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")


def _plex() -> dict:
    return {"command": "npx", "args": ["plex-mcp", "--token", SECRET],
            "env": {"PLEX_TOKEN": SECRET}, "headers": {"Authorization": SECRET}}


def _claude_json(disabled: list[str]) -> dict:
    return {
        "numStartups": 7, "userID": "abc", "mcpServers": {"web": {"url": SECRET}},
        "projects": {
            PROJ: {"mcpServers": {"plex": _plex()}, "disabledMcpServers": disabled,
                   "allowedTools": ["x"]},
            "/p/two": {"mcpServers": {}, "disabledMcpServers": ["keep"]},
        },
    }


@pytest.fixture
def home(fake_home) -> Path:
    root = fake_home.root
    _write(fake_home.claude / "settings.json", json.dumps(
        {"model": "opus", "enabledPlugins": {"a@m": True, "b@m": False},
         "note": "caf\u00e9"}, indent=4, ensure_ascii=False) + "\n")
    _write(root / ".claude.json", json.dumps(_claude_json(["plex"]), indent=2))
    _write(fake_home.claude / "skills" / "on" / "SKILL.md", "---\nname: on\n---\n")
    _write(fake_home.claude / "skills-disabled" / "off" / "SKILL.md", "---\nname: off\n---\n")
    return root


def _run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, check=False,
                          text=True, encoding="utf-8", env=os.environ.copy(), cwd=cwd)


def _ok(*args: str, cwd: Path | None = None) -> str:
    r = _run(*args, cwd=cwd)
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    assert len(lines) == 2 and lines[-1] == REMINDER, r.stdout
    return lines[0]


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _backups(home: Path) -> list[Path]:
    root = home / ".claude" / ".integration-backups"
    return sorted(root.iterdir()) if root.is_dir() else []


def _listing(root: Path) -> list[tuple[str, int, int]]:
    return sorted((str(p.relative_to(root)), p.stat().st_size if p.is_file() else 0,
                   p.stat().st_mtime_ns) for p in root.rglob("*"))


def _no_secret(r: subprocess.CompletedProcess) -> None:
    for word in (SECRET, "npx", "--token", "plex-mcp", "PLEX_TOKEN", "Authorization"):
        assert word not in r.stdout and word not in r.stderr, word


def test_plugin_enable_and_disable(home):
    settings = home / ".claude" / "settings.json"
    assert "Enabled plugin b@m" in _ok("enable", "plugin", "b@m")
    assert _load(settings)["enabledPlugins"] == {"a@m": True, "b@m": True}
    assert "Disabled plugin a@m" in _ok("disable", "plugin", "a@m")
    data = _load(settings)
    assert data["enabledPlugins"] == {"a@m": False, "b@m": True}
    assert data["model"] == "opus" and data["note"] == "caf\u00e9"
    assert "already disabled" in _ok("disable", "plugin", "a@m")


def test_mcp_enable_and_disable(home):
    cj = home / ".claude.json"
    assert f"Enabled MCP server plex for {PROJ}" in _ok("enable", "mcp", "plex", "--project", PROJ)
    data = _load(cj)
    assert data["projects"][PROJ]["disabledMcpServers"] == []
    assert data["projects"][PROJ]["mcpServers"]["plex"] == _plex()
    assert "Disabled MCP server plex" in _ok("disable", "mcp", "plex", "--project", PROJ + "/")
    data = _load(cj)
    assert data["projects"][PROJ]["disabledMcpServers"] == ["plex"]
    expected = _claude_json(["plex"])
    assert data == expected


def test_mcp_project_from_cwd_ancestor(home, tmp_path):
    proj = tmp_path / "work"
    (proj / "sub" / "deeper").mkdir(parents=True)
    data = _claude_json(["plex"])
    data["projects"][str(proj)] = data["projects"].pop(PROJ)
    _write(home / ".claude.json", json.dumps(data, indent=2))
    out = _ok("enable", "mcp", "plex", cwd=proj / "sub" / "deeper")
    assert f"for {proj} in" in out
    assert _load(home / ".claude.json")["projects"][str(proj)]["disabledMcpServers"] == []


def test_mcp_missing_definition_exits_1(home, tmp_path):
    before = _listing(home)
    for args in (("enable", "mcp", "ghost", "--project", PROJ),
                 ("disable", "mcp", "plex", "--project", "/p/two"),
                 ("disable", "mcp", "plex", "--project", "/nowhere")):
        r = _run(*args)
        assert r.returncode == 1, args
        assert r.stdout == "" and len(r.stderr.splitlines()) == 1
        _no_secret(r)
    r = _run("disable", "mcp", "plex", cwd=tmp_path)
    assert r.returncode == 1
    assert _listing(home) == before
    assert "/p/nowhere" not in _load(home / ".claude.json")["projects"]


def test_mcp_user_scope_definition_refused(home):
    before = (home / ".claude.json").read_bytes()
    r = _run("disable", "mcp", "web", "--project", "/p/two")
    assert r.returncode == 1 and "no MCP server web defined" in r.stderr
    assert (home / ".claude.json").read_bytes() == before


def test_secrets_never_printed(home):
    runs = [_run("enable", "mcp", "plex", "--project", PROJ, "--dry-run"),
            _run("enable", "mcp", "plex", "--project", PROJ),
            _run("enable", "mcp", "plex", "--project", PROJ),
            _run("disable", "mcp", "plex", "--project", PROJ),
            _run("disable", "mcp", "nope", "--project", PROJ)]
    for r in runs:
        _no_secret(r)


def test_windows_style_project_key(home):
    data = _claude_json(["plex"])
    data["projects"] = {WIN: data["projects"][PROJ]}
    _write(home / ".claude.json", json.dumps(data, indent=2))
    assert WIN in _ok("enable", "mcp", "plex", "--project", WIN)
    projects = _load(home / ".claude.json")["projects"]
    assert list(projects) == [WIN]
    assert projects[WIN]["disabledMcpServers"] == []
    _ok("disable", "mcp", "plex", "--project", "C:/Users/x/project/homelab-docs")
    projects = _load(home / ".claude.json")["projects"]
    assert list(projects) == [WIN] and projects[WIN]["disabledMcpServers"] == ["plex"]


def test_skill_enable_and_disable(home):
    claude = home / ".claude"
    assert "Enabled skill off" in _ok("enable", "skill", "off")
    assert (claude / "skills" / "off" / "SKILL.md").is_file()
    assert not (claude / "skills-disabled" / "off").exists()
    assert "Disabled skill on" in _ok("disable", "skill", "on")
    assert (claude / "skills-disabled" / "on" / "SKILL.md").is_file()
    assert "already enabled" in _ok("enable", "skill", "off")


def test_skill_refuses_if_target_exists(home):
    _write(home / ".claude" / "skills" / "off" / "SKILL.md", "other\n")
    r = _run("enable", "skill", "off")
    assert r.returncode == 1 and "nothing moved" in r.stderr
    assert (home / ".claude" / "skills-disabled" / "off" / "SKILL.md").is_file()
    assert (home / ".claude" / "skills" / "off" / "SKILL.md").read_text(encoding="utf-8") == "other\n"
    assert _run("enable", "skill", "missing").returncode == 1
    assert _run("enable", "skill", "../x").returncode == 1


def test_dry_run_writes_nothing(home):
    before = _listing(home)
    for args in (("enable", "plugin", "b@m"), ("disable", "plugin", "a@m"),
                 ("enable", "mcp", "plex", "--project", PROJ),
                 ("disable", "skill", "on"), ("enable", "skill", "off")):
        assert _ok(*args, "--dry-run").startswith("Dry run: would ")
    assert _listing(home) == before
    assert not (home / ".claude" / ".integration-backups").exists()


def test_backup_created_and_pruned(home):
    settings = home / ".claude" / "settings.json"
    original = settings.read_bytes()
    _ok("enable", "plugin", "b@m")
    (first,) = _backups(home)
    assert (first / "settings.json").read_bytes() == original
    _ok("enable", "mcp", "plex", "--project", PROJ)
    assert (_backups(home)[-1] / ".claude.json").is_file()
    for i in range(12):
        _ok("disable" if i % 2 == 0 else "enable", "plugin", "x@m")
    kept = _backups(home)
    assert len(kept) == 10 and first not in kept
    (home / ".claude" / ".integration-backups" / "mine").mkdir()
    _ok("disable", "plugin", "y@m")
    assert (home / ".claude" / ".integration-backups" / "mine").is_dir()


def test_no_backup_when_already_so(home):
    _ok("enable", "plugin", "a@m")
    _ok("disable", "mcp", "plex", "--project", PROJ)
    assert _backups(home) == []


@pytest.mark.parametrize("indent", [2, 4, "\t"])
def test_indent_and_layout_preserved(home, indent):
    settings = home / ".claude" / "settings.json"
    text = json.dumps({"enabledPlugins": {"a@m": True}, "z": [1, 2]}, indent=indent)
    _write(settings, text.replace("\n", "\r\n"))
    _ok("disable", "plugin", "a@m")
    raw = settings.read_bytes().decode("utf-8")
    want = json.dumps({"enabledPlugins": {"a@m": False}, "z": [1, 2]}, indent=indent)
    assert raw == want.replace("\n", "\r\n")


def test_compact_file_gets_default_indent(home):
    settings = home / ".claude" / "settings.json"
    _write(settings, '{"enabledPlugins": {}}\n')
    _ok("enable", "plugin", "n@m")
    assert settings.read_text(encoding="utf-8") == json.dumps(
        {"enabledPlugins": {"n@m": True}}, indent=2) + "\n"


def test_malformed_json_left_alone(home):
    settings = home / ".claude" / "settings.json"
    _write(settings, "{not json")
    r = _run("enable", "plugin", "a@m")
    assert r.returncode == 1 and "not valid JSON" in r.stderr
    assert settings.read_text(encoding="utf-8") == "{not json"
    assert _backups(home) == []


def test_missing_settings_exits_1(home):
    (home / ".claude" / "settings.json").unlink()
    r = _run("enable", "plugin", "a@m")
    assert r.returncode == 1 and "not found" in r.stderr
    assert not (home / ".claude" / "settings.json").exists()


def test_registry_untouched(home, fake_home, tmp_path):
    cwd = tmp_path / "cwd"
    regs = [cwd / "integrations.json", fake_home.repo / "integrations.json",
            home / "claude-config" / "integrations.json"]
    for reg in regs:
        _write(reg, '{"off_until_asked": [{"kind": "plugin", "name": "a@m"}]}\n')
    before = [(r.read_bytes(), r.stat().st_mtime_ns) for r in regs]
    _ok("disable", "plugin", "a@m", cwd=cwd)
    _ok("enable", "mcp", "plex", "--project", PROJ, cwd=cwd)
    _ok("disable", "skill", "on", cwd=cwd)
    assert [(r.read_bytes(), r.stat().st_mtime_ns) for r in regs] == before


@pytest.mark.parametrize("args", [
    (), ("enable",), ("enable", "agent", "x"), ("toggle", "plugin", "x"),
    ("enable", "plugin", "x", "--project", "/p"), ("enable", "plugin", "x", "--nope"),
])
def test_bad_arguments_exit_2(home, args):
    assert _run(*args).returncode == 2
