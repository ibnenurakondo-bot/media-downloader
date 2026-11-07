@echo off
cd /d "%~dp0"
title Media Downloader (Local HD Engine)
set LOCAL_MODE=1
set OPEN_BROWSER=1
set HOST=127.0.0.1
set PORT=7860
echo ============================================================
echo   Media Downloader - Full HD 1080p Video & Audio Engine
echo   Starting local server at http://127.0.0.1:7860 ...
echo ============================================================
python app.py
if errorlevel 1 (
    echo.
    echo Server stopped with error.
    pause
)
