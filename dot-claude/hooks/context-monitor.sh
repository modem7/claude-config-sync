#!/bin/bash
# ================================================================
# context-monitor.sh — Context Size Monitor
# ================================================================
# PURPOSE:
#   Warns Claude when a session's context grows large, so long
#   sessions get wrapped up or handed off before they get expensive
#   or hit auto-compaction mid-task.
#
# HOW IT WORKS:
#   1. Reads the hook's JSON input (session_id, transcript_path)
#   2. Takes the token usage of the last assistant message in the
#      transcript: input + cache read + cache creation + output.
#      That is the context the next request will re-read.
#   3. Compares it with absolute token thresholds, not a % of the
#      window. With a 1M window, a % rule first warns at ~600k used,
#      long after the session has become expensive.
#   4. Tells Claude through hookSpecificOutput.additionalContext.
#      Plain stdout from a PostToolUse hook only reaches the debug
#      log, so Claude never saw the old warnings.
#   5. State is kept per session, so one session never affects
#      another.
#   6. At CRITICAL, writes an evacuation template to the mission
#      file so state can be handed off before compaction.
#
# TRIGGER: PostToolUse (all tools)
# MATCHER: "" (empty = every tool invocation)
#
# CONFIGURATION (environment variables, all optional):
#   CC_CONTEXT_CAUTION   — tokens, default 120000
#   CC_CONTEXT_WARNING   — tokens, default 180000
#   CC_CONTEXT_CRITICAL  — tokens, default 225000
#   CC_CONTEXT_MISSION_FILE — default $HOME/mission.md
#   Defaults sit below CLAUDE_CODE_AUTO_COMPACT_WINDOW=300000.
#
# LEVELS:
#   CAUTION   — topic done? suggest /clear or a new session
#   WARNING   — finish the current step, then write a handoff
#   CRITICAL  — write the handoff now, before auto-compaction
#
# BORN FROM:
#   A session that hit 3% context remaining with no warning.
#   Rewritten after the old version was found stuck at EMERGENCY:
#   ~/.claude/debug did not exist, so it fell back to a tool-call
#   counter shared by every session in /tmp.
# ================================================================

CAUTION="${CC_CONTEXT_CAUTION:-120000}"
WARNING="${CC_CONTEXT_WARNING:-180000}"
CRITICAL="${CC_CONTEXT_CRITICAL:-225000}"
MISSION_FILE="${CC_CONTEXT_MISSION_FILE:-$HOME/mission.md}"
# Private per-user dir, never shared /tmp: another user could pre-create
# it or plant values that reach bash arithmetic.
STATE_DIR="${XDG_RUNTIME_DIR:-$HOME/.cache}/cc-context-monitor"
is_num() { [[ "$1" =~ ^[0-9]+$ ]]; }

INPUT=$(cat)

# Prints "<session_id> <tokens>" or nothing.
read -r SESSION_ID TOKENS < <(printf '%s' "$INPUT" | python3 -I -c '
import json, os, sys
try:
    d = json.load(sys.stdin)
    sid = d.get("session_id") or ""
    path = d.get("transcript_path") or ""
    if not sid or not os.path.isfile(path):
        sys.exit(0)
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - 2_000_000))
        lines = f.read().decode("utf-8", "replace").splitlines()
    for line in reversed(lines):
        if "\"usage\"" not in line:
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("type") != "assistant" or e.get("isSidechain"):
            continue
        u = (e.get("message") or {}).get("usage") or {}
        t = sum(int(u.get(k) or 0) for k in (
            "input_tokens", "cache_read_input_tokens",
            "cache_creation_input_tokens", "output_tokens"))
        if t:
            print(sid, t)
        break
except Exception:
    pass
' 2>/dev/null)

[ -n "${SESSION_ID:-}" ] && is_num "${TOKENS:-}" || exit 0
for v in "$CAUTION" "$WARNING" "$CRITICAL"; do is_num "$v" || exit 0; done

mkdir -p -m 700 "$STATE_DIR" 2>/dev/null || exit 0
# Refuse a symlinked or foreign-owned state dir.
[ -d "$STATE_DIR" ] && [ ! -L "$STATE_DIR" ] && [ -O "$STATE_DIR" ] || exit 0
STATE_FILE="$STATE_DIR/${SESSION_ID//[^a-zA-Z0-9_-]/}"
[ -L "$STATE_FILE" ] && exit 0
LAST_LEVEL=$(cat "$STATE_FILE" 2>/dev/null || echo 0)
is_num "$LAST_LEVEL" || LAST_LEVEL=0

if   [ "$TOKENS" -ge "$CRITICAL" ]; then LEVEL=3
elif [ "$TOKENS" -ge "$WARNING" ];  then LEVEL=2
elif [ "$TOKENS" -ge "$CAUTION" ];  then LEVEL=1
else LEVEL=0
fi

# After /clear or compaction the level drops; record it so the
# warnings can fire again later in the same session.
echo "$LEVEL" > "$STATE_FILE"

# Only speak when the level goes up.
[ "$LEVEL" -gt "$LAST_LEVEL" ] || exit 0

K=$((TOKENS / 1000))
TIMESTAMP=$(date '+%Y-%m-%d %H:%M')

# --- Evacuation template (with cooldown to prevent spam) ---
EVAC_COOLDOWN_FILE="$STATE_DIR/evac-last"
EVAC_COOLDOWN_SEC=1800  # 30 min cooldown between template generations

generate_evacuation_template() {
    local level="$1"

    if [ -f "$EVAC_COOLDOWN_FILE" ]; then
        local last_ts now_ts
        last_ts=$(cat "$EVAC_COOLDOWN_FILE" 2>/dev/null || echo 0)
        is_num "$last_ts" || last_ts=0
        now_ts=$(date +%s)
        if [ $((now_ts - last_ts)) -lt "$EVAC_COOLDOWN_SEC" ]; then
            return
        fi
    fi

    # Don't add a new template if there's already an unfilled one
    if [ -f "$MISSION_FILE" ] && grep -q '\[TODO\]' "$MISSION_FILE" 2>/dev/null; then
        return
    fi

    date +%s > "$EVAC_COOLDOWN_FILE"
    mkdir -p "$(dirname "$MISSION_FILE")"

    cat >> "$MISSION_FILE" << EVAC_EOF

## Context Evacuation Template (${level} - ${TIMESTAMP})
<!-- Auto-generated by context-monitor.sh. Fill in before /compact -->
### Current Task
- Task: [TODO]
- Progress: [TODO]
- Files being edited: [TODO]

### Git State
- Branch: [TODO]
- Uncommitted changes: [TODO]

### Next Action
- Next command/action: [TODO]
EVAC_EOF
}

case "$LEVEL" in
    1) MSG="CONTEXT CAUTION: about ${K}k tokens in context. Every turn re-reads all of it. If the current topic is finished, suggest /clear or a new session to the user before starting anything unrelated." ;;
    2) MSG="CONTEXT WARNING: about ${K}k tokens in context. Finish the current step, then write a handoff (state, decisions, next steps) and suggest continuing in a new session. Do not start new large work here." ;;
    3) generate_evacuation_template "CRITICAL"
       MSG="CONTEXT CRITICAL: about ${K}k tokens in context; auto-compaction is close. Stop and write the handoff now. An evacuation template is in ${MISSION_FILE}. Then tell the user to continue in a new session." ;;
esac

python3 -I -c 'import json,sys; print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": sys.argv[1]}}))' "$MSG"
exit 0
