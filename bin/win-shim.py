#!/usr/bin/env python3
"""Write the Windows CLI launcher and refresh its plugin-root cache.

Windows PowerShell cannot execute the extensionless bin/radio script, and the
managed plugin root changes on updates (the build hook does not even receive
HERDR_PLUGIN_ROOT), so the CLI is exposed as a stable radio.cmd in
~/.local/bin that bakes no path at all: it runs the plugin found via
radio-root.txt (a one-line cache refreshed here and by the resolver) and falls
back to radio-resolve.py, which asks herdr for the current plugin_root. POSIX
exposes the CLI with a symlink instead (bin/link-cli.sh). Only files carrying
our markers are ever overwritten, so a same-named foreign file is never
touched. Runs from the plugin build hook (bin/setup.cmd) and the startup hook
(bin/autostart.cmd); no-ops off Windows.
"""

import os
import sys
from pathlib import Path

SHIM_MARKER = "@rem herdr-radio shim"
RESOLVER_MARKER = "# herdr-radio resolver"
ROOT_FILE = "radio-root.txt"
RESOLVER_FILE = "radio-resolve.py"


def shim_text() -> str:
    """The generated radio.cmd body. Stable across plugin updates: it resolves
    the plugin root at run time, so a moved or stale install can never leave a
    dangling path behind."""
    return f"""\
{SHIM_MARKER} — stable across plugin updates; do not edit by hand.
@echo off
setlocal
set "RADIO_PY="
py -3 --version >nul 2>&1 && set "RADIO_PY=py -3"
if not defined RADIO_PY (python --version >nul 2>&1 && set "RADIO_PY=python")
if not defined RADIO_PY (python3 --version >nul 2>&1 && set "RADIO_PY=python3")
if not defined RADIO_PY (echo radio needs Python 3.10+ on PATH 1>&2 & exit /b 1)
set "ROOT="
if exist "%~dp0{ROOT_FILE}" for /f "usebackq delims=" %%R in ("%~dp0{ROOT_FILE}") do set "ROOT=%%R"
if not exist "%ROOT%\\bin\\radio" (
  for /f "usebackq delims=" %%R in (`%RADIO_PY% "%~dp0{RESOLVER_FILE}" --print-root 2^>nul`) do set "ROOT=%%R"
)
if not exist "%ROOT%\\bin\\radio" (
  echo radio: plugin root not found - run: herdr plugin install detailles/AgentRadio 1>&2
  exit /b 1
)
%RADIO_PY% "%ROOT%\\bin\\radio" %*
"""


def resolver_text() -> str:
    """The stable resolver written beside the shim: asks herdr for the current
    plugin root (the installed path changes with every update) and refreshes
    the cache it shares with the shim; falls back to the cache when herdr
    cannot answer."""
    return '''\
#!/usr/bin/env python3
%(marker)s — written by the radio plugin; refreshed on install/startup.
"""Print the current radio plugin root for the Windows radio.cmd shim."""

import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CACHE = HERE / "%(root_file)s"


def current_root() -> str:
    """The radio plugin root reported by herdr, cached for the next call;
    the cached value when herdr cannot answer."""
    herdr = os.environ.get("HERDR_BIN_PATH") or "herdr"
    try:
        out = subprocess.run(
            [herdr, "plugin", "list", "--json"],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
        data = json.loads(out)
        for plugin in data.get("result", {}).get("plugins", []):
            root = str(plugin.get("plugin_root") or "")
            if plugin.get("plugin_id") == "radio" and root:
                try:
                    CACHE.write_text(root, encoding="utf-8")
                except OSError:
                    pass
                return root
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    try:
        return CACHE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def main() -> int:
    """--print-root prints the resolved root; anything else is an error — the
    shim runs the CLI itself."""
    if "--print-root" in sys.argv[1:]:
        print(current_root())
        return 0
    print("radio-resolve: use the radio.cmd shim", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
''' % {"marker": RESOLVER_MARKER, "root_file": ROOT_FILE}


def ensure_shim(root: Path, link_dir: Path | None = None) -> str:
    """Create or refresh the shim, the resolver, and the root cache in
    link_dir (default ~/.local/bin) so the CLI keeps working across plugin
    updates, and report what happened."""
    link_dir = link_dir or Path.home() / ".local" / "bin"
    shim = link_dir / "radio.cmd"
    resolver = link_dir / RESOLVER_FILE
    cache = link_dir / ROOT_FILE
    try:
        if shim.exists() and SHIM_MARKER not in shim.read_text(encoding="utf-8", errors="replace"):
            return f"radio.cmd left alone (not a radio shim): {shim}"
        if resolver.exists() and RESOLVER_MARKER not in resolver.read_text(
            encoding="utf-8", errors="replace"
        ):
            return f"{RESOLVER_FILE} left alone (not a radio resolver): {resolver}"
        link_dir.mkdir(parents=True, exist_ok=True)
        if not shim.exists() or shim.read_text(encoding="utf-8") != shim_text():
            shim.write_text(shim_text(), encoding="utf-8", newline="\r\n")
        if not resolver.exists() or resolver.read_text(encoding="utf-8") != resolver_text():
            resolver.write_text(resolver_text(), encoding="utf-8")
        cache.write_text(str(root) + "\n", encoding="utf-8")
    except OSError as exc:
        return f"radio shim not written: {exc}"
    return f"radio.cmd -> {root}\\bin\\radio"


def path_hint(link_dir: Path | None = None) -> str | None:
    """A one-line PATH hint when the shim directory is not on PATH (Windows
    PATH matching is case-insensitive); None when it is already reachable."""
    link_dir = link_dir or Path.home() / ".local" / "bin"
    entries = {
        os.path.normcase(os.path.normpath(part))
        for part in os.environ.get("PATH", "").split(os.pathsep)
        if part
    }
    if os.path.normcase(os.path.normpath(str(link_dir))) in entries:
        return None
    return (
        f"note: if `radio` is not recognized, add {link_dir} to PATH "
        "(README: Install on Windows)"
    )


def main() -> int:
    """Refresh the shim and cache on Windows; a silent no-op elsewhere."""
    if os.name != "nt":
        return 0
    root = Path(os.environ.get("HERDR_PLUGIN_ROOT") or Path(__file__).resolve().parent.parent)
    print(ensure_shim(root))
    hint = path_hint()
    if hint:
        print(hint)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
