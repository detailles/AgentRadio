#!/usr/bin/env python3
"""Radio view launcher: run bin/radio-view with the venv interpreter when it
exists, else with this launcher's own interpreter (radio-view then reports the
missing dependencies itself).

The venv lives under the plugin state dir (~/.local/share/herdr-radio/venv),
not in the plugin dir: a running view pane must never hold the managed plugin
directory, or Windows refuses `herdr plugin install` updates. Called by the
manifest pane entrypoints and by `radio view`; on Windows bin/run-view.cmd is
the pane entry that finds a Python to run this file.
"""

import os
import sys
from pathlib import Path


def state_dir() -> Path:
    """The plugin state dir, mirroring bin/radio: $RADIO_HOME, else
    ~/.local/share/herdr-radio (the ledger lives here too)."""
    override = os.environ.get("RADIO_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".local" / "share" / "herdr-radio"


def view_python(venv: Path | None = None) -> Path:
    """The interpreter for the view: the state-dir venv when it exists
    (POSIX: bin/python, Windows: Scripts/python.exe), else this launcher's own
    interpreter."""
    venv = venv or state_dir() / "venv"
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return python if python.exists() else Path(sys.executable)


def detach_cwd() -> None:
    """Leave the plugin directory before running the view. Herdr launches the
    pane with the managed plugin dir as cwd; a process that keeps that cwd
    blocks `herdr plugin install` on Windows (sharing violation)."""
    try:
        os.chdir(state_dir())
    except OSError:
        pass


def main() -> int:
    """Hand the terminal to the view. Windows runs it as a waited-for child:
    the CRT's exec would return the shell prompt while the view still runs."""
    detach_cwd()
    python = str(view_python())
    view = str(Path(__file__).resolve().parent / "radio-view")
    if os.name == "nt":
        import subprocess

        return subprocess.run([python, view]).returncode
    os.execv(python, [python, view])
    return 0  # unreachable


if __name__ == "__main__":
    raise SystemExit(main())
