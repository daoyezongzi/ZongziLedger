@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

echo [START] ZongziLedger capture-to-dify launcher

set "PY_CMD="
if exist ".venv312\Scripts\python.exe" set "PY_CMD=.venv312\Scripts\python.exe"
if not defined PY_CMD if exist ".venv\Scripts\python.exe" set "PY_CMD=.venv\Scripts\python.exe"

if not defined PY_CMD (
    where py.exe >nul 2>&1
    if %errorlevel%==0 (
        set "PY_CMD=py -3"
    )
)

if not defined PY_CMD (
    where python.exe >nul 2>&1
    if %errorlevel%==0 (
        set "PY_CMD=python"
    )
)

if not defined PY_CMD (
    echo [ERROR] Python not found. Please install Python 3.12+ and retry.
    pause
    exit /b 1
)

echo [RUN] Launching capture_to_dify.py ...
call %PY_CMD% capture_to_dify.py
set "RUN_CODE=%errorlevel%"

if not "%RUN_CODE%"=="0" (
    echo [ERROR] capture_to_dify exited with code: %RUN_CODE%
    pause
    exit /b %RUN_CODE%
)

echo [DONE] capture_to_dify finished.
pause
exit /b 0
