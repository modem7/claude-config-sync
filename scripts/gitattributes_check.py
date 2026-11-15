#!/usr/bin/env python3
"""Check that swapping a repo's .gitattributes changes nothing real.

Usage:
    python3 scripts/gitattributes_check.py REPO_PATH NEW_GITATTRIBUTES_FILE [--verbose]

Works in a throwaway shared clone of REPO_PATH, so REPO_PATH is never touched.
For every tracked file it compares `git check-attr text eol diff merge binary`
before and after the swap, then runs `git add --renormalize .` and lists the
paths git would restage.

Soft change: `text` goes from set or unspecified to auto.
Real change: anything else, including `text: unset` going to any other value.

Exit codes: 0 no real or renormalize changes, 1 there are some,
2 usage or git error.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ATTRS = ("text", "eol", "diff", "merge", "binary")
# Keeps a user-level autocrlf or safecrlf from skewing the result.
NO_CRLF = ("-c", "core.autocrlf=false", "-c", "core.safecrlf=false")

Attrs = dict[str, dict[str, str]]
Change = tuple[str, str, str, str]  # path, attribute, before, after


def git(repo: Path, *args: str, stdin: bytes | None = None) -> bytes:
    """Runs git in repo and returns raw stdout."""
    argv = ["git", "-C", str(repo), *NO_CRLF, *args]
    return subprocess.run(argv, input=stdin, capture_output=True, check=True).stdout


def split_z(data: bytes) -> list[str]:
    """Splits NUL-separated git output, keeping odd bytes intact."""
    return [p.decode("utf-8", "surrogateescape") for p in data.split(b"\0") if p]


def tracked(repo: Path) -> list[str]:
    return split_z(git(repo, "ls-files", "-z"))


def check_attrs(repo: Path, paths: list[str]) -> Attrs:
    """Batch check-attr for all paths: {path: {attr: value}}."""
    stdin = b"".join(p.encode("utf-8", "surrogateescape") + b"\0" for p in paths)
    fields = git(repo, "check-attr", "-z", "--stdin", *ATTRS, stdin=stdin).split(b"\0")
    out: Attrs = {p: {} for p in paths}
    for i in range(0, len(fields) - 2, 3):
        path, attr, value = (
            f.decode("utf-8", "surrogateescape") for f in fields[i : i + 3]
        )
        out.setdefault(path, {})[attr] = value
    return out


def is_soft(attr: str, before: str, after: str) -> bool:
    return attr == "text" and after == "auto" and before in ("set", "unspecified")


def compare(before: Attrs, after: Attrs) -> tuple[list[Change], list[Change]]:
    soft: list[Change] = []
    real: list[Change] = []
    for path in sorted(before):
        for attr in ATTRS:
            b = before[path].get(attr, "unspecified")
            a = after.get(path, {}).get(attr, "unspecified")
            if a != b:
                (soft if is_soft(attr, b, a) else real).append((path, attr, b, a))
    return soft, real


def rule_count(path: Path) -> int:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return sum(
        1 for line in lines if line.strip() and not line.lstrip().startswith("#")
    )


def renormalize(clone: Path) -> list[str]:
    """Paths that `add --renormalize` restages, minus the swapped file itself."""
    git(clone, "add", "--renormalize", ".")
    changed = split_z(git(clone, "diff", "--cached", "--name-only", "-z"))
    return [p for p in changed if p != ".gitattributes"]


def print_changes(title: str, changes: list[Change]) -> None:
    print(f"\n{title}:")
    for path, attr, b, a in changes:
        print(f"  {path!r}: {attr}: {b} -> {a}")


def run(repo: Path, new_attrs: Path, verbose: bool) -> int:
    work = Path(tempfile.mkdtemp(prefix="gitattr-check."))
    try:
        clone = work / "clone"
        subprocess.run(
            ["git", "clone", "-q", "--shared", *NO_CRLF, str(repo), str(clone)],
            capture_output=True,
            check=True,
        )
        target = clone / ".gitattributes"
        old_rules = rule_count(target) if target.exists() else 0
        old_size = target.stat().st_size if target.exists() else 0
        paths = tracked(clone)
        before = check_attrs(clone, paths)
        shutil.copyfile(new_attrs, target)
        after = check_attrs(clone, paths)
        soft, real = compare(before, after)
        renorm = renormalize(clone)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    new_rules, new_size = rule_count(new_attrs), new_attrs.stat().st_size
    if real:
        print_changes("Real attribute changes", real)
    if soft and verbose:
        print_changes("Soft attribute changes (text -> auto)", soft)
    if renorm:
        print("\nPaths changed by git add --renormalize:")
        for p in renorm:
            print(f"  {p!r}")

    rows = [
        ("Tracked files", str(len(paths))),
        ("Rules (old -> new)", f"{old_rules} -> {new_rules}"),
        ("Bytes (old -> new)", f"{old_size} -> {new_size}"),
        ("Soft changes", str(len(soft))),
        ("Real changes", str(len(real))),
        ("Renormalize changes", str(len(renorm))),
    ]
    ok = not real and not renorm
    rows.append(("Result", "PASS" if ok else "FAIL"))
    width = max(len(k) for k, _ in rows)
    print()
    for key, value in rows:
        print(f"{key:<{width}}  {value}")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check that a new .gitattributes changes nothing real."
    )
    parser.add_argument(
        "repo", type=Path, help="git repository to check (left untouched)"
    )
    parser.add_argument("new_attrs", type=Path, help="replacement .gitattributes file")
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="list soft changes too"
    )
    args = parser.parse_args(argv)  # exits 2 on bad usage

    if not args.new_attrs.is_file():
        print(f"error: not a file: {args.new_attrs}", file=sys.stderr)
        return 2
    if not args.repo.is_dir():
        print(f"error: not a directory: {args.repo}", file=sys.stderr)
        return 2
    try:
        return run(args.repo.resolve(), args.new_attrs.resolve(), args.verbose)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as exc:
        err = (exc.stderr or b"").decode("utf-8", "replace").strip()
        print(f"error: {' '.join(map(str, exc.cmd))} failed: {err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
