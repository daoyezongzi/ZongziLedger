@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

echo [START] ZongziLedger wetrace daily-once workflow runner

set "BASE_PY_EXE="

if exist ".venv312\Scripts\python.exe" (
    set "BASE_PY_EXE=.venv312\Scripts\python.exe"
)

if not defined BASE_PY_EXE if exist ".venv\Scripts\python.exe" (
    set "BASE_PY_EXE=.venv\Scripts\python.exe"
)

if not defined BASE_PY_EXE (
    where py.exe >nul 2>&1
    if %errorlevel%==0 (
        set "BASE_PY_EXE=py"
    )
)

if not defined BASE_PY_EXE (
    where python.exe >nul 2>&1
    if %errorlevel%==0 (
        set "BASE_PY_EXE=python"
    )
)

if not defined BASE_PY_EXE (
    echo [ERROR] Python not found. Please install Python and add it to PATH.
    pause
    exit /b 1
)

set "BRIDGE_PID="
set "RUN_CODE=1"

echo [STEP] Starting local dify bridge...
if /I "%BASE_PY_EXE%"=="py" (
    for /f %%I in ('powershell -NoProfile -Command "$p = Start-Process -FilePath \"py\" -ArgumentList @(\"-3\",\"-u\",\"dify_bridge_server.py\") -WorkingDirectory \"%CD%\" -WindowStyle Hidden -PassThru; $p.Id"') do set "BRIDGE_PID=%%I"
) else (
    for /f %%I in ('powershell -NoProfile -Command "$p = Start-Process -FilePath \"%BASE_PY_EXE%\" -ArgumentList @(\"-u\",\"dify_bridge_server.py\") -WorkingDirectory \"%CD%\" -WindowStyle Hidden -PassThru; $p.Id"') do set "BRIDGE_PID=%%I"
)

if not defined BRIDGE_PID (
    echo [ERROR] Failed to start dify bridge process.
    goto :cleanup
)
echo [STEP] Bridge PID=!BRIDGE_PID!

set "HEALTH_OK=0"
for /l %%N in (1,1,20) do (
    powershell -NoProfile -Command "try { $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 -Uri 'http://127.0.0.1:8787/api/health'; if($r.StatusCode -ge 200 -and $r.StatusCode -lt 500){ exit 0 } else { exit 1 } } catch { exit 1 }"
    if !errorlevel! == 0 (
        set "HEALTH_OK=1"
        goto :run_ingest
    )
    timeout /t 1 /nobreak >nul
)

if not "%HEALTH_OK%"=="1" (
    echo [ERROR] Dify bridge health check failed on http://127.0.0.1:8787/api/health
    goto :cleanup
)

:run_ingest
echo [STEP] Running wetrace ingest...
if /I "%BASE_PY_EXE%"=="py" (
    call py -3 ingest_wetrace_local.py
) else (
    call "%BASE_PY_EXE%" ingest_wetrace_local.py
)
set "RUN_CODE=%errorlevel%"

:cleanup
if defined BRIDGE_PID (
    echo [STEP] Stopping bridge PID !BRIDGE_PID!...
    powershell -NoProfile -Command "Stop-Process -Id !BRIDGE_PID! -Force -ErrorAction SilentlyContinue" >nul 2>&1
)

if not "%RUN_CODE%"=="0" (
    echo [ERROR] Daily-once run failed with code: %RUN_CODE%
    pause
    exit /b %RUN_CODE%
)

echo [DONE] Daily-once run finished.
pause
exit /b 0
