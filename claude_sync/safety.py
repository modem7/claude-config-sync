"""Guards against committing secrets and against publishing personal content.

Walks never follow symlinks.
"""

from __future__ import annotations

import os
from pathlib import Path

from claude_sync.util import Fail

DENYLISTED_FILENAMES = (".credentials.json", ".claude.json")

# Top-level paths the public mirror must never get (the entry and all under it).
PRIVATE_TOP_LEVEL = ("machines", "memory", "private", "weekly-review")


def assert_no_denylisted_files(root: Path) -> None:
    """Raise Fail naming the first denylisted file or directory found at any depth."""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in (*dirnames, *filenames):
            if name in DENYLISTED_FILENAMES:
                found = Path(dirpath) / name
                raise Fail(
                    f"Denylisted file found in repo tree: {found}",
                    "Remove it from the tree; account and auth files must never be synced.",
                )


def assert_public_tree_clean(root: Path) -> None:
    """Raise Fail listing per-machine or personal content in a tree to publish.

    Leaks are machines/, memory/, private/ and weekly-review/ at the top level,
    and any *.settings.json below a directory named machines at any depth.
    A top-level .git is skipped. Paths are reported as "./<relative path>".
    """
    root = Path(root)
    leaked: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = Path(dirpath).relative_to(root)
        at_top = rel_dir == Path(".")
        if at_top:
            if ".git" in dirnames:
                dirnames.remove(".git")
            for name in sorted((*dirnames, *filenames)):
                if name in PRIVATE_TOP_LEVEL:
                    leaked.append(f"./{name}")
            dirnames[:] = [d for d in dirnames if d not in PRIVATE_TOP_LEVEL]
        if "machines" in rel_dir.parts:
            for name in filenames:
                if name.endswith(".settings.json"):
                    leaked.append("./" + (rel_dir / name).as_posix())
    if leaked:
        raise Fail(
            "Refusing to publish: per-machine or personal content is in the public tree:\n"
            + "\n".join(leaked),
            "Remove it from the tree before publishing.",
        )
