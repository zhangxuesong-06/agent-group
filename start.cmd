@echo off
setlocal
cd /d "%~dp0"
title AI Cluster

rem If the server is already running, just open the console.
netstat -ano | findstr ":8000" | findstr "LISTENING" >nul
if not errorlevel 1 (
  echo [info] AI Cluster is already running.
  start "" "http://127.0.0.1:8000"
  timeout /t 2 >nul
  exit /b 0
)

if not exist ".venv\Scripts\python.exe" (
  echo [setup] creating virtual environment...
  python -m venv .venv || goto :error
  ".venv\Scripts\python.exe" -m pip install -r mvp\requirements.txt || goto :error
)

".venv\Scripts\python.exe" -c "import fastapi, uvicorn, segno, websockets" >nul 2>&1
if errorlevel 1 (
  echo [setup] installing dependencies...
  ".venv\Scripts\python.exe" -m pip install -r mvp\requirements.txt || goto :error
)

set OPEN_BROWSER=1
".venv\Scripts\python.exe" mvp\run.py
if errorlevel 1 goto :error
exit /b 0

:error
echo.
echo [error] Startup failed. See the messages above.
echo         This window will stay open so you can read them.
pause
exit /b 1
