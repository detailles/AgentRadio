#!/bin/sh
# Install the Radio view dependencies (Textual) into a venv under the plugin
# state dir (~/.local/share/herdr-radio/venv). The view is the ONLY component
# needing third-party packages — the CLI and relay are stdlib-only — so a
# failure here must not fail the plugin install: warn and continue; the view
# lights up after a manual `sh bin/setup.sh`.
# The venv deliberately lives outside the plugin dir: a running view pane must
# never hold the managed plugin directory (Windows then refuses updates).
# No `set -e`: failures are handled explicitly per section, and the script
# always exits 0 — a build hook must never fail the install.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STATE="${RADIO_HOME:-$HOME/.local/share/herdr-radio}"
VENV="$STATE/venv"
mkdir -p "$STATE"
cd "$ROOT"

# Make the CLI reachable right after install (see bin/link-cli.sh).
sh "$ROOT/bin/link-cli.sh"

# A pre-0.3.1 venv lived inside the managed plugin dir; drop it (best effort).
# A developer clone keeps its own venv: only the managed copy is cleaned.
case "$ROOT" in
  */herdr/plugins/*)
    if [ -d "$ROOT/.venv" ]; then
      rm -rf "$ROOT/.venv" 2>/dev/null || true
    fi
    ;;
  *)
    if [ -d "$ROOT/.venv" ]; then
      echo "note: $ROOT/.venv is a development venv; left in place." >&2
    fi
    ;;
esac

view_ready() {
  [ -x "$VENV/bin/python" ] && "$VENV/bin/python" -c "import textual" >/dev/null 2>&1
}

if view_ready; then
  echo "radio view ready: $VENV (existing)"
else
  rm -rf "$VENV"  # a partial venv would short-circuit the readiness check above
  if command -v uv >/dev/null 2>&1; then
    uv venv "$VENV" && uv pip install --python "$VENV/bin/python" "textual>=1.0"
  fi
  # A broken uv must not disable the view path: fall back to the stdlib venv.
  if ! view_ready && command -v python3 >/dev/null 2>&1; then
    rm -rf "$VENV"
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
    mkdir -p "$(dirname "$KIMI_CONFIG")" 2>/dev/null
    touch "$KIMI_CONFIG" 2>/dev/null
    if printf '\n[[hooks]]\nevent = "UserPromptSubmit"\n%s\n' "$HOOK_LINE" >> "$KIMI_CONFIG" 2>/dev/null; then
      echo "kimi hook installed into $KIMI_CONFIG"
    else
      echo "WARNING: could not write the kimi hook to $KIMI_CONFIG" >&2
    fi
  fi
else
  echo "kimi not found; skipping kimi hook"
fi

# Gemini CLI briefing hook: SessionStart additionalContext (documented
# injection channel). Merged into ~/.gemini/settings.json idempotently.
# Skipped when gemini is not installed, and a settings file that cannot be
# parsed is reported and left untouched — the user's own config always wins.
if command -v gemini >/dev/null 2>&1; then
  GEMINI_SETTINGS="${GEMINI_CLI_HOME:-$HOME/.gemini}/settings.json"
  mkdir -p "$(dirname "$GEMINI_SETTINGS")" 2>/dev/null
  python3 - "$GEMINI_SETTINGS" "$ROOT/bin/radio-gemini-hook.sh" <<'PYEOF' || echo "WARNING: gemini hook not installed" >&2
import json
import os
import sys
import tempfile

path, command = sys.argv[1], sys.argv[2]
settings = {}
if os.path.exists(path):
    try:
        with open(path, encoding="utf-8") as fh:
            settings = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"WARNING: leaving {path} alone: {exc}", file=sys.stderr)
        sys.exit(0)
    if not isinstance(settings, dict):
        print(f"WARNING: leaving {path} alone: not a JSON object", file=sys.stderr)
        sys.exit(0)
if not isinstance(settings.get("hooks"), dict):
    settings["hooks"] = {}
hooks = settings["hooks"]
if not isinstance(hooks.get("SessionStart"), list):
    hooks["SessionStart"] = []
entries = hooks["SessionStart"]
for entry in entries:
    if not isinstance(entry, dict):
        continue
    for hook in entry.get("hooks", []):
        if isinstance(hook, dict) and "radio-gemini-hook.sh" in str(hook.get("command", "")):
            print("gemini hook already installed")
            sys.exit(0)
for matcher in ("startup", "resume", "clear"):
    entries.append({
        "matcher": matcher,
        "hooks": [{"name": "radio-briefing", "type": "command", "command": command}],
    })
# Write through a sibling temp file: a crash mid-write must never truncate the
# user's settings, and the original file mode is preserved.
mode = (os.stat(path).st_mode & 0o777) if os.path.exists(path) else 0o600
directory = os.path.dirname(path) or "."
fd, tmp = tempfile.mkstemp(dir=directory, prefix=".settings-", suffix=".json")
try:
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(settings, fh, indent=2)
        fh.write("\n")
    os.chmod(tmp, mode)
    os.replace(tmp, path)
except OSError as exc:
    print(f"WARNING: could not write {path}: {exc}", file=sys.stderr)
    try:
        os.unlink(tmp)
    except OSError:
        pass
    sys.exit(0)
print(f"gemini hook installed into {path}")
PYEOF
else
  echo "gemini not found; skipping gemini hook"
fi

exit 0
