@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

echo [START] ZongziLedger local image ingest launcher

set "BASE_PY_EXE="
set "BASE_PY_ARGS="

if exist ".venv312\Scripts\python.exe" (
    set "BASE_PY_EXE=.venv312\Scripts\python.exe"
    set "BASE_PY_ARGS="
    echo [INIT] Reusing existing .venv312 environment.
)

if not defined BASE_PY_EXE (
    if exist ".venv\Scripts\python.exe" (
        set "BASE_PY_EXE=.venv\Scripts\python.exe"
        set "BASE_PY_ARGS="
        echo [INIT] Reusing existing .venv environment.
    )
)

if not defined BASE_PY_EXE (
    where py.exe >nul 2>&1
    if %errorlevel%==0 (
        set "BASE_PY_EXE=py"
        set "BASE_PY_ARGS=-3"
        echo [INIT] Using py launcher.
    )
)

if not defined BASE_PY_EXE (
    where python.exe >nul 2>&1
    if %errorlevel%==0 (
        set "BASE_PY_EXE=python"
        set "BASE_PY_ARGS="
        echo [INIT] Using python from PATH.
    )
)

if not defined BASE_PY_EXE (
    echo [ERROR] Python not found. Please install Python and add it to PATH.
    pause
    exit /b 1
)

echo [RUN] Running local image ingest...
call "%BASE_PY_EXE%" %BASE_PY_ARGS% ingest_images_local.py
set "RUN_CODE=%errorlevel%"

if not "%RUN_CODE%"=="0" (
    echo [ERROR] Program exited with code: %RUN_CODE%
    pause
    exit /b %RUN_CODE%
)

echo [DONE] Finished.
pause
exit /b 0
