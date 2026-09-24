@rem Herdr build hook (Windows): install the view deps and refresh the CLI shim.
@echo off
setlocal
if defined HERDR_PLUGIN_ROOT (set "ROOT=%HERDR_PLUGIN_ROOT%") else (set "ROOT=%~dp0..")
call "%ROOT%\bin\find-python.cmd"
if errorlevel 1 exit /b 1
%RADIO_PY% "%ROOT%\bin\setup-win.py"
exit /b %errorlevel%
