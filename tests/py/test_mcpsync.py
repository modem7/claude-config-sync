"""claude_sync.mcpsync: opt-in sync of user-scope MCP servers.

tmp_path only; never the real ~/.claude.json. Every fake credential is
built at runtime, so no token-shaped literal sits in this file (it is
published). Placeholder names and hosts only.
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
from pathlib import Path

import pytest

from claude_sync import mcpsync, safejson

LIST_DISABLED = Path(__file__).resolve().parents[2] / "scripts" / "list_disabled.py"

# Runtime-built fakes: never literal token shapes in the source.
GH = "gh" + "p_" + "A1b2" * 6
SK = "sk-" + "Zq9x" * 8
PW = "pw" + "x" * 8 + "9" * 4
SECRETS = (GH, SK, PW)


class Lines(list):
    def __call__(self, line: str) -> None:
        self.append(line)

    def text(self) -> str:
        return "\n".join(self)


def no_secret(*texts: str) -> None:
    for t in texts:
        for s in SECRETS:
            assert s not in t


def setup(tmp_path: Path, live: object | None = None, repo_servers: dict | None = None):
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    cj = home / ".claude.json"
    if live is not None:
        cj.write_text(live if isinstance(live, str) else json.dumps(live, indent=2), encoding="utf-8")
    if repo_servers is not None:
        write_repo(repo, repo_servers)
    return cj, repo, home / ".claude" / ".claude-sync-backups"


def write_repo(repo: Path, servers: dict) -> None:
    (repo / "mcp").mkdir(exist_ok=True)
    (repo / mcpsync.REPO_FILE).write_bytes(mcpsync.render_repo(servers))


def repo_data(repo: Path) -> dict:
    return json.loads((repo / mcpsync.REPO_FILE).read_text(encoding="utf-8"))["mcpServers"]


def live_data(cj: Path) -> dict:
    return json.loads(cj.read_text(encoding="utf-8"))


NPX = {"command": "npx", "args": ["-y", "some-mcp"], "env": {}}
WEB = {"type": "http", "url": "https://mcp.example.invalid/v1"}


# ── 1. apply merges, never replaces ──────────────────────────────────────


def test_apply_keeps_refused_per_machine_and_local_servers(tmp_path):
    plex = {"command": "npx", "args": ["plex-mcp"], "env": {"PLEX_TOKEN": PW}}
    box = {"command": "uvx", "args": ["box-mcp"]}
    local = {"command": "node", "args": ["local.js"]}
    live = {
        "numStartups": 3,
        "projects": {"/home/user/project/alpha": {"mcpServers": {}}},
        "mcpServers": {"plex": plex, "box": box, "local": local, "shared": {"command": "npx", "args": ["old"]}},
        "userID": "0f3c",
    }
    cj, repo, backups = setup(tmp_path, live, {
        "shared": NPX, "web": WEB, "box": {"command": "uvx", "args": ["other"]},
    })
    policy = mcpsync.Policy(per_machine={"box"})
    say = Lines()
    assert mcpsync.apply(cj, repo, policy, backups, say) is True
    after = live_data(cj)
    assert list(after) == ["numStartups", "projects", "mcpServers", "userID"]
    assert after["projects"] == live["projects"] and after["userID"] == "0f3c"
    servers = after["mcpServers"]
    assert list(servers) == ["plex", "box", "local", "shared", "web"]
    assert servers["plex"] == plex and servers["box"] == box and servers["local"] == local
    assert servers["shared"] == NPX and servers["web"] == WEB
    assert say == ["mcp: skipped box: per-machine, not listed for this host"]


def test_merge_for_live_keeps_order_and_appends_sorted():
    live = {"b": 1, "a": 2}
    assert list(mcpsync.merge_for_live(live, {"z": 0, "a": 9, "c": 3})) == ["b", "a", "c", "z"]
    assert mcpsync.merge_for_live(live, {"a": 9})["a"] == 9
    assert live == {"b": 1, "a": 2}


def test_merge_for_repo_live_wins_and_nothing_deleted():
    assert mcpsync.merge_for_repo({"a": 1, "b": 2}, {"b": 3, "c": 4}) == {"a": 1, "b": 3, "c": 4}


# ── 2. refusal of literal credentials ────────────────────────────────────


REFUSED = {
    "envsrv": ({"command": "npx", "env": {"API_TOKEN": GH}}, "env API_TOKEN"),
    "hdrsrv": ({"type": "http", "url": "https://x.example.invalid", "headers": {"Authorization": "Bearer " + SK}},
               "header Authorization"),
    "argsrv": ({"command": "npx", "args": ["-y", "pkg", "--token", PW]}, "arg #4"),
    "urlsrv": ({"type": "sse", "url": "https://u:" + PW + "@x.example.invalid/s"}, "url"),
    "cmdsrv": ({"command": SK}, "command"),
}


def test_refused_on_capture(tmp_path):
    live = {"mcpServers": {**{n: s for n, (s, _) in REFUSED.items()}, "ok": NPX}}
    cj, repo, _ = setup(tmp_path, live)
    say = Lines()
    assert mcpsync.capture(cj, repo, mcpsync.Policy(), say) is True
    assert repo_data(repo) == {"ok": NPX}
    for name, (_, where) in REFUSED.items():
        assert f"mcp: skipped {name}: literal credential in {where}" in say
    no_secret(say.text(), (repo / mcpsync.REPO_FILE).read_text(encoding="utf-8"))


def test_refused_on_apply_from_hand_edited_repo(tmp_path):
    cj, repo, backups = setup(tmp_path, {"mcpServers": {}},
                              {**{n: s for n, (s, _) in REFUSED.items()}, "ok": NPX})
    say = Lines()
    assert mcpsync.apply(cj, repo, mcpsync.Policy(), backups, say) is True
    assert live_data(cj)["mcpServers"] == {"ok": NPX}
    for name, (_, where) in REFUSED.items():
        assert f"mcp: skipped {name}: literal credential in {where}" in say
    no_secret(say.text(), cj.read_text(encoding="utf-8"))


def test_non_object_server_is_skipped(tmp_path):
    eligible, skipped = mcpsync.classify({"bad": [1], "ok": NPX}, mcpsync.Policy())
    assert eligible == {"ok": NPX} and skipped == [("bad", "not a JSON object")]


def test_unencodable_value_does_not_raise(tmp_path):
    # A lone surrogate parses but cannot be encoded back to UTF-8.
    cj, repo, _ = setup(tmp_path, '{"mcpServers": {"x": {"command": "npx", "args": ["\\ud800"]}}}')
    say = Lines()
    assert mcpsync.capture(cj, repo, mcpsync.Policy(), say) is False
    assert say == ["mcp: capture skipped: internal error"]
    assert not (repo / mcpsync.REPO_FILE).exists()


# ── 3. no deletes ────────────────────────────────────────────────────────


def test_capture_keeps_repo_only_servers(tmp_path):
    cj, repo, _ = setup(tmp_path, {"mcpServers": {"a": NPX}}, {"gone": WEB})
    assert mcpsync.capture(cj, repo, mcpsync.Policy(), Lines()) is True
    assert repo_data(repo) == {"a": NPX, "gone": WEB}


def test_apply_keeps_live_only_servers(tmp_path):
    cj, repo, backups = setup(tmp_path, {"mcpServers": {"mine": NPX}}, {"web": WEB})
    assert mcpsync.apply(cj, repo, mcpsync.Policy(), backups, Lines()) is True
    assert live_data(cj)["mcpServers"] == {"mine": NPX, "web": WEB}


# ── 4. registry ──────────────────────────────────────────────────────────


REGISTRY = {
    "off_until_asked": [{"kind": "mcp", "name": "media", "projects": ["/home/user/project/alpha"]}],
    "uninstalled": [{"kind": "mcp", "name": "old"}, {"kind": "plugin", "name": "web"}],
    "enabled_per_project": [
        {"kind": "mcp", "name": "elsewhere", "machines": ["host-b"]},
        {"kind": "mcp", "name": "here", "machines": ["HOST-A"]},
        {"kind": "mcp", "name": "byprofile", "machines": ["Laptops"]},
    ],
}


def write_registry(tmp_path: Path, data: object) -> Path:
    path = tmp_path / "integrations.json"
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return path


def test_policy_from_registry(tmp_path):
    policy = mcpsync.load_policy(write_registry(tmp_path, REGISTRY), mcpsync.host_names("Host-A", "laptops"))
    assert policy.excluded == {"media": "registry: off_until_asked", "old": "registry: uninstalled"}
    assert policy.per_machine == {"elsewhere"}
    assert policy.reason("media") == "registry: off_until_asked"
    assert policy.reason("elsewhere") == mcpsync.PER_MACHINE
    assert policy.reason("here") is None and policy.reason("byprofile") is None
    assert policy.reason("web") is None  # a plugin entry is not an mcp one
    policy = mcpsync.load_policy(tmp_path / "integrations.json", mcpsync.host_names("host-a", ""))
    assert policy.per_machine == {"elsewhere", "byprofile"}


def test_registry_rules_on_capture_and_apply(tmp_path):
    policy = mcpsync.load_policy(write_registry(tmp_path, REGISTRY), mcpsync.host_names("host-a", "laptops"))
    servers = {n: NPX for n in ("media", "old", "elsewhere", "here", "byprofile", "web")}
    cj, repo, _ = setup(tmp_path, {"mcpServers": servers})
    say = Lines()
    assert mcpsync.capture(cj, repo, policy, say) is True
    assert set(repo_data(repo)) == {"here", "byprofile", "web"}
    assert say == [
        "mcp: skipped elsewhere: per-machine, not listed for this host",
        "mcp: skipped media: registry: off_until_asked",
        "mcp: skipped old: registry: uninstalled",
    ]
    cj2, repo2, backups2 = setup(tmp_path / "b", {"mcpServers": {}}, servers)
    assert mcpsync.apply(cj2, repo2, policy, backups2, Lines()) is True
    assert set(live_data(cj2)["mcpServers"]) == {"here", "byprofile", "web"}


def test_load_policy_missing_and_malformed(tmp_path):
    assert mcpsync.load_policy(tmp_path / "nope.json", {"host-a"}) == mcpsync.Policy()
    assert mcpsync.load_policy(write_registry(tmp_path, "{not json"), {"host-a"}) is None
    assert mcpsync.load_policy(write_registry(tmp_path, "[]"), {"host-a"}) is None
    (tmp_path / "dir.json").mkdir()
    assert mcpsync.load_policy(tmp_path / "dir.json", {"host-a"}) is None


def _list_disabled():
    spec = importlib.util.spec_from_file_location("list_disabled_for_mcpsync", LIST_DISABLED)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses look the module up by name
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop(spec.name, None)
    return mod


@pytest.mark.parametrize("host,profile", [("host-a", ""), ("Host-A", "Laptops"), ("HOST-B", "host-b")])
def test_host_names_agree_with_list_disabled(tmp_path, monkeypatch, host, profile):
    monkeypatch.setenv("CLAUDE_SYNC_HOSTNAME", host)
    if profile:
        monkeypatch.setenv("CLAUDE_SYNC_PROFILE", profile)
    else:
        monkeypatch.delenv("CLAUDE_SYNC_PROFILE", raising=False)
    ld = _list_disabled()
    machine = ld.this_machine(None)
    names = mcpsync.host_names(host, profile)
    assert names == machine
    policy = mcpsync.load_policy(write_registry(tmp_path, REGISTRY), names)
    reg = ld.load_registry(tmp_path / "integrations.json")
    theirs = {e["name"] for b in ld.BUCKETS for e in reg[b] if e["kind"] == "mcp"
              and e["machines"] and not {m.casefold() for m in e["machines"]} & machine}
    assert policy.per_machine == theirs


# ── 5. write safety ──────────────────────────────────────────────────────


def test_noop_apply_touches_nothing(tmp_path, monkeypatch):
    cj, repo, backups = setup(tmp_path, {"mcpServers": {"a": NPX}, "x": 1}, {"a": NPX})
    before, mtime = cj.read_bytes(), cj.stat().st_mtime_ns

    def boom(*a, **k):
        raise AssertionError("write_json_atomic called")

    monkeypatch.setattr(safejson, "write_json_atomic", boom)
    assert mcpsync.apply(cj, repo, mcpsync.Policy(), backups, Lines()) is False
    assert cj.read_bytes() == before and cj.stat().st_mtime_ns == mtime
    assert not backups.exists()


def test_noop_apply_without_mcpservers_key(tmp_path):
    cj, repo, backups = setup(tmp_path, {"x": 1}, {})
    before = cj.read_bytes()
    assert mcpsync.apply(cj, repo, mcpsync.Policy(), backups, Lines()) is False
    assert cj.read_bytes() == before


def test_apply_appends_mcpservers_when_absent(tmp_path):
    cj, repo, backups = setup(tmp_path, {"x": 1, "y": 2}, {"a": NPX})
    assert mcpsync.apply(cj, repo, mcpsync.Policy(), backups, Lines()) is True
    assert list(live_data(cj)) == ["x", "y", "mcpServers"]


def test_real_apply_mode_backup_no_tmp(tmp_path):
    cj, repo, backups = setup(tmp_path, {"mcpServers": {}}, {"a": NPX})
    if os.name != "nt":
        cj.chmod(0o644)
    assert mcpsync.apply(cj, repo, mcpsync.Policy(), backups, Lines()) is True
    if os.name != "nt":
        assert stat.S_IMODE(cj.stat().st_mode) == 0o600
    assert list(backups.glob("*/.claude.json"))
    assert not list(cj.parent.glob(".claude.json.*.tmp"))
    assert not list((repo / "mcp").glob("*.tmp"))


def _touching_reader(monkeypatch, cj: Path, times: int | None):
    real = safejson.read_json_with_meta
    count = {"n": 0}

    def reader(path, label=None):
        out = real(path, label)
        if Path(path) == cj and (times is None or count["n"] < times):
            count["n"] += 1
            st = cj.stat()
            os.utime(cj, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
        return out

    monkeypatch.setattr(safejson, "read_json_with_meta", reader)
    return count


def test_apply_retries_after_a_rewrite(tmp_path, monkeypatch):
    cj, repo, backups = setup(tmp_path, {"mcpServers": {}}, {"a": NPX})
    count = _touching_reader(monkeypatch, cj, 1)
    say = Lines()
    assert mcpsync.apply(cj, repo, mcpsync.Policy(), backups, say) is True
    assert count["n"] == 1 and say == []
    assert live_data(cj)["mcpServers"] == {"a": NPX}


def test_apply_gives_up_after_three_rewrites(tmp_path, monkeypatch):
    cj, repo, backups = setup(tmp_path, {"mcpServers": {}}, {"a": NPX})
    before = cj.read_bytes()
    count = _touching_reader(monkeypatch, cj, None)
    say = Lines()
    assert mcpsync.apply(cj, repo, mcpsync.Policy(), backups, say) is False
    assert count["n"] == 3
    assert say == ["mcp: apply skipped: ~/.claude.json kept changing, try again"]
    assert cj.read_bytes() == before


def test_invalid_live_json_is_left_alone(tmp_path):
    cj, repo, backups = setup(tmp_path, '{"mcpServers": {', {"a": NPX})
    before = cj.read_bytes()
    say = Lines()
    assert mcpsync.apply(cj, repo, mcpsync.Policy(), backups, say) is False
    assert cj.read_bytes() == before and len(say) == 1 and "not valid JSON" in say[0]
    say = Lines()
    assert mcpsync.capture(cj, repo, mcpsync.Policy(), say) is False
    assert len(say) == 1 and "not valid JSON" in say[0]


def test_apply_never_creates_live_file(tmp_path):
    cj, repo, backups = setup(tmp_path, None, {"a": NPX})
    say = Lines()
    assert mcpsync.apply(cj, repo, mcpsync.Policy(), backups, say) is False
    assert not cj.exists() and say == ["mcp: apply skipped: ~/.claude.json not found, not created"]


def test_apply_without_repo_file_is_quiet(tmp_path):
    cj, repo, backups = setup(tmp_path, {"mcpServers": {}})
    say = Lines()
    assert mcpsync.apply(cj, repo, mcpsync.Policy(), backups, say) is False and say == []


@pytest.mark.parametrize("live", ["[1, 2]", '{"mcpServers": [1]}'])
def test_apply_refuses_odd_live_shapes(tmp_path, live):
    cj, repo, backups = setup(tmp_path, live, {"a": NPX})
    before = cj.read_bytes()
    say = Lines()
    assert mcpsync.apply(cj, repo, mcpsync.Policy(), backups, say) is False
    assert cj.read_bytes() == before and len(say) == 1


@pytest.mark.parametrize("text", ["{oops", "[]", '{"other": {}}'])
def test_apply_malformed_repo_file(tmp_path, text):
    cj, repo, backups = setup(tmp_path, {"mcpServers": {}})
    (repo / "mcp").mkdir()
    (repo / mcpsync.REPO_FILE).write_text(text, encoding="utf-8")
    say = Lines()
    assert mcpsync.apply(cj, repo, mcpsync.Policy(), backups, say) is False
    assert len(say) == 1 and say[0].startswith("mcp: apply skipped: mcp/servers.json")


# ── 6. Windows and path commands ─────────────────────────────────────────


@pytest.mark.parametrize("command,ok", [
    ("npx", True), ("uvx", True), ("node", True), ("docker", True),
    ("C:\\x\\npx.cmd", False), ("/usr/bin/node", False), ("npx.cmd", False),
    ("tool.EXE", False), ("run.bat", False), ("x.ps1", False), ("y.com", False),
    ("./x", False), ("~/x", False), ("c:npx", False), ("", False), (None, False), (3, False),
])
def test_bare_command(command, ok):
    assert mcpsync.bare_command(command) is ok


def test_path_command_is_skipped_never_rewritten(tmp_path):
    srv = {"command": "C:\\tools\\npx.cmd", "args": ["-y", "pkg"]}
    cj, repo, backups = setup(tmp_path, {"mcpServers": {"win": srv}}, {"win": NPX})
    say = Lines()
    assert mcpsync.capture(cj, repo, mcpsync.Policy(), say) is False
    assert say == ["mcp: skipped win: command is not a bare program name, kept local"]
    assert repo_data(repo) == {"win": NPX}
    write_repo(repo, {"win": srv})
    before = cj.read_bytes()
    say = Lines()
    assert mcpsync.apply(cj, repo, mcpsync.Policy(), backups, say) is False
    assert cj.read_bytes() == before
    assert say == ["mcp: skipped win: command is not a bare program name, kept local"]
    assert "npx.cmd" not in say[0]


# ── 7. capture scope, idempotence, repo file ─────────────────────────────


def test_capture_reads_only_top_level(tmp_path):
    live = {"projects": {"/home/user/project/alpha": {"mcpServers": {"projonly": NPX}}},
            "mcpServers": {"zeta": NPX, "alpha": WEB}}
    cj, repo, _ = setup(tmp_path, live)
    assert mcpsync.capture(cj, repo, mcpsync.Policy(), Lines()) is True
    raw = (repo / mcpsync.REPO_FILE).read_bytes()
    assert b"projonly" not in raw
    assert raw == mcpsync.render_repo({"alpha": WEB, "zeta": NPX})
    assert raw.endswith(b"}\n") and b"\r\n" not in raw
    assert list(repo_data(repo)) == ["alpha", "zeta"]
    assert mcpsync.capture(cj, repo, mcpsync.Policy(), Lines()) is False
    assert (repo / mcpsync.REPO_FILE).read_bytes() == raw


@pytest.mark.parametrize("live", [None, {"x": 1}, {"mcpServers": [1]}, "[]"])
def test_capture_nothing_to_do(tmp_path, live):
    cj, repo, _ = setup(tmp_path, live)
    say = Lines()
    assert mcpsync.capture(cj, repo, mcpsync.Policy(), say) is False
    assert say == [] and not (repo / "mcp").exists()


def test_capture_no_eligible_does_not_create_repo_file(tmp_path):
    cj, repo, _ = setup(tmp_path, {"mcpServers": {"s": {"command": "npx", "env": {"API_TOKEN": GH}}}})
    assert mcpsync.capture(cj, repo, mcpsync.Policy(), Lines()) is False
    assert not (repo / mcpsync.REPO_FILE).exists()


def test_capture_malformed_repo_file_left_alone(tmp_path):
    cj, repo, _ = setup(tmp_path, {"mcpServers": {"a": NPX}})
    (repo / "mcp").mkdir()
    (repo / mcpsync.REPO_FILE).write_text("{broken", encoding="utf-8")
    say = Lines()
    assert mcpsync.capture(cj, repo, mcpsync.Policy(), say) is False
    assert (repo / mcpsync.REPO_FILE).read_text(encoding="utf-8") == "{broken"
    assert say == ["mcp: capture skipped: mcp/servers.json not valid JSON, left unchanged"]


def test_default_say_prints_to_stderr(capsys):
    mcpsync.default_say("mcp: hello")
    assert capsys.readouterr().err == "claude-sync: mcp: hello\n"


# ── a refused live entry is never replaced by a repo entry ───────────────


@pytest.mark.parametrize("name,srv,why", [
    ("plex", {"command": "npx", "env": {"PLEX_TOKEN": PW}}, "literal credential in env PLEX_TOKEN"),
    ("win", {"command": "C:\\tools\\npx.cmd"}, "command is not a bare program name, kept local"),
])
def test_refused_live_entry_wins_a_name_clash(tmp_path, monkeypatch, name, srv, why):
    clean = {"command": "npx", "env": {"PLEX_TOKEN": "${PLEX_TOKEN}"}}
    cj, repo, backups = setup(tmp_path, {"mcpServers": {name: srv}}, {name: clean})
    before = cj.read_bytes()

    def boom(*a, **k):
        raise AssertionError("write_json_atomic called")

    monkeypatch.setattr(safejson, "write_json_atomic", boom)
    say = Lines()
    assert mcpsync.apply(cj, repo, mcpsync.Policy(), backups, say) is False
    assert cj.read_bytes() == before
    assert say == [f"mcp: kept local {name}: live entry {why}"]
    no_secret(say.text())


def test_clash_held_back_but_others_apply(tmp_path):
    plex = {"command": "npx", "env": {"PLEX_TOKEN": PW}}
    cj, repo, backups = setup(tmp_path, {"mcpServers": {"plex": plex}}, {"plex": NPX, "web": WEB})
    say = Lines()
    assert mcpsync.apply(cj, repo, mcpsync.Policy(), backups, say) is True
    assert live_data(cj)["mcpServers"] == {"plex": plex, "web": WEB}
    assert say == ["mcp: kept local plex: live entry literal credential in env PLEX_TOKEN"]
