@echo off
chcp 65001 >nul
cd /d "%~dp0"
title QuantDesk - Local A-Share Quant Workbench
netstat -ano | findstr ":8765 " | findstr "LISTENING" >nul
if %errorlevel%==0 (
  echo QuantDesk is already running. Opening browser ...
  start "" http://127.0.0.1:8765/
  exit /b 0
)
echo Starting QuantDesk ... please keep this window open.
if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment ...
  py -3 -m venv .venv
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
)
".venv\Scripts\python.exe" -c "import flask, akshare, lightgbm" >nul 2>&1
if errorlevel 1 (
  echo Installing missing Python packages ...
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
)
start "" http://127.0.0.1:8765/
".venv\Scripts\python.exe" app.py
echo.
echo Server stopped. Press any key to close.
pause >nul