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
    dangling path behind.

    The interpreter probe is a real 3.10 gate — the CLI uses 3.10 syntax, so an
    old `python` on PATH must not be picked — and it is written without
    comparison operators or parentheses on purpose: cmd treats both as syntax
    even inside quotes, which silently truncated the command line (found on
    the Windows guest, 2026-09-25). `minor // 10` is 1 for 3.10..3.99 and 0
    for 3.0..3.9, so it needs neither."""
    return f"""\
{SHIM_MARKER} — stable across plugin updates; do not edit by hand.
@echo off
setlocal
set "RADIO_PY="
set "RADIO_PYCHECK=import sys; assert sys.version_info.major == 3 and sys.version_info.minor // 10"
py -3 -c "%RADIO_PYCHECK%" >nul 2>&1 && set "RADIO_PY=py -3"
if defined RADIO_PY goto radio_python
python -c "%RADIO_PYCHECK%" >nul 2>&1 && set "RADIO_PY=python"
if defined RADIO_PY goto radio_python
python3 -c "%RADIO_PYCHECK%" >nul 2>&1 && set "RADIO_PY=python3"
if defined RADIO_PY goto radio_python
echo radio needs Python 3.10+ on PATH 1>&2
exit /b 1
:radio_python
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


def write_atomic(path: Path, text: str, *, newline: str | None = None) -> None:
    """Write text through a sibling temp file and replace it, so a reader — or
    a shim being run at that moment — never sees a half-written file."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8", newline=newline)
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


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
        if not shim.exists() or shim.read_text(encoding="utf-8", errors="replace") != shim_text():
            write_atomic(shim, shim_text(), newline="\r\n")
        if not resolver.exists() or resolver.read_text(
            encoding="utf-8", errors="replace"
        ) != resolver_text():
            write_atomic(resolver, resolver_text())
        write_atomic(cache, str(root) + "\n")
    except OSError as exc:
        return f"radio shim not written: {exc}"
    return f"radio.cmd -> {root}\\bin\\radio"


def path_hint(link_dir: Path | None = None) -> str | None:
    """A one-line PATH hint when the shim directory is not on PATH (Windows
    PATH matching is case-insensitive); None when it is already reachable."""
    link_dir = link_dir or Path.home() / ".local" / "bin"
    if _on_path(link_dir):
        return None
    return (
        f"note: if `radio` is not recognized, add {link_dir} to PATH "
        "(README: Install on Windows)"
    )


def _norm_path(part: str) -> str:
    """One PATH entry normalized for comparison (case-insensitive, with
    %VAR%/$VAR references expanded, surrounding quotes dropped)."""
    return os.path.normcase(os.path.normpath(os.path.expandvars(part.strip().strip('"'))))


def _on_path(link_dir: Path, path: str | None = None) -> bool:
    """True when link_dir is already visible in the given PATH (defaults to
    the process PATH)."""
    entries = (os.environ.get("PATH", "") if path is None else path).split(os.pathsep)
    wanted = _norm_path(str(link_dir))
    return any(_norm_path(entry) == wanted for entry in entries if entry.strip())


def append_path_entry(existing: str, target: str) -> str | None:
    """The user PATH string with target appended, or None when it already
    contains target. Empty entries are dropped so the registry value stays
    clean; every other entry is preserved verbatim."""
    parts = [part.strip() for part in existing.split(";") if part.strip()]
    if any(_norm_path(part) == _norm_path(target) for part in parts):
        return None
    return ";".join(parts + [str(target).strip()])


def _broadcast_environment_change() -> None:
    """Tell running shells and Explorer the environment changed. Best effort:
    new processes read the registry anyway."""
    import ctypes

    try:
        ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x1A, 0, "Environment", 0x2, 5000, None)
    except OSError:
        pass


def ensure_user_path(link_dir: Path | None = None) -> str | None:
    """Windows only: append the shim directory to the user PATH (registry
    HKCU\\Environment) when it is missing anywhere, so `radio` works without a
    manual PATH command. Returns a status line, or None when nothing had to
    change. A running terminal keeps its old environment — new ones see it."""
    if os.name != "nt":
        return None
    import winreg

    link_dir = link_dir or Path.home() / ".local" / "bin"
    if _on_path(link_dir):
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ) as key:
            try:
                value, kind = winreg.QueryValueEx(key, "Path")
            except FileNotFoundError:
                value, kind = "", winreg.REG_EXPAND_SZ
        updated = append_path_entry(str(value), str(link_dir))
        if updated is None:
            return None
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(key, "Path", 0, kind, updated)
    except OSError as exc:
        return f"could not add {link_dir} to the user PATH: {exc}"
    _broadcast_environment_change()
    return f"added {link_dir} to the user PATH — restart the terminal once"


def main() -> int:
    """Refresh the shim, cache and user PATH on Windows; a silent no-op
    elsewhere."""
    if os.name != "nt":
        return 0
    root = Path(os.environ.get("HERDR_PLUGIN_ROOT") or Path(__file__).resolve().parent.parent)
    print(ensure_shim(root))
    added = ensure_user_path()
    if added:
        print(added)
    else:
        hint = path_hint()
        if hint:
            print(hint)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
