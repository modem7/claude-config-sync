"""`claude-sync status --line [--json]`: at most one line, only when this
machine needs attention, so a healthy machine prints nothing. Meant for a
SessionStart hook. Plain hook stdout reaches Claude only, so the hook uses
--json (cli.hook_json): systemMessage shows the line to the user and
hookSpecificOutput.additionalContext gives it to Claude.

It never fetches, never calls gh and never takes the sync lock: it reads
only local state, so it is fast and safe to run beside the hook's own
sync. Claude Code runs a SessionStart event's hooks in parallel, so what
it reports is the state the previous run left (often the last session's
SessionEnd sync, whose failure nobody saw).

What it looks at, most urgent first:

1. The last hook sync failed: the newest result line the hooks append to
   ~/.claude/claude-config-sync.log (`claude-sync-hook: <event> exit=<rc>`).
2. An offline subset of `doctor`: a stuck rebase, a hook settings.json runs
   that does not exist, a hook tracked without the exec bit, a project
   path whose memory folder name is too long.
3. This machine's sync branch has waited more than CLAUDE_SYNC_PR_DAYS
   days (default 3; 0 turns it off) for its PR to be merged. The age is
   the oldest reflog entry of the local sync/<hostname> branch, which a
   rebuild does not reset and a delete clears.
4. The tier changed (TIER= in the conf) and the next sync switches it.

One line at most: the first finding, plus how many more there are.
"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping
from pathlib import Path

from claude_sync import gitflow, machineconf, profile
from claude_sync.gitflow import SyncCtx
from claude_sync.memorypath import long_name_warning
from claude_sync.util import Fail, printable, redact

LOG_NAME = "claude-config-sync.log"
# Only the end of the log is read: it grows without limit.
LOG_TAIL = 16384
_RESULT = re.compile(r"^claude-sync-hook: (\S+) exit=(\d+)", re.MULTILINE)

PR_DAYS_ENV = "CLAUDE_SYNC_PR_DAYS"
DEFAULT_PR_DAYS = 3
_DAY = 86400
_MAX_LINE = 300
_PREFIX = "claude-config: "


def pr_days_from_env(env: Mapping[str, str]) -> int:
    """CLAUDE_SYNC_PR_DAYS as a whole number of days, 0 or more. Fail on
    anything else."""
    raw = env.get(PR_DAYS_ENV, "").strip()
    if not raw:
        return DEFAULT_PR_DAYS
    if not raw.isascii() or not raw.isdigit():
        raise Fail(
            f"{PR_DAYS_ENV}={printable(raw)[:40]} is not a whole number of days.",
            hint=f"  Set {PR_DAYS_ENV} to 0 (off) or more, or unset it for {DEFAULT_PR_DAYS}.",
        )
    return int(raw)


def last_hook_result(claude_home: Path) -> tuple[str, int] | None:
    """(event, exit code) of the newest result line in the sync log, or
    None when there is no log or no result line in its last LOG_TAIL bytes."""
    log = Path(claude_home) / LOG_NAME
    try:
        with open(log, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - LOG_TAIL))
            tail = f.read().decode("utf-8", errors="replace")
    except FileNotFoundError:
        return None
    found = _RESULT.findall(tail)
    if not found:
        return None
    event, rc = found[-1]
    return printable(event)[:40], int(rc)


def quick_issues(ctx: SyncCtx) -> list[str]:
    """The doctor checks that need no network and little time, each as a
    short reason. `doctor` itself prints the details and the fix."""
    issues: list[str] = []
    git_dir = gitflow.git_path(ctx, "")
    if (git_dir / "rebase-merge").is_dir() or (git_dir / "rebase-apply").is_dir():
        issues.append("the clone is stuck mid-rebase")
    if profile.profile_dangling_hook_refs(ctx.claude_home, ctx.claude_home / "settings.json"):
        issues.append("settings.json runs a hook script that does not exist")
    eff = gitflow.effective_commit(ctx)
    for rel, mode in gitflow.tree_modes(ctx, eff).items():
        if mode == "100644" and gitflow.is_hook_path(rel) and gitflow.looks_executable(rel, ctx.repo / rel):
            issues.append("a hook script is tracked without the exec bit")
            break
    if machineconf.read_machine_tier(ctx.repo, ctx.hostname) == machineconf.TIER_FULL:
        project = machineconf.read_primary_project_path(ctx.repo, ctx.hostname)
        if project is not None and long_name_warning(project):
            issues.append("the project path gives a memory folder name that is too long")
    return issues


def pending_since(ctx: SyncCtx) -> int | None:
    """When the local sync branch was created (Unix time), or None when
    there is none. From its oldest reflog entry; without a reflog, the
    tip's commit time, which can only make the age look shorter."""
    if not gitflow.has_sync_branch(ctx):
        return None
    ref = f"refs/heads/{gitflow.sync_branch(ctx.hostname)}"
    result = gitflow._git(ctx, "reflog", "show", "--date=unix", "--format=%gd", ref, "--")
    times = re.findall(r"@\{(\d+)\}", result.stdout) if result.returncode == 0 else []
    if times:
        return int(times[-1])
    tip = gitflow._git(ctx, "log", "-1", "--format=%ct", ref, "--")
    out = tip.stdout.strip()
    return int(out) if tip.returncode == 0 and out.isdigit() else None


def tier_change(ctx: SyncCtx) -> str | None:
    """The tier in the conf, when the local tier record says the last run
    was on the other tier, else None."""
    if not machineconf.machine_conf_path(ctx.repo, ctx.hostname).is_file():
        return None
    tier = gitflow.current_tier(ctx)
    record = gitflow._tier_record(ctx)
    if tier == machineconf.TIER_BASIC and record != machineconf.TIER_BASIC:
        return tier
    if tier == machineconf.TIER_FULL and record in (machineconf.TIER_BASIC, gitflow._MERGED):
        return tier
    return None


def findings(ctx: SyncCtx, pr_days: int, now: float | None = None) -> list[str]:
    """Everything that needs attention, most urgent first, each a sentence
    that says what to do."""
    found: list[str] = []
    result = last_hook_result(ctx.claude_home)
    if result is not None and result[1] != 0:
        event, rc = result
        found.append(
            f"the last claude-config sync ({event}) failed with exit {rc}."
            f" See ~/.claude/{LOG_NAME}, fix the cause, then run 'claude-sync.sh sync'."
        )
    issues = quick_issues(ctx)
    if issues:
        found.append(f"'claude-sync.sh doctor' reports a problem: {issues[0]}. Run it for the fix.")
        found.extend(f"doctor: {issue}." for issue in issues[1:])
    since = pending_since(ctx) if pr_days > 0 else None
    if since is not None:
        age = (time.time() if now is None else now) - since
        if age > pr_days * _DAY:
            sb = gitflow.sync_branch(ctx.hostname)
            found.append(
                f"this machine's changes on {sb} have waited over {pr_days} days for their PR."
                " Merge or close it on GitHub, then run 'claude-sync.sh sync'."
            )
    tier = tier_change(ctx)
    if tier == machineconf.TIER_BASIC:
        found.append("the sync tier changed to basic; the next sync stops syncing memory here.")
    elif tier is not None:
        found.append(
            "the sync tier changed to full; memory is merged in without deleting."
            " Check the memory/ diff of this machine's next sync PR."
        )
    return found


def one_line(found: list[str]) -> str:
    """The first finding, plus how many more, as one printable line."""
    if not found:
        return ""
    line = _PREFIX + found[0]
    if len(found) > 1:
        line += f" ({len(found) - 1} more: run 'claude-sync.sh doctor'.)"
    line = printable(redact(line))
    if len(line) > _MAX_LINE:
        line = line[: _MAX_LINE - 3] + "..."
    return line


def status_line(ctx: SyncCtx, env: Mapping[str, str]) -> str:
    """The status line for this machine, "" when healthy. A check that
    fails becomes the line, so the failure is not silent."""
    try:
        return one_line(findings(ctx, pr_days_from_env(env)))
    except Fail as err:
        return one_line([f"the status check failed: {err.message} Run 'claude-sync.sh doctor'."])
    except OSError as err:
        return one_line([f"the status check could not read a file: {err}. Run 'claude-sync.sh doctor'."])
