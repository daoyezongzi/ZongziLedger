@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

echo [START] ZongziLedger Dify bridge launcher

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

echo [RUN] Launching dify_bridge_server.py ...
call %PY_CMD% dify_bridge_server.py
set "RUN_CODE=%errorlevel%"

if not "%RUN_CODE%"=="0" (
    echo [ERROR] Bridge exited with code: %RUN_CODE%
    pause
    exit /b %RUN_CODE%
)

echo [DONE] Bridge exited.
pause
exit /b 0
