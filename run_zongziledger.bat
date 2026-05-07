@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

echo [START] ZongziLedger one-click launcher

if not exist ".tmp" mkdir ".tmp"
set "TMP=%CD%\.tmp"
set "TEMP=%CD%\.tmp"

set "BASE_PY_EXE="
set "BASE_PY_ARGS="
set "VENV_DIR="

where py.exe >nul 2>&1
if %errorlevel%==0 (
    py -3.12 -V >nul 2>&1
    if not errorlevel 1 (
        set "BASE_PY_EXE=py"
        set "BASE_PY_ARGS=-3.12"
        set "VENV_DIR=.venv312"
        echo [INIT] Python 3.12 detected via py launcher.
    )
)

if not defined BASE_PY_EXE (
    if exist ".venv312\Scripts\python.exe" (
        set "BASE_PY_EXE=.venv312\Scripts\python.exe"
        set "BASE_PY_ARGS="
        set "VENV_DIR=.venv312"
        echo [INIT] Reusing existing .venv312 environment.
    )
)

if not defined BASE_PY_EXE (
    where py.exe >nul 2>&1
    if %errorlevel%==0 (
        set "BASE_PY_EXE=py"
        set "BASE_PY_ARGS=-3"
        set "VENV_DIR=.venv"
        echo [WARN] Python 3.12 not found. Falling back to py -3.
    )
)

if not defined BASE_PY_EXE (
    where python.exe >nul 2>&1
    if %errorlevel%==0 (
        set "BASE_PY_EXE=python"
        set "BASE_PY_ARGS="
        set "VENV_DIR=.venv"
        echo [WARN] Python 3.12 not found. Falling back to default python.
    )
)

if not defined BASE_PY_EXE (
    for /f "usebackq delims=" %%i in (`powershell -NoProfile -Command "$p = Get-Command python -ErrorAction SilentlyContinue; if ($p) { $p.Source }"`) do (
        set "BASE_PY_EXE=%%i"
    )
    if defined BASE_PY_EXE (
        set "BASE_PY_ARGS="
        set "VENV_DIR=.venv"
        echo [WARN] Python 3.12 not found. Falling back to detected python path.
    )
)

if not defined BASE_PY_EXE (
    echo [ERROR] Python not found. Please install Python 3.12+ and add it to PATH.
    pause
    exit /b 1
)

if not defined VENV_DIR set "VENV_DIR=.venv"
if not exist "%VENV_DIR%\Scripts\python.exe" (
    echo [INIT] Creating virtual environment: %VENV_DIR%
    call "%BASE_PY_EXE%" %BASE_PY_ARGS% -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment: %VENV_DIR%
        pause
        exit /b 1
    )
)

set "VENV_PY=%VENV_DIR%\Scripts\python.exe"
if not exist "%VENV_PY%" (
    echo [ERROR] Virtual environment python not found: %VENV_PY%
    pause
    exit /b 1
)

set "PY_VER=0.0"
set "PY_MAJOR=0"
set "PY_MINOR=0"
set "PY_VER_FILE=.tmp\\py_ver.txt"
if exist "%PY_VER_FILE%" del /f /q "%PY_VER_FILE%" >nul 2>&1
"%VENV_PY%" -c "import sys;print(str(sys.version_info[0])+'.'+str(sys.version_info[1]))" > "%PY_VER_FILE%" 2>nul
if exist "%PY_VER_FILE%" set /p PY_VER=<"%PY_VER_FILE%"
for /f "tokens=1,2 delims=." %%a in ("%PY_VER%") do (
    set "PY_MAJOR=%%a"
    set "PY_MINOR=%%b"
)
echo [INFO] Using venv python version: %PY_VER%

"%VENV_PY%" -m pip --version >nul 2>&1
if errorlevel 1 (
    echo [INIT] pip not found in %VENV_DIR%, trying ensurepip...
    call "%VENV_PY%" -m ensurepip --upgrade
    if errorlevel 1 (
        echo [ERROR] Failed to bootstrap pip in %VENV_DIR%.
        echo [ERROR] Please reinstall Python with ensurepip support and rerun.
        pause
        exit /b 1
    )
)

echo [INIT] Checking/installing base dependencies (uiautomation, pyyaml)...
"%VENV_PY%" setup.py
if errorlevel 1 (
    echo [ERROR] Base dependency check/install failed.
    pause
    exit /b 1
)

echo [RUN] Running ledger job...
"%VENV_PY%" main.py
set "RUN_CODE=%errorlevel%"

if not "%RUN_CODE%"=="0" (
    echo [ERROR] Program exited with code: %RUN_CODE%
    pause
    exit /b %RUN_CODE%
)

echo [DONE] Finished.
pause
exit /b 0
