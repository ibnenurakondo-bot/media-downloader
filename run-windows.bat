@echo off
cd /d "%~dp0"
title Media Downloader
set PY=python
where python >nul 2>nul || set PY=py -3
%PY% --version >nul 2>nul
if errorlevel 1 (
  echo Python not found. Install Python 3.10+ from https://www.python.org/downloads/
  echo IMPORTANT: tick "Add python.exe to PATH" in the installer, then run this file again.
  pause
  exit /b 1
)
if not exist .venv\Scripts\python.exe %PY% -m venv .venv
call .venv\Scripts\activate.bat
echo Installing / updating packages (first run takes a minute)...
python -m pip install -q -U pip
python -m pip install -q -U -r requirements-local.txt
set LOCAL_MODE=1
set OPEN_BROWSER=1
set HOST=127.0.0.1
set /p PHONE=Use from your phone too (same Wi-Fi)? [y/N]: 
if /i "%PHONE%"=="y" (
  set HOST=0.0.0.0
  set /p ACCESS_CODE=Set a password for the app (or press Enter to skip): 
)
python app.py
pause
