@rem Resolve a Python 3.10+ interpreter into RADIO_PY; exit 1 with a hint when none is on PATH.
@rem Called by radio.cmd (win-shim.py), setup.cmd, autostart.cmd and run-view.cmd.
@echo off
set "RADIO_PY="
py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1 && set "RADIO_PY=py -3" && exit /b 0
python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1 && set "RADIO_PY=python" && exit /b 0
python3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1 && set "RADIO_PY=python3" && exit /b 0
echo radio needs Python 3.10+ on PATH - install it from https://www.python.org/downloads/ 1>&2
exit /b 1
