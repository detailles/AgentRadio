#!/bin/sh
# Install the Radio view dependencies (Textual) into a venv under the plugin
# state dir (~/.local/share/herdr-radio/venv). The view is the ONLY component
# needing third-party packages — the CLI and relay are stdlib-only — so a
# failure here must not fail the plugin install: warn and continue; the view
# lights up after a manual `sh bin/setup.sh`.
# The venv deliberately lives outside the plugin dir: a running view pane must
# never hold the managed plugin directory (Windows then refuses updates).
# No `set -e`: failures are handled explicitly per section.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STATE="${RADIO_HOME:-$HOME/.local/share/herdr-radio}"
VENV="$STATE/venv"
mkdir -p "$STATE"
cd "$ROOT"

# Make the CLI reachable right after install (see bin/link-cli.sh).
sh "$ROOT/bin/link-cli.sh"

# A pre-0.3.1 venv lived inside the plugin dir; drop it (best effort).
if [ -d "$ROOT/.venv" ]; then
  rm -rf "$ROOT/.venv" 2>/dev/null || true
fi

view_ready() {
  [ -x "$VENV/bin/python" ] && "$VENV/bin/python" -c "import textual" >/dev/null 2>&1
}

if view_ready; then
  echo "radio view ready: $VENV (existing)"
else
  rm -rf "$VENV"  # a partial venv would short-circuit the readiness check above
  if command -v uv >/dev/null 2>&1; then
    uv venv "$VENV" && uv pip install --python "$VENV/bin/python" "textual>=1.0"
  else
    python3 -m venv "$VENV" && "$VENV/bin/pip" install "textual>=1.0"
  fi
  if view_ready; then
    echo "radio view ready: $VENV"
  else
    rm -rf "$VENV"
    echo "WARNING: radio view deps not installed (need python3-venv + pip/uv + PyPI access)." >&2
    echo "  radio CLI and relay still work; the view activates after: sh $ROOT/bin/setup.sh" >&2
  fi
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
