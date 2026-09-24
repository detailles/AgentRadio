#!/bin/sh
# Keep the `radio` CLI reachable from PATH.
#
# The managed plugin dir is content-hashed and changes on every update, so a
# link made by hand would dangle after the next install. This creates the link
# when absent and repairs only links that point into a herdr plugin dir; an
# unrelated binary or symlink in the target directory is never touched. Runs
# from the install build (so the CLI works right after install) and from the
# startup hook (so updates never leave a dangling link).
ROOT="${HERDR_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
LINK_DIR="$HOME/.local/bin"
LINK="$LINK_DIR/radio"
[ -d "$LINK_DIR" ] || exit 0
if [ ! -e "$LINK" ] && [ ! -L "$LINK" ]; then
  ln -s "$ROOT/bin/radio" "$LINK" 2>/dev/null || true
elif [ -L "$LINK" ]; then
  case "$(readlink "$LINK")" in
    *herdr/plugins/*) ln -sfn "$ROOT/bin/radio" "$LINK" 2>/dev/null || true ;;
  esac
fi
