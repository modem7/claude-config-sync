#!/bin/bash
# ================================================================
# comment-strip.sh — Strip bash comments that break permissions
# ================================================================
# PURPOSE:
#   Claude Code sometimes adds comments to bash commands like:
#     # Check the diff
#     git diff HEAD~1
#   This breaks permission allowlists (e.g. Bash(git:*)) because
#   the matcher sees "# Check the diff" instead of "git diff".
#
#   This hook strips only the LEADING run of comment/blank lines and
#   returns the clean command via updatedInput, so permissions match
#   correctly.
#
# TRIGGER: PreToolUse
# MATCHER: "Bash"
#
# INCIDENT: GitHub Issue #29582 (18 reactions)
#   Users on linux/vscode report that bash comments added by Claude
#   cause permission prompts even when the command is allowlisted.
#
# FIX (2026-07-30): the original implementation used
# `sed '/^[[:space:]]*#/d; /^[[:space:]]*$/d'`, which deletes EVERY
# #-prefixed line and EVERY blank line anywhere in the command, not just
# a leading comment. That silently mangled any multi-line heredoc content
# containing markdown headers (##) or paragraph breaks — e.g. a
# `gh pr create --body "$(cat <<'EOF' ... EOF)"` call lost its `##`
# section headers with no error. Rewritten to stop stripping at the first
# real (non-comment, non-blank) line, leaving everything from that point
# on — including any #-lines or blank lines further into a heredoc —
# completely untouched.
#
# HOW IT WORKS:
#   - Reads the command from tool_input
#   - Strips only a leading contiguous block of comment/blank lines
#   - Returns updatedInput with the cleaned command
#   - Uses hookSpecificOutput.permissionDecision = "allow" only if
#     the command was modified (so it doesn't override other hooks)
# ================================================================

INPUT=$(cat)
COMMAND=$(printf '%s' "$INPUT" | jq -r '.tool_input.command // empty' 2>/dev/null)

if [[ -z "$COMMAND" ]]; then
    exit 0
fi

# Strip only a leading contiguous block of comment/blank lines - stop at
# the first real command line so #-lines or blank lines later in the
# command (e.g. inside a heredoc) are left untouched.
CLEAN=$(printf '%s' "$COMMAND" | awk '
  !seen && (/^[[:space:]]*#/ || /^[[:space:]]*$/) { next }
  { seen=1; print }
')

# If nothing changed, pass through
if [[ "$CLEAN" == "$COMMAND" ]]; then
    exit 0
fi

# If command is empty after stripping, don't modify
if [[ -z "$CLEAN" ]]; then
    exit 0
fi

# Return cleaned command via hookSpecificOutput
# permissionDecision is not set — let the normal permission flow handle it
# We only modify the input so the permission matcher sees the real command
jq -n --arg cmd "$CLEAN" '{
  hookSpecificOutput: {
    hookEventName: "PreToolUse",
    updatedInput: {
      command: $cmd
    }
  }
}'
