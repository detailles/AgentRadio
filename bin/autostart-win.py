#!/usr/bin/env python3
"""Herdr startup hook body for Windows (run by bin/autostart.cmd): refresh the
CLI shim, then spawn the relay detached with its output appended to the relay
log. The relay holds its own single-instance lock, so a second spawn exits
harmlessly. Mirrors bin/autostart.sh; the startup hook always exits 0.
"""

import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    """Refresh the shim and launch the relay; a silent no-op off Windows."""
    if os.name != "nt":
        return 0
    root = Path(os.environ.get("HERDR_PLUGIN_ROOT") or Path(__file__).resolve().parent.parent)
    subprocess.run(
        [sys.executable, str(root / "bin" / "win-shim.py")],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    log_dir = Path(
        os.environ.get("RADIO_HOME") or Path.home() / ".local" / "share" / "herdr-radio"
    )
    log_dir.mkdir(parents=True, exist_ok=True)
    with open(log_dir / "relay.log", "a", encoding="utf-8") as log:
        # Detached, own process group, no console: the relay must outlive this
        # hook process.
        subprocess.Popen(
            [sys.executable, str(root / "bin" / "radio"), "relay"],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
