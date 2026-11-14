"""Cheap checks on the one-command setup scripts, get.sh and get.ps1.
get.sh runs only for --help (nothing is installed); get.ps1 is read as
text, since CI has no Windows PowerShell. Placeholder names only: this
file is published.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GET_SH = REPO_ROOT / "get.sh"
GET_PS1 = REPO_ROOT / "get.ps1"

BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(os.name == "nt" or BASH is None, reason="get.sh needs bash on Linux or macOS")
# tests/ is published but the get scripts are not on the publish whitelist.
pytestmark = pytest.mark.skipif(
    not (GET_SH.is_file() and GET_PS1.is_file()), reason="get.sh and get.ps1 are not part of the public mirror"
)

SH_FLAGS = ("--dir", "--project-path", "--profile", "--tier", "--yes", "--dry-run", "--skip-python-deps", "--help")
PS1_PARAMS = ("Repo", "Dir", "ProjectPath", "Profile", "Tier", "Yes", "DryRun", "SkipPythonDeps")
WINGET_IDS = ("Git.Git", "Python.Python.3.12", "GitHub.cli")


@needs_bash
def test_get_sh_parses():
    result = subprocess.run([BASH, "-n", str(GET_SH)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


@needs_bash
def test_get_sh_help_names_every_flag(tmp_path):
    # Inside a venv too: --help comes before the virtualenv refusal.
    env = {**os.environ, "HOME": str(tmp_path), "VIRTUAL_ENV": str(tmp_path / "venv")}
    result = subprocess.run(
        [BASH, str(GET_SH), "--help"], capture_output=True, text=True, timeout=30, env=env, stdin=subprocess.DEVNULL
    )
    assert result.returncode == 0, result.stderr
    assert "OWNER/REPO" in result.stdout
    for flag in SH_FLAGS:
        assert flag in result.stdout, flag


def test_get_ps1_has_every_parameter_and_winget_id():
    text = GET_PS1.read_text(encoding="utf-8")
    for name in PS1_PARAMS:
        assert f"${name}" in text or f"'{name}'" in text, name
    # -Profile binds to $ProfileName, so $PROFILE in the user's session is never set.
    assert "[Alias('Profile')][string]$ProfileName" in text
    for winget_id in WINGET_IDS:
        assert winget_id in text, winget_id
    assert "CLAUDE_SYNC_REPO" in text and "CLAUDE_SYNC_DIR" in text


def test_get_scripts_are_ascii():
    # Windows PowerShell 5.1 reads a .ps1 without a BOM as ANSI.
    for path in (GET_SH, GET_PS1):
        path.read_bytes().decode("ascii")


# --- get.sh refusal rules, run against stand-in git, python3 and gh ---

_FAKE_GIT = """#!/bin/sh
case "$*" in
  --version) echo "git version 2.45.0" ;;
  *"remote get-url origin"*) [ -n "$FAKE_ORIGIN" ] && echo "$FAKE_ORIGIN" || exit 1 ;;
  *) exit 0 ;;
esac
"""
_FAKE_PYTHON = """#!/bin/sh
case "$*" in
  -c*) echo "${FAKE_PY:-3.12}" ;;
  *) exit 0 ;;
esac
"""
_FAKE_GH = "#!/bin/sh\nexit 0\n"


def _run_get(tmp_path, *args, py="3.12", origin="", venv=""):
    fake = tmp_path / "fakebin"
    fake.mkdir(exist_ok=True)
    for name, body in (("git", _FAKE_GIT), ("python3", _FAKE_PYTHON), ("gh", _FAKE_GH)):
        (fake / name).write_text(body)
        (fake / name).chmod(0o755)
    env = {
        "PATH": f"{fake}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "FAKE_PY": py,
        "FAKE_ORIGIN": origin,
        "VIRTUAL_ENV": venv,
    }
    return subprocess.run(
        [BASH, str(GET_SH), *args], capture_output=True, text=True, timeout=60, env=env, stdin=subprocess.DEVNULL
    )


@needs_bash
@pytest.mark.parametrize(
    "repo", ["https://github.com/o/r", "o/r/extra", "o", "o/../r", "./r/x", "o/r name", "o/r;ls"]
)
def test_get_sh_refuses_anything_but_owner_slash_repo(tmp_path, repo):
    result = _run_get(tmp_path, repo, "--dry-run", "--yes")
    assert result.returncode == 1
    assert "OWNER/REPO" in result.stderr


@needs_bash
def test_get_sh_refuses_unknown_option_and_missing_value(tmp_path):
    assert "Unknown option" in _run_get(tmp_path, "o/r", "--nope").stderr
    result = _run_get(tmp_path, "o/r", "--dir")
    assert result.returncode == 1 and "needs a value" in result.stderr


@needs_bash
def test_get_sh_refuses_without_a_repo_and_a_terminal(tmp_path):
    result = _run_get(tmp_path, "--dry-run")
    assert result.returncode == 1
    assert "no terminal" in result.stderr


@needs_bash
def test_get_sh_refuses_an_active_virtualenv(tmp_path):
    result = _run_get(tmp_path, "o/r", "--dry-run", venv=str(tmp_path / "venv"))
    assert result.returncode == 1
    assert "virtualenv" in result.stderr


@needs_bash
@pytest.mark.skipif(os.uname().sysname == "Darwin", reason="on macOS with brew an old python3 is offered an upgrade")
def test_get_sh_refuses_python_older_than_3_12(tmp_path):
    result = _run_get(tmp_path, "o/r", "--dry-run", "--yes", py="3.9")
    assert result.returncode == 1
    assert "3.12" in result.stderr


@needs_bash
def test_get_sh_refuses_a_folder_that_is_not_a_clone(tmp_path):
    folder = tmp_path / "somewhere"
    folder.mkdir()
    result = _run_get(tmp_path, "o/r", "--dry-run", "--yes", "--skip-python-deps", "--dir", str(folder))
    assert result.returncode == 1
    assert "not a git clone" in result.stderr


@needs_bash
def test_get_sh_refuses_a_clone_without_origin(tmp_path):
    folder = tmp_path / "clone"
    (folder / ".git").mkdir(parents=True)
    result = _run_get(tmp_path, "o/r", "--dry-run", "--yes", "--skip-python-deps", "--dir", str(folder))
    assert result.returncode == 1
    assert "no 'origin'" in result.stderr


@needs_bash
@pytest.mark.parametrize(
    ("origin", "warns"),
    [
        ("https://github.com/o/r.git", False),
        ("git@github.com:O/R.git", False),
        ("https://user:secret@github.com/o/r", False),
        ("https://github.com/o/r-old.git", True),
    ],
)
def test_get_sh_reuses_a_clone_and_warns_only_on_another_repo(tmp_path, origin, warns):
    folder = tmp_path / "clone"
    (folder / ".git").mkdir(parents=True)
    result = _run_get(tmp_path, "o/r", "--dry-run", "--yes", "--skip-python-deps", "--dir", str(folder), origin=origin)
    assert result.returncode == 0, result.stderr
    assert ("Warning" in result.stderr) is warns
    assert "secret" not in result.stdout + result.stderr


@needs_bash
def test_get_sh_refuses_an_unknown_tier_and_passes_a_good_one_on(tmp_path):
    bad = _run_get(tmp_path, "o/r", "--tier", "huge", "--dry-run")
    assert bad.returncode == 1 and "basic or full" in bad.stderr
    folder = tmp_path / "clone"
    (folder / ".git").mkdir(parents=True)
    good = _run_get(
        tmp_path, "o/r", "--dry-run", "--yes", "--tier", "basic", "--dir", str(folder), origin="https://github.com/o/r"
    )
    assert good.returncode == 0, good.stderr
    assert "--tier basic" in good.stdout
    assert "not checking pip" in good.stdout
