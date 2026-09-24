#!/usr/bin/env python3
"""Radio build hook body for Windows (run by bin/setup.cmd): refresh the CLI
shim, then install the view dependencies into the plugin-local .venv.

Mirrors bin/setup.sh: a view dependency failure warns but never fails the
install (the CLI and relay are stdlib-only), and the POSIX-only provider
briefing hooks (gemini/kimi) are skipped on Windows.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path


def venv_python(root: Path) -> Path:
    """The interpreter the view runs under (Windows venv layout)."""
    return root / ".venv" / "Scripts" / "python.exe"


def view_ready(python: Path) -> bool:
    """True when the plugin venv exists and Textual imports in it."""
    if not python.exists():
        return False
    try:
        return subprocess.run(
            [str(python), "-c", "import textual"], capture_output=True
        ).returncode == 0
    except OSError:
        return False


def install_view(root: Path) -> None:
    """(Re)create the venv and install Textual; warn instead of failing."""
    python = venv_python(root)
    if view_ready(python):
        print(f"radio view ready: {root / '.venv'} (existing)")
        return
    shutil.rmtree(root / ".venv", ignore_errors=True)  # a partial venv would short-circuit
    try:
        if shutil.which("uv"):
            subprocess.run(["uv", "venv", str(root / ".venv")], cwd=root, check=True)
            subprocess.run(
                ["uv", "pip", "install", "--python", str(python), "textual>=1.0"],
                cwd=root,
                check=True,
            )
        else:
            subprocess.run(
                [sys.executable, "-m", "venv", str(root / ".venv")], cwd=root, check=True
            )
            subprocess.run(
                [str(python), "-m", "pip", "install", "textual>=1.0"], cwd=root, check=True
            )
    except (OSError, subprocess.CalledProcessError):
        pass
    if view_ready(python):
        print(f"radio view ready: {root / '.venv'}")
        return
    shutil.rmtree(root / ".venv", ignore_errors=True)
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
