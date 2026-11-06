# Global Instructions

These apply across all projects and sessions, regardless of which directory Claude Code is launched from.

## Check repo state before starting any task

Before exploring code or writing any implementation in a git repository, check:
- `git status`
- the current branch name
- whether that branch is stale or already merged (`gh pr list --head <branch> --state all`)
- whether the local default branch is behind `origin`

Do this as the first step of the task, not after implementing something. If the checked-out branch is stale or unrelated to the task at hand, switch to a fresh branch off an up-to-date default branch immediately — before writing any code — rather than discovering the problem at commit/push time and having to rework.

## Scratch files and cleanup

Put temporary files (commit messages, PR bodies, intermediate output, test scripts) in the session scratchpad directory, never in the project folder or the repo. If the system prompt gives no scratchpad, use a new `mktemp -d` directory. Use the Write tool and pass the path with `git commit -F` or `gh pr create --body-file`.

Delete safe temporary files you created before ending the task. Do not work around a hook that blocks a delete: leave the file and tell the user its full path.

After a PR merges, remove its worktree and local branch (`git worktree remove`, `git branch -d`) and check that the remote branch is gone. Do not delete other branches unless you have checked they are merged and the user agrees. A dedicated cleanup agent is not needed for this.

## Token usage (recommended)

Match effort to task complexity and keep tool output lean — see global memory `feedback_token_optimisation.md` for the full list.
Delegate broad repo searches and full test runs to the Haiku subagents (`search`, `audit`); do small lookups directly.

## Agents for common jobs

Use these instead of doing the work inline when the job matches. Each runs on the cheapest model that does it well.

- `search` (Haiku): broad repo lookups, returns paths and `file:line` only.
- `audit` (Haiku): check a diff and run tests before a commit or PR.
- `script-writer` (Opus): new scripts, rewrites and hook scripts.
- `pr-shepherd` (Haiku): branch to open PR, then CI state.
- `ci-debugger` (Sonnet): root cause of a failed pipeline. Read-only.
- `renovate-triage` (Sonnet): sort Renovate PRs into safe or needs a look.
- `shell-reviewer` (Sonnet): review bash and hook scripts. Review only.
- `docs-sync` (Sonnet): after a change, check every README, instructions file and doc in the repo agrees. Works on any repo. Run before opening a PR.
- `config-sync-checker` (Haiku): run before `claude-sync.sh pull` or `sync`.
- `research` (Sonnet): sourced web and GET-only API lookups.

Machine-specific agents live in the machine profile and are not listed here.

## Scripts are written by Opus

The default model is Sonnet. When a task needs a new script, a full rewrite, or a non-trivial change to a script (bash, Python, PowerShell, hook scripts), hand that part to the `script-writer` agent, which runs on Opus. Give it the real files and the goal. Then check what it wrote. Do not write scripts yourself on Sonnet. Single-line edits and config files stay with the main session.
