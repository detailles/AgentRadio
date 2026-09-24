#!/usr/bin/env python3
"""Radio build hook body for Windows (run by bin/setup.cmd): refresh the CLI
shim, then install the view dependencies into the venv under the plugin state
dir (%USERPROFILE%\\.local\\share\\herdr-radio\\venv).

The venv deliberately lives outside the plugin dir: a running view pane must
never hold the managed plugin directory, or Windows refuses `herdr plugin
install` updates. Mirrors bin/setup.sh — a view dependency failure warns but
never fails the install (the CLI and relay are stdlib-only), and the POSIX-only
provider briefing hooks (gemini/kimi) are skipped on Windows.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path


def state_dir() -> Path:
    """The plugin state dir, mirroring bin/radio: %RADIO_HOME%, else
    ~/.local/share/herdr-radio (the ledger lives here too)."""
    override = os.environ.get("RADIO_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".local" / "share" / "herdr-radio"


def venv_python() -> Path:
    """The interpreter the view runs under (Windows venv layout)."""
    return state_dir() / "venv" / "Scripts" / "python.exe"


def view_ready(python: Path) -> bool:
    """True when the view venv exists and Textual imports in it."""
    if not python.exists():
        return False
    try:
        return subprocess.run(
            [str(python), "-c", "import textual"], capture_output=True
        ).returncode == 0
    except OSError:
        return False


def install_view(root: Path) -> None:
    """(Re)create the view venv and install Textual; warn instead of failing."""
    python = venv_python()
    venv = state_dir() / "venv"
    # A pre-0.3.1 venv lived inside the plugin dir; drop it (best effort).
    shutil.rmtree(root / ".venv", ignore_errors=True)
    if view_ready(python):
        print(f"radio view ready: {venv} (existing)")
        return
    shutil.rmtree(venv, ignore_errors=True)  # a partial venv would short-circuit
    venv.parent.mkdir(parents=True, exist_ok=True)
    try:
        if shutil.which("uv"):
            subprocess.run(["uv", "venv", str(venv)], cwd=root, check=True)
            subprocess.run(
                ["uv", "pip", "install", "--python", str(python), "textual>=1.0"],
                cwd=root,
                check=True,
            )
        else:
            subprocess.run(
                [sys.executable, "-m", "venv", str(venv)], cwd=root, check=True
            )
            subprocess.run(
                [str(python), "-m", "pip", "install", "textual>=1.0"], cwd=root, check=True
            )
    except (OSError, subprocess.CalledProcessError):
        pass
    if view_ready(python):
        print(f"radio view ready: {venv}")
        return
    shutil.rmtree(venv, ignore_errors=True)
    print(
        "WARNING: radio view deps not installed (need python venv + pip/uv + PyPI access).\n"
        "  radio CLI and relay still work; the view activates after: bin\\setup.cmd",
        file=sys.stderr,
    )


def main() -> int:
    """Run the Windows install step; never fails the install over view deps."""
    if os.name != "nt":
        print("setup-win: Windows-only; use bin/setup.sh on this platform")
        return 0
    root = Path(os.environ.get("HERDR_PLUGIN_ROOT") or Path(__file__).resolve().parent.parent)
    subprocess.run([sys.executable, str(root / "bin" / "win-shim.py")])
    install_view(root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
