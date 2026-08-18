# Global Instructions

These apply across all projects and sessions, regardless of which directory Claude Code is launched from.

## Check repo state before starting any task

Before exploring code or writing any implementation in a git repository, check:
- `git status`
- the current branch name
- whether that branch is stale or already merged (`gh pr list --head <branch> --state all`)
- whether the local default branch is behind `origin`

Do this as the first step of the task, not after implementing something. If the checked-out branch is stale or unrelated to the task at hand, switch to a fresh branch off an up-to-date default branch immediately — before writing any code — rather than discovering the problem at commit/push time and having to rework.

## Token usage (recommended)

Match effort to task complexity and keep tool output lean — see global memory `feedback_token_optimisation.md` for the full list.
