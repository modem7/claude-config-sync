"""A seeded origin and machines that clone it: the test world of
tests/sync_pr.bats. `World.run` composes the steps claude-sync.sh's main
does around the real do_push and the fake gh; `World.cli` runs the real
command line (claude_sync.cli.main) in-process as one machine.
"""

from __future__ import annotations

import io
import os
import sys
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path

from helpers import git, merge_and_converge

from claude_sync import gitflow, machineconf, pr
from claude_sync.gitflow import SyncCtx
from claude_sync.util import Fail


@dataclass
class Machine:
    name: str
    host: str
    repo: Path
    claude: Path
    agents: Path
    gh_state: Path
    gh_down: bool = False

    def env(self) -> dict[str, str]:
        """os.environ plus this machine's CLAUDE_SYNC_* (bats raw_as)."""
        return {
            **os.environ,
            "CLAUDE_SYNC_REPO_DIR": str(self.repo),
            "CLAUDE_SYNC_CLAUDE_HOME": str(self.claude),
            "CLAUDE_SYNC_AGENTS_HOME": str(self.agents),
            "CLAUDE_SYNC_HOSTNAME": self.host,
            "CLAUDE_SYNC_LOCK_WAIT": "2",
        }

    def ctx(self, hostname: str | None = None) -> SyncCtx:
        return SyncCtx(
            repo=self.repo,
            claude_home=self.claude,
            agents_home=self.agents,
            hostname=hostname or self.host,
            lock_wait=0.5,
            pr_state=pr.pr_state_for(self.repo),
        )


class World:
    """An origin seeded like sync_pr.bats, and machines that clone it."""

    def __init__(self, tmp: Path, origin: Path, fake_gh: Path) -> None:
        self.tmp = tmp
        self.origin = origin
        self.gh_state = fake_gh.parent / "state"
        self.gh_log = fake_gh.parent / "calls.log"
        self.machines: dict[str, Machine] = {}
        seed = tmp / "seed2"
        git("clone", "-q", origin, seed)
        (seed / "dot-claude").mkdir(exist_ok=True)
        (seed / "dot-claude" / "CLAUDE.md").write_bytes(b"# shared\n")
        (seed / "dot-claude" / "settings.json").write_bytes(b'{"theme":"dark"}\n')
        machineconf.write_machine_conf(seed, "host-a", "/home/user/project")
        machineconf.write_machine_conf(seed, "host-b", "/home/user/work")
        git("-C", seed, "add", "-A")
        git("-C", seed, "commit", "-q", "-m", "seed config")
        git("-C", seed, "push", "-q", "origin", "master")

    def commit_on_origin(
        self, files: dict[str, bytes | None], msg: str, executable: tuple[str, ...] = ()
    ) -> None:
        """Commits to origin's master from a scratch clone, as another
        machine or a merged PR would (bats edit_origin + push_to_origin).
        A None value deletes the file."""
        edit = self.tmp / "edit"
        if not (edit / ".git").exists():
            git("clone", "-q", "-c", "core.autocrlf=false", self.origin, edit)
        git("-C", edit, "pull", "-q", "--ff-only")
        for rel, data in files.items():
            path = edit / rel
            if data is None:
                path.unlink()
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            if rel in executable:
                path.chmod(0o755)
        git("-C", edit, "add", "-A")
        if executable:
            # chmod does nothing on Windows: set the committed mode directly.
            git("-C", edit, "update-index", "--chmod=+x", "--", *executable)
        git("-C", edit, "commit", "-q", "-m", msg)
        git("-C", edit, "push", "-q", "origin", "master")

    def seed_profiles(self) -> None:
        """origin gets profile.bats' shared entries on top of the seed."""
        self.commit_on_origin(
            {
                "dot-claude/agents/helper.md": b"shared helper\n",
                "dot-claude/agents/reviewer.md": b"shared reviewer\n",
                "dot-claude/hooks/guard.sh": b"#!/bin/bash\necho shared guard\n",
                "dot-agents/skills/shared-skill/SKILL.md": b"shared skill\n",
                "dot-agents/skills/shared-skill/notes.md": b"only in the shared copy\n",
                "dot-agents/skills/other-skill/SKILL.md": b"other skill\n",
            },
            "shared entries",
            executable=("dot-claude/hooks/guard.sh",),
        )

    def machine(self, name: str, *, pull: bool = True) -> Machine:
        m = Machine(
            name=name,
            host=f"host-{name}",
            repo=self.tmp / f"repo_{name}",
            claude=self.tmp / f"claude_home_{name}",
            agents=self.tmp / f"agents_home_{name}",
            gh_state=self.gh_state,
        )
        self.machines[name] = m
        git("clone", "-q", "-c", "core.autocrlf=false", self.origin, m.repo)
        (m.agents / "skills").mkdir(parents=True)
        m.claude.mkdir(parents=True)
        if pull:
            self.ok(m, "pull")
        return m

    def run(self, m: Machine, cmd: str, ctx: SyncCtx | None = None) -> tuple[int, str]:
        """Runs a command as machine m: (exit code, stdout+stderr)."""
        ctx = ctx or m.ctx()
        buf = io.StringIO()
        rc = 0
        saved = os.environ.get("FAKE_GH_AUTH_RC")
        if m.gh_down:  # the fake gh then answers "not logged in"
            os.environ["FAKE_GH_AUTH_RC"] = "1"
        with redirect_stdout(buf), redirect_stderr(buf):
            try:
                with ExitStack() as stack:
                    if cmd == "pull":
                        gitflow.begin_sync(ctx, stack)
                        gitflow.do_integrate(ctx)
                        gitflow.do_apply(ctx)
                    elif cmd == "sync":
                        gitflow.begin_sync(ctx, stack)
                        gitflow.do_capture_and_commit(ctx)
                        gitflow.do_integrate(ctx)
                        gitflow.do_apply(ctx)
                        pr.do_push(ctx)
                    elif cmd == "push":
                        gitflow.begin_sync(ctx, stack)
                        gitflow.do_capture_and_commit(ctx)
                        gitflow.do_squash(ctx)
                        pr.do_push(ctx)
                    elif cmd == "bootstrap":
                        gitflow.begin_sync(ctx, stack)
                        gitflow.do_integrate(ctx)
                        gitflow.do_apply_merge(ctx)
                        gitflow.do_capture_and_commit(ctx)
                        gitflow.do_squash(ctx)
                        pr.do_push(ctx)
                    else:
                        raise AssertionError(cmd)
            except Fail as err:
                print(f"claude-sync: {err.message}")
                if err.hint:
                    print(err.hint)
                rc = 1
            except SystemExit as exc:
                rc = int(exc.code or 0)
        if m.gh_down:
            if saved is None:
                os.environ.pop("FAKE_GH_AUTH_RC", None)
            else:
                os.environ["FAKE_GH_AUTH_RC"] = saved
        return rc, buf.getvalue()

    def ok(self, m: Machine, cmd: str) -> str:
        rc, out = self.run(m, cmd)
        assert rc == 0, f"{cmd} as {m.host} failed:\n{out}"
        return out

    def sync_as(self, m: Machine, cmd: str) -> str:
        """Like ok, then merge its PR and converge (bats sync_as)."""
        out = self.ok(m, cmd)
        merge_and_converge(self.origin, m.host, lambda c: self.run(m, c))
        return out

    def cli(self, m: Machine, *argv: str, stdin: str = "", env: dict | None = None) -> tuple[int, str]:
        """Runs `claude-sync <argv>` as machine m through cli.main:
        (exit code, stdout+stderr). `stdin` is what a prompt reads."""
        from claude_sync import cli

        buf = io.StringIO()
        saved_in, saved_rc = sys.stdin, os.environ.get("FAKE_GH_AUTH_RC")
        if m.gh_down:
            os.environ["FAKE_GH_AUTH_RC"] = "1"
        sys.stdin = io.StringIO(stdin)
        try:
            with redirect_stdout(buf), redirect_stderr(buf):
                rc = cli.main(list(argv), env if env is not None else m.env())
        finally:
            sys.stdin = saved_in
            if m.gh_down:
                if saved_rc is None:
                    os.environ.pop("FAKE_GH_AUTH_RC", None)
                else:
                    os.environ["FAKE_GH_AUTH_RC"] = saved_rc
        return rc, buf.getvalue()

    def cli_as(self, m: Machine, *argv: str, stdin: str = "") -> tuple[int, str]:
        """bats sync_as: like cli, then once a repo-changing command
        succeeded, merge its PR and let the machine sync once more."""
        rc, out = self.cli(m, *argv, stdin=stdin)
        if rc == 0 and argv and argv[0] in ("push", "sync", "bootstrap", "override", "profile", "tier"):
            merge_and_converge(self.origin, m.host, lambda c: self.cli(m, c))
        return rc, out

    def cli_ok(self, m: Machine, *argv: str, stdin: str = "") -> str:
        rc, out = self.cli_as(m, *argv, stdin=stdin)
        assert rc == 0, f"{' '.join(argv)} as {m.host} failed ({rc}):\n{out}"
        return out
