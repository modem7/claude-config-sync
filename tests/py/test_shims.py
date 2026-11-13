"""The entry-point shims: claude-sync.sh / install.sh run the Python port
and the Windows .cmd shims find Python with `py -3` first, then `python`."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from claude_sync import cli, install

REPO_ROOT = Path(__file__).resolve().parents[2]
posix_only = pytest.mark.skipif(os.name == "nt", reason="the .sh shims need bash")


def _clone_tool(dst: Path) -> Path:
    """A throwaway copy of the tool (shims and the package) in
    its own git repo, so nothing runs against this checkout or the real HOME."""
    dst.mkdir()
    for name in ("claude-sync.sh", "install.sh"):
        shutil.copy2(REPO_ROOT / name, dst / name)
    shutil.copytree(REPO_ROOT / "claude_sync", dst / "claude_sync", ignore=shutil.ignore_patterns("__pycache__"))
    subprocess.run(["git", "init", "-q", str(dst)], check=True)
    return dst


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".agents").mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_SYNC_")}
    env.update(
        HOME=str(home),
        CLAUDE_SYNC_CLAUDE_HOME=str(home / ".claude"),
        CLAUDE_SYNC_AGENTS_HOME=str(home / ".agents"),
        CLAUDE_SYNC_HOSTNAME="host-a",
        CLAUDE_SYNC_GH=str(tmp_path / "no-gh"),
    )
    env.update(extra)
    return env


def _run(args: list[str], cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, timeout=60)


@posix_only
def test_sh_shim_runs_python_cli(tmp_path):
    tool = _clone_tool(tmp_path / "tool")
    elsewhere = tmp_path / "cwd"
    elsewhere.mkdir()
    result = _run(["bash", str(tool / "claude-sync.sh"), "bogus"], elsewhere, _env(tmp_path))
    assert result.returncode == 1
    assert result.stderr == cli.USAGE


@posix_only
def test_sh_install_shim_runs_python_installer(tmp_path):
    tool = _clone_tool(tmp_path / "tool")
    result = _run(["bash", str(tool / "install.sh"), "--bogus"], tmp_path, _env(tmp_path))
    assert result.returncode == 1
    assert result.stderr == f"Unknown option: --bogus\n{install.USAGE}\n"


def _cmd_lines(name: str) -> list[str]:
    text = (REPO_ROOT / name).read_text(encoding="utf-8")
    return [line.strip() for line in text.splitlines()]


@pytest.mark.parametrize(("name", "module"), [("claude-sync.cmd", "claude_sync"), ("install.cmd", "claude_sync.install")])
def test_cmd_shim_uses_py_launcher_first(name, module):
    lines = _cmd_lines(name)
    py = [i for i, line in enumerate(lines) if line.startswith(f"py -3 -m {module} %*")]
    python = [i for i, line in enumerate(lines) if line.startswith(f"python -m {module} %*")]
    assert len(py) == 1 and len(python) == 1
    assert py[0] < python[0]
    # The launcher is looked for before it is used...
    where = [i for i, line in enumerate(lines) if line.lower().startswith("where py")]
    assert where and where[0] < py[0]
    # ...and a failed run is never retried with the other interpreter.
    assert not any("||" in line for line in lines)
    # Each run ends the script on its own line: cmd.exe re-reads the file
    # after each command, and --repair replaces the clone that holds it.
    for i in py + python:
        assert "exit /b" in lines[i]


def _stub_python(tmp_path: Path, rc: int) -> Path:
    """A python3 on PATH that records its argv, cwd and PYTHONPATH, then exits rc."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "python3.log"
    stub = bin_dir / "python3"
    stub.write_text(
        "#!/bin/sh\n"
        f'{{ printf "%s\\n" "$@"; echo "cwd=$(pwd)"; echo "pythonpath=$PYTHONPATH"; }} > "{log}"\n'
        f"exit {rc}\n"
    )
    stub.chmod(0o755)
    return bin_dir


@posix_only
@pytest.mark.parametrize(("name", "module"), [("claude-sync.sh", "claude_sync"), ("install.sh", "claude_sync.install")])
def test_sh_shim_passes_arguments_and_exit_code_through(tmp_path, name, module):
    tool = _clone_tool(tmp_path / "tool")
    bin_dir = _stub_python(tmp_path, 7)
    env = _env(tmp_path, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}", PYTHONPATH="/elsewhere")
    result = _run(["bash", str(tool / name), "override", "add", '.a["b c"]', ""], tmp_path, env)
    assert result.returncode == 7
    lines = (tmp_path / "python3.log").read_text().split("\n")
    assert lines[:6] == ["-m", module, "override", "add", '.a["b c"]', ""]
    assert f"cwd={tool}" in lines
    # The clone comes first on the import path; an existing PYTHONPATH is kept after it.
    assert f"pythonpath={tool}{os.pathsep}/elsewhere" in lines


@posix_only
def test_sh_shim_without_python3_says_what_to_do(tmp_path):
    tool = _clone_tool(tmp_path / "tool")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for prog in ("dirname",):  # all the shim needs besides bash builtins
        os.symlink(shutil.which(prog), bin_dir / prog)
    env = _env(tmp_path, PATH=str(bin_dir))
    for name in ("claude-sync.sh", "install.sh"):
        result = _run([shutil.which("bash"), str(tool / name), "sync"], tmp_path, env)
        assert result.returncode == 1
        assert result.stderr == "claude-sync: needs python3 (3.12+)\n"
