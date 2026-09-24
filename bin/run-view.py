#!/usr/bin/env python3
"""Radio view launcher: pick the plugin venv interpreter when present and exec
bin/radio-view in it (Textual lives only in that venv).

Called by the manifest pane entrypoint and by `radio view`; on Windows
bin/run-view.cmd is the pane entry that finds a Python to run this file.
Falls back to this launcher's own interpreter when the venv is missing, and
radio-view itself then reports the missing dependencies.
"""

import os
import subprocess
import sys
from pathlib import Path


def view_python(root: Path) -> Path:
    """The interpreter for the view: the plugin-local venv when it exists
    (POSIX: .venv/bin/python, Windows: .venv/Scripts/python.exe), else this
    launcher's own interpreter."""
    venv = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return venv if venv.exists() else Path(sys.executable)


def main() -> int:
    """Hand the terminal to the view. Windows runs it as a waited-for child:
    the CRT's exec would return the shell prompt while the view still runs."""
    root = Path(__file__).resolve().parent.parent
    python = str(view_python(root))
    view = str(root / "bin" / "radio-view")
    if os.name == "nt":
        return subprocess.run([python, view]).returncode
    os.execv(python, [python, view])
    return 0  # unreachable


if __name__ == "__main__":
    raise SystemExit(main())
