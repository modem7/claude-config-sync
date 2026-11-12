"""Export the generic tool into the public mirror repo. Port of publish-public.sh.

Only the PATHS whitelist (plus README.public.md as README.md) is copied;
machines/, memory/, private/ and weekly-review/ are never published. Links
are skipped, never followed or copied, and files keep their exact bytes and
mode. Only files committed at HEAD are exported, never ignored or untracked
ones. The public tree is regenerated from scratch (except .git) each run,
and the safety guards run before anything is committed or pushed.

CLAUDE_CONFIG_PUBLIC_REPO_URL names the public repo (required);
CLAUDE_CONFIG_PUBLIC_BRANCH its branch (default main).
"""

from __future__ import annotations

import sys

if __name__ == "__main__" and sys.version_info < (3, 12):
    print("publish needs Python 3.12 or newer.", file=sys.stderr)
    sys.exit(1)

import os
import re
import shutil
import stat
import tempfile
from collections.abc import Mapping
from pathlib import Path

from claude_sync import safety
from claude_sync.syncfiles import _remove
from claude_sync.util import Fail, redact, run

# Whitelisted paths to publish; keep in step with PATHS in publish-public.sh
# and the trigger paths in .github/workflows/publish-public.yml.
PATHS = (
    "install.sh", "claude-sync.sh", "npm-tools.sh", "gh-tools.sh", "python-tools.sh",
    "install.cmd", "claude-sync.cmd", "legacy", "claude_sync", "pytest.ini",
    "CONTRIBUTING.md", "CLAUDE.md", "AGENTS.md", "LICENSE", ".gitattributes", ".gitignore",
    ".editorconfig", ".wakatime-project", "renovate.json", ".github", "docs", "lib", "tests",
    "dot-claude/CLAUDE.md", "dot-claude/settings.json", "dot-claude/hooks", "dot-claude/agents",
    "dot-agents",
)
PUBLIC_DESCRIPTION = b"Sync tool for Claude Code global config (CLAUDE.md, settings, skills) across machines"
_SLUG_RE = re.compile(r"github\.com[:/]+([^/\s]+/[^/\s]+?)(?:\.git)?/?$")


def github_slug(url: str) -> str:
    """owner/repo from a GitHub URL (ssh or https, credentials ignored), else ""."""
    m = _SLUG_RE.search(url.strip())
    return m.group(1) if m else ""


def rewrite_issue_config(data: bytes, source_slug: str, public_slug: str) -> bytes:
    """Point the security-advisory link at the public repo instead of the source."""
    old = f"github.com/{source_slug}/security".encode()
    return data.replace(old, f"github.com/{public_slug}/security".encode())


def _git(repo: Path, *args: str):
    return run(["git", "-C", str(repo), *args], env={"GIT_TERMINAL_PROMPT": "0"})


def _tracked(source: Path, paths: tuple[str, ...]) -> list[tuple[str, str]]:
    """(mode, path) of every file committed at HEAD under `paths`. Ignored and
    untracked files are never listed, so they can never be published."""
    result = run(
        ["git", "-C", str(source), "ls-tree", "-r", "-z", "HEAD", "--", *paths],
        env={"GIT_TERMINAL_PROMPT": "0", "GIT_LITERAL_PATHSPECS": "1"},
    )
    if result.returncode != 0:
        raise Fail(f"Could not list the files committed in {source}.", redact(result.stderr.strip()))
    entries = []
    for record in result.stdout.split("\0"):
        if record:
            meta, path = record.split("\t", 1)
            entries.append((meta.split(" ", 1)[0], path))
    return entries


def _copy_tracked(source: Path, mode: str, rel: str, dst: Path) -> None:
    """Copy one committed file. The tree is clean, so its bytes are what HEAD
    holds (git add in the mirror applies the same .gitattributes). Links,
    submodules and anything that is not a plain file on disk are skipped."""
    src = source / rel
    if mode not in ("100644", "100755"):
        what = "link" if mode == "120000" else "submodule" if mode == "160000" else f"mode {mode} entry"
        print(f"publish: skipping {what} {rel} (only plain files are published)", file=sys.stderr)
        return
    if src.is_symlink() or src.is_junction() or not stat.S_ISREG(os.lstat(src).st_mode):
        print(f"publish: skipping {rel}: not a plain file on disk (links and special files are never published)",
              file=sys.stderr)
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst, follow_symlinks=False)


def _keep_modes(pub: Path, modes: Mapping[str, str]) -> None:
    """Give each staged file the mode it has at HEAD in the source. With
    core.filemode=false (Windows) git add stages a new file as 100644 and
    keeps an old entry's mode, so the exec bit would be lost or kept stale.
    Only entries whose staged mode differs are touched."""
    staged = _git(pub, "ls-files", "-s", "-z")
    if staged.returncode != 0:
        raise Fail("Could not read the public index.", redact(staged.stderr.strip()))
    fix: dict[str, list[str]] = {"+x": [], "-x": []}
    for entry in staged.stdout.split("\0"):
        if not entry:
            continue
        meta, rel = entry.split("\t", 1)
        want = modes.get(rel)
        if want in ("100644", "100755") and meta.split()[0] != want:
            fix["+x" if want == "100755" else "-x"].append(rel)
    for flag, rels in fix.items():
        # Batches keep the command line short enough for Windows.
        for i in range(0, len(rels), 100):
            result = _git(pub, "update-index", f"--chmod={flag}", "--", *rels[i:i + 100])
            if result.returncode != 0:
                raise Fail("Could not set file modes in the public index.", redact(result.stderr.strip()))


def _edit(path: Path, pattern: bytes, repl: bytes) -> None:
    if path.is_file() and not path.is_symlink():
        path.write_bytes(re.sub(pattern, repl, path.read_bytes(), flags=re.M))


def _publish(source: Path, env: Mapping[str, str]) -> int:
    url = env.get("CLAUDE_CONFIG_PUBLIC_REPO_URL") or ""
    branch = env.get("CLAUDE_CONFIG_PUBLIC_BRANCH") or "main"
    if not url:
        raise Fail(
            "CLAUDE_CONFIG_PUBLIC_REPO_URL is not set, so there is no public repo to publish to.",
            "Set it to the public mirror's git URL, e.g."
            " CLAUDE_CONFIG_PUBLIC_REPO_URL=git@github.com:<owner>/<repo>.git, then re-run.",
        )
    status = _git(source, "status", "--porcelain")
    if status.returncode != 0:
        raise Fail(f"{source} is not a git repo I can read.", redact(status.stderr.strip()))
    if status.stdout.strip():
        raise Fail(f"Refusing to publish: {source} has uncommitted changes.", "Commit first.")
    sha = _git(source, "rev-parse", "--short", "HEAD").stdout.strip()
    origin = _git(source, "remote", "get-url", "origin").stdout.strip()
    source_slug = env.get("GITHUB_REPOSITORY") or github_slug(origin) or source.name

    workdir = Path(tempfile.mkdtemp(prefix="claude-publish-"))
    try:
        pub = workdir / "pub"
        print(f"Cloning public repo ({redact(url)})...")
        if run(["git", "clone", "-q", url, str(pub)], env={"GIT_TERMINAL_PROMPT": "0"}).returncode != 0:
            print("  (empty/unreachable remote — initializing a fresh repo instead)")
            _remove(pub)
            pub.mkdir()
            for args in (["init", "-q", "-b", branch, str(pub)], ["-C", str(pub), "remote", "add", "origin", url]):
                result = run(["git", *args])
                if result.returncode != 0:
                    raise Fail("Could not set up a fresh public repo.", redact(result.stderr.strip()))

        # Wipe everything except .git: the public tree is fully generated.
        for child in pub.iterdir():
            if child.name != ".git":
                _remove(child)
        # Only files committed at HEAD: ignored files (caches, .env, keys) never go out.
        modes: dict[str, str] = {}
        for mode, rel in _tracked(source, PATHS):
            _copy_tracked(source, mode, rel, pub / rel)
            modes[rel] = mode
        readme = [(mode, rel) for mode, rel in _tracked(source, ("README.public.md",)) if rel == "README.public.md"]
        if not readme:
            raise Fail("README.public.md is not committed in the source repo.", "Add and commit it, then re-run.")
        _copy_tracked(source, readme[0][0], "README.public.md", pub / "README.md")
        modes["README.md"] = readme[0][0]
        if not (pub / "README.md").is_file():
            raise Fail("README.public.md is not a plain file.", "Replace it with a plain file, commit, then re-run.")

        # Private-repo automation, meaningless on the mirror itself.
        _remove(pub / ".github" / "workflows" / "publish-public.yml")
        settings = pub / ".github" / "settings.yml"
        _edit(settings, rb"^  private: true(?=\r?$)", b"  private: false")
        _edit(settings, rb"^  description: [^\r\n]*", b"  description: " + PUBLIC_DESCRIPTION)
        public_slug = github_slug(url)
        issue_config = pub / ".github" / "ISSUE_TEMPLATE" / "config.yml"
        if public_slug and issue_config.is_file() and not issue_config.is_symlink():
            issue_config.write_bytes(rewrite_issue_config(issue_config.read_bytes(), source_slug, public_slug))

        try:
            safety.assert_no_denylisted_files(pub)
            safety.assert_public_tree_clean(pub)
        except Fail as err:
            raise Fail(
                err.message,
                "Nothing was published. Remove the path(s) above from the whitelist or the source tree.",
            ) from None

        run(["git", "-C", str(pub), "add", "-A"], check=True)
        _keep_modes(pub, modes)
        if _git(pub, "diff", "--cached", "--quiet").returncode == 0:
            print("No changes to publish.")
            return 0
        commit = _git(pub, "commit", "-q", "-m", f"sync: publish from {source_slug}@{sha}")
        if commit.returncode != 0:
            raise Fail("Could not commit the public tree.", redact(commit.stderr.strip()))
        push = _git(pub, "push", "-q", "-u", "origin", f"HEAD:{branch}")
        if push.returncode != 0:
            raise Fail(
                f"Push to {redact(url)} ({branch}) failed:\n{redact(push.stderr.strip())}",
                "Check the URL and that you can push to it, then re-run.",
            )
        print(f"Published to {redact(url)} ({branch}).")
        return 0
    finally:
        _remove(workdir)


def publish(source: Path, env: Mapping[str, str]) -> int:
    """Exports `source` to the public repo; returns the exit code. Every
    failure prints what to do on stderr; no traceback."""
    try:
        return _publish(Path(source), env)
    except Fail as err:
        print(err.message, file=sys.stderr)
        if err.hint:
            print(err.hint, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("publish: stopped by Ctrl-C (exit 130). Nothing was pushed unless it said so.", file=sys.stderr)
        return 130
    except OSError as err:
        print(f"publish: {err}", file=sys.stderr)
        print("Check the path named above exists and is readable, then re-run.", file=sys.stderr)
        return 1
    except Exception as err:  # noqa: BLE001 - the last line of defence: no traceback
        print(f"publish: unexpected error: {type(err).__name__}: {redact(str(err))}", file=sys.stderr)
        return 1


def main(argv: list[str], env: Mapping[str, str]) -> int:
    """Publishes the repo this package lives in. Takes no flags (as the bash)."""
    return publish(Path(__file__).resolve().parent.parent, env)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], os.environ))
