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

## Scripts are written by Opus

The default model is Sonnet. When a task needs a new script, a full rewrite, or a non-trivial change to a script (bash, Python, PowerShell, hook scripts), hand that part to the `script-writer` agent, which runs on Opus. Give it the real files and the goal. Then check what it wrote. Do not write scripts yourself on Sonnet. Single-line edits and config files stay with the main session.
