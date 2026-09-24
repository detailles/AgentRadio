#!/bin/sh
# Herdr startup hook: bring the relay back after a server restart/restore.
# The relay itself holds an flock, so a second spawn exits harmlessly.
LOG_DIR="${RADIO_HOME:-$HOME/.local/share/herdr-radio}"
mkdir -p "$LOG_DIR"
# Keep the `radio` CLI reachable after updates (see bin/link-cli.sh).
sh "$(cd "$(dirname "$0")" && pwd)/link-cli.sh"
if [ -n "$HERDR_PLUGIN_ROOT" ]; then
  RADIO_BIN="$HERDR_PLUGIN_ROOT/bin/radio"
else
  RADIO_BIN="$(cd "$(dirname "$0")" && pwd)/radio"
fi
# Leave the plugin dir before spawning: a daemon whose cwd sits inside the
# managed plugin dir blocks Herdr's install/update on Windows.
cd "$LOG_DIR" 2>/dev/null || true
nohup python3 "$RADIO_BIN" relay >> "$LOG_DIR/relay.log" 2>&1 &
exit 0
