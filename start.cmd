@echo off
setlocal

set "DAIRY_BENCH_CODEX_ENABLED=true"
set "DAIRY_BENCH_CODEX_MODEL=gpt-5.6-terra"
set "DAIRY_BENCH_CODEX_REASONING_EFFORT=high"

start "Dairy Bench Backend" cmd.exe /k ^
  "cd /d ""%~dp0backend"" && python -m uvicorn company_bench.web:create_app --factory --host 127.0.0.1 --port 8000"

start "Dairy Bench Frontend" cmd.exe /k ^
  "cd /d ""%~dp0frontend"" && call npm.cmd run dev"

timeout /t 3 /nobreak >nul
start "" "http://127.0.0.1:5173"
