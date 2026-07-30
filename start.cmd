@echo off
setlocal

set "DAIRY_BENCH_CODEX_ENABLED=true"
set "DAIRY_BENCH_CODEX_MODEL=gpt-5.6-terra"
set "DAIRY_BENCH_CODEX_REASONING_EFFORT=high"
set "DAIRY_BENCH_CODEX_SESSION_RETENTION_DAYS=60"
set "DAIRY_BENCH_CODEX_HOME=%~dp0.dairy-bench\codex"
set "CODEX_HOME=%DAIRY_BENCH_CODEX_HOME%"
set "DAIRY_BENCH_CODEX_CLI="

if not exist "%DAIRY_BENCH_CODEX_HOME%" (
  mkdir "%DAIRY_BENCH_CODEX_HOME%" >nul 2>&1
  if errorlevel 1 (
    echo [Dairy Bench] Could not create the isolated Codex home:
    echo %DAIRY_BENCH_CODEX_HOME%
    pause
    exit /b 1
  )
)

for /f "delims=" %%I in ('where.exe codex.exe 2^>nul') do (
  if not defined DAIRY_BENCH_CODEX_CLI set "DAIRY_BENCH_CODEX_CLI=%%I"
)

if not defined DAIRY_BENCH_CODEX_CLI (
  for /f "delims=" %%I in ('dir /b /s /a-d "%LOCALAPPDATA%\OpenAI\Codex\bin\codex.exe" 2^>nul') do (
    if not defined DAIRY_BENCH_CODEX_CLI set "DAIRY_BENCH_CODEX_CLI=%%I"
  )
)

if not defined DAIRY_BENCH_CODEX_CLI (
  echo [Dairy Bench] Codex CLI was not found in PATH or the Codex desktop folder.
  pause
  exit /b 1
)

for %%I in ("%DAIRY_BENCH_CODEX_CLI%") do set "PATH=%%~dpI;%PATH%"

if /i "%~1"=="--check" (
  echo [Dairy Bench] Codex CLI: %DAIRY_BENCH_CODEX_CLI%
  echo [Dairy Bench] Codex home: %DAIRY_BENCH_CODEX_HOME%
  exit /b 0
)

"%DAIRY_BENCH_CODEX_CLI%" login status >nul 2>&1
if errorlevel 1 (
  echo [Dairy Bench] One-time login for the isolated Codex home is required.
  "%DAIRY_BENCH_CODEX_CLI%" login
  if errorlevel 1 (
    echo [Dairy Bench] Codex login did not complete.
    pause
    exit /b 1
  )
)

start "Dairy Bench Backend" cmd.exe /k ^
  "cd /d ""%~dp0backend"" && python -m uvicorn company_bench.web:create_app --factory --host 127.0.0.1 --port 8000"

start "Dairy Bench Frontend" cmd.exe /k ^
  "cd /d ""%~dp0frontend"" && call npm.cmd run dev"

timeout /t 3 /nobreak >nul
start "" "http://127.0.0.1:5173"
