#!/bin/bash
# ================================================================
# branch-guard.sh — Branch Push Protector
# ================================================================
# PURPOSE:
#   Prevents accidental git push to main/master branches AND
#   blocks force-push on ALL branches without explicit approval.
#
#   Force-pushes rewrite history and can destroy teammates' work.
#   Protected branch pushes bypass code review.
#
# TRIGGER: PreToolUse
# MATCHER: "Bash"
#
# WHAT IT BLOCKS (exit 2):
#   - git push origin main/master (any protected branch)
#   - git push --force (any branch — history rewriting)
#   - git push -f (short flag variant)
#   - git push --force-with-lease (still destructive)
#
# WHAT IT ALLOWS (exit 0):
#   - git push origin feature-branch (non-force)
#   - git push -u origin feature-branch
#   - All other git commands
#   - All non-git commands
#
# CONFIGURATION:
#   CC_PROTECT_BRANCHES — colon-separated list of protected branches
#     default: "main:master"
#   CC_ALLOW_FORCE_PUSH=1 — disable force-push protection
# ================================================================

INPUT=$(cat)
COMMAND=$(printf '%s' "$INPUT" | jq -r '.tool_input.command // empty' 2>/dev/null)

if [[ -z "$COMMAND" ]]; then
    exit 0
fi

# Only check git push commands
if ! printf '%s' "$COMMAND" | grep -qE '^\s*git\s+push'; then
    exit 0
fi

# --- Check 1: Force push on ANY branch ---
if [[ "${CC_ALLOW_FORCE_PUSH:-0}" != "1" ]]; then
    if printf '%s' "$COMMAND" | grep -qE 'git\s+push\s+.*(-f\b|--force\b|--force-with-lease\b)'; then
        echo "BLOCKED: Force push detected." >&2
        echo "" >&2
        echo "Command: $COMMAND" >&2
        echo "" >&2
        echo "Force push rewrites remote history and can destroy" >&2
        echo "other people's work. This is almost never what you want." >&2
        echo "" >&2
        echo "If you truly need to force push, set CC_ALLOW_FORCE_PUSH=1" >&2
        exit 2
    fi
fi

# --- Check 2: Push to protected branches ---
PROTECTED="${CC_PROTECT_BRANCHES:-main:master}"

BLOCKED=0
IFS=':' read -ra BRANCHES <<< "$PROTECTED"
for branch in "${BRANCHES[@]}"; do
    if printf '%s' "$COMMAND" | grep -qwE "origin\s+${branch}|${branch}\s|${branch}$"; then
        BLOCKED=1
        break
    fi
done

if (( BLOCKED == 1 )); then
    echo "BLOCKED: Attempted push to protected branch." >&2
    echo "" >&2
    echo "Command: $COMMAND" >&2
    echo "" >&2
    echo "Protected branches: $PROTECTED" >&2
    echo "" >&2
    echo "Push to a feature branch first, then create a pull request." >&2
    exit 2
fi

exit 0
