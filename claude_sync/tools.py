"""Companion tools the installer provisions on every run.

Port of npm-tools.sh, gh-tools.sh and python-tools.sh. Each step is
idempotent and never raises: a missing program or a failed install prints
what happened (warnings on stderr) and the installer carries on, as the
bash `script || true` calls did.

`path` is the search path for programs (default: PATH), so tests can point
it at a folder of fake programs. Every program is looked up with
shutil.which and started by its full path, so `npm.cmd` works on Windows.

What these steps download, and what is verified:
- npm: `npm install -g ccusage@latest`, an unpinned latest version from the
  npm registry. npm checks the package's integrity hash from the registry
  metadata, but nothing is pinned here and package lifecycle scripts run.
- gh: installed by the system package manager (brew, apt-get, dnf, pacman,
  apk), which checks its own signatures. On Windows nothing is installed;
  the step says how to install gh.
- pip: `<python> -I -m pip install mcp<2 aiosqlite pyyaml`, version ranges
  from PyPI without hashes (no --require-hashes).
This module verifies no checksums itself, and runs nothing it downloads.
Every command runs from an empty scratch folder, and pip in isolated mode
(-I), so nothing in the caller's working folder is read or imported.

Windows: the Linux package managers and the python-is-python3 step are
skipped with a message saying what to do by hand; pip runs without
--break-system-packages (python.org installs are not externally managed).
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

from claude_sync.util import redact, run

NPM_TOOLS = ("ccusage",)
PIP_PACKAGES = ("mcp<2", "aiosqlite", "pyyaml")
GH_MANUAL = "https://cli.github.com"

IS_WINDOWS = os.name == "nt"


def _warn(text: str) -> None:
    print(text, file=sys.stderr)


def _is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def _which(name: str, path: str | None) -> str | None:
    return shutil.which(name, path=path)


def _sudo(path: str | None) -> list[str]:
    """["<sudo>"] when not root and sudo exists (bash sudo_cmd), else []."""
    if IS_WINDOWS or _is_root():
        return []
    sudo = _which("sudo", path)
    return [sudo] if sudo else []


def _run(argv: list[str]) -> bool:
    """Runs one install command, shows its output (tokens redacted) and
    says whether it worked."""
    # From an empty scratch folder, never the caller's: npm reads .npmrc and
    # package.json from its working folder, and other tools may load files
    # from it too.
    with tempfile.TemporaryDirectory(prefix="claude-sync-tools.") as neutral:
        result = run(argv, cwd=Path(neutral))
    if result.stdout:
        sys.stdout.write(redact(result.stdout))
    if result.stderr:
        sys.stderr.write(redact(result.stderr))
    return result.returncode == 0


def install_npm_tools(path: str | None = None) -> None:
    """npm install -g <tool>@latest for each tool: installs, upgrades or no-ops."""
    npm = _which("npm", path)
    if npm is None:
        _warn(f"npm not found — skipping companion tool provisioning ({' '.join(NPM_TOOLS)}).")
        return
    for tool in NPM_TOOLS:
        print(f"Ensuring {tool}@latest via npm...")
        if not _run([npm, "install", "-g", f"{tool}@latest"]):
            _warn(f"Failed to install/update {tool} — continuing.")


def install_gh_tools(path: str | None = None) -> None:
    """Makes sure the gh CLI is installed; no-op when it already is."""
    if _which("gh", path):
        print("gh already installed.")
        return
    if IS_WINDOWS:
        _warn("gh not found. Install it with 'winget install --id GitHub.cli',")
        _warn(f"  or from {GH_MANUAL}, then run 'gh auth login'.")
        return

    print("gh not found — attempting to install...")
    sudo = _sudo(path)
    steps = {
        "brew": [["install", "gh"]],
        "apt-get": [["update", "-y"], ["install", "-y", "gh"]],
        "dnf": [["install", "-y", "gh"]],
        "pacman": [["-Sy", "--noconfirm", "github-cli"]],
        "apk": [["add", "github-cli"]],
    }
    for manager, commands in steps.items():
        prog = _which(manager, path)
        if prog is None:
            continue
        prefix = [] if manager == "brew" else sudo
        if all(_run([*prefix, prog, *args]) for args in commands):
            return
        break  # bash tries only the first package manager it finds

    _warn("Could not auto-install gh (no supported package manager found, or the install failed).")
    _warn(f"Install manually: {GH_MANUAL}")


def _ensure_python_alias(path: str | None) -> None:
    if _which("python", path):
        print("python already available.")
        return
    if IS_WINDOWS:
        _warn("No python command on PATH. Install Python from python.org (tick")
        _warn("  'Add python.exe to PATH') or the Microsoft Store, then re-run.")
        return
    apt = _which("apt-get", path)
    if apt is None:
        _warn("No python command and no apt-get — link python to python3 manually.")
        return
    print("python not found — installing python-is-python3...")
    if not _run([*_sudo(path), apt, "install", "-y", "python-is-python3"]):
        _warn("Failed to install python-is-python3 — continuing.")


def _ensure_pip_packages(python: str) -> None:
    packages = " ".join(PIP_PACKAGES)
    if not python or not os.path.isfile(python):
        _warn(f"{python or 'python'} not found — skipping pip packages ({packages}).")
        return
    print(f"Ensuring pip packages: {packages}...")
    flags = [] if IS_WINDOWS else ["--break-system-packages"]
    # -I: isolated mode, so neither the working folder nor PYTHON* variables
    # can put a planted module on sys.path ahead of pip.
    if not _run([python, "-I", "-m", "pip", "install", *flags, *PIP_PACKAGES]):
        _warn("Failed to install pip packages — continuing.")


def install_python_tools(path: str | None = None, python: str | None = None) -> None:
    """What the agent-memory plugin needs: a `python` command and its pip
    packages (mcp 1.x: mcp 2 removed the API its server uses). pip runs
    from `python`, by default the interpreter running this code."""
    _ensure_python_alias(path)
    _ensure_pip_packages(sys.executable if python is None else python)
