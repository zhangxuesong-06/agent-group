@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
title AI Cluster Worker

if not exist ".venv\Scripts\python.exe" (
  echo [setup] creating virtual environment...
  python -m venv .venv || goto :error
)

".venv\Scripts\python.exe" -c "import websockets" >nul 2>&1
if errorlevel 1 (
  echo [setup] installing websockets...
  ".venv\Scripts\python.exe" -m pip install "websockets>=13" || goto :error
)

".venv\Scripts\python.exe" mvp\scripts\worker_launcher.py %*
if errorlevel 1 goto :error
exit /b 0

:error
echo.
echo [error] Worker failed to start. See the messages above.
echo         This window will stay open so you can read them.
pause
exit /b 1
