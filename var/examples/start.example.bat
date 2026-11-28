@echo off
rem Copy this file to var\start.bat and adjust DAGR_PYTHON to the target interpreter.
rem The watchdog supervises the service. When watchdog.enabled=false the watchdog
rem exits with code 5 and this script falls back to running the service directly.
setlocal
if not defined DAGR_PYTHON set "DAGR_PYTHON=python.exe"
pushd "%~dp0.."
set "ROOT=%CD%"
if not exist "%ROOT%\var\logs" mkdir "%ROOT%\var\logs"
set "WD_OUT=%ROOT%\var\logs\watchdog.out.log"
set "WD_ERR=%ROOT%\var\logs\watchdog.err.log"
echo [dagrunner] root:     %ROOT%
echo [dagrunner] watchdog: %DAGR_PYTHON% -m dagrunner.watchdog --role master
"%DAGR_PYTHON%" "%ROOT%\run_tee.py" "%WD_OUT%" "%WD_ERR%" ^
    "%DAGR_PYTHON%" -m dagrunner.watchdog --role master --root "%ROOT%"
if errorlevel 5 goto direct
if errorlevel 4 echo [dagrunner] port already used by a healthy instance; aborting.
if errorlevel 3 echo [dagrunner] another watchdog is already running.
if errorlevel 2 echo [dagrunner] watchdog configuration error; check var\config.yaml.
if errorlevel 1 echo [dagrunner] watchdog internal error; see %WD_ERR%.
goto :eof

:direct
set "LOG_OUT=%ROOT%\var\logs\server.out.log"
set "LOG_ERR=%ROOT%\var\logs\server.err.log"
echo [dagrunner] root:    %ROOT%
echo [dagrunner] log out: %LOG_OUT%
echo [dagrunner] log err: %LOG_ERR%
echo [dagrunner] starting, this window stays open while the server runs ...
"%DAGR_PYTHON%" "%ROOT%\run_tee.py" "%LOG_OUT%" "%LOG_ERR%" "%DAGR_PYTHON%" -m dagrunner.server --config "%ROOT%\var\config.yaml"
if errorlevel 1 pause
goto :eof
