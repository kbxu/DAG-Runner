@echo off
set DAGR_PYTHON=python
cd /d "%~dp0.."
"%DAGR_PYTHON%" -m dagrunner.server --config var/config.example.yaml
