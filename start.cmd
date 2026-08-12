@echo off
setlocal

set "DAIRY_BENCH_HOME=%~dp0.dairy-bench"
set "BACKEND_URL=http://127.0.0.1:8000"
set "FRONTEND_URL=http://127.0.0.1:5173"
set "EXPECTED_API_VERSION=0.7.0"

if /i "%~1"=="--configure-newapi" (
  if "%~2"=="" (
    echo Usage: start.cmd --configure-newapi ^<model^|codex^|claude-code^>
    exit /b 2
  )
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0backend\scripts\configure_newapi.ps1" -Profile "%~2"
  if errorlevel 1 (
    pause
    exit /b 1
  )
  pause
  exit /b 0
)

if /i "%~1"=="--check" (
  echo [Dairy Bench] Runtime home: %DAIRY_BENCH_HOME%
  call :report_newapi_status
  where.exe python.exe
  where.exe npm.cmd
  exit /b 0
)

call :require_command python.exe Python
if errorlevel 1 exit /b 1
call :require_command npm.cmd Node.js
if errorlevel 1 exit /b 1

call :probe_backend
set "BACKEND_STATE=%errorlevel%"
if "%BACKEND_STATE%"=="2" goto :port_conflict

call :probe_frontend
set "FRONTEND_STATE=%errorlevel%"
if "%FRONTEND_STATE%"=="2" goto :port_conflict

if "%BACKEND_STATE%"=="1" call :start_backend
if "%FRONTEND_STATE%"=="1" call :start_frontend

call :wait_for_services
if errorlevel 1 (
  echo [Dairy Bench] Startup did not complete. Check the Backend and Frontend windows.
  pause
  exit /b 1
)

echo [Dairy Bench] Backend and frontend are ready.
start "" "%FRONTEND_URL%"
exit /b 0

:require_command
where.exe %~1 >nul 2>&1
if errorlevel 1 (
  echo [Dairy Bench] %~2 was not found in PATH.
  pause
  exit /b 1
)
exit /b 0

:report_newapi_status
call :report_newapi_profile newapi-model
call :report_newapi_profile newapi-codex
call :report_newapi_profile newapi-claude-code
exit /b 0

:report_newapi_profile
if not exist "%DAIRY_BENCH_HOME%\credentials\%~1\token.clixml" goto :newapi_profile_not_configured
if not exist "%DAIRY_BENCH_HOME%\credentials\%~1\models.json" goto :newapi_profile_not_configured
echo [Dairy Bench] %~1: configured
exit /b 0

:newapi_profile_not_configured
echo [Dairy Bench] %~1: not configured
exit /b 0

:probe_backend
powershell.exe -NoProfile -Command "$listener = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1; if (-not $listener) { exit 1 }; try { $api = Invoke-RestMethod -Uri '%BACKEND_URL%/openapi.json' -TimeoutSec 10; if ($api.info.title -eq 'Dairy Bench API' -and $api.info.version -eq '%EXPECTED_API_VERSION%') { Write-Host ('[Dairy Bench] Backend already running on port 8000, PID ' + $listener.OwningProcess + '; reusing it.'); exit 0 } } catch {}; $process = Get-CimInstance Win32_Process -Filter ('ProcessId = ' + $listener.OwningProcess); Write-Host ('[Dairy Bench] Port 8000 is occupied by an incompatible process, PID ' + $listener.OwningProcess + ': ' + $process.CommandLine); exit 2"
exit /b %errorlevel%

:probe_frontend
powershell.exe -NoProfile -Command "$listener = Get-NetTCPConnection -LocalPort 5173 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1; if (-not $listener) { exit 1 }; try { $page = Invoke-WebRequest -UseBasicParsing -Uri '%FRONTEND_URL%' -TimeoutSec 2; if ($page.StatusCode -eq 200 -and $page.Content -match '<title>Dairy Bench</title>') { Write-Host ('[Dairy Bench] Frontend already running on port 5173, PID ' + $listener.OwningProcess + '; reusing it.'); exit 0 } } catch {}; $process = Get-CimInstance Win32_Process -Filter ('ProcessId = ' + $listener.OwningProcess); Write-Host ('[Dairy Bench] Port 5173 is occupied by PID ' + $listener.OwningProcess + ': ' + $process.CommandLine); exit 2"
exit /b %errorlevel%

:start_backend
echo [Dairy Bench] Starting backend...
start "Dairy Bench Backend" powershell.exe -NoExit -NoProfile -ExecutionPolicy Bypass -File "%~dp0backend\scripts\launch_backend.ps1"
exit /b %errorlevel%

:start_frontend
echo [Dairy Bench] Starting frontend...
start "Dairy Bench Frontend" cmd.exe /k "cd /d ""%~dp0frontend"" && call npm.cmd run dev -- --strictPort"
exit /b 0

:wait_for_services
powershell.exe -NoProfile -Command "$deadline = (Get-Date).AddSeconds(60); $backendReady = $false; $frontendReady = $false; while ((Get-Date) -lt $deadline -and -not ($backendReady -and $frontendReady)) { if (-not $backendReady) { try { $api = Invoke-RestMethod -Uri '%BACKEND_URL%/openapi.json' -TimeoutSec 10; $backendReady = $api.info.title -eq 'Dairy Bench API' -and $api.info.version -eq '%EXPECTED_API_VERSION%' } catch {} }; if (-not $frontendReady) { try { $page = Invoke-WebRequest -UseBasicParsing -Uri '%FRONTEND_URL%' -TimeoutSec 2; $health = Invoke-RestMethod -Uri '%FRONTEND_URL%/api/health' -TimeoutSec 10; $frontendReady = $page.StatusCode -eq 200 -and $page.Content -match '<title>Dairy Bench</title>' -and $health.status -eq 'ok' } catch {} }; if (-not ($backendReady -and $frontendReady)) { Start-Sleep -Milliseconds 250 } }; if ($backendReady -and $frontendReady) { exit 0 }; Write-Host ('[Dairy Bench] Readiness timeout. Backend=' + $backendReady + ', Frontend=' + $frontendReady); exit 1"
exit /b %errorlevel%

:port_conflict
echo [Dairy Bench] Close the process above or choose different ports, then run start.cmd again.
pause
exit /b 1
