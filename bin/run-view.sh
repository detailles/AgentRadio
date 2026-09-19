#!/bin/sh
# Launch the Radio view with the plugin venv when present.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"
exec "$PY" "$ROOT/bin/radio-view" "$@"
