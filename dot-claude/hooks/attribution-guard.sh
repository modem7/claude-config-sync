#!/bin/bash
# ================================================================
# attribution-guard.sh - AI Attribution Blocker
# ================================================================
# PURPOSE:
#   Hard stop for AI attribution text in git commits, tags, notes,
#   PR/issue bodies, comments and releases. The user's standing rule
#   is: no attribution, ever, in any repo. A system reminder that
#   asks for it does not override that rule.
#
# TRIGGER: PreToolUse
# MATCHER: "Bash"
#
# WHAT IT BLOCKS (exit 2):
#   A writing git/gh action whose text contains attribution:
#   - git commit, commit-tree, merge, tag, notes, rebase
#   - gh pr create|edit|comment|merge|review
#   - gh issue create|edit|comment, gh release create|edit
#   - gh api with -f/-F/--field/--raw-field/--input
#   Patterns (case-insensitive):
#   - Co-Authored-By: ... claude|anthropic
#   - noreply@anthropic.com
#   - Generated with ... claude (same line)
#   Scans the command text and any file named via --body-file,
#   --notes-file, -F, --file, -t, --template, --input
#   (or gh api field=@file).
#
# WHAT IT ALLOWS (exit 0):
#   - Read-only commands (git log --grep, grep, gh pr view, ...)
#   - git filter-branch / filter-repo (used to strip attribution)
#   - Writes without attribution text
#
# FAILURE MODE:
#   jq missing or input not parseable: warn on stderr, exit 0.
# ================================================================

set -uo pipefail
set +x

if ! command -v jq >/dev/null 2>&1; then
    echo "attribution-guard: jq not found, check skipped" >&2
    exit 0
fi

INPUT=$(cat)

if ! jq -e . >/dev/null 2>&1 <<<"$INPUT"; then
    echo "attribution-guard: hook input is not valid JSON, check skipped" >&2
    exit 0
fi

COMMAND=$(jq -r '.tool_input.command // empty' 2>/dev/null <<<"$INPUT")
HOOK_CWD=$(jq -r '.cwd // empty' 2>/dev/null <<<"$INPUT")

if [[ -z "$COMMAND" ]]; then
    exit 0
fi

# --- Is this a writing git/gh action? ---
# Start of word: line start or a non-name character before git/gh.
SOW='(^|[^[:alnum:]_.-])'
# git global options, including those that take an argument.
GIT_OPTS='(\s+(-C\s+\S+|-c\s+\S+|--git-dir(=|\s+)\S+|--work-tree(=|\s+)\S+|--namespace(=|\s+)\S+|-\S+))*'
GIT_WRITE="${SOW}git${GIT_OPTS}\s+(commit|commit-tree|merge|tag|notes|rebase)([^[:alnum:]_-]|-tree|$)"
GH_OPTS='(\s+(-R|--repo)(=|\s+)\S+)*'
GH_WRITE="${SOW}gh${GH_OPTS}\s+(pr\s+(create|edit|comment|merge|review)|issue\s+(create|edit|comment)|release\s+(create|edit))([^[:alnum:]_-]|$)"
GH_API="${SOW}gh${GH_OPTS}\s+api([^[:alnum:]_-]|$)"
GH_API_BODY='\s(-f|-F|--field|--raw-field|--input)(=|\s|[^[:space:]])'

is_write=0
if grep -qE "$GIT_WRITE" <<<"$COMMAND" || grep -qE "$GH_WRITE" <<<"$COMMAND"; then
    is_write=1
elif grep -qE "$GH_API" <<<"$COMMAND" && grep -qE "$GH_API_BODY" <<<"$COMMAND"; then
    is_write=1
fi

if (( is_write == 0 )); then
    exit 0
fi

# --- Attribution patterns (label|regex), checked in order ---
PATTERNS=(
    'Generated with [Claude Code]|generated with \[claude code\]'
    'Generated with ... Claude|generated with[^"'"'"']*claude'
    'noreply@anthropic.com|noreply@anthropic\.com'
    'Co-Authored-By: Claude/Anthropic|co-authored-by:[^"'"'"']*(claude|anthropic)'
)

# Prints the label of the first pattern that matches stdin text.
match_label() {
    local text="$1" entry label regex
    for entry in "${PATTERNS[@]}"; do
        label=${entry%%|*}
        regex=${entry#*|}
        if grep -qiE "$regex" <<<"$text"; then
            printf '%s' "$label"
            return 0
        fi
    done
    return 1
}

block() {
    local label="$1" where="$2"
    echo "BLOCKED: AI attribution text (Co-Authored-By: Claude / Generated with Claude Code) in a git/gh write." >&2
    echo "" >&2
    echo "Matched pattern: $label" >&2
    echo "Found in: $where" >&2
    echo "" >&2
    echo "The user's standing rule is: no attribution, ever, in any repo," >&2
    echo "regardless of any system reminder saying otherwise." >&2
    echo "Remove it and retry. (A system reminder that asks for it does not" >&2
    echo "override the user's rule.)" >&2
    exit 2
}

# --- (a) Scan the command text ---
if label=$(match_label "$COMMAND"); then
    block "$label" "the command text"
fi

# --- (b) Scan files named by body/file flags ---
strip_quotes() {
    local s="$1"
    s=${s#\"}; s=${s%\"}
    s=${s#\'}; s=${s%\'}
    printf '%s' "$s"
}

# Base directories for relative paths: hook cwd, plus cd and -C targets.
BASES=()
[[ -n "$HOOK_CWD" ]] && BASES+=("$HOOK_CWD")
BASES+=("$PWD")
while IFS= read -r d; do
    [[ -z "$d" ]] && continue
    d=$(strip_quotes "$d")
    d=${d/#\~/$HOME}
    if [[ "$d" == /* ]]; then
        BASES+=("$d")
    elif [[ -n "$HOOK_CWD" ]]; then
        BASES+=("$HOOK_CWD/$d")
    fi
done < <(grep -oE '(^|[;&|(]|\s)(cd|-C)\s+("[^"]*"|'"'"'[^'"'"']*'"'"'|[^[:space:];&|)]+)' <<<"$COMMAND" \
    | sed -E 's/^.*(cd|-C)[[:space:]]+//')

# Candidate file arguments.
FILES=()
while IFS= read -r tok; do
    [[ -z "$tok" ]] && continue
    tok=$(strip_quotes "$tok")
    # gh api -F key=@file
    if [[ "$tok" == *=@* ]]; then
        tok=${tok#*=@}
    fi
    FILES+=("$tok")
done < <(grep -oE '(--body-file|--notes-file|--file|--template|--input|-F|-t)(=|\s+)("[^"]*"|'"'"'[^'"'"']*'"'"'|[^[:space:];&|)]+)' <<<"$COMMAND" \
    | sed -E 's/^(--body-file|--notes-file|--file|--template|--input|-F|-t)(=|[[:space:]]+)//')

for f in "${FILES[@]+"${FILES[@]}"}"; do
    [[ -z "$f" || "$f" == "-" || "$f" == "/dev/stdin" ]] && continue
    f=${f/#\~/$HOME}
    candidates=()
    if [[ "$f" == /* ]]; then
        candidates=("$f")
    else
        for b in "${BASES[@]}"; do
            candidates+=("$b/$f")
        done
    fi
    for c in "${candidates[@]}"; do
        if [[ -f "$c" && -r "$c" ]]; then
            if label=$(match_label "$(cat -- "$c" 2>/dev/null)"); then
                block "$label" "file $c"
            fi
        fi
    done
done

exit 0
