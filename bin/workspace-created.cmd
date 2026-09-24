@rem Windows hook entry: open this workspace's Radio view pane.
@echo off
setlocal
if defined HERDR_PLUGIN_ROOT (set "ROOT=%HERDR_PLUGIN_ROOT%") else (set "ROOT=%~dp0..")
call "%ROOT%\bin\find-python.cmd"
if errorlevel 1 exit /b 0
%RADIO_PY% "%ROOT%\bin\workspace-created.py"
exit /b 0
