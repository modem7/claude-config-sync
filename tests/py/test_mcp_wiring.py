"""MCP sync wired into the sync flow (gitflow): MCP_SYNC=on in the conf
syncs the user-scope servers in ~/.claude.json through mcp/servers.json.

Placeholder hosts and names only: this file is published. Every fake
credential is built at runtime, so no token-shaped literal sits here.
The live ~/.claude.json is the world's (claude home's parent), never the
real one.
"""

from __future__ import annotations

import builtins
import io
import json
import os
from contextlib import ExitStack
from pathlib import Path

import pytest
from helpers import effective_show, git

from claude_sync import gitflow, mcpsync, safejson

# Runtime-built fake: never a literal token shape in the source.
PW = "pw" + "x" * 8 + "9" * 4

CONF = "PRIMARY_PROJECT_PATH=/home/user/project\n"
REGISTRY = {
    "off_until_asked": [{"kind": "mcp", "name": "quiet-srv", "note": "on when asked"}],
    "enabled_per_project": [
        {"kind": "mcp", "name": "far-srv", "machines": ["host-z"], "note": "other box only"}
    ],
    "uninstalled": [],
}
SHARED = {"command": "npx", "args": ["-y", "shared-pkg"]}
PLEX_CLEAN = {"command": "npx", "args": ["plex-mcp"], "env": {"PLEX_TOKEN": "${PLEX_TOKEN}"}}
PLEX_LIVE = {"command": "npx", "args": ["plex-mcp"], "env": {"PLEX_TOKEN": PW}}
GOOD = {"command": "uvx", "args": ["good-pkg"]}
QUIET = {"command": "npx", "args": ["quiet-pkg"]}
FAR = {"command": "npx", "args": ["far-pkg"]}
PROJECT_SCOPE = {"/home/user/project": {"mcpServers": {"proj": {"command": "npx"}}}}


def claude_json(m) -> Path:
    return m.claude.parent / ".claude.json"


def write_live(m, servers: dict | None) -> bytes:
    data: dict = {"numStartups": 3, "projects": PROJECT_SCOPE}
    if servers is not None:
        data["mcpServers"] = servers
    raw = (json.dumps(data, indent=2) + "\n").encode()
    claude_json(m).write_bytes(raw)
    return raw


def live_servers(m) -> dict:
    return json.loads(claude_json(m).read_text(encoding="utf-8"))["mcpServers"]


def repo_servers(m) -> dict:
    return json.loads(effective_show(m.repo, m.host, mcpsync.REPO_FILE))["mcpServers"]


def has_repo_file(m) -> bool:
    ref = f"sync/{m.host}"
    if git("-C", m.repo, "show-ref", "-q", "--verify", f"refs/heads/{ref}", check=False).returncode:
        ref = "HEAD"
    return git("-C", m.repo, "cat-file", "-e", f"{ref}:{mcpsync.REPO_FILE}", check=False).returncode == 0


def seed(world, conf_extra: str = "MCP_SYNC=on\n", servers: dict | None = None,
         registry: object = REGISTRY) -> None:
    files: dict[str, bytes | None] = {"machines/host-a.conf": (CONF + conf_extra).encode()}
    if registry is not None:
        text = registry if isinstance(registry, str) else json.dumps(registry, indent=2)
        files["integrations.json"] = text.encode()
    if servers is not None:
        files[mcpsync.REPO_FILE] = mcpsync.render_repo(servers)
    world.commit_on_origin(files, "mcp setup")


@pytest.fixture
def forbid_live(monkeypatch):
    """Records (and refuses) every open of a guarded ~/.claude.json path."""
    guarded: list[str] = []
    hits: list[str] = []

    def check(path) -> None:
        if isinstance(path, int) or not guarded:
            return
        try:
            p = os.path.abspath(os.fspath(path))
        except TypeError:
            return
        if p in guarded:
            hits.append(p)
            raise PermissionError(f"test: {p} must not be opened")

    def wrap(fn):
        def inner(file, *args, **kwargs):
            check(file)
            return fn(file, *args, **kwargs)
        return inner

    def wrap_method(fn):
        def inner(self, *args, **kwargs):
            check(self)
            return fn(self, *args, **kwargs)
        return inner

    real_read = safejson.read_json_with_meta

    def read_json(path, *args, **kwargs):
        check(path)
        return real_read(path, *args, **kwargs)

    monkeypatch.setattr(safejson, "read_json_with_meta", read_json)
    monkeypatch.setattr(builtins, "open", wrap(builtins.open))
    monkeypatch.setattr(io, "open", wrap(io.open))
    monkeypatch.setattr(os, "open", wrap(os.open))
    for name in ("read_bytes", "read_text", "open"):
        monkeypatch.setattr(Path, name, wrap_method(getattr(Path, name)))

    def arm(path: Path) -> list[str]:
        """Guard `path`; returns the hits. Reading it back disarms first."""
        guarded.append(os.path.abspath(path))
        return hits

    arm.off = guarded.clear
    return arm


# ── off: ~/.claude.json is never opened ──────────────────────────────────


@pytest.mark.parametrize(
    "conf_extra", ["", "MCP_SYNC=off\n", "TIER=basic\nMCP_SYNC=on\n"], ids=["no-line", "off", "basic-on"]
)
def test_off_or_basic_never_opens_claude_json(world, forbid_live, conf_extra):
    seed(world, conf_extra, servers={"shared": SHARED})
    a = world.machine("a", pull=False)
    before = write_live(a, {"good": GOOD})
    hits = forbid_live(claude_json(a))
    world.ok(a, "pull")
    (a.claude / "CLAUDE.md").write_bytes(b"# edited\n")
    world.ok(a, "sync")
    world.ok(a, "sync")
    forbid_live.off()
    assert hits == []
    assert claude_json(a).read_bytes() == before
    assert repo_servers(a) == {"shared": SHARED}  # left as origin had it
    assert not gitflow.mcp_backups_root(a.ctx()).exists()
    assert not (a.claude / "mcp").exists()


def test_bootstrap_with_the_flag_off_never_opens_claude_json(world, forbid_live):
    seed(world, "")
    a = world.machine("a", pull=False)
    before = write_live(a, {"good": GOOD})
    hits = forbid_live(claude_json(a))
    world.ok(a, "bootstrap")
    forbid_live.off()
    assert hits == [] and claude_json(a).read_bytes() == before
    assert not has_repo_file(a)


# ── on: capture, apply, no-op ────────────────────────────────────────────


def test_on_syncs_only_eligible_servers_and_keeps_local_ones(world):
    seed(world, servers={"shared": SHARED, "plexish": PLEX_CLEAN})
    a = world.machine("a", pull=False)
    write_live(a, {"plexish": PLEX_LIVE, "good": GOOD, "quiet-srv": QUIET, "far-srv": FAR})

    out = world.ok(a, "pull")
    assert "mcp: kept local plexish: live entry literal credential in env PLEX_TOKEN" in out
    live = live_servers(a)
    assert live == {"plexish": PLEX_LIVE, "good": GOOD, "quiet-srv": QUIET, "far-srv": FAR,
                    "shared": SHARED}
    data = json.loads(claude_json(a).read_text(encoding="utf-8"))
    assert data["projects"] == PROJECT_SCOPE and data["numStartups"] == 3

    out = world.ok(a, "sync")
    assert PW not in out
    assert "mcp: skipped quiet-srv: registry: off_until_asked" in out
    assert f"mcp: skipped far-srv: {mcpsync.PER_MACHINE}" in out
    # Only the syncable ones; the repo keeps its clean plexish.
    assert repo_servers(a) == {"shared": SHARED, "plexish": PLEX_CLEAN, "good": GOOD}
    assert PW not in effective_show(a.repo, a.host, mcpsync.REPO_FILE)
    assert live_servers(a)["plexish"] == PLEX_LIVE  # survives the apply
    assert not (a.claude / "mcp").exists()

    # Nothing changed: the next sync leaves the file byte for byte.
    raw, mtime = claude_json(a).read_bytes(), claude_json(a).stat().st_mtime_ns
    world.ok(a, "sync")
    assert claude_json(a).read_bytes() == raw
    assert claude_json(a).stat().st_mtime_ns == mtime


def test_on_with_no_repo_file_creates_it_on_capture(world):
    seed(world)
    a = world.machine("a")
    write_live(a, {"good": GOOD, "plexish": PLEX_LIVE})
    world.ok(a, "sync")
    assert repo_servers(a) == {"good": GOOD}
    assert live_servers(a) == {"good": GOOD, "plexish": PLEX_LIVE}


def test_apply_backs_up_into_its_own_folder(world):
    seed(world, servers={"shared": SHARED})
    a = world.machine("a", pull=False)
    write_live(a, {"good": GOOD})
    world.ok(a, "pull")
    root = gitflow.mcp_backups_root(a.ctx())
    assert root == a.claude / ".claude-sync-mcp-backups"
    assert root.is_dir() and any(root.iterdir())
    assert not (a.claude / ".claude-sync-backups").exists() or not any(
        p.name == ".claude.json" for p in (a.claude / ".claude-sync-backups").rglob("*")
    )


# ── a local edit is captured, never overwritten ──────────────────────────


def _converged(world):
    seed(world, servers={"shared": SHARED})
    a = world.machine("a", pull=False)
    write_live(a, {"good": GOOD})
    world.sync_as(a, "sync")
    assert repo_servers(a) == {"shared": SHARED, "good": GOOD}
    return a


def test_local_edit_to_a_repo_server_is_captured(world):
    a = _converged(world)
    edited = {"command": "npx", "args": ["-y", "shared-pkg@2"]}
    live = live_servers(a)
    live["shared"] = edited
    write_live(a, live)
    world.ok(a, "sync")
    assert repo_servers(a)["shared"] == edited
    assert live_servers(a)["shared"] == edited


def test_live_matches_commit_sees_an_mcp_edit(world, monkeypatch):
    a = _converged(world)
    ctx = a.ctx()
    sha = gitflow.applied_commit_stamp(ctx).read_text(encoding="utf-8").strip()
    memory = gitflow.memory_dir_of(ctx)
    assert gitflow.live_matches_commit(ctx, sha, memory) is True  # control
    live = live_servers(a)
    live["shared"] = {"command": "npx", "args": ["edited"]}
    write_live(a, live)
    assert gitflow.live_matches_commit(ctx, sha, memory) is False
    monkeypatch.setattr(gitflow, "mcp_sync_enabled", lambda _ctx: False)
    assert gitflow.live_matches_commit(ctx, sha, memory) is True


def test_moved_clone_with_an_mcp_edit_refuses_instead_of_overwriting(world):
    a = _converged(world)
    world.commit_on_origin({"dot-claude/CLAUDE.md": b"# newer shared\n"}, "newer")
    ctx = a.ctx()
    with ExitStack() as stack:  # integrate without apply: the clone moved
        gitflow.begin_sync(ctx, stack)
        gitflow.do_integrate(ctx)
    edited = {"command": "npx", "args": ["edited"]}
    live = live_servers(a)
    live["shared"] = edited
    write_live(a, live)
    rc, out = world.run(a, "sync")
    assert rc == 1 and "refusing to capture" in out
    assert live_servers(a)["shared"] == edited


def test_moved_clone_with_a_hand_formatted_repo_file_still_skips_cleanly(world):
    seed(world)
    hand = json.dumps({"mcpServers": {"shared": SHARED}}, indent=4).encode()
    world.commit_on_origin({mcpsync.REPO_FILE: hand}, "hand edit")
    a = world.machine("a", pull=False)
    write_live(a, {})
    world.ok(a, "pull")
    assert live_servers(a) == {"shared": SHARED}
    world.commit_on_origin({"dot-claude/CLAUDE.md": b"# newer shared\n"}, "newer")
    ctx = a.ctx()
    with ExitStack() as stack:
        gitflow.begin_sync(ctx, stack)
        gitflow.do_integrate(ctx)
    rc, out = world.run(a, "sync")
    assert rc == 0, out
    assert "skipping capture" in out


# ── MCP trouble never fails a sync ───────────────────────────────────────


def test_refused_servers_never_fail_a_sync(world):
    seed(world)
    a = world.machine("a")
    write_live(a, {
        "plexish": PLEX_LIVE,
        "pathcmd": {"command": "/opt/tool/bin/srv"},
        "wincmd": {"command": "srv.cmd"},
        "odd": "not an object",
    })
    rc, out = world.run(a, "sync")
    assert rc == 0, out
    assert PW not in out
    assert "mcp: skipped pathcmd" in out and "mcp: skipped odd" in out
    assert not has_repo_file(a)


@pytest.mark.parametrize("text", ["{not json", "[1, 2]"])
def test_bad_live_file_does_not_stop_the_apply(world, text):
    seed(world, servers={"shared": SHARED})
    a = world.machine("a", pull=False)
    claude_json(a).write_text(text, encoding="utf-8")
    rc, out = world.run(a, "pull")
    assert rc == 0, out
    assert "mcp: apply skipped" in out
    assert claude_json(a).read_text(encoding="utf-8") == text
    stamp = gitflow.applied_commit_stamp(a.ctx()).read_text(encoding="utf-8").strip()
    assert stamp == gitflow.effective_commit(a.ctx())
    rc, out = world.run(a, "sync")
    assert rc == 0, out


def test_an_internal_mcp_error_does_not_stop_sync(world, monkeypatch):
    seed(world, servers={"shared": SHARED})
    a = world.machine("a", pull=False)
    before = write_live(a, {"good": GOOD})

    def boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(mcpsync, "_apply", boom)
    monkeypatch.setattr(mcpsync, "_capture", boom)
    rc, out = world.run(a, "pull")
    assert rc == 0, out
    assert "mcp: apply skipped: internal error" in out
    assert gitflow.applied_commit_stamp(a.ctx()).is_file()
    rc, out = world.run(a, "sync")
    assert rc == 0, out
    assert "mcp: capture skipped: internal error" in out
    assert claude_json(a).read_bytes() == before


def test_unreadable_registry_skips_mcp_sync(world, forbid_live):
    seed(world, servers={"shared": SHARED}, registry="{oops")
    a = world.machine("a", pull=False)
    before = write_live(a, {"good": GOOD})
    hits = forbid_live(claude_json(a))
    rc, out = world.run(a, "pull")
    assert rc == 0, out
    assert "mcp: integrations.json unreadable, MCP sync skipped" in out
    rc, out = world.run(a, "sync")
    assert rc == 0, out
    forbid_live.off()
    assert hits == [] and claude_json(a).read_bytes() == before


def test_bad_mcp_sync_value_fails_before_anything_changes(world):
    seed(world, "MCP_SYNC=yes\n", servers={"shared": SHARED})
    a = world.machine("a", pull=False)
    before = write_live(a, {"good": GOOD})
    rc, out = world.run(a, "pull")
    assert rc == 1
    assert "Not a valid MCP_SYNC value in machines/host-a.conf" in out
    assert claude_json(a).read_bytes() == before
    assert not gitflow.applied_commit_stamp(a.ctx()).exists()
