#!/bin/bash
# Radio briefing injector for kimi-code (UserPromptSubmit hook).
#
# kimi has no --append-system-prompt flag, and its SessionStart hook stdout
# is NOT added to context (verified by probe), while UserPromptSubmit stdout
# is. So the opening briefing rides the first prompt submitted in a
# radio-launched session — the agent sees briefing + first message together.
#
# No-op for non-radio sessions (RADIO_HANDLE is only set by `radio join`).
# Fires once per kimi session id; a resumed session already carries the
# briefing in its recorded context.
[ -z "$RADIO_HANDLE" ] && exit 0

payload=$(cat)
event=$(printf "%s" "$payload" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("hook_event_name",""))' 2>/dev/null)
[ "$event" = "UserPromptSubmit" ] || exit 0

session_id=$(printf "%s" "$payload" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("session_id",""))' 2>/dev/null)
[ -n "$session_id" ] || exit 0

STATE="${RADIO_HOME:-$HOME/.local/share/herdr-radio}/kimi-briefed"
mkdir -p "$STATE"
[ -f "$STATE/$session_id" ] && exit 0

DIR="$(cd "$(dirname "$0")" && pwd)"
"$DIR/radio" briefing "$RADIO_HANDLE" && touch "$STATE/$session_id"
exit 0
