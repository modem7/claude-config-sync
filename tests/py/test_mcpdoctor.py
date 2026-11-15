"""claude_sync.mcpdoctor: the read-only MCP server check in `doctor`.

Most tests build a bare SyncCtx over a fake home (no git), so they are
quick and run on Windows. The doctor tests use `world`. Every fake
credential is built at runtime, so no token-shaped literal sits in this
file (it is published). Placeholder names and paths only.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

from claude_sync import mcpdoctor
from claude_sync.gitflow import SyncCtx

LIST_DISABLED = Path(__file__).resolve().parents[2] / "scripts" / "list_disabled.py"

# Runtime-built fakes: never literal token shapes in the source.
FAKE_TOKEN = "tok" + "en" * 10 + "42" * 6
FAKE_PREFIXED = "gh" + "p_" + "a1" * 15
FAKE_PASSWORD = "pw" + "x" * 8 + "9" * 4
SECRETS = (FAKE_TOKEN, FAKE_PREFIXED, FAKE_PASSWORD)


def make_ctx(tmp_path: Path, registry: dict | None = None) -> SyncCtx:
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    if registry is not None:
        (repo / "integrations.json").write_text(json.dumps(registry), encoding="utf-8")
    return SyncCtx(repo=repo, claude_home=home / ".claude", agents_home=home / ".agents", hostname="host-a")


def write_config(ctx: SyncCtx, data: object, mode: int = 0o600) -> Path:
    path = ctx.claude_home.parent / ".claude.json"
    text = data if isinstance(data, str) else json.dumps(data)
    path.write_text(text, encoding="utf-8")
    if os.name != "nt":
        path.chmod(mode)
    return path


def report(ctx: SyncCtx) -> str:
    return "\n".join(mcpdoctor.mcp_report(ctx))


def assert_no_secret(text: str) -> None:
    for s in SECRETS:
        assert s not in text


def warnings(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("WARNING: MCP server")]


# ── skipped ──────────────────────────────────────────────────────────────


def test_no_file_is_one_skipped_line(tmp_path):
    assert mcpdoctor.mcp_report(make_ctx(tmp_path)) == ["MCP check skipped: ~/.claude.json not found."]


def test_invalid_json_is_skipped_without_contents(tmp_path):
    ctx = make_ctx(tmp_path)
    write_config(ctx, '{"mcpServers": {"x": {"env": {"A": "' + FAKE_TOKEN + '"}}')
    lines = mcpdoctor.mcp_report(ctx)
    assert lines[0].startswith("MCP check skipped: ~/.claude.json is not valid JSON (line 1 column")
    assert len(lines) == 1
    assert_no_secret("\n".join(lines))


def test_not_an_object_is_skipped(tmp_path):
    ctx = make_ctx(tmp_path)
    write_config(ctx, "[1, 2]")
    assert mcpdoctor.mcp_report(ctx) == ["MCP check skipped: ~/.claude.json is not a JSON object."]


def test_an_internal_error_is_one_line_not_a_traceback(tmp_path, monkeypatch):
    ctx = make_ctx(tmp_path)
    write_config(ctx, {"mcpServers": {"a": {}}})

    def boom(_server):
        raise RuntimeError(FAKE_TOKEN)

    monkeypatch.setattr(mcpdoctor, "scan_server", boom)
    assert mcpdoctor.mcp_report(ctx) == ["MCP check skipped: internal error."]


# ── listing ──────────────────────────────────────────────────────────────


def test_user_and_local_scope_with_transport(tmp_path):
    ctx = make_ctx(tmp_path)
    write_config(ctx, {
        "mcpServers": {"docs": {"type": "http", "url": "https://example.invalid/mcp"}},
        "projects": {
            "/home/user/project/alpha": {"mcpServers": {"files": {"command": "npx", "args": ["-y", "@scope/pkg"]}}},
            "/home/user/project/beta": {"allowedTools": []},
        },
        "oauthAccount": {"emailAddress": "someone@example.invalid"},
    })
    lines = mcpdoctor.mcp_report(ctx)
    text = "\n".join(lines)
    assert "  docs (user): http, unclassified" in lines
    assert "  files (alpha): stdio, unclassified" in lines
    assert lines[-1].startswith("MCP servers: 1 user, 1 local; 0 with literal credentials")
    assert "not synced yet" in lines[-1]
    # Never the command, args, url or other keys.
    for hidden in ("npx", "@scope/pkg", "example.invalid", "someone"):
        assert hidden not in text
    assert not warnings(text)


def test_no_servers_says_none(tmp_path):
    ctx = make_ctx(tmp_path)
    write_config(ctx, {"projects": {}})
    lines = mcpdoctor.mcp_report(ctx)
    assert "  none" in lines
    assert lines[-1].startswith("MCP servers: 0 user, 0 local; 0 with")


# ── secret scan ──────────────────────────────────────────────────────────


def scan(server: dict) -> list[str]:
    return mcpdoctor.scan_server(server)


@pytest.mark.parametrize("value", ["", "${PLACEHOLDER_TOKEN}", "${PLACEHOLDER_TOKEN:-}", " ${A}${B} "])
def test_references_are_allowed(value):
    assert scan({"env": {"API_TOKEN": value}, "headers": {"X-Api-Key": value}}) == []


def test_bearer_reference_header_is_allowed():
    assert scan({"headers": {"Authorization": "Bearer ${PLACEHOLDER_TOKEN}"}}) == []


def test_literal_env_token_is_flagged():
    assert scan({"env": {"SERVICE_TOKEN": "short" + "val"}}) == ["env SERVICE_TOKEN"]


def test_literal_by_shape_under_any_key():
    assert scan({"env": {"VALUE": FAKE_PREFIXED, "OTHER": FAKE_TOKEN}}) == ["env VALUE", "env OTHER"]


def test_literal_header_is_flagged():
    assert scan({"headers": {"Authorization": "Bearer " + FAKE_PASSWORD}}) == ["header Authorization"]
    assert scan({"headers": {"Cookie": "session=" + FAKE_PASSWORD}}) == ["header Cookie"]


@pytest.mark.parametrize(("args", "want"), [
    (["server", "--token", FAKE_PASSWORD], ["arg #3"]),
    (["--api-key=" + FAKE_PASSWORD], ["arg #1"]),
    (["--password", "${PLACEHOLDER_PW}"], []),
    (["run", "-e", "SERVICE_TOKEN=" + FAKE_PASSWORD, "image"], ["arg #3"]),
    (["run", "-e", "SERVICE_TOKEN", "image"], []),
    (["serve", FAKE_TOKEN], ["arg #2"]),
    (["mcp-remote", "https://example.invalid/mcp", "--header", "X-Api-Key: " + FAKE_PASSWORD], ["arg #4"]),
    (["--header=X-Api-Key: " + FAKE_PASSWORD], ["arg #1"]),
    (["--header", "Authorization: Bearer ${PLACEHOLDER_TOKEN}"], []),
    (["--header", "Accept: application/json", "https://example.invalid:8443/mcp"], []),
    (["--auth", "none", "--api-key", "null"], []),
])
def test_args(args, want):
    assert scan({"args": args}) == want


def test_url_userinfo_and_query_are_flagged():
    assert scan({"url": "https://user:" + FAKE_PASSWORD + "@example.invalid/mcp"}) == ["url"]
    assert scan({"url": "https://example.invalid/mcp?api_key=" + FAKE_PASSWORD}) == ["url"]
    assert scan({"url": "https://example.invalid/mcp?api_key=${PLACEHOLDER_KEY}"}) == []


def test_clean_servers_are_not_flagged():
    clean = {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "/home/user/project/some-long-folder-2026",
                 "--port", "8080", "mcp-server-placeholder-v2", "https://example.invalid/path?page=2"],
        "env": {
            "NODE_TLS_REJECT_UNAUTHORIZED": "0",
            "AUTHOR": "someone",
            "TOKENIZER_PATH": "/opt/models/tokenizer-v2-2026-base-model",
            "API_KEY_FILE": "/run/secrets/key",
            "AUTH_ENABLED": "true",
            "LOG_LEVEL": "debug",
            "REQUEST_ID": "123e4567-e89b-12d3-a456-426614174000",
        },
        "headers": {"Accept": "application/json", "X-Client": "placeholder"},
        "url": "https://example.invalid/mcp/v1",
    }
    assert scan(clean) == []


def test_values_never_appear_in_the_report(tmp_path):
    ctx = make_ctx(tmp_path)
    write_config(ctx, {
        "mcpServers": {"svc": {"env": {"SERVICE_TOKEN": FAKE_TOKEN}, "args": ["--token", FAKE_PREFIXED]}},
        "projects": {"/home/user/project/alpha": {"mcpServers": {
            "web": {"type": "http", "url": "https://u:" + FAKE_PASSWORD + "@example.invalid",
                    "headers": {"Authorization": "Bearer " + FAKE_TOKEN}}}}},
    })
    text = report(ctx)
    assert_no_secret(text)
    assert "example.invalid" not in text
    assert warnings(text) == [
        "WARNING: MCP server svc (user): literal credential in env SERVICE_TOKEN",
        "WARNING: MCP server svc (user): literal credential in arg #2",
        "WARNING: MCP server web (alpha): literal credential in header Authorization",
        "WARNING: MCP server web (alpha): literal credential in url",
    ]
    assert "1 user, 1 local; 2 with literal credentials (kept local, they will never sync)" in text


# ── registry buckets ─────────────────────────────────────────────────────


REGISTRY = {
    "off_until_asked": [{"kind": "mcp", "name": "media", "projects": ["/elsewhere/alpha"]}],
    "enabled_per_project": [{"kind": "mcp", "name": "notes", "projects": ["/home/user/project/beta"]}],
    "uninstalled": [{"kind": "mcp", "name": "old"}, {"kind": "plugin", "name": "files"}],
}


def test_bucket_labels(tmp_path):
    ctx = make_ctx(tmp_path, REGISTRY)
    write_config(ctx, {
        "mcpServers": {"old": {}, "media": {}},
        "projects": {
            "/home/user/project/alpha": {"mcpServers": {"media": {}, "files": {}}},
            "/home/user/project/beta": {"mcpServers": {"notes": {}}},
            "/home/user/project/gamma": {"mcpServers": {"notes": {}}},
        },
    })
    lines = mcpdoctor.mcp_report(ctx)
    assert "  old (user): stdio, uninstalled" in lines
    assert "  media (user): stdio, off_until_asked (listed for other projects)" in lines
    assert "  media (alpha): stdio, off_until_asked" in lines  # basename fallback
    assert "  files (alpha): stdio, unclassified" in lines  # a plugin entry is not an mcp one
    assert "  notes (beta): stdio, enabled_per_project" in lines
    assert "  notes (gamma): stdio, enabled_per_project (listed for other projects)" in lines


def test_no_registry_means_unclassified(tmp_path):
    ctx = make_ctx(tmp_path)
    write_config(ctx, {"mcpServers": {"a": {}}})
    lines = mcpdoctor.mcp_report(ctx)
    assert "  a (user): stdio, unclassified" in lines
    assert any("no usable integrations.json" in line for line in lines)


def _list_disabled():
    spec = importlib.util.spec_from_file_location("list_disabled_for_test", LIST_DISABLED)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses look the module up by name
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop(spec.name, None)
    return mod


@pytest.mark.parametrize("wanted", [
    "/home/user/project/alpha",  # exact path wins over the basename
    "/other/place/ALPHA",  # basename fallback, case-folded
    "C:\\Users\\user\\beta",  # Windows path, basename fallback
    "/nowhere/delta",
])
def test_matcher_agrees_with_list_disabled(tmp_path, wanted):
    live_paths = ["/home/user/project/alpha", "/srv/alpha", "/home/user/project/beta"]
    ld = _list_disabled()
    live = ld.Live(tmp_path)
    live.mcp = {p: {"defined": {}, "disabled": []} for p in live_paths}
    assert mcpdoctor.match(wanted, live_paths) == live.match(wanted)


# ── file mode and read-only ──────────────────────────────────────────────


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_mode_warning(tmp_path):
    ctx = make_ctx(tmp_path)
    write_config(ctx, {"mcpServers": {}}, mode=0o644)
    lines = mcpdoctor.mcp_report(ctx)
    assert "WARNING: ~/.claude.json has mode 0644; 0600 is expected (owner read and write only)." in lines
    write_config(ctx, {"mcpServers": {}}, mode=0o600)
    assert not any("has mode" in line for line in mcpdoctor.mcp_report(ctx))


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_mode_warning_even_when_the_json_is_invalid(tmp_path):
    ctx = make_ctx(tmp_path)
    write_config(ctx, "{", mode=0o640)
    lines = mcpdoctor.mcp_report(ctx)
    assert lines[0].startswith("MCP check skipped:")
    assert any("has mode 0640" in line for line in lines)


def test_the_check_changes_nothing(tmp_path):
    ctx = make_ctx(tmp_path, REGISTRY)
    path = write_config(ctx, {"mcpServers": {"svc": {"env": {"SERVICE_TOKEN": FAKE_TOKEN}}}})
    reg = ctx.repo / "integrations.json"
    before = [(p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode) for p in (path, reg)]
    listing = sorted(p.name for p in tmp_path.rglob("*"))
    mcpdoctor.mcp_report(ctx)
    assert [(p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode) for p in (path, reg)] == before
    assert sorted(p.name for p in tmp_path.rglob("*")) == listing


# ── through doctor ───────────────────────────────────────────────────────


# The Windows smoke leg has never run the git and fake-gh world; the unit
# tests above cover everything platform-specific.
WORLD = pytest.mark.skipif(os.name == "nt", reason="world tests run on Linux CI")


def _doctor(world, m):
    from test_doctor import run_doctor

    return run_doctor(world, m)


@WORLD
def test_doctor_prints_the_check_and_keeps_exit_code_0(world):
    a = world.machine("a")
    world.sync_as(a, "sync")
    path = a.claude.parent / ".claude.json"
    path.write_text(json.dumps({
        "mcpServers": {"svc": {"env": {"SERVICE_TOKEN": FAKE_TOKEN}, "args": ["--token", FAKE_PREFIXED]}},
        "projects": {"/home/user/project/alpha": {"mcpServers": {
            "web": {"type": "http", "url": "https://u:" + FAKE_PASSWORD + "@example.invalid"}}}},
    }), encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o644)
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    rc, out = _doctor(world, a)
    assert rc == 0, out
    assert "No issues found - sync is healthy." in out
    assert "WARNING: MCP server svc (user): literal credential in env SERVICE_TOKEN" in out
    assert "MCP servers: 1 user, 1 local; 2 with literal credentials" in out
    assert "ISSUE:" not in out
    assert_no_secret(out)
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


@WORLD
def test_doctor_without_claude_json_says_skipped(world):
    a = world.machine("a")
    world.sync_as(a, "sync")
    rc, out = _doctor(world, a)
    assert rc == 0, out
    assert "MCP check skipped: ~/.claude.json not found." in out
