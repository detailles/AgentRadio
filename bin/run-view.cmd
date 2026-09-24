@rem Windows pane entry: launch the Radio view through the state-dir venv.
@rem The working directory deliberately leaves the plugin dir: a process whose
@rem cwd sits inside the managed plugin dir blocks Herdr's install/update on
@rem Windows (sharing violation).
@echo off
setlocal
if defined HERDR_PLUGIN_ROOT (set "ROOT=%HERDR_PLUGIN_ROOT%") else (set "ROOT=%~dp0..")
call "%ROOT%\bin\find-python.cmd"
if errorlevel 1 exit /b 1
cd /d "%USERPROFILE%" 2>nul
%RADIO_PY% "%ROOT%\bin\run-view.py" %*
