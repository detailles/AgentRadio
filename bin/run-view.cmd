@rem Windows pane entry: launch the Radio view through the plugin venv.
@echo off
setlocal
if defined HERDR_PLUGIN_ROOT (set "ROOT=%HERDR_PLUGIN_ROOT%") else (set "ROOT=%~dp0..")
call "%ROOT%\bin\find-python.cmd"
if errorlevel 1 exit /b 1
%RADIO_PY% "%ROOT%\bin\run-view.py" %*
