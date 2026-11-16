---
name: batch-run
description: Use when an investigation needs several read-only shell commands (git/gh status, log triage, listing, grepping, CI or container checks) or output that would be long or noisy. Runs them in one Bash call, in parallel, and returns a compact trimmed summary. Emulates Claude Code's Code Mode token saving for shell work.
---

# Batch run

One Bash call instead of many, and only the useful part of the output reaches context. Use it for read-only shell investigation. It does not replace Read, Edit, MCP tools or subagents.

## When to use

- Two or more independent read-only commands whose results you need together (repo state checks, `gh pr list` plus `git log`, several `docker`/`systemctl` status calls).
- A command whose raw output is long or noisy (CI logs, `docker logs`, big JSON from an API). Trim it before it enters context.

Skip it for a single short command, for anything that edits or deletes, and when a dedicated tool (Read, Grep, Glob) does the job.

## How

```bash
python3 ~/.agents/skills/batch-run/batch.py [options] -- 'git status -sb' -- 'git log --oneline -5' -- 'gh pr list --state all --limit 5'
```

Or put one command per line in a file (a line may be `label=>command`) and pass `-f FILE`; use the scratchpad for the file.

| Option | Default | Effect |
|---|---|---|
| `--max-lines N` | 20 | Lines kept per command |
| `--max-chars N` | 2000 | Characters kept per command |
| `--tail` | off | Keep the last lines instead of the first (logs) |
| `--json-keys` | off | For JSON output, show keys and sizes instead of the body |
| `--timeout S` | 60 | Per-command limit |
| `--jobs N` | 8 | Parallel commands |
| `--cwd DIR` | current | Working directory |

Each command prints a header `== [i] label (exit N, secs) ==` followed by its trimmed output, in the order given. The script always exits 0 unless the usage is wrong, so read failures from the headers. A command that hits `--timeout` shows `TIMEOUT` in its header instead of an exit code.

## Tips

- Filter in the command itself when you know what you want (`| grep -E 'ERROR|FAIL'`, `jq '.items[].name'`); the script trims whatever is left.
- Start narrow. If a header shows `... N more lines omitted` and you need them, re-run only that command with a larger `--max-lines` or a better filter.
- Do not put secrets on the command line.

## Safety

The script refuses commands that look like they change things (`rm`, `mv`, `sudo`, `git push|commit|reset`, `gh ... create|merge|close`, `sed -i`, file redirects, and similar) and runs nothing in that case. That is a guard against accidents, not a sandbox, and it does not replace the permission rules and hooks that apply to each separate tool call. Do not use `--allow-write` unless the user has approved the specific commands.
