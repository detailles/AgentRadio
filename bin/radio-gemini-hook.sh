#!/bin/bash
# Radio briefing injector for Gemini CLI (SessionStart hook).
#
# Gemini has no --append-system-prompt flag, but its SessionStart hook
# supports documented context injection via additionalContext JSON on stdout.
# No-op (empty JSON) outside radio-launched sessions (RADIO_HANDLE is only
# set by `radio join`).
if [ -z "$RADIO_HANDLE" ]; then
  echo "{}"
  exit 0
fi
DIR="$(cd "$(dirname "$0")" && pwd)"
briefing=$("$DIR/radio" briefing "$RADIO_HANDLE") || { echo "{}"; exit 0; }
printf "%s" "$briefing" | python3 -c '
import json, sys
print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": sys.stdin.read(),
    }
}))
'
exit 0
