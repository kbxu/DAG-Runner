@echo off
rem Copy this file to var\start.bat and adjust DAGR_PYTHON to the target interpreter.
rem Copy var\example_configs\config.example.yaml to var\config.yaml and edit it.
rem Console output is echoed here; full output is appended to var\logs\*.log.
setlocal
if not defined DAGR_PYTHON set "DAGR_PYTHON=python.exe"
pushd "%~dp0.."
set "ROOT=%CD%"
if not exist "%ROOT%\var\logs" mkdir "%ROOT%\var\logs"
set "LOG_OUT=%ROOT%\var\logs\server.out.log"
set "LOG_ERR=%ROOT%\var\logs\server.err.log"
set "CURRENT="
if exist "%ROOT%\var\current" set /p "CURRENT=" < "%ROOT%\var\current"
echo [dagrunner] root:    %ROOT%
echo [dagrunner] log out: %LOG_OUT%
echo [dagrunner] log err: %LOG_ERR%
if defined CURRENT (
  if exist "%ROOT%\var\versions\%CURRENT%\run_server.py" (
    echo [dagrunner] version: %CURRENT%
    echo [dagrunner] starting, this window stays open while the server runs ...
    "%DAGR_PYTHON%" "%ROOT%\var\versions\%CURRENT%\run_server.py" --config "%ROOT%\var\config.yaml" >> "%LOG_OUT%" 2>> "%LOG_ERR%"
    if errorlevel 1 goto failed
    echo [dagrunner] server stopped, exit code %errorlevel%.
    goto :eof
  )
)
echo [dagrunner] starting, this window stays open while the server runs ...
"%DAGR_PYTHON%" -m dagrunner.server --config "%ROOT%\var\config.yaml" >> "%LOG_OUT%" 2>> "%LOG_ERR%"
if errorlevel 1 goto failed
echo [dagrunner] server stopped, exit code %errorlevel%.
goto :eof

:failed
echo [dagrunner] FAILED to start, error code %errorlevel%.
echo [dagrunner] see the log files above.
set /p "_close_=Press Enter to close..."
exit /b 1
