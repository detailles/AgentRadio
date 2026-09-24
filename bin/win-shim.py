#!/usr/bin/env python3
"""Write or refresh the Windows CLI shim (~/.local/bin/radio.cmd).

Windows PowerShell cannot execute the extensionless bin/radio script, and the
managed plugin root changes on every update, so the CLI is exposed as a small
generated radio.cmd that calls the current plugin root through
bin/find-python.cmd. POSIX exposes the CLI with a symlink instead
(bin/link-cli.sh). Only a shim carrying SHIM_MARKER is ever overwritten, so a
same-named foreign file is never touched. Runs from the plugin build hook
(bin/setup.cmd) and the startup hook (bin/autostart.cmd); no-ops off Windows.
"""

import os
import sys
from pathlib import Path

SHIM_MARKER = "@rem herdr-radio shim"


def shim_text(root: Path) -> str:
    """The generated radio.cmd body: call the plugin root's radio through
    find-python.cmd, forwarding all arguments and the exit code."""
    return (
        f"{SHIM_MARKER} — regenerated on plugin install and startup; edit the plugin, not this file.\n"
        "@echo off\n"
        "setlocal\n"
        f'set "ROOT={root}"\n'
        'call "%ROOT%\\bin\\find-python.cmd"\n'
        "if errorlevel 1 exit /b 1\n"
        '%RADIO_PY% "%ROOT%\\bin\\radio" %*\n'
    )


def ensure_shim(root: Path, link_dir: Path | None = None) -> str:
    """Create or refresh radio.cmd in link_dir (default ~/.local/bin) so the
    CLI keeps working across plugin updates, and report what happened."""
    link_dir = link_dir or Path.home() / ".local" / "bin"
    shim = link_dir / "radio.cmd"
    try:
        if shim.exists():
            existing = shim.read_text(encoding="utf-8", errors="replace")
            if SHIM_MARKER not in existing:
                return f"radio.cmd left alone (not a radio shim): {shim}"
            if existing == shim_text(root):
                return f"radio.cmd already current: {shim}"
        link_dir.mkdir(parents=True, exist_ok=True)
        shim.write_text(shim_text(root), encoding="utf-8", newline="\r\n")
    except OSError as exc:
        return f"radio.cmd not written: {exc}"
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
    """Refresh the shim on Windows; a silent no-op elsewhere."""
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
