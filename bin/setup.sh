#!/bin/sh
# Install the Radio view dependencies (Textual) into a plugin-local venv.
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if [ -x .venv/bin/python ]; then
  echo "radio view ready: $ROOT/.venv (existing)"
elif command -v uv >/dev/null 2>&1; then
  uv venv .venv
  uv pip install --python .venv/bin/python "textual>=1.0"
  echo "radio view ready: $ROOT/.venv"
else
  python3 -m venv .venv
  .venv/bin/pip install "textual>=1.0"
  echo "radio view ready: $ROOT/.venv"
fi

# kimi-code briefing hook: kimi has no append-system-prompt flag, so the
# briefing is injected by a UserPromptSubmit hook (stdout lands in context).
# The hook no-ops unless RADIO_HANDLE is set (i.e. radio-launched sessions).
# Skipped entirely when kimi is not installed — no config files are created
# for providers the user does not have.
if command -v kimi >/dev/null 2>&1; then
  KIMI_CONFIG="${KIMI_CODE_HOME:-$HOME/.kimi-code}/config.toml"
  HOOK_LINE="command = \"$ROOT/bin/radio-kimi-hook.sh\""
  if [ -f "$KIMI_CONFIG" ] && grep -qF "radio-kimi-hook.sh" "$KIMI_CONFIG"; then
    echo "kimi hook already installed"
  else
    mkdir -p "$(dirname "$KIMI_CONFIG")"
    touch "$KIMI_CONFIG"
    printf '\n[[hooks]]\nevent = "UserPromptSubmit"\n%s\n' "$HOOK_LINE" >> "$KIMI_CONFIG"
    echo "kimi hook installed into $KIMI_CONFIG"
  fi
else
  echo "kimi not found; skipping kimi hook"
fi

# Gemini CLI briefing hook: SessionStart additionalContext (documented
# injection channel). Merged into ~/.gemini/settings.json idempotently.
# Skipped when gemini is not installed.
if command -v gemini >/dev/null 2>&1; then
  GEMINI_SETTINGS="${GEMINI_CLI_HOME:-$HOME/.gemini}/settings.json"
  mkdir -p "$(dirname "$GEMINI_SETTINGS")"
  python3 - "$GEMINI_SETTINGS" "$ROOT/bin/radio-gemini-hook.sh" <<'PYEOF'
import json
import sys

path, command = sys.argv[1], sys.argv[2]
try:
    with open(path, encoding="utf-8") as fh:
        settings = json.load(fh)
except (OSError, json.JSONDecodeError):
    settings = {}
hooks = settings.setdefault("hooks", {})
entries = hooks.setdefault("SessionStart", [])
for entry in entries:
    for hook in entry.get("hooks", []):
        if "radio-gemini-hook.sh" in str(hook.get("command", "")):
            print("gemini hook already installed")
            sys.exit(0)
for matcher in ("startup", "resume", "clear"):
    entries.append({
        "matcher": matcher,
        "hooks": [{"name": "radio-briefing", "type": "command", "command": command}],
    })
with open(path, "w", encoding="utf-8") as fh:
    json.dump(settings, fh, indent=2)
    fh.write("\n")
print(f"gemini hook installed into {path}")
PYEOF
else
  echo "gemini not found; skipping gemini hook"
fi
