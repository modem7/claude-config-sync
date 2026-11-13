"""Tests for claude_sync.tools, ported from tests/npm_tools.bats,
tests/gh_tools.bats and tests/python_tools.bats.

The bats `env -i PATH=<dir>` runs become `path=<dir>`: each function looks
its programs up only there. The fake programs are /bin/sh scripts that log
their arguments, so nothing real is ever installed; they need a POSIX
shell, so these tests are skipped on Windows (the Windows branch is tested
through the module's IS_WINDOWS flag instead).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from claude_sync import tools

posix_only = pytest.mark.skipif(os.name == "nt", reason="fake programs are /bin/sh scripts")


@pytest.fixture
def fakebin(tmp_path):
    d = tmp_path / "fakebin"
    d.mkdir()
    return d


@pytest.fixture
def calls(tmp_path) -> Path:
    return tmp_path / "calls.log"


def fake_cmd(fakebin: Path, calls: Path, name: str, rc: int = 0) -> Path:
    """bats fake_cmd: a program that logs `<name> <args>` and exits rc."""
    prog = fakebin / name
    prog.write_text(
        f'#!/bin/sh\necho "{name} $*" >> "{calls}"\nexit {rc}\n', encoding="utf-8", newline="\n"
    )
    prog.chmod(0o755)
    return prog


def logged(calls: Path) -> str:
    return calls.read_text(encoding="utf-8") if calls.is_file() else ""


def out(capsys) -> str:
    captured = capsys.readouterr()
    return captured.out + captured.err


# ── npm (npm_tools.bats) ─────────────────────────────────────────────────


@posix_only
def test_npm_provisions_each_declared_tool_at_latest(fakebin, calls, capsys):
    fake_cmd(fakebin, calls, "npm")
    tools.install_npm_tools(path=str(fakebin))
    assert "npm install -g ccusage@latest" in logged(calls)
    assert "Ensuring ccusage@latest via npm..." in out(capsys)


@posix_only
def test_npm_continues_past_a_failed_install(fakebin, calls, capsys):
    fake_cmd(fakebin, calls, "npm", rc=1)
    tools.install_npm_tools(path=str(fakebin))  # does not raise
    assert "Failed to install/update ccusage" in out(capsys)


def test_npm_skips_with_a_clear_message_when_missing(fakebin, capsys):
    tools.install_npm_tools(path=str(fakebin))
    text = out(capsys)
    assert "npm not found" in text
    assert "ccusage" in text


# ── gh (gh_tools.bats) ───────────────────────────────────────────────────


@posix_only
def test_gh_does_nothing_if_already_installed(fakebin, calls, capsys):
    fake_cmd(fakebin, calls, "gh")
    fake_cmd(fakebin, calls, "apt-get")
    tools.install_gh_tools(path=str(fakebin))
    assert "already installed" in out(capsys)
    assert "apt-get" not in logged(calls)


@posix_only
def test_gh_installs_via_apt_get_when_missing(fakebin, calls, monkeypatch):
    monkeypatch.setattr(tools, "_is_root", lambda: True)  # no sudo in front
    fake_cmd(fakebin, calls, "apt-get")
    tools.install_gh_tools(path=str(fakebin))
    log = logged(calls)
    assert "apt-get update -y" in log
    assert "apt-get install -y gh" in log


@posix_only
def test_gh_uses_sudo_when_not_root_and_sudo_exists(fakebin, calls, monkeypatch):
    monkeypatch.setattr(tools, "_is_root", lambda: False)
    fake_cmd(fakebin, calls, "apt-get")
    sudo = fakebin / "sudo"
    sudo.write_text(f'#!/bin/sh\necho "sudo $*" >> "{calls}"\nexec "$@"\n', encoding="utf-8")
    sudo.chmod(0o755)
    tools.install_gh_tools(path=str(fakebin))
    log = logged(calls)
    assert f"sudo {fakebin / 'apt-get'} install -y gh" in log
    assert "apt-get install -y gh" in log


@posix_only
@pytest.mark.parametrize(
    ("manager", "expected"),
    [
        ("brew", "brew install gh"),
        ("dnf", "dnf install -y gh"),
        ("pacman", "pacman -Sy --noconfirm github-cli"),
        ("apk", "apk add github-cli"),
    ],
)
def test_gh_other_package_managers(fakebin, calls, monkeypatch, manager, expected):
    monkeypatch.setattr(tools, "_is_root", lambda: True)
    fake_cmd(fakebin, calls, manager)
    tools.install_gh_tools(path=str(fakebin))
    assert expected in logged(calls)


@posix_only
def test_gh_continues_when_the_package_manager_fails(fakebin, calls, monkeypatch, capsys):
    monkeypatch.setattr(tools, "_is_root", lambda: True)
    fake_cmd(fakebin, calls, "apt-get", rc=1)
    tools.install_gh_tools(path=str(fakebin))
    assert "Install manually: https://cli.github.com" in out(capsys)


def test_gh_skips_with_a_clear_message_without_a_package_manager(fakebin, monkeypatch, capsys):
    monkeypatch.setattr(tools, "IS_WINDOWS", False)
    tools.install_gh_tools(path=str(fakebin))
    assert "Could not auto-install gh" in out(capsys)


def test_gh_on_windows_says_how_to_install_and_runs_nothing(fakebin, calls, monkeypatch, capsys):
    monkeypatch.setattr(tools, "IS_WINDOWS", True)
    if os.name != "nt":
        fake_cmd(fakebin, calls, "apt-get")
    tools.install_gh_tools(path=str(fakebin))
    text = out(capsys)
    assert "winget install --id GitHub.cli" in text
    assert "https://cli.github.com" in text
    assert logged(calls) == ""


# ── python (python_tools.bats) ───────────────────────────────────────────


@posix_only
def test_python_skips_apt_get_when_python_is_available(fakebin, calls, capsys):
    fake_cmd(fakebin, calls, "python")
    py3 = fake_cmd(fakebin, calls, "python3")
    fake_cmd(fakebin, calls, "apt-get")
    tools.install_python_tools(path=str(fakebin), python=str(py3))
    assert "python already available" in out(capsys)
    assert "apt-get" not in logged(calls)


@posix_only
def test_python_installs_python_is_python3_when_python_is_missing(fakebin, calls, monkeypatch):
    monkeypatch.setattr(tools, "_is_root", lambda: True)
    py3 = fake_cmd(fakebin, calls, "python3")
    fake_cmd(fakebin, calls, "apt-get")
    tools.install_python_tools(path=str(fakebin), python=str(py3))
    assert "apt-get install -y python-is-python3" in logged(calls)


@posix_only
def test_python_installs_the_pinned_pip_packages_with_break_system_packages(fakebin, calls):
    fake_cmd(fakebin, calls, "python")
    py3 = fake_cmd(fakebin, calls, "python3")
    tools.install_python_tools(path=str(fakebin), python=str(py3))
    assert "python3 -I -m pip install --break-system-packages mcp<2 aiosqlite pyyaml" in logged(calls)


@posix_only
def test_python_pip_runs_the_given_interpreter_by_default(fakebin, calls, monkeypatch):
    py = fake_cmd(fakebin, calls, "python3")
    monkeypatch.setattr(tools.sys, "executable", str(py))
    fake_cmd(fakebin, calls, "python")
    tools.install_python_tools(path=str(fakebin))
    assert "python3 -I -m pip install" in logged(calls)


@posix_only
def test_python_on_windows_pip_has_no_break_system_packages(fakebin, calls, monkeypatch, capsys):
    monkeypatch.setattr(tools, "IS_WINDOWS", True)
    fake_cmd(fakebin, calls, "python")
    py3 = fake_cmd(fakebin, calls, "python3")
    fake_cmd(fakebin, calls, "apt-get")
    tools.install_python_tools(path=str(fakebin), python=str(py3))
    log = logged(calls)
    assert "python3 -I -m pip install mcp<2 aiosqlite pyyaml" in log
    assert "--break-system-packages" not in log
    assert "apt-get" not in log


def _pip_that_refuses_the_system_install(fakebin: Path, calls: Path, name: str = "python3") -> Path:
    """A python that fails under -I (the system-wide try) and works otherwise."""
    prog = fakebin / name
    prog.write_text(
        f'#!/bin/sh\necho "{name} $*" >> "{calls}"\ncase " $* " in *" -I "*) exit 1;; esac\nexit 0\n',
        encoding="utf-8",
        newline="\n",
    )
    prog.chmod(0o755)
    return prog


@posix_only
def test_python_on_windows_retries_pip_with_user_when_the_system_install_is_refused(
    fakebin, calls, monkeypatch, capsys
):
    """A Microsoft Store Python is read-only: pip needs --user, and --user
    does not work under -I, so the retry uses -E -P instead."""
    monkeypatch.setattr(tools, "IS_WINDOWS", True)
    fake_cmd(fakebin, calls, "python")
    py3 = _pip_that_refuses_the_system_install(fakebin, calls)
    tools.install_python_tools(path=str(fakebin), python=str(py3))
    log = logged(calls)
    assert "python3 -I -m pip install mcp<2 aiosqlite pyyaml" in log
    assert "python3 -E -P -m pip install --user mcp<2 aiosqlite pyyaml" in log
    assert "Failed to install pip packages" not in out(capsys)


@posix_only
def test_python_on_windows_warns_when_the_user_install_fails_too(fakebin, calls, monkeypatch, capsys):
    monkeypatch.setattr(tools, "IS_WINDOWS", True)
    fake_cmd(fakebin, calls, "python")
    py3 = fake_cmd(fakebin, calls, "python3", rc=1)
    tools.install_python_tools(path=str(fakebin), python=str(py3))
    assert "--user" in logged(calls)
    assert "Failed to install pip packages" in out(capsys)


@posix_only
def test_python_off_windows_never_retries_with_user(fakebin, calls, monkeypatch, capsys):
    monkeypatch.setattr(tools, "IS_WINDOWS", False)
    fake_cmd(fakebin, calls, "python")
    py3 = fake_cmd(fakebin, calls, "python3", rc=1)
    tools.install_python_tools(path=str(fakebin), python=str(py3))
    assert "--user" not in logged(calls)
    assert "Failed to install pip packages" in out(capsys)


def test_python_on_windows_without_python_says_what_to_do(fakebin, calls, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(tools, "IS_WINDOWS", True)
    tools.install_python_tools(path=str(fakebin), python=str(tmp_path / "no-such-python"))
    text = out(capsys)
    assert "No python command" in text
    assert "Add python.exe to PATH" in text
    assert logged(calls) == ""


@posix_only
def test_python_continues_when_installs_fail(fakebin, calls, monkeypatch, capsys):
    monkeypatch.setattr(tools, "_is_root", lambda: True)
    py3 = fake_cmd(fakebin, calls, "python3", rc=1)
    fake_cmd(fakebin, calls, "apt-get", rc=1)
    tools.install_python_tools(path=str(fakebin), python=str(py3))
    text = out(capsys)
    assert "Failed to install python-is-python3" in text
    assert "Failed to install pip packages" in text


def test_python_skips_when_neither_python_nor_apt_get_exist(fakebin, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(tools, "IS_WINDOWS", False)
    tools.install_python_tools(path=str(fakebin), python=str(tmp_path / "no-such-python"))
    text = out(capsys)
    assert "link python to python3 manually" in text
    assert "not found" in text
    assert "skipping pip packages" in text


@posix_only
def test_tool_output_has_tokens_redacted(fakebin, calls, monkeypatch, capsys):
    monkeypatch.setenv("GH_TOKEN", "sekrit-token-value")
    prog = fakebin / "npm"
    prog.write_text('#!/bin/sh\necho "auth sekrit-token-value failed" >&2\nexit 1\n', encoding="utf-8")
    prog.chmod(0o755)
    tools.install_npm_tools(path=str(fakebin))
    text = out(capsys)
    assert "sekrit-token-value" not in text
    assert "Failed to install/update ccusage" in text


@posix_only
@pytest.mark.parametrize("step", ["npm", "pip", "apt-get"])
def test_tools_run_from_a_neutral_folder_and_pip_is_isolated(fakebin, calls, tmp_path, monkeypatch, step):
    """A planted file in the caller's folder (pip.py, .npmrc) is never seen."""
    work = tmp_path / "work"
    work.mkdir()
    (work / "pip.py").write_text("raise SystemExit('planted')\n", encoding="utf-8")
    (work / ".npmrc").write_text("registry=http://evil.invalid/\n", encoding="utf-8")
    monkeypatch.chdir(work)
    monkeypatch.setattr(tools, "_is_root", lambda: True)
    pwd_log = tmp_path / "pwd.log"
    name = {"npm": "npm", "pip": "python3", "apt-get": "apt-get"}[step]
    prog = fakebin / name
    prog.write_text(
        f'#!/bin/sh\necho "{name} $*" >> "{calls}"\npwd > "{pwd_log}"\nls -A > "{pwd_log}.ls"\n',
        encoding="utf-8",
    )
    prog.chmod(0o755)
    if step == "npm":
        tools.install_npm_tools(path=str(fakebin))
    elif step == "pip":
        fake_cmd(fakebin, calls, "python")
        tools.install_python_tools(path=str(fakebin), python=str(prog))
        assert "python3 -I -m pip install" in logged(calls)
    else:
        tools.install_gh_tools(path=str(fakebin))
    ran_in = Path(pwd_log.read_text(encoding="utf-8").strip())
    assert ran_in.resolve() != work.resolve()
    assert (Path(f"{pwd_log}.ls")).read_text(encoding="utf-8") == ""
    assert not ran_in.exists()  # the scratch folder is cleaned up
