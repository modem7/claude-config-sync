"""scripts/list_disabled.py: reports switched-off integrations by registry bucket.

Each test fills the fake HOME with config files and a registry, runs the
script there and checks the report. It is read-only and never prints secrets.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "list_disabled.py"
SECRET = "SECRET-TOKEN-123"
NOTE = "Plex media, ask first"


def _write(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = data if isinstance(data, str) else json.dumps(data)
    path.write_text(text, encoding="utf-8", newline="\n")


def _plex() -> dict:
    return {
        "command": "npx",
        "args": ["plex-mcp", "--token", SECRET],
        "env": {"PLEX_TOKEN": SECRET},
    }


REGISTRY = {
    "off_until_asked": [
        {"kind": "mcp", "name": "plex", "projects": ["/p/one", "/p/two"], "note": NOTE},
        {"kind": "plugin", "name": "on@market", "note": "should be off"},
    ],
    "enabled_per_project": [
        {"kind": "mcp", "name": "web", "projects": ["/p/two"], "note": "web search"},
    ],
    "uninstalled": [
        {"kind": "plugin", "name": "gone@market", "note": "removed"},
        {"kind": "skill", "name": "foo", "note": "should be gone"},
    ],
}


@pytest.fixture
def home(fake_home) -> Path:
    root = fake_home.root
    _write(fake_home.claude / "settings.json", {
        "enabledPlugins": {"off@market": False, "on@market": True},
    })
    _write(fake_home.claude / "plugins" / "installed_plugins.json", {
        "plugins": {"off@market": [], "on@market": [], "extra@market": []},
    })
    _write(root / ".claude.json", {
        "projects": {
            "/p/one": {
                "mcpServers": {"plex": _plex()},
                "disabledMcpServers": ["plex", "ghost"],
            },
            "/p/two": {
                "mcpServers": {"plex": _plex(), "web": {"type": "http", "url": SECRET}},
                "disabledMcpServers": ["plex"],
            },
            "/p/three": {
                "mcpServers": {"plex": _plex(), "web": {"type": "http", "headers": {"a": SECRET}}},
                "disabledMcpServers": ["plex"],
            },
        },
    })
    _write(
        fake_home.claude / "skills-disabled" / "foo" / "SKILL.md",
        "---\nname: foo\ndescription: Does foo things\n---\nbody\n",
    )
    _write(fake_home.claude / "skills-disabled" / "bar" / "SKILL.md", "---\nname: bar\n---\n")
    _write(root / "registry.json", REGISTRY)
    return root


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, env=os.environ.copy(), check=False,
    )


def _reg(home: Path) -> list[str]:
    return ["--registry", str(home / "registry.json")]


def _json(home: Path) -> dict:
    result = _run("--json", *_reg(home))
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _listing(root: Path) -> list[tuple[str, int, int]]:
    return sorted(
        (str(p.relative_to(root)), p.stat().st_size, p.stat().st_mtime_ns)
        for p in root.rglob("*")
    )


def test_text_report_by_bucket(home):
    result = _run(*_reg(home))
    assert result.returncode == 0, result.stderr
    out = result.stdout
    off, rest = out.split("\nEnabled per project:\n")
    assert f"mcp plex - {NOTE}" in off
    assert "/p/one: disabled as expected" in off
    assert 'remove "plex" from disabledMcpServers for /p/one in ~/.claude.json' in off
    assert "plugin on@market - should be off\n    MISMATCH: enabled" in off
    per, rest = rest.split("\nUninstalled:\n")
    assert "/p/two: enabled" in per
    assert "/p/three: MISMATCH: enabled, not listed" in per
    gone, rest = rest.split("\nUnclassified (decide which bucket):\n")
    assert "plugin gone@market - removed\n    not present here" in gone
    assert "skill foo - should be gone\n    MISMATCH: still installed" in gone
    unc, mism = rest.split("\nMismatches:\n")
    assert "plugin off@market (disabled)" in unc
    assert "plugin extra@market (installed, not enabled)" in unc
    assert "mcp plex [stdio] (1 project)\n    /p/three" in unc
    assert "    /p/one\n" not in unc and "    /p/two\n" not in unc
    assert "mcp ghost (listed but not defined): /p/one" in unc
    assert "skill bar (disabled)" in unc
    assert "move ~/.claude/skills-disabled/bar back to ~/.claude/skills/" in unc
    assert "foo" not in unc
    assert "Off until asked: plugin on@market: enabled" in mism
    assert "Enabled per project: mcp web in /p/three: enabled, not listed" in mism


def test_json_buckets_and_mismatches(home):
    data = _json(home)
    plex = data["off_until_asked"][0]
    assert plex["bucket"] == "off_until_asked"
    assert [c["project"] for c in plex["checks"]] == ["/p/one", "/p/two"]
    assert all(c["ok"] for c in plex["checks"])
    unc = {(i["kind"], i["name"]): i for i in data["unclassified"]}
    assert set(unc) == {("plugin", "off@market"), ("plugin", "extra@market"),
                        ("mcp", "plex"), ("mcp", "ghost"), ("skill", "bar")}
    assert unc[("mcp", "plex")]["projects"] == ["/p/three"]
    assert set(unc[("mcp", "plex")]) == {"bucket", "kind", "name", "types", "projects", "enable"}
    assert all(i["bucket"] == "unclassified" for i in data["unclassified"])
    assert {"bucket": "off_until_asked", "kind": "plugin", "name": "on@market",
            "project": None, "state": "enabled"} in data["mismatches"]
    assert len(data["mismatches"]) == 3


def test_mismatch_registry_off_but_plugin_enabled(fake_home):
    _write(fake_home.claude / "settings.json", {"enabledPlugins": {"p@m": True}})
    _write(fake_home.root / "r.json", {"off_until_asked": [{"kind": "plugin", "name": "p@m"}]})
    out = _run("--registry", str(fake_home.root / "r.json")).stdout
    assert "plugin p@m\n    MISMATCH: enabled" in out
    assert "Mismatches:\n  Off until asked: plugin p@m: enabled" in out


def test_note_printed_but_no_secrets(home):
    for args in ((), ("--json",)):
        result = _run(*args, *_reg(home))
        assert NOTE in result.stdout
        assert SECRET not in result.stdout
        assert SECRET not in result.stderr
        for word in ("npx", "PLEX_TOKEN", "--token", "plex-mcp"):
            assert word not in result.stdout


def test_missing_registry_means_all_unclassified(home):
    result = _run("--json", "--registry", str(home / "nope.json"))
    assert result.returncode == 0
    assert "nope.json not found" in result.stderr
    assert "everything is unclassified" in result.stderr
    data = json.loads(result.stdout)
    assert all(data[b] == [] for b in ("off_until_asked", "enabled_per_project", "uninstalled"))
    names = {(i["kind"], i["name"]) for i in data["unclassified"]}
    assert {("skill", "foo"), ("skill", "bar"), ("mcp", "plex")} <= names
    plex = next(i for i in data["unclassified"] if i["name"] == "plex")
    assert plex["projects"] == ["/p/one", "/p/three", "/p/two"]


def test_malformed_registry_warns(home):
    _write(home / "bad.json", "{not json")
    result = _run("--registry", str(home / "bad.json"))
    assert result.returncode == 0
    assert "cannot read" in result.stderr
    assert "Off until asked:\n  none" in result.stdout


def test_default_registry_in_repo(home, tmp_path):
    repo = tmp_path / "cfg"
    _write(repo / "integrations.json", REGISTRY)
    out = _run("--repo", str(repo)).stdout
    assert f"mcp plex - {NOTE}" in out


def test_nothing_written(home):
    before = _listing(home)
    _run(*_reg(home))
    _run("--json", *_reg(home))
    assert _listing(home) == before


def test_missing_files_do_not_crash(fake_home):
    result = _run()
    assert result.returncode == 0, result.stderr
    assert "warning:" in result.stderr
    for title in ("Off until asked", "Enabled per project", "Uninstalled",
                  "Unclassified (decide which bucket)", "Mismatches"):
        assert f"{title}:\n  none" in result.stdout


def test_malformed_config_warns(fake_home):
    _write(fake_home.claude / "settings.json", "{not json")
    _write(fake_home.root / ".claude.json", "[1, 2]")
    result = _run("--json")
    assert result.returncode == 0
    assert "cannot read" in result.stderr
    assert "not a JSON object" in result.stderr
    assert json.loads(result.stdout)["unclassified"] == []


def test_profile_markers_and_agents(home, tmp_path):
    repo = tmp_path / "cfg"
    _write(repo / "machines" / "box" / "skills" / "baz.disabled", "")
    _write(repo / "machines" / "box" / "agents" / "helper.md.disabled", "")
    _write(home / ".claude" / "agents-disabled" / "old.md", "x")
    out = _run("--repo", str(repo), *_reg(home)).stdout
    assert "skill baz (hidden by profile box)" in out
    assert "delete machines/box/skills/baz.disabled in" in out
    assert "agent helper (hidden by profile box)" in out
    assert "agent old (disabled)" in out
    assert "move ~/.claude/agents-disabled/old.md back to ~/.claude/agents/" in out


def test_per_project_plugin_and_skill(fake_home, tmp_path):
    proj = tmp_path / "proj"
    _write(proj / ".claude" / "settings.json", {"enabledPlugins": {"p@m": True}})
    _write(fake_home.root / "r.json", {"enabled_per_project": [
        {"kind": "plugin", "name": "p@m", "projects": [str(proj)]},
        {"kind": "skill", "name": "s", "projects": [str(proj)]},
    ]})
    data = json.loads(_run("--json", "--registry", str(fake_home.root / "r.json")).stdout)
    plugin, skill = data["enabled_per_project"]
    assert plugin["checks"] == [{"project": str(proj), "state": "enabled", "ok": True, "enable": None}]
    assert skill["checks"][0]["state"] == "not enabled"
    assert not skill["checks"][0]["ok"]


def test_block_scalar_description_and_truncation(fake_home):
    base = fake_home.claude / "skills-disabled"
    _write(base / "fold" / "SKILL.md",
           "---\ndescription: >\n  first line\n  second line\nname: fold\n---\n")
    _write(base / "long" / "SKILL.md", "---\ndescription: " + "x" * 150 + "\n---\n")
    items = {s["name"]: s for s in json.loads(_run("--json").stdout)["unclassified"]}
    assert items["fold"]["description"] == "first line second line"
    assert len(items["long"]["description"]) == 100


def test_bad_argument_exits_2(fake_home):
    assert _run("--nope").returncode == 2


def test_projects_match_by_basename_any_case_and_separator(fake_home):
    win = r"C:\Users\x\project\homelab-docs"
    nix = "/home/x/project/HOMELAB-DOCS/"
    _write(fake_home.root / ".claude.json", {"projects": {
        win: {"mcpServers": {"plex": _plex()}, "disabledMcpServers": ["plex"]},
        nix: {"mcpServers": {"plex": _plex()}, "disabledMcpServers": ["plex"]},
        "/home/x/project/other": {"mcpServers": {"plex": _plex()},
                                  "disabledMcpServers": ["plex"]},
    }})
    _write(fake_home.root / "r.json", {"off_until_asked": [
        {"kind": "mcp", "name": "plex", "projects": ["Homelab-Docs", "/old/homelab-docs"]},
    ]})
    data = json.loads(_run("--json", "--registry", str(fake_home.root / "r.json")).stdout)
    checks = data["off_until_asked"][0]["checks"]
    assert sorted(c["project"] for c in checks) == sorted([win, nix])
    assert all(c["ok"] and c["state"] == "disabled as expected" for c in checks)
    assert any(f"for {win} in ~/.claude.json" in c["enable"] for c in checks)
    assert data["mismatches"] == []
    unc = {i["name"]: i for i in data["unclassified"]}
    assert unc["plex"]["projects"] == ["/home/x/project/other"]


def test_exact_project_path_beats_basename(fake_home):
    _write(fake_home.root / ".claude.json", {"projects": {
        "/home/x": {"mcpServers": {}},
        "/home/x/project/x": {"mcpServers": {"plex": _plex()}, "disabledMcpServers": ["plex"]},
    }})
    _write(fake_home.root / "r.json", {"off_until_asked": [
        {"kind": "mcp", "name": "plex", "projects": ["/home/x/project/x"]},
    ]})
    data = json.loads(_run("--json", "--registry", str(fake_home.root / "r.json")).stdout)
    assert [c["project"] for c in data["off_until_asked"][0]["checks"]] == ["/home/x/project/x"]


def test_machines_filter(fake_home, monkeypatch):
    monkeypatch.delenv("CLAUDE_SYNC_PROFILE", raising=False)
    _write(fake_home.claude / "settings.json",
           {"enabledPlugins": {"a@m": True, "b@m": True}})
    _write(fake_home.root / "r.json", {"off_until_asked": [
        {"kind": "plugin", "name": "a@m", "machines": ["host-b"]},
        {"kind": "plugin", "name": "b@m", "machines": ["HOST-A"]},
    ]})
    data = json.loads(_run("--json", "--registry", str(fake_home.root / "r.json")).stdout)
    other, here = data["off_until_asked"]
    assert other["checks"] == [{"project": None, "state": "not for this machine",
                                "ok": True, "enable": None}]
    assert here["checks"][0]["state"] == "enabled"
    assert data["mismatches"] == [{"bucket": "off_until_asked", "kind": "plugin",
                                   "name": "b@m", "project": None, "state": "enabled"}]


def test_machines_match_profile(fake_home, monkeypatch):
    monkeypatch.setenv("CLAUDE_SYNC_PROFILE", "laptop")
    _write(fake_home.claude / "settings.json", {"enabledPlugins": {"a@m": True}})
    _write(fake_home.root / "r.json", {"off_until_asked": [
        {"kind": "plugin", "name": "a@m", "machines": ["Laptop"]},
    ]})
    data = json.loads(_run("--json", "--registry", str(fake_home.root / "r.json")).stdout)
    assert data["off_until_asked"][0]["checks"][0]["state"] == "enabled"


def test_absent_items_are_not_mismatches(fake_home):
    _write(fake_home.root / ".claude.json", {"projects": {"/p/one": {"mcpServers": {}}}})
    reg = {
        "off_until_asked": [
            {"kind": "plugin", "name": "nope@m"},
            {"kind": "mcp", "name": "plex", "projects": ["one", "missing"]},
            {"kind": "skill", "name": "nope"},
        ],
        "enabled_per_project": [
            {"kind": "mcp", "name": "web", "projects": ["elsewhere"]},
            {"kind": "skill", "name": "s", "projects": ["elsewhere"]},
        ],
        "uninstalled": [
            {"kind": "plugin", "name": "gone@m"},
            {"kind": "mcp", "name": "gone"},
            {"kind": "agent", "name": "gone.md"},
        ],
    }
    _write(fake_home.root / "r.json", reg)
    data = json.loads(_run("--json", "--registry", str(fake_home.root / "r.json")).stdout)
    for bucket in ("off_until_asked", "enabled_per_project", "uninstalled"):
        for entry in data[bucket]:
            assert entry["checks"], entry
            for c in entry["checks"]:
                assert c["ok"] and c["state"] == "not present here", (entry["name"], c)
    assert data["mismatches"] == []
    out = _run("--registry", str(fake_home.root / "r.json")).stdout
    assert "Mismatches:\n  none" in out
    assert "MISMATCH" not in out
