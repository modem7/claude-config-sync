"""Tests for PLUGINS=full in machines/<hostname>.conf: sync, pull and remote
then install what settings.json enables (doctor.auto_install_plugins), and
`machine NAME plugins full|off`. The fake claude only logs its argv and never
records an install, so each test runs one command through world.cli (not
cli_as, whose second sync would install again).
Placeholder hostnames only: this file is published.
"""

from __future__ import annotations

import json
from contextlib import ExitStack

from helpers import git, has_ref
from test_doctor import (  # noqa: F401 - fake_claude is a fixture
    EXPECTED_ORDER,
    PLUGIN_SETTINGS,
    _path_without_claude,
    fake_claude,
    needs_posix,
)
from test_doctor import install_plugins as record_installed

from claude_sync import cli, doctor, gitflow

PROJECT = b"PRIMARY_PROJECT_PATH=/home/user/project\n"
FULL = PROJECT + b"PLUGINS=full\n"
REMOTE_ENV = {"CLAUDE_SYNC_REMOTE": "1", "CLAUDE_SYNC_HOSTNAME": "cloud"}


def _machine(world, conf=FULL, settings=PLUGIN_SETTINGS):
    """Machine a, with the plugin settings and the given conf on origin."""
    world.commit_on_origin(
        {"dot-claude/settings.json": (json.dumps(settings) + "\n").encode(), "machines/host-a.conf": conf},
        "plugins enabled",
    )
    return world.machine("a")


def _warnings(out):
    return [ln for ln in out.splitlines() if ln.startswith("claude-sync: warning: PLUGINS=full")]


def _bad_value(out):
    return [ln for ln in out.splitlines() if ln.startswith("claude-sync: warning: PLUGINS: ")]


def _notes(out):
    return [ln for ln in out.splitlines() if ln.startswith("claude-sync: note: PLUGINS=full")]


@needs_posix
def test_sync_installs_what_settings_enables_when_plugins_is_full(world, fake_claude):
    a = _machine(world)
    rc, out = world.cli(a, "sync")
    assert rc == 0, out
    assert fake_claude() == EXPECTED_ORDER
    assert _warnings(out) == []


@needs_posix
def test_pull_installs_when_plugins_is_full(world, fake_claude):
    a = _machine(world)
    rc, out = world.cli(a, "pull")
    assert rc == 0, out
    assert fake_claude() == EXPECTED_ORDER


@needs_posix
def test_off_or_no_line_never_installs(world, fake_claude):
    a = _machine(world, conf=PROJECT + b"PLUGINS=off\n")
    for argv in (["sync"], ["pull"]):
        rc, out = world.cli(a, *argv)
        assert rc == 0, out
        assert "Plugins:" not in out
    world.commit_on_origin({"machines/host-a.conf": PROJECT}, "no line")
    rc, out = world.cli(a, "pull")
    assert rc == 0, out
    assert fake_claude() == []


@needs_posix
def test_nothing_missing_prints_nothing_and_runs_nothing(world, fake_claude):
    a = _machine(world)
    record_installed(
        a, "tool@market-a", "helper@claude-plugins-official",
        marketplaces=("market-a", "claude-plugins-official"),
    )  # fmt: skip
    rc, out = world.cli(a, "sync")
    assert rc == 0, out
    assert fake_claude() == []
    assert "Plugins" not in out and "claude plugin" not in out


def test_only_notes_left_is_one_stderr_note_even_without_the_claude_cli(world, monkeypatch, capsys):
    """A plugin from an undeclared marketplace can't be installed here: one
    stderr line says so and points at doctor, which lists it."""
    settings = {"enabledPlugins": {"x@undeclared": True}}
    a = _machine(world, settings=settings)
    monkeypatch.setenv("PATH", _path_without_claude())
    doctor.auto_install_plugins(a.ctx())
    captured = capsys.readouterr()
    assert captured.out == ""
    (note,) = captured.err.splitlines()
    assert note == (
        "claude-sync: note: PLUGINS=full: settings.json enables 1 plugin(s) this machine lacks"
        " and cannot install here (see 'claude-sync.sh doctor')."
    )


@needs_posix
def test_only_unrunnable_names_left_is_one_stderr_note(world, fake_claude, capsys):
    """The later quiet exit: the marketplace is here, but the name can't be run as shown."""
    settings = {"enabledPlugins": {"-evil@market-a": True}}
    a = _machine(world, settings=settings)
    record_installed(a, marketplaces=("market-a",))
    doctor.auto_install_plugins(a.ctx())
    captured = capsys.readouterr()
    assert captured.out == ""
    assert len(_notes(captured.err)) == 1 and len(captured.err.splitlines()) == 1
    assert fake_claude() == []


@needs_posix
def test_a_failed_install_warns_once_and_keeps_syncs_exit_code(world, fake_claude, monkeypatch):
    a = _machine(world)
    monkeypatch.setenv("FAKE_CLAUDE_FAIL_ON", "install")
    rc, out = world.cli(a, "sync")
    assert rc == 0, out
    assert fake_claude() == EXPECTED_ORDER[:3]  # stopped at the first install
    (warning,) = _warnings(out)
    assert "failed (exit 3)" in warning and "'claude-sync.sh doctor install-plugins'" in warning


def test_a_missing_claude_cli_is_a_warning_on_stderr_only(world, monkeypatch, capsys):
    a = _machine(world)
    monkeypatch.setenv("PATH", _path_without_claude())
    rc = cli.main(["pull"], a.env())
    captured = capsys.readouterr()
    assert rc == 0, captured
    (warning,) = _warnings(captured.err)
    assert "the claude CLI is not on PATH" in warning
    assert "doctor install-plugins" in warning
    assert _warnings(captured.out) == []


def test_any_failure_inside_the_install_is_caught(world, monkeypatch, capsys):
    a = _machine(world)

    def boom(*args, **kwargs):
        raise OSError("disk on fire")

    monkeypatch.setattr(doctor, "install_plugins", boom)
    doctor.auto_install_plugins(a.ctx())
    (warning,) = _warnings(capsys.readouterr().err)
    assert "disk on fire." in warning


BAD_WARNING = (
    "claude-sync: warning: PLUGINS: Not a valid PLUGINS value in machines/host-a.conf: 'yes'."
    " Treated as off; fix it with 'claude-sync.sh machine host-a plugins full' (or off)."
)


def test_a_bad_plugins_value_is_a_warning_and_counts_as_off(world, monkeypatch):
    """Never a lockout: sync and pull still run (and push), installing nothing."""
    a = _machine(world, conf=PROJECT + b"PLUGINS=yes\n")
    calls = []
    monkeypatch.setattr(cli.doctor, "install_plugins", lambda *a, **k: calls.append(1) or 0)
    (a.claude / "CLAUDE.md").write_bytes(b"# local edit\n")
    for cmd in ("sync", "pull"):
        rc, out = world.cli(a, cmd)
        assert rc == 0, out
        assert _bad_value(out) == [BAD_WARNING]
        assert _warnings(out) == []
    assert calls == []
    assert has_ref(world.origin, "refs/heads/sync/host-a")
    assert (a.repo / "dot-claude" / "CLAUDE.md").read_bytes() == b"# local edit\n"


def test_a_bad_value_merged_in_by_the_run_is_only_a_warning(world, monkeypatch):
    a = _machine(world, conf=PROJECT)
    world.commit_on_origin({"machines/host-a.conf": PROJECT + b"PLUGINS=yes\n"}, "bad line")
    rc, out = world.cli(a, "pull")
    assert rc == 0, out
    assert _bad_value(out) == [BAD_WARNING]


@needs_posix
def test_pull_recovers_once_origin_has_the_fix(world, fake_claude):
    a = _machine(world, conf=PROJECT + b"PLUGINS=yes\n")
    assert b"PLUGINS=yes" in (a.repo / "machines" / "host-a.conf").read_bytes()
    world.commit_on_origin({"machines/host-a.conf": FULL}, "fix the line")
    rc, out = world.cli(a, "pull")
    assert rc == 0, out
    assert _bad_value(out) == [] and _warnings(out) == []
    assert (a.repo / "machines" / "host-a.conf").read_bytes() == FULL
    assert fake_claude() == EXPECTED_ORDER


def test_doctor_shows_a_bad_plugins_value(world):
    a = _machine(world, conf=PROJECT + b"PLUGINS=yes\n")
    _, out = world.cli(a, "doctor")
    assert (
        "Warning: Not a valid PLUGINS value in machines/host-a.conf: 'yes'. sync and pull treat it as off."
        " Fix it with 'claude-sync.sh machine host-a plugins full' (or off)."
    ) in out, out
    assert "Traceback" not in out


def test_doctor_shows_plugins_full(world):
    a = _machine(world)
    _, out = world.cli(a, "doctor")
    assert "Plugins: PLUGINS=full: sync, pull and remote install what settings.json enables." in out
    world.commit_on_origin({"machines/host-a.conf": PROJECT}, "off")
    assert world.cli(a, "pull")[0] == 0
    assert "PLUGINS" not in world.cli(a, "doctor")[1]


def test_machine_plugins_full_rewrites_a_bad_value(world):
    a = _machine(world, conf=PROJECT + b"PLUGINS=yes\n")
    rc, out = world.cli_as(a, "machine", "host-a", "plugins", "full")
    assert rc == 0, out
    assert git("-C", world.origin, "show", "HEAD:machines/host-a.conf").stdout == FULL.decode()


@needs_posix
def test_the_auto_install_runs_after_the_lock_is_released(world, fake_claude, monkeypatch):
    a = _machine(world)
    real = doctor.install_plugins
    seen = []

    def check_lock(ctx, *args, **kwargs):
        with ExitStack() as stack:
            probe = gitflow.SyncCtx(ctx.repo, ctx.claude_home, ctx.agents_home, ctx.hostname, lock_wait=0)
            gitflow.acquire_lock(probe, stack)  # a Fail if the run still held it
            seen.append("free")
        return real(ctx, *args, **kwargs)

    monkeypatch.setattr(doctor, "install_plugins", check_lock)
    for cmd in ("sync", "pull"):
        rc, out = world.cli(a, cmd)
        assert rc == 0, out
        assert _warnings(out) == []
    assert seen == ["free", "free"]


@needs_posix
def test_a_refused_push_still_installs_and_keeps_the_failure(world, fake_claude, monkeypatch):
    a = _machine(world)
    (a.claude / "CLAUDE.md").write_bytes(b"# local edit\n")
    monkeypatch.setenv("FAKE_GH_VISIBILITY", "public")
    rc, out = world.cli(a, "sync")
    assert rc == 1, out
    assert "refusing to push sync/host-a: origin (test/repo) is a public repo." in out
    assert fake_claude() == EXPECTED_ORDER
    assert _warnings(out) == []


def test_a_failed_apply_does_not_install(world, monkeypatch):
    a = _machine(world)
    calls = []
    monkeypatch.setattr(doctor, "install_plugins", lambda *a, **k: calls.append(1) or 0)

    def boom(ctx):
        raise cli.Fail("apply broke")

    monkeypatch.setattr(gitflow, "do_apply", boom)
    rc, out = world.cli(a, "sync")
    assert rc == 1 and "apply broke" in out
    assert calls == []


# ── remote ───────────────────────────────────────────────────────────────


def _cloud(world, conf=b"TIER=basic\nPLUGINS=full\n"):
    world.commit_on_origin(
        {"dot-claude/settings.json": (json.dumps(PLUGIN_SETTINGS) + "\n").encode(), "machines/cloud.conf": conf},
        "cloud plugins",
    )
    return world.machine("c", pull=False)


@needs_posix
def test_remote_installs_when_the_cloud_conf_has_plugins_full(world, fake_claude):
    c = _cloud(world)
    rc, out = world.cli(c, "remote", env={**c.env(), **REMOTE_ENV})
    assert rc == 0, out
    assert fake_claude() == EXPECTED_ORDER


@needs_posix
def test_remote_without_plugins_full_installs_nothing(world, fake_claude):
    c = _cloud(world, conf=b"TIER=basic\n")
    rc, out = world.cli(c, "remote", env={**c.env(), **REMOTE_ENV})
    assert rc == 0, out
    assert fake_claude() == []


@needs_posix
def test_remote_plugins_flag_with_plugins_full_installs_once(world, fake_claude):
    c = _cloud(world)
    rc, out = world.cli(c, "remote", "--plugins", env={**c.env(), **REMOTE_ENV})
    assert rc == 0, out
    assert fake_claude() == EXPECTED_ORDER


@needs_posix
def test_the_explicit_flag_still_fails_the_run(world, fake_claude, monkeypatch):
    c = _cloud(world)
    monkeypatch.setenv("FAKE_CLAUDE_FAIL_ON", "install")
    rc, out = world.cli(c, "remote", "--plugins", env={**c.env(), **REMOTE_ENV})
    assert rc == 1, out
    assert _warnings(out) == []
    rc, out = world.cli(c, "remote", env={**c.env(), **REMOTE_ENV})
    assert rc == 0, out
    assert len(_warnings(out)) == 1


# ── machine NAME plugins full|off ────────────────────────────────────────


def _origin_conf(world, name):
    return git("-C", world.origin, "show", f"HEAD:machines/{name}.conf").stdout


@needs_posix
def test_machine_plugins_full_reaches_a_cloud_session(world, fake_claude):
    world.commit_on_origin({"dot-claude/settings.json": (json.dumps(PLUGIN_SETTINGS) + "\n").encode()}, "plugins")
    a = world.machine("a")
    record_installed(
        a, "tool@market-a", "helper@claude-plugins-official",
        marketplaces=("market-a", "claude-plugins-official"),
    )  # fmt: skip
    rc, out = world.cli_as(a, "machine", "cloud", "plugins", "full")
    assert rc == 0, out
    assert "cloud now installs the plugins its settings.json enables" in out
    conf = _origin_conf(world, "cloud")
    assert "TIER=basic" in conf and "PLUGINS=full" in conf
    assert "  plugins: full" in world.cli(a, "machine", "cloud", "show")[1]
    assert fake_claude() == []  # host-a has everything, and its own conf is off
    c = world.machine("c", pull=False)
    rc, out = world.cli(c, "remote", env={**c.env(), **REMOTE_ENV})
    assert rc == 0, out
    assert fake_claude() == EXPECTED_ORDER


def test_machine_plugins_off_removes_the_line_and_repeats_change_nothing(world):
    world.commit_on_origin({"machines/cloud.conf": b"TIER=basic\nPLUGINS=full\n"}, "cloud full")
    a = world.machine("a")
    rc, out = world.cli(a, "machine", "cloud", "plugins", "full")
    assert rc == 0 and "already has plugins full. Nothing was changed." in out
    rc, out = world.cli_as(a, "machine", "cloud", "plugins", "off")
    assert rc == 0, out
    assert "cloud no longer installs plugins by itself" in out
    assert _origin_conf(world, "cloud") == "TIER=basic\n"
    assert "  plugins: off" in world.cli(a, "machine", "cloud", "show")[1]
    before = git("-C", world.origin, "rev-parse", "HEAD").stdout
    rc, out = world.cli(a, "machine", "cloud", "plugins", "off")
    assert rc == 0 and "Nothing was changed" in out
    assert git("-C", world.origin, "rev-parse", "HEAD").stdout == before


def test_machine_plugins_refuses_typos_and_bad_values(world):
    a = world.machine("a")
    rc, out = world.cli(a, "machine", "clould", "plugins", "full")
    assert rc == 1 and "typo" in out
    for argv in (("plugins",), ("plugins", "on"), ("plugins", "full", "x")):
        rc, out = world.cli(a, "machine", "cloud", *argv)
        assert rc == 1 and out.startswith("Usage: "), argv
    assert not has_ref(world.origin, "refs/heads/sync/host-a")


def test_show_reports_a_bad_plugins_value_without_failing(world):
    world.commit_on_origin({"machines/cloud.conf": b"TIER=basic\nPLUGINS=yes\n"}, "bad")
    a = world.machine("a")
    rc, out = world.cli(a, "machine", "cloud", "show")
    assert rc == 0, out
    assert "  plugins: Not a valid PLUGINS value" in out
