@echo off
setlocal
title Stock Bubble
cd /d "%~dp0"

where uv >nul 2>nul
if errorlevel 1 goto local_python
uv run python scripts\start_local.py
goto finished

:local_python
if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] uv was not found and the project environment is missing.
    echo Install uv, reopen this window, and double-click start.bat again.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" scripts\start_local.py

:finished
if errorlevel 1 (
    echo.
    echo [ERROR] Stock Bubble could not start. See the error above.
    pause
    exit /b 1
)
endlocal
