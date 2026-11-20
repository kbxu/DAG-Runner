@echo off
rem Copy this file to var\start-satellite.bat and adjust DAGR_PYTHON to the target interpreter.
rem Copy var\example_configs\satellite.example.yaml to var\satellite.yaml and edit it.
rem Program output is mirrored to this console and appended to var\logs\*.log.
setlocal
if not defined DAGR_PYTHON set "DAGR_PYTHON=python.exe"
pushd "%~dp0.."
set "ROOT=%CD%"
if not exist "%ROOT%\var\logs" mkdir "%ROOT%\var\logs"
set "LOG_OUT=%ROOT%\var\logs\satellite.out.log"
set "LOG_ERR=%ROOT%\var\logs\satellite.err.log"
set "CURRENT="
if exist "%ROOT%\var\current" set /p "CURRENT=" < "%ROOT%\var\current"
echo [dagrunner] root:    %ROOT%
echo [dagrunner] log out: %LOG_OUT%
echo [dagrunner] log err: %LOG_ERR%
if defined CURRENT (
  if exist "%ROOT%\var\versions\%CURRENT%\run_satellite.py" (
    echo [dagrunner] version: %CURRENT%
    echo [dagrunner] starting, this window stays open while the satellite runs ...
    "%DAGR_PYTHON%" "%ROOT%\var\versions\%CURRENT%\run_tee.py" "%LOG_OUT%" "%LOG_ERR%" "%DAGR_PYTHON%" "%ROOT%\var\versions\%CURRENT%\run_satellite.py" --config "%ROOT%\var\satellite.yaml"
    if errorlevel 1 goto failed
    echo [dagrunner] satellite stopped, exit code %errorlevel%.
    goto :eof
  )
)
echo [dagrunner] starting, this window stays open while the satellite runs ...
"%DAGR_PYTHON%" "%ROOT%\run_tee.py" "%LOG_OUT%" "%LOG_ERR%" "%DAGR_PYTHON%" -m dagrunner.satellite --config "%ROOT%\var\satellite.yaml"
if errorlevel 1 goto failed
echo [dagrunner] satellite stopped, exit code %errorlevel%.
goto :eof

:failed
echo [dagrunner] FAILED to start, error code %errorlevel%.
set /p "_close_=Press Enter to close..."
exit /b 1
