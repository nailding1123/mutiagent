@echo off
setlocal

set "PROJECT_DIR=%~dp0.."
if defined PYTHONPATH (
    set "PYTHONPATH=%PROJECT_DIR%;%PYTHONPATH%"
) else (
    set "PYTHONPATH=%PROJECT_DIR%"
)

where py >nul 2>nul
if not errorlevel 1 (
    py -3 -m multiagent_cli.web_launcher %*
    exit /b %errorlevel%
)

python -m multiagent_cli.web_launcher %*
exit /b %errorlevel%
