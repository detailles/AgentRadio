@rem Herdr startup hook (Windows): restore the relay after a restart and refresh the CLI shim.
@echo off
setlocal
if defined HERDR_PLUGIN_ROOT (set "ROOT=%HERDR_PLUGIN_ROOT%") else (set "ROOT=%~dp0..")
call "%ROOT%\bin\find-python.cmd"
if errorlevel 1 exit /b 0
%RADIO_PY% "%ROOT%\bin\autostart-win.py"
exit /b 0
