---
name: standup
description: Use when the user asks for a standup, daily update, "what did I do yesterday", or a short summary of recent GitLab activity and what we have worked on together. Produces a ~30 second spoken summary.
---

# Standup

Produce a short summary the user can say aloud in about 30 seconds (roughly 70-90 words).

## Steps

1. Run `~/.claude/skills/standup/gitlab-activity.sh` (optional `--since YYYY-MM-DD`; default is the previous working day). It prints GitLab pushes, MRs, issues, open MRs and reviews waiting.
2. Pull in what we discussed and did together from this conversation. If the conversation is fresh or thin, also check recent session transcripts with `mcp__ccd_session_mgmt__search_session_transcripts` / `list_sessions`, and the memory directory, for work since the same date. Do not invent anything.
3. Merge the two sources. The same work often shows up in both; mention it once.

## Output format

Three short bullets, first person, plain language, no links or IDs unless the user asks:

- **Yesterday:** the 2-4 things that mattered (merged, shipped, fixed, investigated).
- **Today:** what is next, inferred from open MRs and unfinished threads. Mark guesses with "likely".
- **Blockers:** only real ones (waiting on review, failing pipeline, decision needed). Write "None" if none.

## Rules

- Only report work that is assigned to the user, closed or merged by them, or worked on by them. Never list MRs where they are just a reviewer, approver or commenter. The script already filters this way; keep it that way when summarising.
- If the user says an item isn't theirs (GitLab can list them as assignee for work they don't own), leave it out for the rest of the session.

- Outcomes over activity: "fixed the failing pipeline in ssc_tools", not "pushed 6 commits".
- Skip trivia (typo fixes, config sync, comment-only activity) unless it was the main work.
- If the GitLab script fails, say so in one line and summarise from the conversation alone.
- Offer one-line tweaks afterwards (shorter, more detail, different date). Do not ask questions first; give the draft.
