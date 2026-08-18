#!/bin/bash
INPUT=$(cat)
REASON=$(printf '%s' "$INPUT" | jq -r '.stop_reason // "unknown"' 2>/dev/null)
HOOK_EVENT=$(printf '%s' "$INPUT" | jq -r '.hook_event_name // ""' 2>/dev/null)
if [[ "$REASON" == "user" || "$REASON" == "normal" || -z "$REASON" ]]; then
    exit 0
fi
LOG="${CC_ERROR_ALERT_LOG:-$HOME/.claude/session-errors.log}"
MISSION="${CC_CONTEXT_MISSION_FILE:-$HOME/mission.md}"
TS=$(date -Iseconds)
mkdir -p "$(dirname "$LOG")" 2>/dev/null
echo "[$TS] Session stopped: reason=$REASON event=$HOOK_EVENT" >> "$LOG"
if [ -z "$WSL_DISTRO_NAME" ]; then
    notify-send "Claude Code" "Session stopped: $REASON" 2>/dev/null || true
    osascript -e "display notification \"Session stopped: $REASON\" with title \"Claude Code\"" 2>/dev/null || true
else
    powershell.exe -Command "Write-Host 'Claude Code: Session stopped - $REASON'" 2>/dev/null || true
fi
exit 0
